"""Адаптер Deepgram: потоковое распознавание Pipecat (DeepgramSTTService), модель nova-3, русский.

Сокет один на всё время работы, тишину держит KeepAlive; Deepgram берёт деньги за присланный звук, поэтому шлём только речь —
от начала по Silero VAD (с предысторией: VAD узнаёт речь с опозданием) до её конца. Конец речи — Finalize, Deepgram досылает
окончательный текст. Ключ — DEEPGRAM_API_KEY в ~/.config/deepgram.env.
"""
import os
from collections import deque
from pathlib import Path

from pipecat.frames.frames import VADUserStartedSpeakingFrame, VADUserStoppedSpeakingFrame
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language

from adapters.recognizer import BLOCK_S, Recognizer

PRE_S = 0.6  # предыстория речи: столько звука до того, как VAD её узнал


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
            model="nova-3", language=Language.RU, punctuate=True, smart_format=True))


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

    async def process_audio_frame(self, frame, direction) -> None:
        if not self.speaking:
            return self.pre.append(frame)
        while self.pre:
            await super().process_audio_frame(self.pre.popleft(), direction)
        await super().process_audio_frame(frame, direction)
