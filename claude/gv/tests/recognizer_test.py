# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy", "pipecat-ai[deepgram]>=1.15,<2"]
# ///
"""Конвейер адаптеров распознавания (adapters/recognizer.py) на настоящих Silero VAD и Smart Turn, без сети и микрофона:
речь синтезирует macOS (say), вместо сервиса распознавания — ненастоящий: на каждый отрезок речи по VAD отвечает «отрезок N».
Проверяется, сколько реплик дошло до ядра. Запуск: uv run --script tests/recognizer_test.py"""
import asyncio
import logging
import subprocess
import sys
import tempfile
import unittest
import wave
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from pipecat.classifiers.base_classifier import BaseClassifier, ChoiceResult  # noqa: E402
from pipecat.frames.frames import TranscriptionFrame  # noqa: E402
from pipecat.services.settings import STTSettings  # noqa: E402
from pipecat.services.stt_service import SegmentedSTTService  # noqa: E402

import adapters.recognizer as recognizer  # noqa: E402
from adapters.recognizer import RATE, Recognizer  # noqa: E402

PAUSE_MS = 2500
logging.getLogger("asyncio").setLevel(logging.ERROR)  # отладочный режим цикла тестов ругается на долгие шаги моделей


def speech(text: str) -> bytes:
    """PCM 16 бит, 16 кГц, моно — голосом Milena."""
    with tempfile.TemporaryDirectory() as d:
        aiff, wav = Path(d, "a.aiff"), Path(d, "a.wav")
        subprocess.run(["say", "-v", "Milena", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{RATE}", "-c", "1", aiff, wav], check=True)
        with wave.open(str(wav)) as w:
            return w.readframes(w.getnframes())


def silence(seconds: float) -> bytes:
    return bytes(int(RATE * seconds) * 2)


class Segments(SegmentedSTTService):
    """Распознавание без сети: отрезок речи → «отрезок N»."""

    def __init__(self) -> None:
        super().__init__(settings=STTSettings(model=None, language=None), ttfs_p99_latency=0.5)
        self.n = 0

    async def run_stt(self, audio: bytes):
        self.n += 1
        yield TranscriptionFrame(f"отрезок {self.n}", "", "")


class Complete(BaseClassifier):
    """Классификатор без сети: любая реплика законченна — конец решает один Smart Turn."""

    async def _ask(self, state, questions, images=()):
        return {name: ChoiceResult(choice="complete", probabilities={"complete": 1.0}, confidence=1.0) for name in questions}, None


class Fake(Recognizer):
    title = "без сети"

    def stt(self):
        return Segments()

    def classifier(self):
        return Complete()


class Core:
    def __init__(self) -> None:
        self.utterances: list[str] = []

    def utter(self, text: str) -> None:
        self.utterances.append(text)


class NoMic:
    """Вместо микрофона: поток не открывается, звук даёт тест."""

    def __init__(self, *_, **__) -> None: ...

    def start(self) -> None: ...

    def close(self) -> None: ...


CASES = [  # (что сказано, сколько реплик дойдёт до ядра)
    ("законченная фраза — одна реплика", lambda: speech("Что у меня в списке задач на сегодня?"), 1),
    ("две фразы через долгую паузу — две реплики",
     lambda: speech("Открой файл с заметками.") + silence(PAUSE_MS / 1000 + 1.5) + speech("Сколько сейчас времени?"), 2),
    ("недосказанная мысль ждёт продолжения через короткую паузу — одна реплика",
     lambda: speech("Я хочу, чтобы ты, когда закончишь с тестами, и") + silence(1.2) + speech("потом обновил документацию."), 1),
    ("тишина — ничего", lambda: silence(1), 0),
]


class Pipeline(unittest.IsolatedAsyncioTestCase):
    async def test_cases(self):
        """Один конвейер на все случаи: Silero и Smart Turn грузятся один раз."""
        recognizer.sd.RawInputStream = NoMic
        recognizer.emit = lambda e: None
        recognizer.log = lambda *a, **k: None
        core = Core()
        adapter = Fake(core)
        adapter.pause_ms = PAUSE_MS
        run = asyncio.create_task(adapter.run())
        await asyncio.sleep(1)  # модели загружены, конвейер запущен
        try:
            for name, audio, count in CASES:
                with self.subTest(name):
                    core.utterances.clear()
                    await adapter.mic.play(audio() + silence(PAUSE_MS / 1000 + 1.5))
                    self.assertEqual(len(core.utterances), count, core.utterances)
        finally:
            run.cancel()
            await asyncio.gather(run, return_exceptions=True)


if __name__ == "__main__":
    unittest.main()
