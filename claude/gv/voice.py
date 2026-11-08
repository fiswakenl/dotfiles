# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "websockets>=15", "python-socks[asyncio]", "sounddevice", "numpy"]
# ///
"""Голосовой мост Gemini Live ⇄ сессия Claude Code (плагин gv). Запускает плагин, сам не нужен.

Микрофон и колонки — самого компьютера (sounddevice): сервис слушает микрофон, а голос Gemini играет из колонок.
Страница http://127.0.0.1:8790 — только журнал разговора, как devtools: ни звука, ни микрофона в браузере нет.
Сокет Gemini открыт всё время, пока работает сервис (оборвался — открываем заново).
Тишину в Gemini не шлём: она тарифицируется как речь. Пока Gemini говорит, микрофон не слушаем (эхо из колонок);
GV_DUPLEX=1 — слушать всегда (в наушниках).

Gemini не отвечает от себя на рабочее: пересказывает слова человека функцией say_to_claude, ответ Claude приходит ей текстом.

Ручки для плагина (127.0.0.1:8790), все с заголовком x-gv-token, если задан GV_TOKEN (кроме /, /ws, /takeover):
    GET  /next      вопрос для Claude {"id", "text"}; ждёт, пока он появится и Claude свободен; 204 — пока ничего
    POST /busy      {"busy": true|false} — Claude занят/свободен
    POST /answer    тело — ответ Claude текстом; Gemini озвучит
    POST /btw       {"text"} — быстрый вопрос Claude по разговору, ответ в теле; основной ход не прерывается
    GET  /quick     быстрый вопрос для ветки разговора {"id", "text"} (плагин; идёт и пока основной ход занят)
    POST /quickanswer?id=N  тело — ответ ветки
    POST /say       {"text"} — то же, что say_to_claude, но текстом (отладка без голоса)
    POST /audio     речь PCM 16 бит, 16 кГц в сокет Gemini вместо микрофона (проверка через TTS)
    GET  /last      последний ответ Claude, пришедший в /answer
    POST /quit      сессия закрылась — сервис выходит
    POST /takeover  новая сессия просит уступить порт — выходим (без токена)
    POST /ping      плагин жив; пропал на PING_LOST секунд — сервис выходит сам

Ключ и прокси Gemini — ~/.config/gemini.env.
"""
import asyncio
import base64
import itertools
import json
import os
import signal
import sys
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
LOUD = 500  # уровень (RMS int16), с которого блок звука считается речью
PAUSE_MS = int(os.environ.get("GV_PAUSE_MS", 2500))  # столько тишины ждём, пока человек думает, прежде чем считать мысль законченной
HANG, PRE = PAUSE_MS // 100, 3  # блоков по 100 мс: тишина после речи и предыстория перед ней

TOKEN = os.environ.get("GV_TOKEN", "")  # токен сессии-хозяина: без него ручки плагина закрыты; пусто — открыты (отладка)
PUBLIC = {"/", "/ws", "/takeover", "/prompt"}  # страница, журнал и просьба уступить порт — без токена

ids = itertools.count(1)
inbox: list[dict] = []  # вопросы Gemini → Claude
quick_q: list[dict] = []  # быстрые вопросы /btw → ветка разговора Claude (идут и пока основной ход занят)
quick_f: dict[int, asyncio.Future] = {}  # ответы на них
waiting: dict[int, asyncio.Future] = {}  # вызовы say_to_claude, ждущие ответа основной сессии
WAIT_ANSWER = 900  # секунд Gemini ждёт ответа основной сессии
viewers: set[web.WebSocketResponse] = set()  # открытые страницы-журналы
history: list[dict] = []  # события журнала: новая или обновлённая страница получает их все
HISTORY_KEEP = 1000
S = {"sent": 0, "got": 0, "cur": 0, "last": "", "busy": False, "ping": time.monotonic(), "live": None, "touch": time.monotonic(), "lock": None, "spk": None}

