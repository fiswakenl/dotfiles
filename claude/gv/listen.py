"""Слух GV: микрофон → цикл разговора LiveKit Agents → реплики → решение, куда их отправить. Цикл — AgentSession как библиотека:
без сервера, комнаты и аккаунта LiveKit, без модели-собеседника и голоса; мозг — Claude Code.

    микрофон ──звук──▶ AgentSession ─┬─ распознавание: Deepgram (nova-3) | OpenAI ──текст──┐
                                     ├─ детектор речи Silero, локальный                     ├─▶ реплика ──▶ решение ──▶ core.call(ask | quick)
                                     └─ конец реплики: модель по звуку, локальная (Ending) ──┘

Цикл сам ведёт детектор речи, поток распознавания (и открывает его заново после обрыва), копит куски текста и объявляет конец
реплики. Наше в нём — три гнезда: вход звука (Sound), узел распознавания (Ear.stt_node) и детектор конца реплики (Ending).

Конец реплики. Речь стихла — локальная модель turn-detector-v1-mini по последним 1,2 с звука отвечает вероятностью «человек
договорил»: не ниже END_SURE — реплика закрывается через QUICK_MS тишины, ниже — через PAUSE_MS. Заговорил раньше — ожидание
отменяется. Порог и обе паузы подобраны на записях диктовки (tests/loop_bench.py): порога для русского в пакете нет.
Текст — без знаков препинания и заглавных: у Deepgram punctuate и smart_format выключены, у OpenAI их снимает Segments.

Куда реплику — классификатор OpenAI Decisions, вероятность: продолжает ли она последний переданный ask (договаривает, уточняет,
исправляет). Да — ask с replaces=N: ядро склеит текст и откатит №N. Нет — новая мысль: пока Claude занят — quick, свободен — ask.
Неуверенное «да» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.
Тот же вопрос задаётся и рано — по первым словам новой фразы, пока Claude работает над последней репликой: «да» останавливает его
ход сразу (core.interrupt); слов мало — вопрос повторяется на каждом новом куске текста. Решает всё равно вопрос по всей фразе.
Оба видят паузу перед фразой, и в вопросе сказано, что она значит: без этого классификатор её не замечал (17 с тишины — «продолжение» 0,86).
Вопрос по всей фразе получает вход тегами и паузу словами (said): так он на замере в devtools делит увереннее всего; ранний — строками (state).

Чем слушать — Deepgram или OpenAI (gpt-live-transcribe по сокету Realtime): команды /gv:openai и /gv:deepgram пишут выбор в
~/.claude/gv.ears (GV_EARS в окружении — важнее). Отличия OpenAI прячет узел распознавания: в него идёт только речь (speech) — он
берёт за каждую секунду отправленного звука, даже за нули, а сокет без звука открыт и тёплый бесплатно; текст куска он отдаёт
через секунду после конца речи, поэтому Ending ждёт текст на каждый услышанный конец речи, иначе запоздавший кусок ушёл бы
отдельной репликой. Ходит через HTTPS_PROXY; список KEYWORDS подсказывает ему термины («Claude Code», а не «Cloud код»).

Связь с распознаванием: подключение и обрыв ловит плагин (таймаут подключения, тишина в сокете); поток сдался — узел ждёт RETRY_S,
и цикл открывает новый, вечно. Звук, накопленный без связи, в новый поток не идёт (STALE_S). Deepgram ходит напрямую, без
прокси: сон мака и туннель его не касаются. Закрылась сама сессия цикла — поднимается новая.

Всё услышанное — в запись запуска (journal.trace): текст с флагами и словами (stt.interim, stt.final), события Deepgram SpeechStarted
и UtteranceEnd, ответ модели конца реплики (turn.check), отсечка тишины (gate.sent), звук микрофона в WAV. Ключи: Deepgram —
DEEPGRAM_API_KEY в ~/.config/deepgram.env, OpenAI — OPENAI_API_KEY в ~/.config/openai.env (прокси — HTTPS_PROXY из окружения).
"""
import asyncio
import logging
import os
import re
import time
import wave
from collections import deque
from pathlib import Path

import aiohttp
import numpy as np
import sounddevice as sd
from livekit import rtc
from livekit.agents import Agent, AgentSession, APIConnectOptions, APIError, StopResponse, inference
from livekit.agents.llm import Choice, Probability
from livekit.agents.stt import SpeechEventType
from livekit.agents.vad import VADEventType
from livekit.agents.voice import io
from livekit.agents.voice.agent_session import SessionConnectOptions
from livekit.local_inference import EOT, EOT_MAX_SAMPLES
from livekit.plugins import deepgram, openai
from livekit.plugins.openai import stt as openai_stt

from journal import emit, log, trace, trace_open

