"""Слух GV: микрофон → Deepgram → реплики → решение, куда их отправить. Конвейер Pipecat, решения — классификатор OpenAI Decisions.

    микрофон ──весь звук──▶ Deepgram (nova-3) ──текст──▶ Turns ──реплика──▶ решение ──▶ core.call(ask | quick)

Паузы слышит сам Deepgram. Реплика кончается, когда он прислал окончательный кусок с speech_final (услышал паузу) и классификатор
по тексту отвечает «мысль закончена»; не закончена или он не ответил — когда PAUSE_MS нет нового текста.

Куда реплику — тот же классификатор, вопрос да или нет: продолжает ли она последний переданный ask (договаривает, уточняет,
исправляет). Да — ask с replaces=N: ядро склеит текст и откатит №N. Нет — новая мысль: пока Claude занят — quick, свободен — ask.
Неуверенное «да» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.
Тот же вопрос задаётся и рано — по первым словам новой фразы, пока Claude работает над последней репликой: «да» останавливает его
ход сразу (core.interrupt); слов мало — вопрос повторяется на каждом новом куске текста. Решает всё равно вопрос по всей фразе.
Оба видят паузу перед фразой: договаривают сразу, к новой мысли переходят после паузы.

Всё, что прошло по конвейеру, — в запись запуска (journal.trace): текст Deepgram с флагами и словами, его события SpeechStarted
и UtteranceEnd, звук микрофона в WAV. Ключи: Deepgram — DEEPGRAM_API_KEY в ~/.config/deepgram.env, OpenAI — OPENAI_API_KEY
в ~/.config/openai.env (прокси — HTTPS_PROXY из окружения).
"""
import asyncio
import os
import sys
import time
import wave
from pathlib import Path

import sounddevice as sd
from deepgram.listen.v1 import ListenV1SpeechStarted, ListenV1UtteranceEnd
from loguru import logger
from pipecat.classifiers.base_classifier import YesNoQuestion
from pipecat.classifiers.openai.decisions.classifier import OpenAIDecisionsClassifier
from pipecat.frames.frames import ErrorFrame, InputAudioRawFrame, InterimTranscriptionFrame, TranscriptionFrame
from pipecat.observers.base_observer import BaseObserver, FramePushed
from pipecat.pipeline.pipeline import Pipeline
from pipecat.pipeline.worker import PipelineWorker
from pipecat.processors.frame_processor import FrameDirection, FrameProcessor
from pipecat.services.deepgram.stt import DeepgramSTTService
from pipecat.transcriptions.language import Language
from pipecat.transports.base_input import BaseInputTransport
from pipecat.transports.base_transport import TransportParams
from pipecat.turns.user_stop.classifier_user_turn_stop_strategy import TURN_COMPLETION_QUESTION
from pipecat.workers.runner import WorkerRunner

from journal import emit, log, trace, trace_open

RATE = 16000
BLOCK_S = 0.02  # блок микрофона
PAUSE_MS = int(os.environ.get("GV_PAUSE_MS", 2500))  # столько без нового текста — реплика закончена в любом случае
UTTERANCE_END_MS = 1000  # UtteranceEnd Deepgram: столько без новых слов; меньше 1000 он не принимает
SURE = 0.7  # вероятность продолжения ниже — не поправка, а новая мысль
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}
CUT = "Claude начал отвечать, но человек заговорил снова, и ответ остановлен"
QUESTION = {"continues": YesNoQuestion(instructions=(
    "Человек диктует голосом, речь нарезана по паузам. Новая реплика — продолжение или поправка предыдущей "
    "(договаривает, уточняет, исправляет её), а не отдельная законченная мысль?"))}
EARLY = {"continues": YesNoQuestion(instructions=(
    "Человек диктует голосом, речь нарезана по паузам. Предыдущая реплика уже передана, а человек заговорил снова: слышно только "
    "начало новой фразы, она ещё не договорена. Это начало продолжения или поправки предыдущей реплики "
    "(договаривает, уточняет, исправляет её), а не начало отдельной мысли?"))}

logger.remove()
logger.add(sys.stderr, level="WARNING")  # в ~/.claude/gv.out — только сбои Pipecat


def env(path: str) -> dict:
    return {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / path).read_text().splitlines() if "=" in l and not l.startswith("#"))}


def state(text: str, last, pause: float | None, cut: bool = False, new: str = "Новая реплика") -> str:
    """Что видит классификатор: прошлый ask с его состоянием, пауза перед новой репликой (None — неизвестна) и сама реплика."""
    gap = "" if pause is None else f"Пауза в речи перед новой репликой: {pause:.1f} с\n"
    return (f"Предыдущая реплика ({CUT if cut else STATE[last.state]}, передана {time.monotonic() - last.at:.1f} с назад): {last.text}\n"
            f"{gap}{new}: {text}")


