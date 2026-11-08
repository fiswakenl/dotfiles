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

Ручки для плагина (127.0.0.1:8790); хозяина сервиса определяет файл ~/.claude/gv.token, который проверяет плагин:
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
import re
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


ids = itertools.count(1)
inbox: list[dict] = []  # вопросы Gemini → Claude
quick_q: list[dict] = []  # быстрые вопросы /btw → ветка разговора Claude (идут и пока основной ход занят)
quick_f: dict[int, asyncio.Future] = {}  # ответы на них
reqs: dict[int, dict] = {}  # запросы к основной сессии: id → {text, state: queued|served|answered|superseded, replaces}
viewers: set[web.WebSocketResponse] = set()  # открытые страницы-журналы
history: list[dict] = []  # события журнала: новая или обновлённая страница получает их все
HISTORY_KEEP = 1000
S = {"poll_at": 0.0, "mute": False, "rec": None, "rec_t0": 0.0, "why": "закрыт сервером", "after_tool": False, "pre_audio": bytearray(), "pre_text": [], "sent": 0, "got": 0, "cur": 0, "last": "", "busy": False, "ping": time.monotonic(), "live": None, "touch": time.monotonic(), "lock": None, "spk": None}

TOOLS = [{"functionDeclarations": [{
    "name": "say_to_claude",
    "description": "Передать Claude Code то, что сказал человек: вопрос, просьба или поручение, своими словами, самодостаточно и коротко. Возвращается сразу с номером запроса; ответ придёт позже сообщением. Если человек продолжил или поправил мысль после отправки, вызови снова с replaces=номер прежнего запроса и ПОЛНЫМ текстом (прежнее и новое вместе)",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Что сказал человек, ясным письменным текстом"},
                                                    "replaces": {"type": "INTEGER", "description": "Номер прежнего запроса, который этот заменяет; не указывай для нового вопроса"}},
                   "required": ["text"]}}, {
    "name": "btw",
    "description": "Мгновенный вопрос Claude Code по тому, что уже есть в разговоре сессии: ответ за пару секунд, пока основная работа идёт. Не для поручений и не для того, что требует смотреть файлы",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Короткий вопрос ясным письменным текстом"}},
                   "required": ["text"]}}]}]


LOG = Path.home() / ".claude" / "gv.log"
LOG_KEEP = 2000
SESSIONS = Path.home() / ".claude" / "gv-sessions"  # по файлу на каждое подключение сокета Gemini: шапка, весь диалог, итог
SESSIONS_KEEP = 200
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