RATE = 16000
BLOCK_S = 0.02  # блок микрофона
END_SURE = 0.4  # вероятность «договорил» от модели по звуку: не ниже — ждём QUICK_MS тишины, ниже — PAUSE_MS
QUICK_MS = 800  # столько тишины закрывает реплику, когда модель уверена, что человек договорил
PAUSE_MS = int(os.environ.get("GV_PAUSE_MS", 2500))  # и столько — когда не уверена
UTTERANCE_END_MS = 1000  # UtteranceEnd Deepgram: столько без новых слов; меньше 1000 он не принимает
DEEPGRAM = dict(model="nova-3", language="ru", punctuate=False, smart_format=False, filler_words=False, vad_events=True,
                utterance_end_ms=UTTERANCE_END_MS)  # настройки потока; без знаков — см. шапку
EARS_FILE = Path.home() / ".claude" / "gv.ears"
EARS = os.environ.get("GV_EARS") or (EARS_FILE.read_text().strip() if EARS_FILE.exists() else "") or "deepgram"  # чем слушать: deepgram | openai
KEYWORDS = ["Claude Code", "Claude", "Codex", "Deepgram", "devtools"]  # термины, которые OpenAI иначе слышит по-своему
OPENAI_STT = dict(model="gpt-live-transcribe", language="ru", keywords=KEYWORDS)
ANSWER_CHARS = 300  # столько от начала и от конца ответа Claude видит «продолжение?»: суть в первых строках, вопрос человеку — в последних
VAD_SILENCE_S = 0.4  # столько тишины детектор речи плагина OpenAI считает концом куска
# столько тишины детектор речи цикла считает концом речи — в этот момент спрашиваем модель конца реплики: при хвосте тишины
# 0,4–0,5 с она реже путает настоящий конец с паузой, чем при 0,25 с. Больше VAD_SILENCE_S: конец куска от плагина OpenAI
# должен прийти раньше, чтобы Ending уже знал, что текст в пути
SPEECH_END_S = 0.5
TEXT_WAIT_S = 2  # дольше текст куска OpenAI не ждём (обычно он приходит за секунду): реплику закроет то, что уже есть
GATE_BEFORE_S = 0.6  # столько звука до начала речи досылаем в OpenAI: детектор замечает речь с опозданием, начало слова не теряется
GATE_AFTER_S = 0.8  # столько тишины после речи ещё шлём: больше VAD_SILENCE_S, чтобы плагин успел закрыть кусок
RETRY_S = 2  # поток распознавания сдался — новый через столько
STALE_S = 1  # звук старше этого в распознавание не идёт: накопился, пока не было связи
CONNECT = APIConnectOptions(max_retry=0, timeout=10)  # повторы — наш узел: новый поток не тащит звук, накопленный без связи
ALWAYS = SessionConnectOptions(stt_conn_options=CONNECT, max_unrecoverable_errors=10**9)  # по умолчанию цикл закрывает сессию после трёх обрывов подряд, а нам слушать вечно
ROUTE_WAIT = APIConnectOptions(max_retry=0, timeout=5)  # не ответил — новая мысль
DECIDE_FILE = Path.home() / ".claude" / "gv.decide"
DECIDE = os.environ.get("GV_DECIDE") or (DECIDE_FILE.read_text().strip() if DECIDE_FILE.exists() else "") or "openai"  # чем решать: openai | gemini
GEMINI_DECISIONS = "https://generativelanguage.googleapis.com/v1beta/decisions"
GEMINI_MODEL = "gemini-decision-1"
# вопрос для прогрева — как настоящий: на пустое «привет» модель OpenAI отвечать отказывается
WARM = "Предыдущая реплика (ждёт отправки, передана 1.0 с назад): расскажи про регуляризацию\nПауза в речи перед новой репликой: 0.5 с\nНачало новой реплики: и какие у неё"
WARM_AFTER_S = 5  # столько без вопросов к модели решений — и первый же ответ вдвое медленнее (0,5–0,9 с вместо 0,25): греем, как только человек заговорил
DECISIONS_MODEL = "gpt-6-luna"  # gpt-6-luna-decisions, что плагин берёт по умолчанию, нашему ключу недоступна
SURE = 0.7  # вероятность продолжения ниже — не поправка, а новая мысль
WRITING_WAIT_S = 0.5  # дольше окно Claude о том, что он уже написал, не ждём: решаем без этого
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}
CUT = "Claude начал отвечать, но человек заговорил снова, и ответ остановлен"
QUESTION = {"continues": Probability(
    "Ты собираешь диктовку голосом в сообщения для Claude. Вопрос: новую реплику (после_паузы) надо дописать в то же сообщение, "
    "что и предыдущая (до_паузы), а не отправить отдельным?\n"
    "Дописать — когда человек договаривает ту же мысль: продолжает оборванную фразу, уточняет или исправляет сказанное.\n"
    "Отдельным — когда Claude уже ответил на предыдущую (есть ответ_claude), а человек реагирует на этот ответ: соглашается, "
    "отказывается, отвечает на его вопрос, оценивает, спрашивает дальше. Отдельным — и когда пауза долгая, а новая реплика "
    "понятна сама по себе.\n"
    "Длина паузы — такой же сигнал, как смысл: люди договаривают почти без паузы, а замолчав надолго, обычно начинают новое.")}