class Listen:
    def __init__(self, core) -> None:
        self.core = core
        self.mic = Mic()
        self.decisions = OpenAIDecisionsClassifier(api_key=os.environ.get("OPENAI_API_KEY") or env(".config/openai.env")["OPENAI_API_KEY"])
        self.order = asyncio.Lock()  # реплики решаются по одной, в порядке речи: следующая видит, куда ушла прошлая
        self.partial = self.asked = ""  # что уже слышно в идущей фразе; по какому её началу уже спросили классификатор
        self.settled = False  # ход Claude этой фразой уже остановлен
        self.checking: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        @self.decisions.event_handler("on_metrics")
        async def timing(_c, data) -> None:  # сколько думал классификатор: штатные метрики Pipecat
            log("turn.metrics", **{type(d).__name__: getattr(d.value, "model_dump", lambda: d.value)() for d in data})

        self.spawn(self.keep_warm())
        stt = Traced(api_key=os.environ.get("DEEPGRAM_API_KEY") or env(".config/deepgram.env")["DEEPGRAM_API_KEY"],
                     settings=DeepgramSTTService.Settings(model="nova-3", language=Language.RU, punctuate=True, smart_format=True,
                                                          utterance_end_ms=UTTERANCE_END_MS, extra={"vad_events": True}))
        worker = PipelineWorker(Pipeline([self.mic, stt, Turns(self)]), idle_timeout_secs=None, enable_rtvi=False,
                                enable_turn_tracking=False, observers=[Record(trace_open().with_suffix(".wav"))])

        @worker.event_handler("on_pipeline_error")
        async def on_error(_worker, frame) -> None:
            self.error(str(frame.error))

        log("mic.start")
        runner = WorkerRunner(handle_sigint=False)
        await runner.add_workers(worker)
        await runner.run()
        self.error("конвейер распознавания остановился")

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def keep_warm(self) -> None:
        """Соединение с классификатором держим тёплым: простаивающее закрывается через 240 с, и первый вопрос после перерыва
        ждал бы рукопожатия — 1,5–3 с. Поэтому пустой вопрос сразу при запуске (иначе холодным выйдет первый настоящий) и раз в 200 с."""
        while True:
            try:
                t0 = time.monotonic()
                await asyncio.wait_for(self.decisions.choice("привет", {"turn": TURN_COMPLETION_QUESTION}), 10)
                log("turn.warm", s=round(time.monotonic() - t0, 2))
            except Exception as ex:  # noqa: BLE001 — не прогрелось: первый вопрос просто подождёт
                log("turn.warm.error", error=f"{type(ex).__name__}: {ex}")
            await asyncio.sleep(200)

    def error(self, why: str) -> None:
        log("recognize.error", error=why[:500])
        emit({"error": f"Deepgram: {why}"})

    # ── фраза ещё звучит: ранняя проверка ───────────────────────────────────────
    def hearing(self, text: str, pause: float | None) -> None:
        """text — всё, что в идущей фразе уже распознано: пока Claude работает над последней репликой, по этому началу проверяем,
        не её ли это продолжение, — тогда его ход останавливается сразу. Проверка одна за раз, каждая — по свежему тексту."""
        self.partial = text
        if not self.settled and (self.checking is None or self.checking.done()):
            self.checking = self.spawn(self.check(pause))

    async def check(self, pause: float | None) -> None:
        while not self.settled and (text := self.partial) != self.asked:
            self.asked = text
            if not (last := self.core.working_on()):
                return
            if await self.ask_continues(EARLY, state(text, last, pause, new="Начало новой реплики"), "route.early", last=last.id, text=text):
                self.settled = self.core.interrupt(last.id)

    # ── фраза закончена: куда её ────────────────────────────────────────────────
    def heard(self, text: str, pause: float | None) -> None:
        log("recognized", text=text)
        if self.checking:  # опоздавшее раннее «да» остановило бы ход уже после решения по всей фразе
            self.checking.cancel()
        self.partial = self.asked = ""
        self.settled = False
        emit({"me": text})
        self.spawn(self.deliver(text, pause))

    async def deliver(self, text: str, pause: float | None) -> None:
        async with self.order:
            last, cut = self.core.last_ask(), self.core.cut
            continues = bool(last) and await self.ask_continues(QUESTION, state(text, last, pause, cut == last.id), "route",
                                                                 last=last.id, pause=pause and round(pause, 1), cut=cut == last.id)
            # Остановленный запрос ядро отправит заново: Claude снова занят им, как был до остановки.
            name = "ask" if continues or not (self.core.claude_busy() or cut) else "quick"
            self.core.call(name, {"heard": text, "replaces": last.id if continues else 0})

    async def ask_continues(self, question: dict, situation: str, event: str, **fields) -> bool:
        """Да или нет классификатора «продолжение?»; не ответил — нет: ход Claude не трогаем, новая мысль ничего не стирает."""
        try:
            t0 = time.monotonic()
            p = (await self.decisions.yes_no(situation, question))["continues"].probability
            log(event, p=round(p, 2), s=round(time.monotonic() - t0, 2), **fields)
            return p >= SURE
        except Exception as ex:  # noqa: BLE001
            log("route.error", error=f"{type(ex).__name__}: {ex}")
            return False