TOOLS = [{"functionDeclarations": [{
    "name": "say_to_claude",
    "description": "Передать Claude Code то, что сказал человек: вопрос, просьба или поручение, своими словами, самодостаточно и коротко",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Что сказал человек, ясным письменным текстом"}},
                   "required": ["text"]}}, {
    "name": "btw",
    "description": "Мгновенный вопрос Claude Code по тому, что уже есть в разговоре сессии: ответ за пару секунд, пока основная работа идёт. Не для поручений и не для того, что требует смотреть файлы",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Короткий вопрос ясным письменным текстом"}},
                   "required": ["text"]}}]}]


LOG = Path.home() / ".claude" / "gv.log"
LOG_KEEP = 2000
_log_n = [0]


def log(kind: str, **data) -> None:
    """Строка JSON в ~/.claude/gv.log: что произошло и сколько данных прошло. Звука и ключей в журнале нет."""
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "kind": kind, **data}, ensure_ascii=False) + "\n")
        _log_n[0] += 1
        if _log_n[0] % 200 == 0:
            lines = LOG.read_text().splitlines()
            if len(lines) > LOG_KEEP:
                LOG.write_text("\n".join(lines[-LOG_KEEP:]) + "\n")
    except OSError:
        pass


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
    """Событие всем открытым журналам; и в историю, чтобы обновлённая страница не начинала с чистого листа."""
    history.append(obj)
    del history[:-HISTORY_KEEP]
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
                 "inputAudioTranscription": {}, "outputAudioTranscription": {},
                 # терпеливый конец речи: пауза на размышление не считается концом (запасной вариант к нашему сигналу audioStreamEnd)
                 "realtimeInputConfig": {"automaticActivityDetection": {"endOfSpeechSensitivity": "END_SENSITIVITY_LOW", "silenceDurationMs": PAUSE_MS}}}
        g = await websockets.connect(GEMINI_WS, additional_headers={"x-goog-api-key": env["GEMINI_API_KEY"]}, proxy=env["GEMINI_PROXY"], max_size=None)
        await g.send(json.dumps({"setup": setup}))
        S["live"] = g
        touch()
        log("gemini.open", model=MODEL, duplex=DUPLEX, pause_ms=PAUSE_MS)
        asyncio.create_task(read(g))
        await emit({"state": "сокет Gemini открыт"})
        return g


async def live_send(obj: dict) -> None:
    g = await ensure_live()
    touch()
    if "audio" in obj.get("realtimeInput", {}):
        S["sent"] += len(obj["realtimeInput"]["audio"]["data"]) * 3 // 4  # base64 → байты
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
                log("gemini.error", error=str(e["error"])[:500])
                await emit({"error": str(e["error"])})
            for fc in e.get("toolCall", {}).get("functionCalls", []):
                text = fc.get("args", {}).get("text", "")
                log("gemini.call", name=fc["name"], text=text)
                if fc["name"] == "btw":
                    asyncio.create_task(answer_btw(g, fc, text))
                    continue
                asyncio.create_task(ask_claude(g, fc, text))
            c = e.get("serverContent", {})
            for p in c.get("modelTurn", {}).get("parts", []):
                if "inlineData" in p and S["spk"]:
                    pcm = base64.b64decode(p["inlineData"]["data"])
                    S["got"] += len(pcm)
                    S["spk"].add(pcm)
            if t := c.get("inputTranscription", {}).get("text"):
                log("heard", text=t)
                await emit({"me": t})
            if t := c.get("outputTranscription", {}).get("text"):
                if "<no speech" not in t and "{pause}" not in t:  # метка «молчу» от самой модели — не речь
                    log("said", text=t)
                    await emit({"ai": t})
                else:
                    log("silent")
            if c.get("interrupted") and S["spk"]:
                S["spk"].clear()
                log("interrupted")
            if c.get("turnComplete"):
                log("turn", audio_sent_bytes=S["sent"], audio_got_bytes=S["got"])
    except Exception as ex:  # noqa: BLE001
        log("gemini.error", error=f"{type(ex).__name__}: {ex}")
        await emit({"error": f"Gemini: {type(ex).__name__}: {ex}"})
    finally:
        log("gemini.close", audio_sent_bytes=S["sent"], audio_got_bytes=S["got"])
        if S["live"] is g:
            S["live"] = None
        await emit({"state": "сокет Gemini закрыт"})


