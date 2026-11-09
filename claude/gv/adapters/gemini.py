"""Адаптер Gemini Live: Gemini слушает человека и сама решает, когда и какую функцию вызвать; читает вслух ответы Claude.

Текст вызова — не её пересказ, а её же расшифровка речи; её собственная речь в колонки не идёт, кроме чтения ответов.
О человеке и ходах Gemini сообщает ядру: core.hear(), core.turn_end(), core.heartbeat().
"""
import asyncio
import base64
import json
import os
import re
import time
from pathlib import Path

from adapters import PROMPTS, Adapter
from adapters.mic import Mic
from journal import emit, log, rec_close, rec_open

GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODEL = "models/gemini-3.8-live"
IDLE_S = 30  # столько тишины — и сокет закрываем: открытый копит память разговора и закрывается Google сам
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
    """Инструкция Gemini и необязательная дописка к ней (prompts/context.md)."""
    return "\n\n".join(f.read_text() for f in (PROMPTS / "gemini.md", PROMPTS / "context.md") if f.exists())


def about() -> str:
    return system_text() + "\n\n— Функции —\n" + "\n".join(f"{t['name']}: {t['description']}" for t in tools()[0]["functionDeclarations"])


class Live(Adapter):
    """Gemini Live: сокет открывается по требованию (заговорил, пришёл ответ) и закрывается после тишины; микрофон и колонки компьютера."""

    title = "Gemini Live"
    speaks = True

    def __init__(self, core) -> None:
        super().__init__(core)
        self.duplex = os.environ.get("GV_DUPLEX", "on") == "on"  # слушать и пока Gemini говорит: её можно перебить (громкие колонки дают эхо)
        self.ws = None
        self.lock = asyncio.Lock()
        self.speaking = False  # Gemini читает ответ Claude; остальная её речь (ответы от себя) не звучит — она телефон
        self.voiced = False  # с начала чтения от неё уже пришёл голос: только после этого «ход окончен» — конец чтения
        self.sent = self.got = 0
        self.last = time.monotonic()  # последнее движение в голосовом канале
        self.heard: list[str] = []  # расшифровка речи человека после прошлого вызова — её и передаём, слово в слово
        self.called = 0.0  # когда был прошлый вызов
        self.spk = Speaker()

    def start(self) -> list:
        return [self.sleeper(), self.mic()]

    def about(self) -> str:
        return about()

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
            if history := self.history():
                setup["historyConfig"] = {"initialHistoryInClientContent": True}
            ws = await websockets.connect(GEMINI_WS, additional_headers={"x-goog-api-key": env["GEMINI_API_KEY"]}, proxy=env["GEMINI_PROXY"], max_size=None)
            await ws.send(json.dumps({"setup": setup}))
            await asyncio.wait_for(ws.recv(), 10)  # setupComplete; отказ (ключ, деньги) — исключение здесь, у того, кто открывал
            if history:
                await ws.send(json.dumps({"clientContent": {"turns": [{"role": "user", "parts": [{"text": history}]}], "turnComplete": False}}))
            self.ws, self.last = ws, time.monotonic()
            log("gemini.open", model=MODEL, duplex=self.duplex, pause_ms=self.pause_ms, history=len(history))
            rec_open(model=MODEL, duplex=self.duplex, pause_ms=self.pause_ms)
            asyncio.create_task(self.read(ws))
            emit({"state": "Gemini проснулась"})
            return ws

    def history(self) -> str:
        """Что было до этого сокета: последние реплики с номерами и ответы — чтобы replaces работал и после сна."""
        lines = []
        for r in self.core.recent():
            lines.append(f"№{r.id} ({r.kind}): {r.text}")
            if r.answer:
                lines.append(f"  ответ Claude: {r.answer[:300]}")
        return "Переданные раньше реплики (для replaces):\n" + "\n".join(lines) if lines else ""

    async def send(self, obj: dict) -> None:
        ws = await self.open()
        if audio := obj.get("realtimeInput", {}).get("audio"):
            self.sent += len(audio["data"]) * 3 // 4
        self.last = time.monotonic()
        await ws.send(json.dumps(obj))

    async def sleeper(self) -> None:
        """Тишина IDLE_S секунд — закрываем сокет сами; проснётся на речи или ответе Claude."""
        while True:
            await asyncio.sleep(2)
            if self.ws and self.core.quiet() and time.monotonic() - self.last > IDLE_S:
                log("gemini.sleep", idle_s=IDLE_S)
                await self.ws.close()

    async def read(self, ws) -> None:
        why = "закрыт сервером"
        try:
            async for raw in ws:
                self.core.heartbeat()
                self.last = time.monotonic()
                e = json.loads(raw)
                if "error" in e:
                    log("gemini.error", error=str(e["error"])[:500])
                    emit({"error": str(e["error"])})
                for fc in e.get("toolCall", {}).get("functionCalls", []):
                    asyncio.create_task(self.on_call(fc))  # в фоне: вызов ждёт расшифровку, а она приходит тем же сокетом
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
            self.speaking = self.voiced = False  # чтение, если шло, оборвалось вместе с сокетом
            self.core.turn_end()  # ход, которого ждали, уже не закончится
            emit({"state": "Gemini спит"})

    # ── что пришло от Gemini ──
    async def on_call(self, fc: dict) -> None:
        """Gemini решает, когда и что вызвать; текст — не её пересказ, а всё, что она расшифровала после прошлого вызова."""
        await self.settle()
        args, heard = fc.get("args", {}), "".join(self.heard).strip()
        self.heard.clear()
        log("gemini.call", name=fc["name"], args=args, heard=heard)
        if not heard and time.monotonic() - self.called < 5:  # повтор того же вызова: нового ничего не сказано
            result = "Уже передано."
        else:
            self.called = time.monotonic()
            self.spk.add(chime())
            result = self.core.call(fc["name"], {**args, "heard": heard} if heard else args)
        try:
            await self.ws.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": result, "scheduling": "SILENT"}}]}}))
        except Exception:  # noqa: BLE001 — сокет оборвался: вызов и так пропал
            pass

    async def settle(self, most: float = 1.5, quiet: float = 0.3) -> None:
        """Расшифровка иногда приходит после вызова: ждём, пока она появится и перестанет прибывать (не дольше most)."""
        end, count, since = time.monotonic() + most, len(self.heard), time.monotonic()
        while time.monotonic() < end:
            await asyncio.sleep(0.1)
            if len(self.heard) != count:
                count, since = len(self.heard), time.monotonic()
            elif self.heard and time.monotonic() - since > quiet:
                return

    async def on_content(self, c: dict) -> None:
        for p in c.get("modelTurn", {}).get("parts", []):
            if "inlineData" in p:
                pcm = base64.b64decode(p["inlineData"]["data"])
                self.got += len(pcm)
                if self.speaking:
                    self.voiced = True
                    self.spk.add(pcm)
        if t := c.get("inputTranscription", {}).get("text"):
            self.heard.append(t)
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
            if self.speaking and not self.voiced:  # запоздалый конец прошлого хода: чтение ещё не началось
                return log("turn.stale")
            if self.heard and not self.heard[-1].endswith((" ", "\n")):
                self.heard.append(" ")  # следующая реплика — через пробел, не слитно
            self.speaking = False
            log("turn", audio_sent_bytes=self.sent, audio_got_bytes=self.got)
            self.core.turn_end()

    # ── что уходит в Gemini ──
    async def say(self, text: str) -> bool:
        """False — не ушло: ядро вернёт ответ в очередь и попробует позже."""
        self.speaking, self.voiced = True, False
        try:
            await self.send({"realtimeInput": {"text": text}})
            return True
        except Exception as ex:  # noqa: BLE001
            self.speaking = False
            emit({"error": f"отправка в Gemini: {type(ex).__name__}: {ex}"})
            return False

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
        mic = Mic(self.pause_ms, muted=lambda: self.spk.busy and not self.duplex)  # без duplex свой голос из колонок не слушаем
        log("mic.start", duplex=self.duplex)
        async for event, pcm in mic.blocks():
            try:
                if event == "start":
                    self.core.hear(True)
                elif event == "audio":
                    await self.send(chunk(pcm))
                else:
                    await self.send({"realtimeInput": {"audioStreamEnd": True}})
                    self.core.hear(False)
                    emit({"state": "жду ответа"})
            except Exception as ex:  # noqa: BLE001
                mic.stop()
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