class Turns(FrameProcessor):
    """Реплики из потока Deepgram. Черновики и окончательные куски — в раннюю проверку; окончательный с speech_final — вопрос
    классификатору «мысль закончена?»: да — реплика закрыта. Пауза перед репликой — от конца прошлой до первого куска этой."""

    def __init__(self, listen: Listen) -> None:
        super().__init__()
        self.listen = listen
        self.parts: list[str] = []
        self.closed: float | None = None  # когда закрыта прошлая реплика
        self.pause: float | None = None
        self.asking: asyncio.Task | None = None
        self.waiting: asyncio.TimerHandle | None = None

    async def process_frame(self, frame, direction) -> None:
        await super().process_frame(frame, direction)
        if isinstance(frame, (InterimTranscriptionFrame, TranscriptionFrame)) and (text := frame.text.strip()):
            self.more()
            if isinstance(frame, TranscriptionFrame):
                self.parts.append(text)
                if getattr(frame.result, "speech_final", False):
                    self.asking = asyncio.create_task(self.ask(" ".join(self.parts)))
                self.waiting = asyncio.get_running_loop().call_later(PAUSE_MS / 1000, self.close)
                self.listen.hearing(" ".join(self.parts), self.pause)
            else:
                self.listen.hearing(" ".join([*self.parts, text]), self.pause)
        await self.push_frame(frame, direction)

    def more(self) -> None:
        """Пришёл текст: начало реплики — запомнить паузу перед ней; продолжение — прежние ожидания конца отменить."""
        if not self.parts and self.pause is None and self.closed is not None:
            self.pause = time.monotonic() - self.closed
        for pending in (self.asking, self.waiting):
            if pending:
                pending.cancel()
        self.asking = self.waiting = None

    async def ask(self, text: str) -> None:
        t0 = time.monotonic()
        try:
            r = (await asyncio.wait_for(self.listen.decisions.choice({"user": text}, {"turn": TURN_COMPLETION_QUESTION}), 2.0))["turn"]
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
            self.listen.heard(text, self.pause)
        self.pause = None


class Traced(DeepgramSTTService):
    """Сервис Pipecat как есть, плюс события самого Deepgram в запись запуска."""

    async def _on_message(self, message):
        if isinstance(message, ListenV1SpeechStarted):  # время — секунды от начала присланного ему звука
            trace("dg.speech_started", dg_at=message.timestamp)
        elif isinstance(message, ListenV1UtteranceEnd):
            trace("dg.utterance_end", dg_last_word_end=message.last_word_end)
        await super()._on_message(message)


class Mic(BaseInputTransport):
    """Вход конвейера — микрофон компьютера блоками по BLOCK_S. Не открылся — ошибка на страницу, конвейер ждёт без звука."""

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
        """Звук вместо микрофона, в реальном времени (прогоны без человека)."""
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


class Record(BaseObserver):
    """Запись запуска: звук микрофона в WAV (момент первого куска — audio.start, от него считается время в файле), текст Deepgram
    с флагами и словами (секунды от начала присланного ему звука), ошибки. Решения пишет journal.log."""

    def __init__(self, wav: Path) -> None:
        super().__init__()
        self.path = wav
        self.wav: wave.Wave_write | None = None

    async def on_push_frame(self, data: FramePushed) -> None:
        f = data.frame
        if not data.first_push or data.direction != FrameDirection.DOWNSTREAM:
            return
        if isinstance(f, InputAudioRawFrame):
            if self.wav is None:
                self.wav = wave.open(str(self.path), "wb")
                self.wav.setnchannels(f.num_channels)
                self.wav.setsampwidth(2)
                self.wav.setframerate(f.sample_rate)
                trace("audio.start", file=self.path.name, rate=f.sample_rate)
            self.wav.writeframes(f.audio)
        elif isinstance(f, (InterimTranscriptionFrame, TranscriptionFrame)):
            trace("stt.final" if isinstance(f, TranscriptionFrame) else "stt.interim", text=f.text, **flags(f.result))
        elif isinstance(f, ErrorFrame):
            trace("error", error=str(f.error))


def flags(result) -> dict:
    """Из ответа Deepgram — его флаги и слова со временем."""
    try:
        d = result.model_dump()
    except Exception:  # noqa: BLE001
        return {}
    alt = ((d.get("channel") or {}).get("alternatives") or [{}])[0]
    return {"is_final": d.get("is_final"), "speech_final": d.get("speech_final"), "from_finalize": d.get("from_finalize"),
            "dg_start": d.get("start"), "dg_duration": d.get("duration"), "confidence": alt.get("confidence"),
            "words": [[w.get("word"), w.get("start"), w.get("end"), w.get("confidence")] for w in alt.get("words") or []]}