def rec(obj: dict) -> None:
    """Строка в файл текущего подключения (если оно открыто)."""
    f = S.get("rec")
    if f:
        try:
            with f.open("a") as h:
                h.write(json.dumps({"at": time.strftime("%H:%M:%S"), **obj}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def rec_open(**head) -> None:
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        old = sorted(SESSIONS.glob("*.jsonl"))
        for f in old[:-SESSIONS_KEEP]:
            f.unlink()
        S["rec"] = SESSIONS / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl"
        S["rec_t0"] = time.monotonic()
        rec({"connect": time.strftime("%Y-%m-%dT%H:%M:%S"), **head})
    except OSError:
        S["rec"] = None


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
    rec(obj)
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


def chime() -> bytes:
    """Короткий сигнал «передано» (PCM 24 кГц) вместо слов: сервис подаёт его сам."""
    n = int(24000 * 0.12)
    t = np.arange(n) / 24000
    env = np.minimum(1.0, np.minimum(t / 0.01, (0.12 - t) / 0.03))
    return (np.sin(2 * np.pi * 880 * t) * env * 0.25 * 32767).astype(np.int16).tobytes()


async def flush_pre() -> None:
    """Ход без вызова функции: то, что придержали, говорим как есть."""
    if S["pre_audio"] and S["spk"]:
        S["spk"].add(bytes(S["pre_audio"]))
    for t in S["pre_text"]:
        log("said", text=t)
        await emit({"ai": t})
    S["pre_audio"].clear()
    S["pre_text"].clear()


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
        S["why"] = "закрыт сервером"
        rec_open(model=MODEL, duplex=DUPLEX, pause_ms=PAUSE_MS, prompt_chars=len(system_text()))
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


def speech_mode() -> str:
    """Что делать с речью Gemini сейчас: молчать (ждём ответ), говорить сразу (ответ на вызов) или придержать (ход по вашей реплике)."""
    if S["mute"]:
        return "mute"
    return "live" if S["after_tool"] else "hold"


def drop_held() -> None:
    """Придержанное выбрасываем: всё, что Gemini сказала перед вызовом функции, не произносим."""
    if S["pre_audio"] or S["pre_text"]:
        log("dropped", chars=sum(len(x) for x in S["pre_text"]), audio_bytes=len(S["pre_audio"]))
    S["pre_audio"].clear()
    S["pre_text"].clear()


def on_audio(pcm: bytes) -> None:
    S["got"] += len(pcm)
    mode = speech_mode()
    if mode == "live" and S["spk"]:
        S["spk"].add(pcm)
    elif mode == "hold" and S["spk"]:
        S["pre_audio"].extend(pcm)


SILENCE_MARK = re.compile(r"\s*<(no( speech>?)?)?\s*|\s*speech>\s*|.*\{pause\}.*|.*<no speech.*", re.S)


async def on_text(t: str) -> None:
    if SILENCE_MARK.fullmatch(t):  # метка «молчу» от самой модели приходит кусками («<no », «speech>»): это не речь
        return log("silent")
    mode = speech_mode()
    if mode == "mute":
        return log("muted", text=t)
    if mode == "hold":
        return S["pre_text"].append(t)
    log("said", text=t)
    await emit({"ai": t})


def on_interrupted() -> None:
    if S["spk"]:
        S["spk"].clear()
    S["pre_audio"].clear()
    S["pre_text"].clear()
    log("interrupted")


async def on_turn_end() -> None:
    await flush_pre()
    S["after_tool"] = False
    S["mute"] = False
    log("turn", audio_sent_bytes=S["sent"], audio_got_bytes=S["got"])


async def on_call(g, fc: dict) -> None:
    text = fc.get("args", {}).get("text", "")
    log("gemini.call", name=fc["name"], text=text)
    drop_held()
    if fc["name"] == "btw":
        asyncio.create_task(answer_btw(g, fc, text))
        return
    if S["spk"]:
        S["spk"].add(chime())
    asyncio.create_task(ask_claude(g, fc, text))


async def on_content(c: dict) -> None:
    for p in c.get("modelTurn", {}).get("parts", []):
        if "inlineData" in p:
            on_audio(base64.b64decode(p["inlineData"]["data"]))
    if t := c.get("inputTranscription", {}).get("text"):
        log("heard", text=t)
        await emit({"me": t})
    if t := c.get("outputTranscription", {}).get("text"):
        await on_text(t)
    if c.get("interrupted"):
        on_interrupted()
    if c.get("turnComplete"):
        await on_turn_end()


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
                await on_call(g, fc)
            await on_content(e.get("serverContent", {}))
    except Exception as ex:  # noqa: BLE001
        log("gemini.error", error=f"{type(ex).__name__}: {ex}")
        S["why"] = f"{type(ex).__name__}: {ex}"
        await emit({"error": f"Gemini: {type(ex).__name__}: {ex}"})
    finally:
        log("gemini.close", audio_sent_bytes=S["sent"], audio_got_bytes=S["got"])
        rec({"close": S["why"], "seconds": round(time.monotonic() - S["rec_t0"]), "audio_sent_bytes": S["sent"], "audio_got_bytes": S["got"]})
        S["rec"] = None
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


async def reply_tool(g, fc: dict, result: str) -> None:
    """Результат вызова функции для Gemini; оборвавшийся сокет не ошибка."""
    try:
        await g.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": result}}]}}))
    except Exception:  # noqa: BLE001
        pass


def normalize(text: str) -> str:
    return " ".join(text.lower().split())


def find_duplicate(text: str) -> int | None:
    """Gemini иногда зовёт функцию дважды подряд с тем же текстом: возвращает номер уже переданного запроса."""
    key = normalize(text)
    now = time.monotonic()
    return next((i for i, r in reqs.items() if r["key"] == key and now - r["at"] < 30 and r["state"] in ("queued", "served")), None)


SUPERSEDED_NOTE = {
    "queued": " Прежний запрос №{old} заменён.",
    "served": " Claude уже работает над №{old}: его ответ не озвучу, ответ придёт на №{n}.",
}


def register_request(text: str, replaces) -> tuple[int, str]:
    """Новый запрос к основной сессии. Поправка (replaces) заменяет прежний: в очереди — подменяет его, в работе — его ответ будет отброшен,
    уже отвеченный остаётся как был, а поправка идёт следом."""
    n = next(ids)
    old = reqs.get(replaces) if isinstance(replaces, int) else None
    abort = bool(old) and old["state"] == "served"  # Claude уже работает над прежним: плагин прервёт его ход и вставит поправку сразу
    note = ""
    if old:
        note = SUPERSEDED_NOTE.get(old["state"], "").format(old=replaces, n=n)
        if old["state"] == "queued":
            inbox[:] = [i for i in inbox if i["id"] != replaces]
        if old["state"] != "answered":
            old["state"] = "superseded"
    reqs[n] = {"text": text, "state": "queued", "replaces": replaces if old else None, "key": normalize(text), "at": time.monotonic()}
    inbox.append({"id": n, "text": text, "replaces": reqs[n]["replaces"], "abort": abort})
    log("ask", id=n, text=text, replaces=reqs[n]["replaces"])
    return n, note


async def ask_claude(g, fc: dict, text: str) -> None:
    """Неблокирующая передача: номер возвращается сразу, ответ придёт позже сообщением."""
    if (dup := find_duplicate(text)) is not None:
        log("ask.duplicate", id=dup, text=text)
        return await reply_tool(g, fc, f"Запрос №{dup} уже передан. Молчи.")
    n, note = register_request(text, fc.get("args", {}).get("replaces"))
    await emit({"ask": text, "id": n, "replaces": reqs[n]["replaces"]})
    S["mute"] = True  # остаток этого хода не озвучиваем: ждём ответ молча
    await reply_tool(g, fc, f"Запрос №{n} передан.{note} Молчи. Если человек продолжит говорить по этой теме, вызови say_to_claude с replaces={n} и полным текстом.")


async def answer_btw(g, fc: dict, text: str) -> None:
    res = await quick(text)
    S["after_tool"] = True
    await reply_tool(g, fc, res)


def chunk_msg(pcm: bytes) -> dict:
    return {"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm).decode()}}}


def is_loud(b: bytes) -> bool:
    return float(np.sqrt(np.mean(np.frombuffer(b, dtype=np.int16).astype(np.float32) ** 2))) > LOUD


class Gate:
    """Шлём в Gemini только пока есть речь (тишина стоит как речь); перед речью — немного предыстории, после — пауза на размышление."""

    def __init__(self) -> None:
        self.talking, self.quiet, self.pre = False, 0, deque(maxlen=PRE)

    async def start(self) -> None:
        self.talking = True
        log("mic.speech.start")
        await emit({"state": "слышу вас"})
        for p in self.pre:
            await live_send(chunk_msg(p))
        self.pre.clear()

    async def end(self) -> None:
        self.talking = False
        log("mic.speech.end", pause_ms=PAUSE_MS)
        await live_send({"realtimeInput": {"audioStreamEnd": True}})
        await emit({"state": "жду ответа"})

    async def feed(self, b: bytes) -> None:
        if is_loud(b):
            if not self.talking:
                await self.start()
            self.quiet = 0
            return await live_send(chunk_msg(b))
        if not self.talking:
            return self.pre.append(b)
        await live_send(chunk_msg(b))
        self.quiet += 1
        if self.quiet > HANG:
            await self.end()


async def mic_loop() -> None:
    """Микрофон компьютера → Gemini через ворота речи."""
    loop, q = asyncio.get_running_loop(), asyncio.Queue()

    def cb(indata, _frames, _time, _status) -> None:
        loop.call_soon_threadsafe(q.put_nowait, bytes(indata))
    try:
        stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1600, callback=cb)
        stream.start()
    except Exception as ex:  # noqa: BLE001
        return await emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})
    log("mic.start", duplex=DUPLEX)
    await emit({"state": "микрофон слушает"})
    gate = Gate()
    while True:
        b = await q.get()
        if S["spk"] and S["spk"].busy and not DUPLEX:  # Gemini говорит: свой голос из колонок не слушаем
            gate.pre.clear()
            continue
        try:
            await gate.feed(b)
        except Exception as ex:  # noqa: BLE001
            gate.talking = False
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
    if not history:  # состояние уже в истории; отдельно — только когда истории ещё нет
        await ws.send_json({"state": "сокет Gemini открыт" if S["live"] else "сокет Gemini закрыт"})
    try:
        async for _ in ws:
            pass
    finally:
        viewers.discard(ws)
        log("viewer.close", viewers=len(viewers))
    return ws


