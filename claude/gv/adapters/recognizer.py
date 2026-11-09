"""Общее у адаптеров распознавания — конвейер Pipecat: реплика в ядро как распознана, что с ней делать, решает оркестратор плагина.

    Mic (sounddevice) → Silero VAD → распознавание (у наследника: stt()) → Utterances: конец реплики (Smart Turn) → core.utter()

Конец реплики решает модель Smart Turn по звуку (локально, на CPU): закончил ли человек мысль; молчит дольше паузы (GV_PAUSE_MS) —
реплика закончена и так. Всё распознанное между концами реплик — одна реплика. Синтеза речи и языковой модели в конвейере нет.
"""
import asyncio
import base64
import sys
import time

import sounddevice as sd
from loguru import logger
from pipecat.audio.turn.smart_turn.base_smart_turn import SmartTurnParams
from pipecat.audio.turn.smart_turn.local_smart_turn_v3 import LocalSmartTurnAnalyzerV3
from pipecat.audio.vad.silero import SileroVADAnalyzer
from pipecat.classifiers.base_classifier import BaseClassifier
from pipecat.classifiers.openai.decisions.classifier import OpenAIDecisionsClassifier
from pipecat.frames.frames import (InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame, VADUserStartedSpeakingFrame,
                                   VADUserStoppedSpeakingFrame)
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.audio.vad_processor import VADProcessor
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_stop.classifier_user_turn_stop_strategy import TURN_COMPLETION_QUESTION, ClassifierUserTurnStopStrategy
from pipecat.turns.user_stop import TurnAnalyzerUserTurnStopStrategy
from pipecat.turns.user_turn_processor import UserTurnProcessor
from pipecat.turns.user_turn_strategies import UserTurnStrategies
from pipecat.workers.runner import WorkerRunner

from adapters import Adapter, router
from adapters.trace import Trace
from journal import emit, log, trace_open

RATE = 16000
BLOCK_S = 0.02  # блок микрофона

logger.remove()
logger.add(sys.stderr, level="WARNING")  # в ~/.claude/gv.out — только сбои Pipecat
# Решения о конце реплики — штатные отладочные записи Pipecat (Smart Turn, классификатор: вердикт и уверенность) — в журнал GV.
TURN_LOGS = ("pipecat.audio.turn", "pipecat.turns.user_stop", "pipecat.services.deepgram")
logger.add(lambda m: log("turn", src=m.record["name"].rsplit(".", 1)[-1], text=m.record["message"]),
           level="DEBUG", filter=lambda r: r["name"].startswith(TURN_LOGS), format="{message}")


