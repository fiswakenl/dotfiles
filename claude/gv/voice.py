# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "websockets>=15", "python-socks[asyncio]", "sounddevice", "numpy"]
# ///
"""Голосовой мост Gemini Live ⇄ сессия Claude Code (плагин gv). Запускает плагин, сам не нужен.

Микрофон и колонки — самого компьютера (sounddevice): сервис слушает микрофон, а голос Gemini играет из колонок.
Страница http://127.0.0.1:8790 — только журнал разговора, как devtools: ни звука, ни микрофона в браузере нет.
Сокет Gemini открывается, когда есть что сказать или услышать, и закрывается после простоя (IDLE_CLOSE).
Тишину в Gemini не шлём: она тарифицируется как речь. Пока Gemini говорит, микрофон не слушаем (эхо из колонок);
GV_DUPLEX=1 — слушать всегда (в наушниках).

Gemini не отвечает от себя на рабочее: пересказывает слова человека функцией say_to_claude, ответ Claude приходит ей текстом.

Ручки для плагина (127.0.0.1:8790):
    GET  /next      вопрос для Claude {"id", "text"}; ждёт, пока он появится и Claude свободен; 204 — пока ничего
    POST /busy      {"busy": true|false} — Claude занят/свободен
    POST /answer    тело — ответ Claude текстом; Gemini озвучит
    POST /btw       {"text"} — вставить реплику прямо Gemini, мимо Claude (он занят — его не трогаем)
    POST /say       {"text"} — то же, что say_to_claude, но текстом (отладка без голоса)
    POST /audio     речь PCM 16 бит, 16 кГц в сокет Gemini вместо микрофона (проверка через TTS)
    GET  /last      последний ответ Claude, пришедший в /answer
    POST /quit      сессия закрылась — сервис выходит
    POST /ping      плагин жив; пропал на PING_LOST секунд — сервис выходит сам

Ключ и прокси Gemini — ~/.config/gemini.env.
"""
import asyncio
import base64
import itertools
import json
import os
import time
from collections import deque
from pathlib import Path

import numpy as np
import sounddevice as sd
import websockets
from aiohttp import web

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("GV_PORT", 8790))
DUPLEX = os.environ.get("GV_DUPLEX") == "1"
GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODEL = "models/gemini-3.8-live"
NEXT_WAIT = 25  # секунд ждёт /next, потом 204: плагин спрашивает снова
PING_LOST = 90  # секунд без /ping — выходим
IDLE_CLOSE = 180  # секунд без речи и звука — закрываем сокет Gemini (откроется заново сам)
LOUD = 500  # уровень (RMS int16), с которого блок звука считается речью
HANG, PRE = 10, 3  # блоков по 100 мс: сколько тишины держим после речи и сколько предыстории шлём перед ней

ids = itertools.count(1)
inbox: list[dict] = []  # вопросы Gemini → Claude
viewers: set[web.WebSocketResponse] = set()  # открытые страницы-журналы
S = {"last": "", "busy": False, "ping": time.monotonic(), "live": None, "touch": time.monotonic(), "lock": None, "spk": None}

TOOLS = [{"functionDeclarations": [{
    "name": "say_to_claude",
    "description": "Передать Claude Code то, что сказал человек: вопрос, просьба или поручение, своими словами, самодостаточно и коротко",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Что сказал человек, ясным письменным текстом"}},
                   "required": ["text"]}}]}]