EARLY = {"continues": Probability(
    "Человек диктует голосом, речь нарезана по паузам. Предыдущая реплика уже передана, а человек заговорил снова: слышно только "
    "начало новой фразы, она ещё не договорена. Это начало продолжения или поправки предыдущей реплики "
    "(договаривает, уточняет, исправляет её), а не начало отдельной мысли? "
    "Учитывай паузу перед новой репликой: меньше секунды — человек договаривает фразу; несколько секунд и больше — он уже "
    "закончил и начинает новую мысль, если начало само по себе звучит как новая мысль.")}

# Зацепка во внутренность плагина OpenAI: черновик он отдаёт не чаще раза в 0,5 с, и последние слова фразы приходят только с готовым
# текстом, на 0,3–0,4 с позже. Нам черновик нужен целиком и сразу: по нему решение «продолжение?» принимается заранее.
openai_stt._delta_transcript_interval = 0

logging.basicConfig(level=logging.WARNING)  # в ~/.claude/gv.out — только сбои библиотек


def plain(text: str) -> str:
    """Текст как от Deepgram без знаков: строчными, без пунктуации."""
    return re.sub(r"[^\w\s-]", "", text).lower()


def sec(x: float) -> str:
    return f"{x:.1f}".replace(".", ",") + " с"


def env(path: str) -> dict:
    return {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / path).read_text().splitlines() if "=" in l and not l.startswith("#"))}


def words(sec: float) -> str:
    """Пауза словами: голое число классификатор читает хуже."""
    return (f"{sec} с — короткая, человек почти не останавливался" if sec < 1 else
            f"{sec} с — средняя, человек запнулся или подбирал слово" if sec < 3 else
            f"{sec} с — долгая, человек замолчал и потом заговорил снова")


def brief(text: str) -> str:
    return text if len(text) <= 2 * ANSWER_CHARS else f"{text[:ANSWER_CHARS]} … {text[-ANSWER_CHARS:]}"


def said(text: str, last, pause: float | None, cut: bool, writing: str = "") -> str:
    """Что видит «продолжение?» по всей фразе: прошлый ask с его состоянием и ответом Claude на него (если уже ответил; ещё пишет —
    writing, то, что человеку уже видно), пауза перед новой репликой (None — неизвестна, тега нет) и сама реплика — тегами.
    По ответу видно, договаривает человек своё или отвечает Claude."""
    d = {"до_паузы": last.text, "состояние": CUT if cut else STATE[last.state],
         **({"ответ_claude": brief(last.answer)} if last.answer else {"claude_пишет": brief(writing)} if writing else {}),
         **({} if pause is None else {"пауза": words(round(pause, 1))}), "после_паузы": text}
    return "\n".join(f"<{k}>{v}</{k}>" for k, v in d.items())


def state(text: str, last, pause: float | None) -> str:
    """Что видит ранняя проверка: прошлый ask с его состоянием, пауза перед новой репликой (None — неизвестна) и начало реплики."""
    gap = "" if pause is None else f"Пауза в речи перед новой репликой: {pause:.1f} с\n"
    return (f"Предыдущая реплика ({STATE[last.state]}, передана {time.monotonic() - last.at:.1f} с назад): {last.text}\n"
            f"{gap}Начало новой реплики: {text}")


