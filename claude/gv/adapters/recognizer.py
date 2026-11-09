"""Общее у адаптеров распознавания — конвейер Pipecat: реплика в ядро как распознана, что с ней делать, решает оркестратор плагина.

    Mic (sounddevice) → Silero VAD → распознавание (у наследника: stt()) → Utterances: конец реплики (Smart Turn) → core.utter()

Конец реплики решает модель Smart Turn по звуку (локально, на CPU): закончил ли человек мысль; молчит дольше паузы (GV_PAUSE_MS) —
реплика закончена и так. Всё распознанное между концами реплик — одна реплика. Синтеза речи и языковой модели в конвейере нет.
"""
import asyncio
import base64
import os
import sys

import sounddevice as sd
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.classifiers.llm.classifier import LLMClassifier
from pipecat.frames.frames import InputAudioRawFrame, TranscriptionFrame
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.services.openai.llm import OpenAILLMService
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_stop.classifier_user_turn_stop_strategy import ClassifierUserTurnStopStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from adapters import Adapter

TURN_MODEL = os.environ.get("GV_TURN_MODEL", "gemini-3.5-flash-lite")
from journal import emit, log

RATE = 16000
BLOCK_S = 0.02  # блок микрофона

logger.remove()
logger.add(sys.stderr, level="WARNING")  # в ~/.claude/gv.out — только сбои Pipecat


class Recognizer(Adapter):
    def __init__(self, core) -> None:
        super().__init__(core)
        self.mic = Mic()

    def stt(self):
        """Сервис распознавания Pipecat."""
        raise NotImplementedError

    def start(self) -> list:
        return [self.run()]

    async def run(self) -> None:
        pause = self.pause_ms / 1000
        turn = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=pause)))
        pipeline = Pipeline([self.mic, VADProcessor(vad_analyzer=SileroVADAnalyzer()), self.stt(),
                             Utterances(self, UserTurnStrategies(stop=[turn, completion()]), stop_timeout=pause + 2)])
        worker = PipelineWorker(pipeline, idle_timeout_secs=None, enable_rtvi=False, enable_turn_tracking=False)

        @worker.event_handler("on_pipeline_error")
        async def on_error(_worker, frame) -> None:
            self.error(str(frame.error))

        log("mic.start", adapter=self.title)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
        self.error("конвейер распознавания остановился")

    def heard(self, text: str) -> None:
        """Реплика закончена: распознанный текст — в ядро."""
        log("recognized", adapter=self.title, text=text)
        if text:
            emit({"me": text})
            self.core.utter(text)
        else:
            emit({"state": "ничего не распознано"})

    def error(self, why: str) -> None:
        why = f"{self.title}: {why}"
        log("recognize.error", error=why[:500])
        emit({"error": why})

    async def listen(self, args: dict) -> str:
        """Речь вместо микрофона: args.pcm — base64 PCM 16 бит, 16 кГц, моно, одной репликой; за ней — тишина дольше паузы."""
        pcm = base64.b64decode(args.get("pcm", ""))
        log("audio.inject", bytes=len(pcm))
        await self.mic.play(pcm + bytes(int(RATE * (self.pause_ms / 1000 + 1)) * 2))
        return f"ok {len(pcm) / (RATE * 2):.1f} с"


class Mic(BaseInputTransport):
    """Вход конвейера — микрофон компьютера блоками по BLOCK_S. Не открылся — ошибка в журнал, конвейер ждёт без звука."""

    def __init__(self) -> None:
        super().__init__(TransportParams(audio_in_enabled=True, audio_in_sample_rate=RATE))
        self.stream = None

    async def start(self, frame) -> None:
        await super().start(frame)
        loop = asyncio.get_running_loop()
        captured = lambda data, *_: asyncio.run_coroutine_threadsafe(self.push_audio_frame(block(bytes(data))), loop)  # noqa: E731
        try:
            self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=int(RATE * BLOCK_S), callback=captured)
            self.stream.start()
            emit({"state": "микрофон слушает"})
        except Exception as ex:  # noqa: BLE001
            emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})
        await self.set_transport_ready(frame)

    async def play(self, pcm: bytes) -> None:
        """Звук вместо микрофона, в реальном времени."""
        step = int(RATE * BLOCK_S) * 2
        for i in range(0, len(pcm), step):
            await self.push_audio_frame(block(pcm[i:i + step]))
            await asyncio.sleep(BLOCK_S)

    async def cleanup(self) -> None:
        await super().cleanup()
        if self.stream:
            self.stream.close()


def block(pcm: bytes) -> InputAudioRawFrame:
    return InputAudioRawFrame(audio=pcm, sample_rate=RATE, num_channels=1)


def completion() -> ClassifierUserTurnStopStrategy:
    """Второе мнение о конце реплики — по тексту: Smart Turn слышит интонацию, классификатор читает, закончена ли мысль.
    Реплика кончается, когда согласны оба (штатный режим Pipecat); «не договорил» — ждёт продолжения, заговорил — ожидание
    сбрасывается. Классификатор не ответил вовремя — решает один Smart Turn.
    Модель — Gemini (GV_TURN_MODEL) через её OpenAI-совместимый вход; ключ и прокси — ~/.config/gemini.env, как у адаптера Gemini."""
    from adapters.gemini import gemini_env
    e = gemini_env()
    if e["GEMINI_PROXY"]:
        os.environ["HTTPS_PROXY"] = e["GEMINI_PROXY"]  # клиент OpenAI берёт прокси из окружения
    llm = OpenAILLMService(api_key=e["GEMINI_API_KEY"], base_url="https://generativelanguage.googleapis.com/v1beta/openai/",
                           settings=OpenAILLMService.Settings(model=TURN_MODEL))
    return ClassifierUserTurnStopStrategy(classifier=LLMClassifier(llm=llm, max_tokens=200), classification_timeout=2.0)


class Utterances(UserTurnProcessor):
    """Конец конвейера вместо модели: тот же UserTurnProcessor, что решает конец реплики, плюс сбор распознанного.
    Текст собирается здесь же: кадр с текстом проходит сюда раньше, чем по нему решается конец реплики."""

    def __init__(self, adapter: Recognizer, strategies: UserTurnStrategies, stop_timeout: float) -> None:
        super().__init__(user_turn_strategies=strategies, user_turn_stop_timeout=stop_timeout)
        self.adapter = adapter
        self.parts: list[str] = []
        self.add_event_handler("on_user_turn_started", self.started)
        self.add_event_handler("on_user_turn_stopped", self.stopped)

    async def process_frame(self, frame, direction) -> None:
        if isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.parts.append(frame.text.strip())
        await super().process_frame(frame, direction)

    async def started(self, _processor, _strategy) -> None:
        emit({"state": "слышу вас"})

    async def stopped(self, _processor, _strategy) -> None:
        text, self.parts = " ".join(self.parts), []
        self.adapter.heard(text)