async def quick(text: str) -> str:
    """Быстрый вопрос Claude: плагин спрашивает ветку разговора, основной ход не трогаем."""
    n = next(ids)
    fut = asyncio.get_running_loop().create_future()
    quick_f[n] = fut
    quick_q.append({"id": n, "text": text})
    log("btw.ask", id=n, text=text)
    await emit({"btwq": text})
    try:
        res = await asyncio.wait_for(fut, 60)
    except TimeoutError:
        res = "Claude не успел ответить."
    quick_f.pop(n, None)
    log("btw.answer", id=n, chars=len(res))
    await emit({"btwa": res})
    return res


async def ask_claude(g, fc: dict, text: str) -> None:
    """Блокирующая передача: вызов функции висит, пока Claude не закончит ответ, и Gemini молчит; ответ — результат вызова."""
    n = next(ids)
    fut = asyncio.get_running_loop().create_future()
    waiting[n] = fut
    inbox.append({"id": n, "text": text})
    log("ask", id=n, text=text)
    await emit({"ask": text})
    try:
        res = await asyncio.wait_for(fut, WAIT_ANSWER)
    except TimeoutError:
        res = "Claude не ответил вовремя."
    waiting.pop(n, None)
    log("answer.to_gemini", id=n, chars=len(res))
    await emit({"answer": res})
    try:
        await g.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": res}}]}}))
    except Exception:  # noqa: BLE001
        pass


async def answer_btw(g, fc: dict, text: str) -> None:
    res = await quick(text)
    try:
        await g.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": res}}]}}))
    except Exception:  # noqa: BLE001
        pass


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
    log("mic.start", duplex=DUPLEX)
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
                    log("mic.speech.start")
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
                    log("mic.speech.end", pause_ms=PAUSE_MS)
                    await live_send({"realtimeInput": {"audioStreamEnd": True}})
                    await emit({"state": "жду ответа"})
            else:
                pre.append(b)
        except Exception as ex:  # noqa: BLE001
            talking = False
            await emit({"error": f"отправка в Gemini: {type(ex).__name__}: {ex}"})
            await asyncio.sleep(2)


async def keep_live() -> None:
    """Сокет Gemini открыт всё время, пока работает сервис: слушает сразу, не ждёт разговора. Оборвался (лимит сессии, сеть) — открываем заново."""
    wait = 2
    while True:
        try:
            g = await ensure_live()
            wait = 2
            await g.wait_closed()
        except Exception as ex:  # noqa: BLE001
            log("gemini.error", error=f"открыть сокет: {type(ex).__name__}: {ex}")
            await emit({"error": f"сокет Gemini: {type(ex).__name__}: {ex}"})
            wait = min(wait * 2, 30)
        await asyncio.sleep(wait)