class Listen:
    def __init__(self, core, bridge=None) -> None:
        self.core = core
        self.bridge = bridge  # мост в окно Claude: у него спрашиваем, что Claude уже написал
        self.mic = Mic(self)
        self.decisions = openai.DecisionModel(model=DECISIONS_MODEL, api_key=os.environ.get("OPENAI_API_KEY") or env(".config/openai.env")["OPENAI_API_KEY"])
        self.gemini: aiohttp.ClientSession | None = None  # соединение с Gemini Decision: заводится первым вопросом
        self.decided = 0.0  # когда последний раз спрашивали модель решений
        self.stilled = False  # речь стихла, а реплика ещё не закрыта: текст дошёптывается, решение можно принять заранее
        self.guess: tuple[str, asyncio.Task] | None = None  # решение «продолжение?» по черновику фразы: её текст и задача
        self.draft = self.drafted = ""  # что из идущей фразы показать в поле ввода Claude; что там уже показано
        self.drafting: asyncio.Task | None = None
        self.eot = EOT()  # модель конца реплики по звуку: в процессе, без сети
        self.sound: Sound | None = None  # вход звука идущей сессии цикла
        self.recent: deque = deque(maxlen=int(EOT_MAX_SAMPLES / RATE / BLOCK_S))  # последние 1,2 с звука: столько слышит модель конца реплики
        self.wav: wave.Wave_write | None = None
        self.audio_t0 = 0.0  # когда пошёл звук записи: от него время кусков в записи запуска
        self.order = asyncio.Lock()  # реплики решаются по одной, в порядке речи: следующая видит, куда ушла прошлая
        self.closed: float | None = None  # когда закрыта прошлая реплика
        self.quiet_at: float | None = None  # когда детектор речи цикла последний раз сказал «речь кончилась»
        self.tone: float | None = None  # последний ответ модели конца реплики
        self.partial = self.asked = ""  # что уже слышно в идущей фразе; по какому её началу уже спросили классификатор
        self.settled = False  # ход Claude этой фразой уже остановлен
        self.checking: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()

    def ears(self, http: aiohttp.ClientSession):
        """Распознавание по выбору EARS."""
        if EARS == "openai":
            stt = openai.STT(api_key=os.environ.get("OPENAI_API_KEY") or env(".config/openai.env")["OPENAI_API_KEY"],
                             vad=inference.VAD(model="silero", min_silence_duration=VAD_SILENCE_S), **OPENAI_STT)
            stt._session = http  # зацепка во внутренность плагина: сессию с прокси он параметром не принимает, а свою берёт из задания LiveKit, которого у нас нет
            return stt
        return Deepgram(api_key=os.environ.get("DEEPGRAM_API_KEY") or env(".config/deepgram.env")["DEEPGRAM_API_KEY"], http_session=http, **DEEPGRAM)

    async def run(self) -> None:
        self.decisions.on("metrics_collected", lambda m: log("decide.metrics", s=round(m.duration, 2), tokens=m.input_tokens))
        self.spawn(self.keep_warm())
        self.wav_path = trace_open().with_suffix(".wav")
        # без trust_env: Deepgram — напрямую, мимо HTTPS_PROXY; OpenAI — через него
        async with aiohttp.ClientSession(proxy=os.environ.get("HTTPS_PROXY") if EARS == "openai" else None) as http:
            stt = self.ears(http)
            self.mic.start()
            log("mic.start", ears=EARS)
            while True:
                ear, over = Ear(self), asyncio.Event()
                session = AgentSession(stt=stt, vad=inference.VAD(model="silero", min_silence_duration=SPEECH_END_S), conn_options=ALWAYS,
                                       user_away_timeout=None, turn_handling={"turn_detection": Ending(ear),
                                                                              "endpointing": {"min_delay": QUICK_MS / 1000, "max_delay": PAUSE_MS / 1000}})
                session.on("user_state_changed", ear.quiet)
                session.on("close", lambda _: over.set())
                session.input.audio = sound = Sound()  # вместо комнаты; вывода звука нет вовсе
                try:
                    await session.start(ear)
                    self.sound = sound
                    await over.wait()  # сессия закрылась сама: так быть не должно, но слух от этого умереть не может
                    self.error("сессия цикла закрылась")
                except Exception as ex:  # noqa: BLE001
                    self.error(f"{type(ex).__name__}: {ex}")
                finally:
                    self.sound = None
                    await session.aclose()
                await asyncio.sleep(RETRY_S)

    def audio(self, pcm: bytes) -> None:
        """Блок звука: в WAV записи и в цикл; сессии нет — в цикл звук пропадает, в WAV — нет."""
        if self.wav is None:
            self.audio_t0 = time.time()
            self.wav = wave.open(str(self.wav_path), "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(RATE)
            trace("audio.start", file=self.wav_path.name, rate=RATE)
        self.wav.writeframes(pcm)
        self.recent.append(pcm)
        if self.sound:
            self.sound.frames.put_nowait(rtc.AudioFrame(pcm, RATE, 1, len(pcm) // 2, userdata={"at": time.monotonic()}))

    def spawn(self, coro) -> asyncio.Task:
        task = asyncio.create_task(coro)
        self.tasks.add(task)
        task.add_done_callback(self.tasks.discard)
        return task

    async def keep_warm(self) -> None:
        """Соединение с классификатором держим открытым: простаивающее закрывается через 240 с, и первый вопрос после перерыва
        ждал бы рукопожатия — 1,5–3 с. Поэтому пустой вопрос сразу при запуске (иначе холодным выйдет первый настоящий) и раз в 200 с.
        Быстрым ответ это не делает — для этого warm."""
        while True:
            try:
                t0 = time.monotonic()
                await self.probability(EARLY, WARM)
                log("turn.warm", s=round(time.monotonic() - t0, 2), model=DECIDE)
            except Exception as ex:  # noqa: BLE001 — не прогрелось: первый вопрос просто подождёт
                log("turn.warm.error", error=f"{type(ex).__name__}: {ex}")
            await asyncio.sleep(200)

    def error(self, why: str) -> None:
        log("recognize.error", error=why[:500])
        emit({"error": f"{EARS}: {why}"})

    # ── фраза ещё звучит: черновик в поле ввода и ранняя проверка ───────────────
    def show(self, text: str) -> None:
        """Распознанное — в поле ввода Claude, пока человек говорит: видно, что уже услышано и когда реплика улетает. Пусто — убрать."""
        self.draft = text
        if self.bridge and (self.drafting is None or self.drafting.done()):
            self.drafting = self.spawn(self.showing())

    async def showing(self) -> None:
        """По одному запросу за раз, каждый — со свежим текстом: черновики идут чаще, чем мост успевает отвечать."""
        while self.draft != self.drafted:
            self.drafted = self.draft
            try:
                await asyncio.wait_for(self.bridge("draft", {"text": self.drafted}), 1)
            except Exception:  # noqa: BLE001 — мост молчит: без черновика голос работает как прежде
                return

    def hearing(self, text: str, pause: float | None) -> None:
        """text — всё, что в идущей фразе уже распознано: пока Claude работает над последней репликой, по этому началу проверяем,
        не её ли это продолжение, — тогда его ход останавливается сразу. Проверка одна за раз, каждая — по свежему тексту."""
        self.partial = text
        self.show(f"🎙 {text}")
        if self.stilled and not (self.guess and self.guess[0] == text):
            if self.guess:
                self.guess[1].cancel()
            self.guess = (text, self.spawn(self.route(text, pause)))
        if not self.settled and (self.checking is None or self.checking.done()):
            self.checking = self.spawn(self.check(pause))

    async def check(self, pause: float | None) -> None:
        while not self.settled and (text := self.partial) != self.asked:
            self.asked = text
            if not (last := self.core.working_on()):
                return
            if await self.ask_continues(EARLY, state(text, last, pause), "route.early", last=last.id, text=text):
                self.settled = self.core.interrupt(last.id)

    # ── фраза закончена: куда её ────────────────────────────────────────────────
    def heard(self, text: str, pause: float | None) -> None:
        log("recognized", text=text)
        if self.checking:  # опоздавшее раннее «да» остановило бы ход уже после решения по всей фразе
            self.checking.cancel()
        self.partial = self.asked = ""
        self.stilled = False
        self.show("")
        self.settled = False
        self.closed = time.monotonic()
        # речь стихла на SPEECH_END_S раньше, чем это заметил детектор; тишина — от последнего звука речи до закрытия реплики
        ended = self.quiet_at - SPEECH_END_S if self.quiet_at else self.closed
        note = [f"тишина {sec(self.closed - ended)}", *([] if self.tone is None else [f"интонация {self.tone:.2f}".replace(".", ",")])]
        self.quiet_at = self.tone = None
        emit({"me": text})
        self.spawn(self.deliver(text, pause, ended, note))

    async def route(self, text: str, pause: float | None) -> tuple:
        """Продолжает ли text последний ask: (каким он был в момент вопроса, ответ). Зовётся и заранее, по черновику фразы."""
        last, cut = self.core.last_ask(), self.core.cut
        if not last:
            return None, False
        writing = await self.writing(last)
        return (last.id, last.state, cut), await self.ask_continues(QUESTION, said(text, last, pause, cut == last.id, writing), "route", last=last.id,
                                                                    pause=pause and round(pause, 1), cut=cut == last.id, writing=len(writing))

    async def deliver(self, text: str, pause: float | None, ended: float, note: list[str]) -> None:
        """ended — когда стихла речь; note — пометка о скорости: её видно перед сообщением в окне Claude."""
        async with self.order:
            last, cut = self.core.last_ask(), self.core.cut
            t0, guess, self.guess = time.monotonic(), self.guess, None
            # Решение по черновику годится, если черновик совпал с готовым текстом и прошлый ask с тех пор не изменился
            # (не ушёл Claude, не получил ответ): тогда оно уже принято или вот-вот будет, пока мы ждали готовый текст.
            was, continues, early = None, False, False
            if guess and guess[0].split() == text.split():
                try:
                    was, continues = await guess[1]
                    early = was == ((last.id, last.state, cut) if last else None)
                except asyncio.CancelledError:
                    pass
            elif guess:
                guess[1].cancel()
            if not early:
                _, continues = await self.route(text, pause)
            # Остановленный запрос ядро отправит заново: Claude снова занят им, как был до остановки. Остановка — на момент ответа
            # классификатора: пока он думал, ранний вопрос по следующей фразе мог её устроить.
            name = "ask" if continues or not (self.core.claude_busy() or self.core.cut) else "quick"
            took = f"решение {'заранее' if early else sec(time.monotonic() - t0)} {DECIDE}"
            note += [*([took] if last else []), f"всего {sec(time.monotonic() - ended)}"]
            self.core.call(name, {"heard": text, "replaces": last.id if continues and last else 0, "note": " · ".join(note)})

    async def writing(self, last) -> str:
        """Что Claude уже написал в ответ на last, пока ход ещё идёт, — прямо сейчас, из окна Claude; не узнали — пусто."""
        if not self.bridge or last.state != "sent" or last.answer:
            return ""
        try:
            return str((await asyncio.wait_for(self.bridge("said", {"text": last.text}), WRITING_WAIT_S)).get("text") or "")
        except Exception:  # noqa: BLE001 — мост молчит: решаем, как раньше, без написанного
            return ""

    async def probability(self, question: dict, situation: str) -> float:
        """Вероятность «да» на вопрос question по входу situation — от модели решений по выбору DECIDE."""
        self.decided = time.monotonic()
        if DECIDE == "gemini":
            if self.gemini is None:
                g = env(".config/gemini.env")  # Gemini — только через свой прокси
                self.gemini = aiohttp.ClientSession(proxy=g["GEMINI_PROXY"], headers={"x-goog-api-key": g["GEMINI_API_KEY"]},
                                                    timeout=aiohttp.ClientTimeout(total=ROUTE_WAIT.timeout))
            ask = {"model": GEMINI_MODEL, "input": situation, "questions": {"continues": {"type": "noul", "instructions": question["continues"].prompt}}}
            async with self.gemini.post(GEMINI_DECISIONS, json=ask) as r:
                body = await r.json()
            if "answers" not in body:
                raise APIError(str(body.get("error", body))[:300])
            return body["answers"]["continues"]["noul"]
        r = await self.decisions.evaluate(input=situation, decisions=question, conn_options=ROUTE_WAIT)
        if "continues" not in r.results:
            raise APIError(r.errors.get("continues", "нет ответа"))
        return r.results["continues"].value

    async def warm(self) -> None:
        """Человек заговорил, а модель решений давно не спрашивали: пустой вопрос сейчас — и к концу фразы она ответит быстро."""
        try:
            await self.probability(EARLY, WARM)
        except Exception:  # noqa: BLE001 — не прогрелось: настоящий вопрос просто подождёт
            pass

    async def ask_continues(self, question: dict, situation: str, event: str, **fields) -> bool:
        """Вероятность «продолжение» от классификатора; не ответил — нет: ход Claude не трогаем, новая мысль ничего не стирает."""
        try:
            t0 = time.monotonic()
            p = await self.probability(question, situation)
            log(event, p=round(p, 2), s=round(time.monotonic() - t0, 2), model=DECIDE, **fields, input=situation)  # вход — чтобы спорное решение можно было повторить
            return p >= SURE
        except Exception as ex:  # noqa: BLE001
            log("route.error", error=f"{type(ex).__name__}: {ex}")
            return False


class Sound(io.AudioInput):
    """Вход звука цикла вместо комнаты LiveKit: блоки микрофона из Listen.audio."""

    def __init__(self) -> None:
        super().__init__(label="микрофон")
        self.frames: asyncio.Queue = asyncio.Queue()

    async def __anext__(self) -> rtc.AudioFrame:
        return await self.frames.get()


class Ear(Agent):
    """Агент цикла без модели и голоса: только слушает. Через его узел распознавания идут звук и события: черновики и куски текста
    — в раннюю проверку, конец реплики (on_user_turn_completed) — в решение, куда её. Пауза перед репликой — от конца прошлой
    до первого текста этой."""

    def __init__(self, listen: Listen) -> None:
        super().__init__(instructions="")
        self.listen = listen
        self.same = lambda ev: ev  # приводит события распознавания к одному виду: у OpenAI — Segments, свой на каждый поток
        self.parts: list[str] = []  # куски текста идущей реплики
        self.pause: float | None = None
        self.end: asyncio.Future | None = None  # ответ модели конца реплики на последний конец речи

    def quiet(self, ev) -> None:
        """Детектор речи цикла сказал «речь кончилась»: последние 1,2 с звука — модели конца реплики, пока в них только что стихшая речь."""
        if ev.new_state == "speaking":
            self.listen.stilled = False
            if time.monotonic() - self.listen.decided > WARM_AFTER_S:
                self.listen.spawn(self.listen.warm())
        if (ev.old_state, ev.new_state) == ("speaking", "listening"):
            pcm = np.frombuffer(b"".join(self.listen.recent), dtype=np.int16)[-EOT_MAX_SAMPLES:]
            self.end = asyncio.ensure_future(asyncio.to_thread(self.listen.eot.predict, pcm))
            self.listen.quiet_at, self.listen.stilled = time.monotonic(), True
            if self.listen.partial:  # всё слышное уже могло прийти до этого момента: решение — по тому, что есть
                self.listen.hearing(self.listen.partial, self.pause)

    async def texted(self) -> None:
        """Ждать текст на каждый конец речи, услышанный плагином OpenAI; придёт — цикл сам спросит о конце реплики заново."""
        t0 = time.monotonic()
        while isinstance(self.same, Segments) and self.same.waiting() and time.monotonic() - t0 < TEXT_WAIT_S:
            await asyncio.sleep(BLOCK_S)

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        text, pause, self.parts, self.pause = (new_message.text_content or "").strip(), self.pause, [], None
        if text:
            self.listen.heard(text, pause)
        raise StopResponse  # отвечать в цикле некому

    async def stt_node(self, audio, model_settings):
        """Штатный узел распознавания, вокруг него наше: в поток — только свежий звук (у OpenAI — только речь), события — в запись
        запуска и раннюю проверку. Поток сдался или кончился — ошибка на страницу, RETRY_S, и цикл открывает новый."""
        fresh = (f async for f in audio if time.monotonic() - f.userdata.get("at", time.monotonic()) < STALE_S)
        if EARS == "openai":
            self.same = Segments(self.listen)  # новый поток — новая очередь концов речи: оборванный на старые уже не ответит
        try:
            async for ev in Agent.default.stt_node(self, self.speech(fresh) if EARS == "openai" else fresh, model_settings):
                ev = self.same(ev)
                text = ev.alternatives[0].text.strip() if ev.alternatives else ""
                if text and ev.type in (SpeechEventType.INTERIM_TRANSCRIPT, SpeechEventType.FINAL_TRANSCRIPT):
                    if not self.parts and self.pause is None and self.listen.closed is not None:
                        self.pause = time.monotonic() - self.listen.closed
                    if ev.type == SpeechEventType.FINAL_TRANSCRIPT:
                        self.parts.append(text)
                    self.listen.hearing(" ".join(self.parts if ev.type == SpeechEventType.FINAL_TRANSCRIPT else [*self.parts, text]), self.pause)
                yield ev
            why = "поток распознавания кончился"
        except Exception as ex:  # noqa: BLE001 — обычно APIError
            why = f"{type(ex).__name__}: {ex}"
        self.listen.error(why)
        await asyncio.sleep(RETRY_S)
        raise APIError(why)  # новый поток цикл открывает только после APIError

    async def speech(self, audio):
        """В поток распознавания — только речь. OpenAI берёт за каждую секунду отправленного звука, даже за нули (сверено со счётом:
        180 с тишины — 180 с расхода), а за открытый сокет без звука — нет. Свой Silero слушает весь звук: речь началась — в поток
        уходит запас GATE_BEFORE_S до неё и всё дальше; GATE_AFTER_S тишины — поток снова молчит."""
        vad = inference.VAD(model="silero", min_silence_duration=GATE_AFTER_S).stream()
        before: deque = deque(maxlen=int(GATE_BEFORE_S / BLOCK_S))
        gate = {"open": False, "sent": 0}  # sent — блоков ушло в поток за этот отрезок речи

        async def watch() -> None:
            async for ev in vad:
                if ev.type == VADEventType.START_OF_SPEECH:
                    gate["open"] = True
                elif ev.type == VADEventType.END_OF_SPEECH and gate["open"]:
                    trace("gate.sent", s=round(gate["sent"] * BLOCK_S, 1))
                    gate.update(open=False, sent=0)
        watching = asyncio.create_task(watch())
        try:
            async for frame in audio:
                vad.push_frame(frame)
                before.append(frame)
                while gate["open"] and before:
                    gate["sent"] += 1
                    yield before.popleft()
        finally:
            watching.cancel()
            await vad.aclose()


class Ending:
    """Детектор конца реплики для цикла: вероятность «человек договорил» от локальной модели по звуку, посчитанная в момент, когда
    речь стихла (Ear.quiet). Цикл сравнивает её с порогом и ждёт QUICK_MS или PAUSE_MS тишины. Свой, а не штатный детектор пакета
    с той же моделью: штатный не ждёт запоздавший текст OpenAI, а свой ответ отдаёт только в отладочный журнал.
    Зацепка: это протокол _TurnDetector из livekit.agents.voice.turn — имя внутреннее, но иначе свой детектор циклу не дать."""

    model, provider = "turn-detector-v1-mini", "local"

    def __init__(self, ear: Ear) -> None:
        self.ear = ear

    async def unlikely_threshold(self, language) -> float:
        return END_SURE

    async def supports_language(self, language) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx, *, timeout=None) -> float:
        t0 = time.monotonic()
        await self.ear.texted()
        # shield: цикл отменяет этот вопрос, когда приходит новый кусок текста, а ответ модели нужен и следующему вопросу;
        # нет ответа — текст есть, а речи детектор цикла не слышал: закрываем по короткой паузе
        p = float(await asyncio.shield(self.ear.end)) if self.ear.end else 1.0
        self.ear.listen.tone = p
        log("turn.check", verdict="complete" if p >= END_SURE else "short", p=round(p, 2), s=round(time.monotonic() - t0, 2),
            wait=(QUICK_MS if p >= END_SURE else PAUSE_MS) / 1000, text=chat_ctx.items[-1].text_content)
        return p


class Segments:
    """События OpenAI — в том же виде, что у Deepgram: текст без знаков и заглавных, в запись запуска — те же stt.interim и stt.final.
    Времени слов у OpenAI нет: кусок размечен одним «словом» от начала до конца речи по локальному детектору (точность около 0,3 с);
    конец речи приходит раньше текста куска, поэтому концы ждут свои куски в очереди (ended). На каждый конец OpenAI отвечает либо
    текстом и счётом расхода, либо одним счётом (шум, слов нет) — тогда конец из очереди уходит без куска."""

    def __init__(self, listen: "Listen") -> None:
        self.listen = listen
        self.start = 0.0
        self.ended: deque = deque()
        self.texted = False  # у текущего ответа OpenAI был текст

    def waiting(self) -> bool:
        """Есть конец речи, на который текст ещё может прийти. Конец старше TEXT_WAIT_S уже не дождётся (OpenAI промолчал: ни текста,
        ни счёта) — без этого он застревал в очереди, и каждая следующая реплика ждала свой текст лишние TEXT_WAIT_S."""
        while self.ended and time.monotonic() - self.ended[0][2] > TEXT_WAIT_S:
            self.ended.popleft()
        return bool(self.ended)

    def __call__(self, ev):
        at = round(time.time() - self.listen.audio_t0, 2)
        if ev.type == SpeechEventType.START_OF_SPEECH:
            self.start = at
        elif ev.type == SpeechEventType.END_OF_SPEECH:
            self.ended.append((self.start, round(at - VAD_SILENCE_S, 2), time.monotonic()))
        elif ev.type == SpeechEventType.RECOGNITION_USAGE:
            if not self.texted and self.ended:
                self.ended.popleft()
            self.texted = False
        elif ev.type in (SpeechEventType.INTERIM_TRANSCRIPT, SpeechEventType.FINAL_TRANSCRIPT) and ev.alternatives:
            alt = ev.alternatives[0]
            alt.text = plain(alt.text)
            if not alt.text.strip():
                pass
            elif ev.type == SpeechEventType.INTERIM_TRANSCRIPT:
                trace("stt.interim", text=alt.text, is_final=False, speech_final=False, dg_start=self.start)
            else:
                start, end, _ = self.ended.popleft() if self.ended else (self.start, at, 0)
                self.texted = True
                trace("stt.final", text=alt.text, is_final=True, speech_final=True, dg_start=start, words=[[alt.text, start, end, None]])
        return ev


class Deepgram(deepgram.STT):
    """Плагин как есть, только поток — Traced."""

    def stream(self, *, conn_options: APIConnectOptions = CONNECT, **_) -> "Traced":
        s = Traced(stt=self, conn_options=conn_options, opts=self._sanitize_options(), api_key=self._api_key,
                   http_session=self._ensure_session(), base_url=self._opts.endpoint_url)
        self._streams.add(s)
        return s


class Traced(deepgram.stt.SpeechStream):
    """Поток плагина как есть, плюс сообщения самого Deepgram в запись запуска (время в них — секунды от начала звука потока)."""

    def _process_stream_event(self, data: dict) -> None:
        kind = data.get("type")
        if kind == "Results" and (text := data["channel"]["alternatives"][0].get("transcript", "")):
            trace("stt.final" if data.get("is_final") else "stt.interim", text=text, **flags(data))
        elif kind == "SpeechStarted":
            trace("dg.speech_started", dg_at=data.get("timestamp"))
        elif kind == "UtteranceEnd":
            trace("dg.utterance_end", dg_last_word_end=data.get("last_word_end"))
        super()._process_stream_event(data)


def flags(d: dict) -> dict:
    """Из ответа Deepgram — его флаги и слова со временем."""
    alt = ((d.get("channel") or {}).get("alternatives") or [{}])[0]
    return {"is_final": d.get("is_final"), "speech_final": d.get("speech_final"), "from_finalize": d.get("from_finalize"),
            "dg_start": d.get("start"), "dg_duration": d.get("duration"), "confidence": alt.get("confidence"),
            "words": [[w.get("word"), w.get("start"), w.get("end"), w.get("confidence")] for w in alt.get("words") or []]}


class Mic:
    """Микрофон компьютера блоками по BLOCK_S — в Listen.audio. Не открылся — ошибка на страницу, слух ждёт без звука."""

    def __init__(self, listen: Listen) -> None:
        self.listen = listen
        self.stream = None

    def start(self) -> None:
        loop = asyncio.get_running_loop()
        captured = lambda data, *_: loop.call_soon_threadsafe(self.listen.audio, bytes(data))  # noqa: E731
        try:
            self.stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=int(RATE * BLOCK_S), callback=captured)
            self.stream.start()
            emit({"state": "микрофон слушает"})
        except Exception as ex:  # noqa: BLE001
            emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})

    async def play(self, pcm: bytes) -> None:
        """Звук вместо микрофона, в реальном времени (прогоны без человека)."""
        step = int(RATE * BLOCK_S) * 2
        for i in range(0, len(pcm), step):
            self.listen.audio(pcm[i:i + step])
            await asyncio.sleep(BLOCK_S)


