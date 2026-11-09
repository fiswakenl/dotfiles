"""Голосовой канал: Gemini Live (сокет, микрофон, колонки) и его текстовая замена для тестов.

Оба дают одно и то же: say(text) — сказать человеку, listen(args) — речь вместо микрофона, start() — фоновые задачи.
О человеке и ходах Gemini сообщают ядру: core.hear(), core.turn_end(), core.heartbeat().
"""
import asyncio
import base64
import json
import os
import re
import time
from collections import deque
from pathlib import Path

from journal import emit, log, rec_close, rec_open

HERE = Path(__file__).resolve().parent
GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODEL = "models/gemini-3.8-live"
LOUD = 500  # уровень (RMS int16), с которого блок звука считается речью
PRE = 3  # блоков по 100 мс предыстории перед речью
SILENCE_MARK = re.compile(r"\s*<(no( speech>?)?)?\s*|\s*speech>\s*|.*\{pause\}.*|.*<no speech.*", re.S)  # «молчу» от модели кусками


def tools() -> list[dict]:
    """Обе функции неблокирующие: номер приходит ответом SILENT — Gemini не начинает из-за него нового хода (иначе повторяет вызов)."""
    return [{"functionDeclarations": [{
        "name": "ask", "behavior": "NON_BLOCKING",
        "description": "Передать Claude Code реплику человека — любую. Возвращает её номер; ответ Claude придёт позже сообщением",
        "parameters": {"type": "OBJECT", "properties": {
            "text": {"type": "STRING", "description": "Слова человека почти дословно"},
            "replaces": {"type": "INTEGER", "description": "Номер реплики, которую эта продолжает или исправляет; текст тогда полный — прежнее и новое вместе"}},
            "required": ["text"]}}, {
        "name": "quick", "behavior": "NON_BLOCKING",
        "description": "Быстрый вопрос Claude Code по уже сказанному, пока он занят: основную работу не прерывает",
        "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Слова человека почти дословно"}},
                       "required": ["text"]}}]}]


def system_text() -> str:
    return "\n\n".join(f.read_text() for f in (HERE / "prompt.md", HERE / "context.md") if f.exists())


class Text:
    """Без звука и сокета (GV_TEXT_ONLY=1): то, что ушло бы Gemini, — событие say; речь длится SAY_MS."""

    def __init__(self, core) -> None:
        self.core = core
        self.say_s = int(os.environ.get("GV_SAY_MS", 1500)) / 1000

    def start(self) -> list:
        return []

    async def say(self, text: str) -> None:
        log("say", chars=len(text))
        await asyncio.sleep(self.say_s)
        self.core.turn_end()

    async def listen(self, args: dict) -> str:
        """Человек говорит args.seconds секунд, затем Gemini отвечает ему (SAY_MS)."""
        self.core.hear(True)
        await asyncio.sleep(float(args.get("seconds", 3)))
        self.core.hear(False)
        await asyncio.sleep(self.say_s)
        self.core.turn_end()
        return "ok"