def gemini_env() -> dict:
    e = dict(l.split("=", 1) for l in (Path.home() / ".config/gemini.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
    e = {k: v.strip() for k, v in e.items()}
    e.update({k: v for k, v in os.environ.items() if k.startswith("GEMINI_")})
    e["GEMINI_PROXY"] = e.get("GEMINI_PROXY") or None
    return e


def system_text() -> str:
    parts = [(HERE / "prompt.md").read_text()]
    if (HERE / "context.md").exists():
        parts.append((HERE / "context.md").read_text())
    return "\n\n".join(parts)


async def emit(obj: dict) -> None:
    """Событие всем открытым журналам."""
    for v in list(viewers):
        try:
            await v.send_json(obj)
        except Exception:  # noqa: BLE001
            viewers.discard(v)


class Speaker:
    """Колонки компьютера: голос Gemini (PCM 24 кГц) копится в буфере, звуковая карта забирает его сама."""

    def __init__(self) -> None:
        self.buf = bytearray()
        self.until = 0.0
        self.stream = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", callback=self._fill)
        self.stream.start()

    def _fill(self, outdata, frames, _time, _status) -> None:
        n = frames * 2
        chunk = bytes(self.buf[:n])
        del self.buf[:n]
        outdata[:len(chunk)] = chunk
        outdata[len(chunk):] = bytes(n - len(chunk))

    def add(self, pcm: bytes) -> None:
        self.buf.extend(pcm)
        self.until = time.monotonic() + len(self.buf) / 48000 + 0.5  # хвост: эхо от колонок затихает

    def clear(self) -> None:
        self.buf.clear()
        self.until = 0.0

    @property
    def busy(self) -> bool:
        return bool(self.buf) or time.monotonic() < self.until


def touch() -> None:
    S["touch"] = time.monotonic()


async def ensure_live():
    """Сокет Gemini: открыт — он; нет — открываем."""
    async with S["lock"]:
        if S["live"] is not None:
            return S["live"]
        env = gemini_env()
        setup = {"model": MODEL, "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"languageCode": "ru-RU"}},
                 "systemInstruction": {"parts": [{"text": system_text()}]}, "tools": TOOLS,
                 "inputAudioTranscription": {}, "outputAudioTranscription": {}}
        g = await websockets.connect(GEMINI_WS, additional_headers={"x-goog-api-key": env["GEMINI_API_KEY"]}, proxy=env["GEMINI_PROXY"], max_size=None)
        await g.send(json.dumps({"setup": setup}))
        S["live"] = g
        touch()
        asyncio.create_task(read(g))
        await emit({"state": "сокет Gemini открыт"})
        return g


async def live_send(obj: dict) -> None:
    g = await ensure_live()
    touch()
    await g.send(json.dumps(obj))


async def to_gemini(text: str) -> None:
    await live_send({"realtimeInput": {"text": text}})


async def read(g) -> None:
    """Всё, что приходит от Gemini: голос — в колонки, расшифровки и вызовы — в журнал и очередь."""
    try:
        async for raw in g:
            touch()
            e = json.loads(raw)
            if "error" in e:
                await emit({"error": str(e["error"])})
            for fc in e.get("toolCall", {}).get("functionCalls", []):
                text = fc.get("args", {}).get("text", "")
                inbox.append({"id": next(ids), "text": text})
                await emit({"ask": text})
                if S["busy"]:
                    res = ("Claude сейчас занят прошлым запросом, этот в очереди и уйдёт, когда он закончит: его не перебиваем. "
                           "Скажи это одной фразой и дальше поддерживай разговор сама; ответ придёт сообщением «[Ответ Claude]».")
                else:
                    res = "Передано Claude, он работает. Скажи одной фразой, что передала; ответ придёт сообщением «[Ответ Claude]»."
                await g.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": res}}]}}))
            c = e.get("serverContent", {})
            for p in c.get("modelTurn", {}).get("parts", []):
                if "inlineData" in p and S["spk"]:
                    S["spk"].add(base64.b64decode(p["inlineData"]["data"]))
            if t := c.get("inputTranscription", {}).get("text"):
                await emit({"me": t})
            if t := c.get("outputTranscription", {}).get("text"):
                await emit({"ai": t})
            if c.get("interrupted") and S["spk"]:
                S["spk"].clear()
    except Exception as ex:  # noqa: BLE001
        await emit({"error": f"Gemini: {type(ex).__name__}: {ex}"})
    finally:
        if S["live"] is g:
            S["live"] = None
        await emit({"state": "сокет Gemini закрыт"})


def chunk_msg(pcm: bytes) -> dict:
    return {"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm).decode()}}}


async def mic_loop() -> None:
    """Микрофон компьютера → Gemini, только пока есть речь: тишина стоит как речь."""
    loop, q = asyncio.get_running_loop(), asyncio.Queue()

    def cb(indata, _frames, _time, _status) -> None:
        loop.call_soon_threadsafe(q.put_nowait, bytes(indata))
    try:
        stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1600, callback=cb)
        stream.start()
    except Exception as ex:  # noqa: BLE001
        await emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})
        return
    await emit({"state": "микрофон слушает"})
    talking, quiet, pre = False, 0, deque(maxlen=PRE)
    while True:
        b = await q.get()
        if S["spk"] and S["spk"].busy and not DUPLEX:  # Gemini говорит: свой голос из колонок не слушаем
            pre.clear()
            continue
        loud = float(np.sqrt(np.mean(np.frombuffer(b, dtype=np.int16).astype(np.float32) ** 2))) > LOUD
        try:
            if loud:
                quiet = 0
                if not talking:
                    talking = True
                    await emit({"state": "слышу вас"})
                    for p in pre:
                        await live_send(chunk_msg(p))
                    pre.clear()
                await live_send(chunk_msg(b))
            elif talking:
                await live_send(chunk_msg(b))
                quiet += 1
                if quiet > HANG:
                    talking = False
                    await live_send({"realtimeInput": {"audioStreamEnd": True}})
                    await emit({"state": "жду ответа"})
            else:
                pre.append(b)
        except Exception as ex:  # noqa: BLE001
            talking = False
            await emit({"error": f"отправка в Gemini: {type(ex).__name__}: {ex}"})
            await asyncio.sleep(2)


async def idle_loop() -> None:
    """Простой: сокет Gemini не держим впустую. Закрыт — откроется сам, когда нужен."""
    while True:
        await asyncio.sleep(10)
        g = S["live"]
        if g is not None and time.monotonic() - S["touch"] > IDLE_CLOSE and not S["busy"] and not (S["spk"] and S["spk"].busy):
            await g.close()