def next_item() -> dict | None:
    """Что отдать плагину: срочная поправка (прерывает занятого Claude) или, когда он свободен, первый в очереди."""
    urgent = next((i for i in inbox if i.get("abort")), None)
    if urgent:
        return urgent
    return inbox[0] if inbox and not S["busy"] else None


async def nxt(request: web.Request) -> web.Response:
    """Плагин забирает следующий вопрос; пока Claude занят, вопросы ждут — его не пинаем."""
    if time.monotonic() - S["poll_at"] > 30:  # плагин жив и опрашивает: в журнал не чаще раза в 30 с
        log("poll", queue=len(inbox), busy=S["busy"])
    S["poll_at"] = time.monotonic()
    end = time.monotonic() + min(float(request.query.get("wait", NEXT_WAIT)), NEXT_WAIT)  # плагин спрашивает коротко: висящих запросов нет
    while time.monotonic() < end:
        if item := next_item():
            S["busy"] = True  # с этой минуты Claude занят, не ждём /busy от плагина
            inbox.remove(item)
            S["cur"] = item["id"]  # на этот вопрос придёт ближайший /answer
            if item["id"] in reqs:
                reqs[item["id"]]["state"] = "served"
            log("next.served", id=item["id"], replaces=item.get("replaces"), abort=item.get("abort"))
            return web.json_response(item)
        await asyncio.sleep(0.2)
    return web.Response(status=204)