# ── только для devtools/server.py ───────────────────────────────────────────────
# Его страница прогоняет вопрос «мысль закончена?» и ждёт по patience: так слух закрывал реплику до модели по звуку. Слух этим
# больше не пользуется; блок уйдёт вместе с правкой devtools (в переходе на цикл devtools не менялся).
LONG_MS = 4000
DOUBT, CERTAIN = 0.5, 0.9
TURN = {"turn": Choice(
    "The user is talking to a voice assistant. Their words come from speech recognition, without punctuation, and may have been cut off. Decide whether "
    "the user's turn is complete. Complete means conversationally complete, not long: one word can be a complete answer, a question "
    "is complete, a correction is complete.",
    {"complete": "the user has taken their turn and the assistant should answer",
     "short": "the user stopped mid-sentence and will continue in a few seconds: the last words leave a phrase open, such as ending "
              "on a conjunction, a preposition, an article, or an unfinished list or number",
     "long": "the user needs time to think or asked the assistant to wait, or has only acknowledged the question without answering it"})}


def patience(verdict: str, p: float) -> float:
    """Сколько секунд тишины ждать после ответа «мысль не закончена»: чем слабее уверенность, тем меньше."""
    if verdict == "long":
        return LONG_MS / 1000
    sure = min(1, max(0, (p - DOUBT) / (CERTAIN - DOUBT)))
    return (QUICK_MS + (PAUSE_MS - QUICK_MS) * sure) / 1000
