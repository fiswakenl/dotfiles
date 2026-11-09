"""Адаптер Deepgram: потоковое распознавание Pipecat (DeepgramSTTService), модель nova-3, русский.

В Deepgram идёт весь звук микрофона, без своего детектора речи: паузы слышит сам Deepgram. Конец реплики — когда он прислал
окончательный кусок с speech_final (услышал паузу) и классификатор по тексту отвечает, что мысль закончена; не закончена —
ждём, пока человек продолжит, но не дольше паузы (GV_PAUSE_MS) без нового текста. Silero, Smart Turn и Finalize здесь не нужны.
Включены и события самого Deepgram: SpeechStarted (vad_events) — услышал голос, UtteranceEnd (utterance_end_ms) — после
последнего слова прошло UTTERANCE_END_MS; они только в запись запуска. Ключ — DEEPGRAM_API_KEY в ~/.config/deepgram.env.
"""
import asyncio
import os
import time
from pathlib import Path

from deepgram.listen.v1 import ListenV1Results, ListenV1SpeechStarted, ListenV1UtteranceEnd
from pipecat.frames.frames import InterimTranscriptionFrame, TranscriptionFrame
from pipecat.processors.frame_processor import FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language

from adapters.recognizer import Recognizer
from journal import log, trace
from pipecat.turns.user_stop.classifier_user_turn_stop_strategy import TURN_COMPLETION_QUESTION

UTTERANCE_END_MS = 1000  # UtteranceEnd: столько без новых слов; меньше 1000 Deepgram не принимает


def key() -> str:
    """Переменная окружения DEEPGRAM_API_KEY поверх ~/.config/deepgram.env."""
    if k := os.environ.get("DEEPGRAM_API_KEY"):
        return k
    for line in (Path.home() / ".config/deepgram.env").read_text().splitlines():
        if line.startswith("DEEPGRAM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("нет DEEPGRAM_API_KEY в ~/.config/deepgram.env")


class Deepgram(Recognizer):
    title = "Deepgram"

    def stt(self):
        return Traced(api_key=key(), settings=DeepgramSTTService.Settings(
            model="nova-3", language=Language.RU, punctuate=True, smart_format=True,
            utterance_end_ms=UTTERANCE_END_MS, extra={"vad_events": True}))

    def processors(self) -> list:
        return [self.stt(), Turns(self)]


class Traced(DeepgramSTTService):
    """Сервис Pipecat как есть, плюс события Deepgram в запись запуска."""

    async def _on_message(self, message):
        if isinstance(message, ListenV1Results) and message.is_final and getattr(message, "from_finalize", False):
            alt = message.channel.alternatives[0] if message.channel and message.channel.alternatives else None
            trace("dg.finalize.reply", text=alt.transcript if alt else "")
        elif isinstance(message, ListenV1SpeechStarted):  # время — секунды от начала присланного ему звука
            trace("dg.speech_started", dg_at=message.timestamp)
        elif isinstance(message, ListenV1UtteranceEnd):
            trace("dg.utterance_end", dg_last_word_end=message.last_word_end)
        await super()._on_message(message)


class Turns(FrameProcessor):
    """Реплики из потока Deepgram. Черновики и окончательные куски — в раннюю проверку адаптера; окончательный с speech_final —
    вопрос классификатору «мысль закончена?»: да — реплика закрыта. Нет или он не ответил — закрываем, если PAUSE_MS нет нового
    текста. Пауза перед репликой — от конца прошлой до первого куска этой (по приходу текста, а не по звуку)."""

    def __init__(self, adapter: Deepgram) -> None:
        super().__init__()
        self.adapter = adapter
        self.parts: list[str] = []
        self.closed: float | None = None  # когда закрыта прошлая реплика
        self.pause: float | None = None
        self.asking: asyncio.Task | None = None
        self.waiting: asyncio.TimerHandle | None = None

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and frame.text.strip():
            self.heard_more()
            if isinstance(frame, TranscriptionFrame):
                self.parts.append(frame.text.strip())
                if getattr(frame.result, "speech_final", False):
                    self.asking = asyncio.create_task(self.ask(" ".join(self.parts)))
                self.waiting = asyncio.get_running_loop().call_later(self.adapter.pause_ms / 1000, self.close)
            self.adapter.hearing(" ".join([*self.parts] + ([] if isinstance(frame, TranscriptionFrame) else [frame.text.strip()])), self.pause)
        await self.push_frame(frame, direction)

    def heard_more(self) -> None:
        """Пришёл текст: начало реплики — запомнить паузу перед ней; продолжение — прежние ожидания конца отменить."""
        if not self.parts and self.pause is None:
            self.pause = None if self.closed is None else time.monotonic() - self.closed
        for task in (self.asking, self.waiting):
            if task:
                task.cancel()
        self.asking = self.waiting = None

    async def ask(self, text: str) -> None:
        t0 = time.monotonic()
        try:
            r = (await asyncio.wait_for(self.adapter.decisions.choice({"user": text}, {"turn": TURN_COMPLETION_QUESTION}), 2.0))["turn"]
        except Exception as ex:  # noqa: BLE001 — не ответил: реплику закроет пауза без текста
            return log("turn.check.error", error=f"{type(ex).__name__}: {ex}")
        log("turn.check", verdict=r.choice, p=round(r.confidence, 2), s=round(time.monotonic() - t0, 2), text=text)
        if r.choice == "complete" and self.parts:
            self.close()

    def close(self) -> None:
        if self.waiting:
            self.waiting.cancel()
        text, self.parts, self.waiting = " ".join(self.parts), [], None
        if text:
            self.closed = time.monotonic()
            self.adapter.heard(text, self.pause)
        self.pause = None