async def busy(request: web.Request) -> web.Response:
    S["busy"] = bool((await request.json()).get("busy"))
    log("busy", busy=S["busy"])
    await emit({"state": "Claude занят" if S["busy"] else f"Claude свободен, в очереди {len(inbox)}"})
    return web.Response(text="ok")


async def answer(request: web.Request) -> web.Response:
    """Ответ Claude (?id=N — на какой запрос). Устаревший (заменён поправкой) отбрасываем, остальной озвучивает Gemini."""
    text = (await request.read()).decode().strip()
    n = int(request.query.get("id", S["cur"]))
    S["last"] = text
    r = reqs.get(n)
    if r and r["state"] == "superseded":
        log("answer.dropped", id=n, chars=len(text), why="запрос заменён поправкой")
        await emit({"state": f"ответ на №{n} отброшен: запрос заменён"})
        return web.Response(text="dropped")
    if r:
        r["state"] = "answered"
    log("answer.from_claude", id=n, chars=len(text))
    await emit({"answer": text, "id": n})
    if text:
        S["mute"] = False
        S["after_tool"] = True  # ответ говорим сразу
        await to_gemini(f"[Ответ Claude на запрос №{n}] {text}\n\nПрочитай это человеку вслух почти дословно, от первого лица, ничего не добавляя.")
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
    """Запрос текстом, как вызов say_to_claude: {"text", "replaces"?}; для отладки без голоса."""
    body = await request.json()
    n, _ = register_request(body.get("text", ""), body.get("replaces"))
    return web.json_response({"id": n})


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
        S["spk"] = None if os.environ.get("GV_SPK") == "0" else Speaker()  # GV_SPK=0 — без колонок (тесты не шумят)
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
  if(m.ask)add('ask','→ Claude №'+m.id+(m.replaces?' (поправка к №'+m.replaces+')':'')+': '+m.ask);if(m.answer)add('ask','← Claude №'+m.id+': '+m.answer);
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
    app = web.Application()
    app.on_startup.append(start)
    app.add_routes([web.get("/", index), web.get("/ws", viewer), web.get("/next", nxt), web.post("/busy", busy),
                    web.post("/answer", answer), web.post("/btw", btw), web.get("/quick", quick_next), web.post("/quickanswer", quick_answer), web.post("/say", say), web.post("/audio", audio),
                    web.get("/last", last), web.get("/prompt", prompt), web.post("/ping", ping), web.post("/quit", quit_), web.post("/takeover", takeover)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