class Recognizer(Adapter):
    def __init__(self, core) -> None:
        super().__init__(core)
        self.mic = Mic()

    def stt(self):
        """Сервис распознавания Pipecat."""
        raise NotImplementedError

    def classifier(self) -> BaseClassifier:
        """Классификатор законченности по тексту — OpenAI Decisions (~0,2 с на ответ); ключ — как у адаптера OpenAI,
        прокси — HTTPS_PROXY из окружения."""
        from adapters.openai import env
        return OpenAIDecisionsClassifier(api_key=env()["OPENAI_API_KEY"])

    def completion(self) -> ClassifierUserTurnStopStrategy:
        """Второе мнение о конце реплики — по тексту: Smart Turn слышит интонацию, классификатор читает, закончена ли мысль.
        Реплика кончается, когда согласны оба (штатный режим Pipecat); «не договорил» — ждёт продолжения, заговорил — ожидание
        сбрасывается. Классификатор не ответил вовремя — решает один Smart Turn."""
        return ClassifierUserTurnStopStrategy(classifier=self.decisions, classification_timeout=2.0)

    def processors(self) -> list:
        """Конвейер после микрофона: Silero режет речь на отрезки, распознавание, конец реплики — Smart Turn и классификатор."""
        pause = self.pause_ms / 1000
        turn = TurnAnalyzerUserTurnStopStrategy(turn_analyzer=LocalSmartTurnAnalyzerV3(params=SmartTurnParams(stop_secs=pause)))
        return [VADProcessor(vad_analyzer=SileroVADAnalyzer()), self.stt(),
                Utterances(self, UserTurnStrategies(stop=[turn, self.completion()]), stop_timeout=pause + 2)]

    async def keep_warm(self, classifier: BaseClassifier) -> None:
        """Соединение с классификатором держим тёплым. Первое Pipecat открывает сам при запуске конвейера, а простаивающее
        закрывает через 240 с — тогда первая пауза после перерыва ждала бы рукопожатия, 2–3 с. Раньше этого — пустой вопрос."""
        while True:
            await asyncio.sleep(200)
            try:
                t0 = time.monotonic()
                await asyncio.wait_for(classifier.choice("привет", {"turn": TURN_COMPLETION_QUESTION}), 10)
                log("turn.warm", s=round(time.monotonic() - t0, 2))
            except Exception as ex:  # noqa: BLE001 — не прогрелось: первая пауза просто подождёт
                log("turn.warm.error", error=f"{type(ex).__name__}: {ex}")

    def start(self) -> list:
        return [self.run()]

    async def run(self) -> None:
        self.decisions = self.classifier()  # один на конец реплики и на маршрут: одно тёплое соединение
        self.order = asyncio.Lock()
        self.delivering: set[asyncio.Task] = set()
        self.partial = self.asked = ""  # что уже слышно в идущей фразе; по какому её началу уже спросили классификатор
        self.settled = False  # ход Claude этой фразой уже остановлен
        self.checking: asyncio.Task | None = None

        @self.decisions.event_handler("on_metrics")
        async def timing(_c, data) -> None:  # сколько думал классификатор: штатные метрики Pipecat
            log("turn.metrics", **{type(d).__name__: getattr(d.value, "model_dump", lambda: d.value)() for d in data})

        self.warm = asyncio.create_task(self.keep_warm(self.decisions))
        pipeline = Pipeline([self.mic, *self.processors()])
        record = trace_open()
        worker = PipelineWorker(pipeline, idle_timeout_secs=None, enable_rtvi=False, enable_turn_tracking=False,
                                observers=[Trace(record.with_suffix(".wav"))])

        @worker.event_handler("on_pipeline_error")
        async def on_error(_worker, frame) -> None:
            self.error(str(frame.error))

        log("mic.start", adapter=self.title)
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
        self.error("конвейер распознавания остановился")

    def hearing(self, text: str, pause: float | None) -> None:
        """Фраза ещё звучит, text — всё, что в ней уже распознано: пока Claude работает над последней репликой, по этому началу
        проверяем, не её ли это продолжение, — тогда его ход останавливается сразу. Проверка одна за раз, каждая — по свежему тексту."""
        self.partial = text
        if not self.settled and (self.checking is None or self.checking.done()):
            self.checking = asyncio.create_task(self.check(pause))

    async def check(self, pause: float | None) -> None:
        while not self.settled and (text := self.partial) != self.asked:
            self.asked = text
            if last := await router.early(self.decisions, self.core, text, pause):
                self.settled = self.core.interrupt(last.id)

    def heard(self, text: str, pause: float | None = None) -> None:
        """Реплика закончена: распознанный текст — на решение, куда его отправить."""
        log("recognized", adapter=self.title, text=text)
        if self.checking:  # опоздавшее раннее «да» остановило бы ход уже после решения по всей фразе
            self.checking.cancel()
        self.partial = self.asked = ""
        self.settled = False
        if text:
            emit({"me": text})
            task = asyncio.create_task(self.deliver(text, pause))
            self.delivering.add(task)
            task.add_done_callback(self.delivering.discard)
        else:
            emit({"state": "ничего не распознано"})
            self.core.resume()  # ход остановили, а фразы не оказалось: прерванный запрос — заново

    async def deliver(self, text: str, pause: float | None) -> None:
        """Решение классификатора — в ядро, по одной реплике в порядке речи: следующая видит, куда ушла прошлая."""
        async with self.order:
            d = await router.decide(self.decisions, self.core, text, pause)
            self.core.call(d.name, {"heard": text, "replaces": d.replaces, "sure": True})

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


class Utterances(UserTurnProcessor):
    """Конец конвейера вместо модели: тот же UserTurnProcessor, что решает конец реплики, плюс сбор распознанного.
    Текст собирается здесь же: кадр с текстом проходит сюда раньше, чем по нему решается конец реплики.
    Незакрытый кусок распознанного (он ещё может поменяться) в реплику не идёт — только в раннюю проверку адаптера."""

    def __init__(self, adapter: Recognizer, strategies: UserTurnStrategies, stop_timeout: float) -> None:
        super().__init__(user_turn_strategies=strategies, user_turn_stop_timeout=stop_timeout)
        self.adapter = adapter
        self.parts: list[str] = []
        self.quiet: float | None = None  # когда человек в последний раз замолчал (по VAD)
        self.pause: float | None = None  # сколько он молчал перед идущей репликой
        self.fresh = True  # идущая реплика ещё не началась
        self.add_event_handler("on_user_turn_started", self.started)
        self.add_event_handler("on_user_turn_stopped", self.stopped)

    async def process_frame(self, frame, direction) -> None:
        if isinstance(frame, VADUserStartedSpeakingFrame) and self.fresh:
            self.pause, self.fresh = None if self.quiet is None else time.monotonic() - self.quiet, False
        elif isinstance(frame, VADUserStoppedSpeakingFrame):
            self.quiet = time.monotonic()
        elif isinstance(frame, TranscriptionFrame) and frame.text.strip():
            self.parts.append(frame.text.strip())
            self.adapter.hearing(" ".join(self.parts), self.pause)
        elif isinstance(frame, InterimTranscriptionFrame) and frame.text.strip():
            self.adapter.hearing(" ".join([*self.parts, frame.text.strip()]), self.pause)
        await super().process_frame(frame, direction)

    async def started(self, _processor, _strategy) -> None:
        emit({"state": "слышу вас"})

    async def stopped(self, _processor, _strategy) -> None:
        text, self.parts, self.fresh = " ".join(self.parts), [], True
        self.adapter.heard(text, self.pause)