class Live:
    """Gemini Live: сокет открыт всё время, микрофон и колонки компьютера."""

    def __init__(self, core, duplex: bool, pause_ms: int) -> None:
        """duplex — слушать и пока Gemini говорит: её можно перебить (громкие колонки дают эхо — тише или наушники)."""
        self.core, self.duplex, self.pause_ms = core, duplex, pause_ms
        self.ws = None
        self.lock = asyncio.Lock()
        self.speaking = False  # Gemini читает ответ Claude; остальная её речь (ответы от себя) не звучит — она телефон
        self.sent = self.got = 0
        self.spk = Speaker()

    def start(self) -> list:
        return [self.keep_open(), self.mic()]

    # ── сокет ──
    async def open(self):
        async with self.lock:
            if self.ws is not None:
                return self.ws
            import websockets
            env = gemini_env()
            setup = {"model": MODEL, "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"languageCode": "ru-RU"}},
                     "systemInstruction": {"parts": [{"text": system_text()}]}, "tools": tools(),
                     "inputAudioTranscription": {}, "outputAudioTranscription": {},
                     # терпеливый конец речи: пауза на размышление не считается концом (запасной вариант к нашему audioStreamEnd)
                     "realtimeInputConfig": {"automaticActivityDetection": {"endOfSpeechSensitivity": "END_SENSITIVITY_LOW", "silenceDurationMs": self.pause_ms}}}
            ws = await websockets.connect(GEMINI_WS, additional_headers={"x-goog-api-key": env["GEMINI_API_KEY"]}, proxy=env["GEMINI_PROXY"], max_size=None)
            await ws.send(json.dumps({"setup": setup}))
            self.ws = ws
            log("gemini.open", model=MODEL, duplex=self.duplex, pause_ms=self.pause_ms)
            rec_open(model=MODEL, duplex=self.duplex, pause_ms=self.pause_ms)
            asyncio.create_task(self.read(ws))
            emit({"state": "сокет Gemini открыт"})
            return ws

    async def send(self, obj: dict) -> None:
        ws = await self.open()
        if audio := obj.get("realtimeInput", {}).get("audio"):
            self.sent += len(audio["data"]) * 3 // 4
        await ws.send(json.dumps(obj))

    async def keep_open(self) -> None:
        """Оборвался (лимит сессии, сеть) — открываем заново."""
        wait = 2
        while True:
            try:
                await (await self.open()).wait_closed()
                wait = 2
            except Exception as ex:  # noqa: BLE001
                log("gemini.error", error=f"открыть сокет: {type(ex).__name__}: {ex}")
                emit({"error": f"сокет Gemini: {type(ex).__name__}: {ex}"})
                wait = min(wait * 2, 30)
            await asyncio.sleep(wait)

    async def read(self, ws) -> None:
        why = "закрыт сервером"
        try:
            async for raw in ws:
                self.core.heartbeat()
                e = json.loads(raw)
                if "error" in e:
                    log("gemini.error", error=str(e["error"])[:500])
                    emit({"error": str(e["error"])})
                for fc in e.get("toolCall", {}).get("functionCalls", []):
                    await self.on_call(fc)
                await self.on_content(e.get("serverContent", {}))
        except Exception as ex:  # noqa: BLE001
            why = f"{type(ex).__name__}: {ex}"
            log("gemini.error", error=why)
            emit({"error": f"Gemini: {why}"})
        finally:
            log("gemini.close", why=why, audio_sent_bytes=self.sent, audio_got_bytes=self.got)
            rec_close(close=why, audio_sent_bytes=self.sent, audio_got_bytes=self.got)
            if self.ws is ws:
                self.ws = None
            self.core.turn_end()  # ход, которого ждали, уже не закончится
            emit({"state": "сокет Gemini закрыт"})

    # ── что пришло от Gemini ──
    async def on_call(self, fc: dict) -> None:
        args = fc.get("args", {})
        log("gemini.call", name=fc["name"], args=args)
        self.spk.add(chime())
        result = self.core.call(fc["name"], args)
        try:
            await self.ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": result, "scheduling": "SILENT"}}]}}))
        except Exception:  # noqa: BLE001 — сокет оборвался: вызов и так пропал
            pass

    async def on_content(self, c: dict) -> None:
        for p in c.get("modelTurn", {}).get("parts", []):
            if "inlineData" in p:
                pcm = base64.b64decode(p["inlineData"]["data"])
                self.got += len(pcm)
                if self.speaking:
                    self.spk.add(pcm)
        if t := c.get("inputTranscription", {}).get("text"):
            log("heard", text=t)
            emit({"me": t})
        if (t := c.get("outputTranscription", {}).get("text")) and not SILENCE_MARK.fullmatch(t):
            log("said" if self.speaking else "unsaid", text=t)  # unsaid — сказала от себя, в колонки не пустили
            if self.speaking:
                emit({"ai": t})
        if c.get("interrupted"):
            self.spk.clear()
            log("interrupted")
        if c.get("turnComplete"):
            self.speaking = False
            log("turn", audio_sent_bytes=self.sent, audio_got_bytes=self.got)
            self.core.turn_end()

    # ── что уходит в Gemini ──
    async def say(self, text: str) -> None:
        self.speaking = True
        try:
            await self.send({"realtimeInput": {"text": text}})
        except Exception as ex:  # noqa: BLE001
            emit({"error": f"отправка в Gemini: {type(ex).__name__}: {ex}"})
            self.core.turn_end()

    async def listen(self, args: dict) -> str:
        """Речь вместо микрофона: args.pcm — base64 PCM 16 бит, 16 кГц, моно (проверка через TTS)."""
        pcm = base64.b64decode(args.get("pcm", ""))
        log("audio.inject", bytes=len(pcm))
        self.core.hear(True)
        for i in range(0, len(pcm), 3200):  # 100 мс, как в реальном времени
            await self.send(chunk(pcm[i:i + 3200]))
            await asyncio.sleep(0.1)
        await self.send({"realtimeInput": {"audioStreamEnd": True}})
        self.core.hear(False)
        return f"ok {len(pcm) / 32000:.1f} с"

    async def mic(self) -> None:
        """Микрофон → Gemini, только пока есть речь (тишина тарифицируется как речь); после речи — пауза на размышление."""
        import numpy as np
        import sounddevice as sd
        loop, q = asyncio.get_running_loop(), asyncio.Queue()
        try:
            stream = sd.RawInputStream(samplerate=16000, channels=1, dtype="int16", blocksize=1600,
                                       callback=lambda data, *_: loop.call_soon_threadsafe(q.put_nowait, bytes(data)))
            stream.start()
        except Exception as ex:  # noqa: BLE001
            return emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})
        log("mic.start", duplex=self.duplex)
        emit({"state": "микрофон слушает"})
        talking, quiet, pre, hang = False, 0, deque(maxlen=PRE), self.pause_ms // 100
        while True:
            b = await q.get()
            if self.spk.busy and not self.duplex:  # Gemini говорит: свой голос из колонок не слушаем
                pre.clear()
                continue
            loud = float(np.sqrt(np.mean(np.frombuffer(b, dtype=np.int16).astype(np.float32) ** 2))) > LOUD
            try:
                if loud and not talking:
                    talking = True
                    self.core.hear(True)
                    log("mic.speech.start")
                    emit({"state": "слышу вас"})
                    for p in pre:
                        await self.send(chunk(p))
                    pre.clear()
                if not talking:
                    pre.append(b)
                    continue
                await self.send(chunk(b))
                quiet = 0 if loud else quiet + 1
                if quiet > hang:
                    talking = False
                    log("mic.speech.end", pause_ms=self.pause_ms)
                    await self.send({"realtimeInput": {"audioStreamEnd": True}})
                    self.core.hear(False)
                    emit({"state": "жду ответа"})
            except Exception as ex:  # noqa: BLE001
                talking = False
                self.core.hear(False)
                emit({"error": f"отправка в Gemini: {type(ex).__name__}: {ex}"})
                await asyncio.sleep(2)