async def viewer(request: web.Request) -> web.WebSocketResponse:
    """Журнал в браузере: только события, звук сюда не идёт."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    viewers.add(ws)
    await ws.send_json({"state": "сокет Gemini открыт" if S["live"] else "сокет Gemini закрыт, откроется, когда заговорите"})
    try:
        async for _ in ws:
            pass
    finally:
        viewers.discard(ws)
    return ws


async def nxt(_: web.Request) -> web.Response:
    """Плагин забирает следующий вопрос; пока Claude занят, вопросы ждут — его не пинаем."""
    end = time.monotonic() + NEXT_WAIT
    while time.monotonic() < end:
        if inbox and not S["busy"]:
            S["busy"] = True  # с этой минуты Claude занят, не ждём /busy от плагина
            return web.json_response(inbox.pop(0))
        await asyncio.sleep(0.2)
    return web.Response(status=204)


async def busy(request: web.Request) -> web.Response:
    S["busy"] = bool((await request.json()).get("busy"))
    return web.Response(text="ok")


async def answer(request: web.Request) -> web.Response:
    text = (await request.read()).decode().strip()
    S["last"] = text
    if text:
        await to_gemini(f"[Ответ Claude] {text}\n\nПерескажи это человеку голосом своими словами, коротко и без списков.")
    return web.Response(text="ok")


async def audio(request: web.Request) -> web.Response:
    """Речь вместо микрофона (тело — PCM 16 бит, 16 кГц, моно): для проверки через TTS. Ответ играет из колонок, как обычно."""
    pcm = await request.read()

    async def play() -> None:
        try:
            for i in range(0, len(pcm), 3200):  # 100 мс, как в реальном времени
                await live_send(chunk_msg(pcm[i:i + 3200]))
                await asyncio.sleep(0.1)
            await live_send({"realtimeInput": {"audioStreamEnd": True}})
        except Exception as ex:  # noqa: BLE001
            await emit({"error": f"/audio: {type(ex).__name__}: {ex}"})
    asyncio.create_task(play())
    return web.Response(text=f"ok {len(pcm) / 32000:.1f} с")


async def say(request: web.Request) -> web.Response:
    inbox.append({"id": next(ids), "text": (await request.json()).get("text", "")})
    return web.Response(text="ok")


async def last(_: web.Request) -> web.Response:
    return web.Response(text=S["last"])


async def btw(request: web.Request) -> web.Response:
    text = (await request.json()).get("text", "").strip()
    if text:
        await to_gemini(f"[Побочная реплика, Claude не трогаем] {text}")
    return web.Response(text="ok")


async def quit_(_: web.Request) -> web.Response:
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.Response(text="ok")


async def ping(_: web.Request) -> web.Response:
    S["ping"] = time.monotonic()
    return web.Response(text="ok")


async def start(_: web.Application) -> None:
    S["lock"] = asyncio.Lock()
    try:
        S["spk"] = Speaker()
    except Exception as ex:  # noqa: BLE001
        print(f"колонки: {type(ex).__name__}: {ex}", flush=True)

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(10)
            if time.monotonic() - S["ping"] > PING_LOST:
                os._exit(0)
    for c in (watchdog(), mic_loop(), idle_loop()):
        asyncio.create_task(c)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Gemini ⇄ Claude</title>
<style>body{font:15px ui-monospace,monospace;max-width:720px;margin:1.5rem auto;padding:0 1rem}#st{color:#888}
#log div{margin:.35rem 0}.me{color:#06c}.ai{color:#222}.ask{color:#a50;font-style:italic}.err{color:#c00}.state{color:#888}</style>
<div id=st>жду сессию Claude…</div><div id=log></div>
<script>
const log=document.getElementById('log'),st=document.getElementById('st');let cur={};
function line(k,pre,t){let d=cur[k];if(!d||!d.isConnected){d=document.createElement('div');d.className=k;d.textContent=pre;log.append(d)}
 cur={};cur[k]=d;d.textContent+=t;scrollTo(0,document.body.scrollHeight)}
function add(k,t){cur={};const d=document.createElement('div');d.className=k;d.textContent=t;log.append(d);scrollTo(0,document.body.scrollHeight)}
function connect(){
 const ws=new WebSocket('ws://'+location.host+'/ws');
 ws.onopen=()=>st.textContent='журнал подключён · звук и микрофон — на компьютере';
 ws.onmessage=ev=>{const m=JSON.parse(ev.data);
  if(m.me)line('me','Вы: ',m.me);if(m.ai)line('ai','Gemini: ',m.ai);
  if(m.ask)add('ask','→ Claude: '+m.ask);if(m.error)add('err',m.error);if(m.state)add('state','· '+m.state)};
 ws.onclose=()=>{st.textContent='сессия закрыта, жду новую…';setTimeout(connect,2000)};}
connect();
</script>"""


async def index(_: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def main() -> None:
    app = web.Application()
    app.on_startup.append(start)
    app.add_routes([web.get("/", index), web.get("/ws", viewer), web.get("/next", nxt), web.post("/busy", busy),
                    web.post("/answer", answer), web.post("/btw", btw), web.post("/say", say), web.post("/audio", audio),
                    web.get("/last", last), web.post("/ping", ping), web.post("/quit", quit_)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
