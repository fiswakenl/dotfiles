"""Адаптер Deepgram: потоковое распознавание Pipecat (DeepgramSTTService), модель nova-3, русский.

Сокет один на всё время работы, тишину держит KeepAlive; Deepgram берёт деньги за присланный звук, поэтому шлём только речь —
от начала по Silero VAD (с предысторией: VAD узнаёт речь с опозданием) до её конца. Конец речи — Finalize, Deepgram досылает
окончательный текст. Закрыть кусок он может и сам, услышав паузу раньше нас; в запись запуска идут и наше Finalize, и ответ на
него — по ним видно, кто закрыл кусок. Включены и события самого Deepgram: SpeechStarted (vad_events) — услышал голос,
UtteranceEnd (utterance_end_ms) — после последнего слова прошло UTTERANCE_END_MS; они только в запись, решений по ним нет. Ключ — DEEPGRAM_API_KEY в ~/.config/deepgram.env.
"""
import os
from collections import deque
from pathlib import Path

from deepgram.listen.v1 import ListenV1Results, ListenV1SpeechStarted, ListenV1UtteranceEnd
from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language

from adapters.recognizer import BLOCK_S, Recognizer
from journal import trace

PRE_S = 0.6  # предыстория речи: столько звука до того, как VAD её узнал
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
        return SpeechOnly(api_key=key(), settings=DeepgramSTTService.Settings(
            model="nova-3", language=Language.RU, punctuate=True, smart_format=True,
            utterance_end_ms=UTTERANCE_END_MS, extra={"vad_events": True}))


class SpeechOnly(DeepgramSTTService):
    """В Deepgram — только речь; дальше по конвейеру (Smart Turn) звук идёт весь."""

    def __init__(self, **kwargs) -> None:
        super().__init__(**kwargs)
        self.speaking = False
        self.pre = deque(maxlen=round(PRE_S / BLOCK_S))

    async def process_frame(self, frame, direction) -> None:
        if isinstance(frame, VADUserStartedSpeakingFrame):
            self.speaking = True
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.speaking = False
        await super().process_frame(frame, direction)
        if isinstance(frame, VADUserStoppedSpeakingFrame) and self._connection:  # базовый класс только что послал Finalize
            trace("dg.finalize", why="silero: пауза")

    async def _on_message(self, message):
        if isinstance(message, ListenV1Results) and message.is_final and getattr(message, "from_finalize", False):
            alt = message.channel.alternatives[0] if message.channel and message.channel.alternatives else None
            trace("dg.finalize.reply", text=alt.transcript if alt else "")
        elif isinstance(message, ListenV1SpeechStarted):  # время — секунды от начала присланного ему звука
            trace("dg.speech_started", dg_at=message.timestamp)
        elif isinstance(message, ListenV1UtteranceEnd):
            trace("dg.utterance_end", dg_last_word_end=message.last_word_end)
        await super()._on_message(message)

    async def process_audio_frame(self, frame, direction) -> None:
        if not self.speaking:
            return self.pre.append(frame)
        while self.pre:
            await super().process_audio_frame(self.pre.popleft(), direction)
        await super().process_audio_frame(frame, direction)