async def viewer(request: web.Request) -> web.WebSocketResponse:
    """Журнал в браузере: только события, звук сюда не идёт."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    viewers.add(ws)
    log("viewer.open", viewers=len(viewers))
    await ws.send_json({"history": history})
    await ws.send_json({"state": "сокет Gemini открыт" if S["live"] else "сокет Gemini закрыт, откроется, когда заговорите"})
    try:
        async for _ in ws:
            pass
    finally:
        viewers.discard(ws)
        log("viewer.close", viewers=len(viewers))
    return ws


async def nxt(_: web.Request) -> web.Response:
    """Плагин забирает следующий вопрос; пока Claude занят, вопросы ждут — его не пинаем."""
    end = time.monotonic() + NEXT_WAIT
    while time.monotonic() < end:
        if inbox and not S["busy"]:
            S["busy"] = True  # с этой минуты Claude занят, не ждём /busy от плагина
            item = inbox.pop(0)
            S["cur"] = item["id"]  # на этот вопрос придёт ближайший /answer
            log("next.served", id=item["id"])
            return web.json_response(item)
        await asyncio.sleep(0.2)
    return web.Response(status=204)


async def busy(request: web.Request) -> web.Response:
    S["busy"] = bool((await request.json()).get("busy"))
    log("busy", busy=S["busy"])
    return web.Response(text="ok")


async def answer(request: web.Request) -> web.Response:
    text = (await request.read()).decode().strip()
    S["last"] = text
    log("answer.from_claude", id=S["cur"], chars=len(text))
    fut = waiting.get(S["cur"])
    if fut and not fut.done():
        fut.set_result(text or "Claude ответил пусто.")  # Gemini ждёт этот ответ как результат вызова
    elif text:  # вызов уже не ждёт (вышло время, сокет закрылся) — отдаём текстом
        await to_gemini(f"[Ответ Claude] {text}\n\nПерескажи это человеку голосом своими словами, коротко и без списков.")
    return web.Response(text="ok")


async def audio(request: web.Request) -> web.Response:
    """Речь вместо микрофона (тело — PCM 16 бит, 16 кГц, моно): для проверки через TTS. Ответ играет из колонок, как обычно."""
    pcm = await request.read()
    log("audio.inject", bytes=len(pcm))

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
    """Быстрый вопрос Claude вручную: тело {"text"}; ответ приходит в ответе на запрос."""
    return web.Response(text=await quick((await request.json()).get("text", "").strip()))


async def quick_next(_: web.Request) -> web.Response:
    """Плагин забирает быстрый вопрос; идёт и пока основной ход занят."""
    end = time.monotonic() + NEXT_WAIT
    while time.monotonic() < end:
        if quick_q:
            return web.json_response(quick_q.pop(0))
        await asyncio.sleep(0.1)
    return web.Response(status=204)


async def quick_answer(request: web.Request) -> web.Response:
    fut = quick_f.get(int(request.query["id"]))
    if fut and not fut.done():
        fut.set_result((await request.read()).decode().strip())
    return web.Response(text="ok")


async def quit_(_: web.Request) -> web.Response:
    log("exit", why="/quit от плагина")
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.Response(text="ok")


async def ping(_: web.Request) -> web.Response:
    if time.monotonic() - S["ping"] > 60:  # в журнал не чаще раза в минуту
        log("ping", after_s=round(time.monotonic() - S["ping"]))
    S["ping"] = time.monotonic()
    return web.Response(text="ok")


@web.middleware
async def guard(request: web.Request, handler):
    if TOKEN and request.path not in PUBLIC and request.headers.get("x-gv-token") != TOKEN:
        return web.Response(status=403, text="занято другой сессией")
    return await handler(request)


async def takeover(_: web.Request) -> web.Response:
    """Новая сессия просит уступить: выходим."""
    log("exit", why="/takeover: пришла другая сессия")
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.Response(text="ok")


async def start(_: web.Application) -> None:
    S["lock"] = asyncio.Lock()

    def on_signal(signum, _frame) -> None:
        log("exit", why=f"сигнал {signal.Signals(signum).name}")
        os._exit(0)
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, on_signal)
    log("start", port=PORT, duplex=DUPLEX, pause_ms=PAUSE_MS, mic=os.environ.get("GV_MIC") != "0")
    try:
        S["spk"] = Speaker()
    except Exception as ex:  # noqa: BLE001
        print(f"колонки: {type(ex).__name__}: {ex}", flush=True)

    watched = [HERE / "voice.py", HERE / "prompt.md", HERE / "context.md"]
    stamp = {f: f.stat().st_mtime for f in watched if f.exists()}

    async def watchdog() -> None:
        while True:
            await asyncio.sleep(10)
            if time.monotonic() - S["ping"] > PING_LOST:
                log("exit", why=f"плагин не пинговал {PING_LOST} с")
                os._exit(0)
            if any(f.exists() and f.stat().st_mtime != stamp.get(f) for f in watched):  # код или инструкция изменились — перезапускаемся сами
                log("reload")
                os.execv(sys.executable, [sys.executable, *sys.argv])
    for c in ((watchdog(), keep_live()) if os.environ.get("GV_MIC") == "0" else (watchdog(), keep_live(), mic_loop())):  # GV_MIC=0 — без микрофона (тесты)
        asyncio.create_task(c)


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Gemini ⇄ Claude</title>
<style>body{font:15px ui-monospace,monospace;max-width:720px;margin:1.5rem auto;padding:0 1rem}#st{color:#888}
details{margin:.6rem 0;color:#444}pre{white-space:pre-wrap;font:inherit;background:#f4f4f4;padding:.6rem;border-radius:6px}#log div{margin:.35rem 0}.me{color:#06c}.ai{color:#222}.ask{color:#a50;font-style:italic}.err{color:#c00}.state{color:#888}</style>
<div id=st>жду сессию Claude…</div>
<details id=pp><summary>Инструкция и функции Gemini</summary><pre id=prompt></pre></details><div id=log></div>
<script>
const log=document.getElementById('log'),st=document.getElementById('st');let cur={};
function line(k,pre,t){let d=cur[k];if(!d||!d.isConnected){d=document.createElement('div');d.className=k;d.textContent=pre;log.append(d)}
 cur={};cur[k]=d;d.textContent+=t;scrollTo(0,document.body.scrollHeight)}
function add(k,t){cur={};const d=document.createElement('div');d.className=k;d.textContent=t;log.append(d);scrollTo(0,document.body.scrollHeight)}
function connect(){
 const ws=new WebSocket('ws://'+location.host+'/ws');
 ws.onopen=()=>{st.textContent='журнал подключён · звук и микрофон — на компьютере';
  fetch('/prompt').then(r=>r.json()).then(p=>{document.getElementById('prompt').textContent=p.system+'\\n\\n— Функции —\\n'+p.tools.map(t=>t.name+': '+t.description).join('\\n')}).catch(()=>{})};
 const handle=m=>{
  if(m.history){log.innerHTML='';cur={};m.history.forEach(handle);return}
  if(m.me)line('me','Вы: ',m.me);if(m.ai)line('ai','Gemini: ',m.ai);
  if(m.ask)add('ask','→ Claude: '+m.ask);if(m.answer)add('ask','← Claude: '+m.answer);
  if(m.btwq)add('ask','⚡ /btw: '+m.btwq);if(m.btwa)add('ask','⚡ ← '+m.btwa);
  if(m.error)add('err',m.error);if(m.state)add('state','· '+m.state)};
 ws.onmessage=ev=>handle(JSON.parse(ev.data));
 ws.onclose=()=>{st.textContent='сессия закрыта, жду новую…';setTimeout(connect,2000)};}
connect();
</script>"""


async def prompt(_: web.Request) -> web.Response:
    """Что сейчас видит Gemini: инструкция и функции (для журнала в браузере)."""
    return web.json_response({"system": system_text(), "tools": TOOLS[0]["functionDeclarations"]})


async def index(_: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def main() -> None:
    app = web.Application(middlewares=[guard])
    app.on_startup.append(start)
    app.add_routes([web.get("/", index), web.get("/ws", viewer), web.get("/next", nxt), web.post("/busy", busy),
                    web.post("/answer", answer), web.post("/btw", btw), web.get("/quick", quick_next), web.post("/quickanswer", quick_answer), web.post("/say", say), web.post("/audio", audio),
                    web.get("/last", last), web.get("/prompt", prompt), web.post("/ping", ping), web.post("/quit", quit_), web.post("/takeover", takeover)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