class Speaker:
    """Колонки компьютера: голос Gemini (PCM 24 кГц) копится в буфере, звуковая карта забирает его сама."""

    def __init__(self) -> None:
        import sounddevice as sd
        self.buf = bytearray()
        self.until = 0.0
        self.stream = sd.RawOutputStream(samplerate=24000, channels=1, dtype="int16", callback=self._fill)
        self.stream.start()

    def _fill(self, outdata, frames, _time, _status) -> None:
        n = frames * 2
        part = bytes(self.buf[:n])
        del self.buf[:n]
        outdata[:len(part)] = part
        outdata[len(part):] = bytes(n - len(part))

    def add(self, pcm: bytes) -> None:
        self.buf.extend(pcm)
        self.until = time.monotonic() + len(self.buf) / 48000 + 0.5  # хвост: эхо от колонок затихает

    def clear(self) -> None:
        self.buf.clear()
        self.until = 0.0

    @property
    def busy(self) -> bool:
        return bool(self.buf) or time.monotonic() < self.until


def chunk(pcm: bytes) -> dict:
    return {"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(pcm).decode()}}}


def chime() -> bytes:
    """Короткий сигнал «передано» (PCM 24 кГц) вместо слов."""
    import numpy as np
    n = int(24000 * 0.12)
    t = np.arange(n) / 24000
    env = np.minimum(1.0, np.minimum(t / 0.01, (0.12 - t) / 0.03))
    return (np.sin(2 * np.pi * 880 * t) * env * 0.25 * 32767).astype(np.int16).tobytes()


def gemini_env() -> dict:
    """Ключ и прокси — ~/.config/gemini.env, переменные окружения GEMINI_* поверх."""
    e = {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / ".config/gemini.env").read_text().splitlines() if "=" in l and not l.startswith("#"))}
    e.update({k: v for k, v in os.environ.items() if k.startswith("GEMINI_")})
    e["GEMINI_PROXY"] = e.get("GEMINI_PROXY") or None
    return e
