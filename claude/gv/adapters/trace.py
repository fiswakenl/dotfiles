"""Наблюдатель конвейера Pipecat — всё, что прошло по конвейеру, в запись запуска (journal.trace) для разбора таймлайном.

Пишет по одному разу на кадр (first_push): речь и тишину по Silero, начало и конец реплики, промежуточный и окончательный
текст Deepgram с его флагами и словами, ошибки. Звук микрофона — в WAV рядом с записью; момент первого куска — событие
audio.start: от него считается время в файле звука. Решения (Smart Turn, классификатор, маршрут, отправки) пишет journal.log.
"""
import wave
from pathlib import Path

from pipecat.frames.frames import (ErrorFrame, InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame,
                                   UserStartedSpeakingFrame, UserStoppedSpeakingFrame, VADUserStartedSpeakingFrame,
                                   VADUserStoppedSpeakingFrame)
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.processors.frame_processor import FrameDirection

from journal import trace

EVENTS = {VADUserStartedSpeakingFrame: "vad.start", VADUserStoppedSpeakingFrame: "vad.stop",
          UserStartedSpeakingFrame: "user.start", UserStoppedSpeakingFrame: "user.stop"}


def deepgram(result) -> dict:
    """Из ответа Deepgram — его флаги и слова со временем (секунды от начала присланного ему звука)."""
    try:
        d = result.to_dict() if hasattr(result, "to_dict") else result.model_dump() if hasattr(result, "model_dump") else dict(result)
    except Exception:  # noqa: BLE001 — другой сервис или формат: только текст
        return {}
    alt = ((d.get("channel") or {}).get("alternatives") or [{}])[0]
    return {"is_final": d.get("is_final"), "speech_final": d.get("speech_final"), "dg_start": d.get("start"),
            "dg_duration": d.get("duration"), "confidence": alt.get("confidence"),
            "words": [[w.get("word"), w.get("start"), w.get("end"), w.get("confidence")] for w in alt.get("words") or []]}


class Trace(BaseObserver):
    def __init__(self, wav: Path) -> None:
        super().__init__()
        self.path = wav
        self.wav: wave.Wave_write | None = None

    async def on_push_frame(self, data: FramePushed) -> None:
        f = data.frame
        if not data.first_push or data.direction != FrameDirection.DOWNSTREAM:  # события Silero и реплики Pipecat
            return  # рассылает в обе стороны копиями — пишем ту, что идёт вниз
        if isinstance(f, InputAudioRawFrame):
            if self.wav is None:
                self.wav = wave.open(str(self.path), "wb")
                self.wav.setnchannels(f.num_channels)
                self.wav.setsampwidth(2)
                self.wav.setframerate(f.sample_rate)
                trace("audio.start", file=self.path.name, rate=f.sample_rate)
            self.wav.writeframes(f.audio)
            return
        if ev := EVENTS.get(type(f)):
            trace(ev)
        elif isinstance(f, InterimTranscriptionFrame):
            trace("stt.interim", text=f.text, **deepgram(f.result))
        elif isinstance(f, TranscriptionFrame):
            trace("stt.final", text=f.text, finalized=f.finalized, **deepgram(f.result))
        elif isinstance(f, ErrorFrame):
            trace("error", error=str(f.error))
