"""Слух GV: микрофон → Deepgram → реплики → решение, куда их отправить. Deepgram и классификатор OpenAI Decisions — плагины LiveKit
Agents как библиотеки, без сервера и сессии LiveKit.

    микрофон ──весь звук──▶ Deepgram (nova-3) ──текст──▶ Turns ──реплика──▶ решение ──▶ core.call(ask | quick)

Текст — без знаков препинания и заглавных (punctuate и smart_format выключены): Deepgram сам ставил точку на паузе, и классификатор
принимал её за конец мысли. Паузы слышит сам Deepgram. Реплика кончается, когда он сказал «речь кончилась» (speech_final или UtteranceEnd) и классификатор
по тексту отвечает «мысль закончена»; не закончена — когда нового текста нет столько, сколько он в этом уверен (patience:
от QUICK_MS до PAUSE_MS, «подожди» — LONG_MS); не ответил — PAUSE_MS.

Куда реплику — тот же классификатор, вероятность: продолжает ли она последний переданный ask (договаривает, уточняет,
исправляет). Да — ask с replaces=N: ядро склеит текст и откатит №N. Нет — новая мысль: пока Claude занят — quick, свободен — ask.
Неуверенное «да» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.
Тот же вопрос задаётся и рано — по первым словам новой фразы, пока Claude работает над последней репликой: «да» останавливает его
ход сразу (core.interrupt); слов мало — вопрос повторяется на каждом новом куске текста. Решает всё равно вопрос по всей фразе.
Оба видят паузу перед фразой, и в вопросе сказано, что она значит: без этого классификатор её не замечал (17 с тишины — «продолжение» 0,86).
Вопрос по всей фразе получает вход тегами и паузу словами (said): так он на замере в devtools делит увереннее всего; ранний — строками (state).

Вместо Deepgram слушать может OpenAI (gpt-live-transcribe по сокету Realtime): команды /gv:openai и /gv:deepgram пишут выбор в ~/.claude/gv.ears (GV_EARS в окружении — важнее).
Дальше всё то же — Turns и три вопроса; отличия прячет Segments: OpenAI ставит знаки и заглавные (убираем), конца речи не слышит
(его ловит локальный Silero из плагина — VAD_SILENCE_S тишины) и текст куска отдаёт уже после «речь кончилась», а не до. Ходит через
HTTPS_PROXY; список KEYWORDS подсказывает ему термины («Claude Code», а не «Cloud код»).

Связь с Deepgram: подключение и обрыв ловит плагин (таймаут подключения, тишина в сокете); сдался — поток пересоздаётся через
RETRY_S, звук за это время не копится, вечно. Ходит напрямую, без прокси: сон мака и туннель его не касаются.

Всё, что пришло от Deepgram, — в запись запуска (journal.trace): текст с флагами и словами, события SpeechStarted и UtteranceEnd,
звук микрофона в WAV. Ключи: Deepgram — DEEPGRAM_API_KEY в ~/.config/deepgram.env, OpenAI — OPENAI_API_KEY в ~/.config/openai.env
(прокси — HTTPS_PROXY из окружения).
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
import sounddevice as sd
from livekit import rtc
from livekit.agents import APIConnectOptions, APIError, inference
from livekit.agents.llm import Choice, Probability
from livekit.agents.stt import SpeechEventType
from livekit.plugins import deepgram, openai

from journal import emit, log, trace, trace_open

RATE = 16000
BLOCK_S = 0.02  # блок микрофона
PAUSE_MS = int(os.environ.get("GV_PAUSE_MS", 2500))  # столько без нового текста — реплика закончена: классификатор уверен, что человек договорит, или не ответил
QUICK_MS = 700  # столько же — когда он в «договорит» не уверен
LONG_MS = 4000  # и столько — когда человек думает или просил подождать
DOUBT, CERTAIN = 0.5, 0.9  # уверенность в «договорит»: ниже первой — ждём QUICK_MS, выше второй — PAUSE_MS, между — по прямой
UTTERANCE_END_MS = 1000  # UtteranceEnd Deepgram: столько без новых слов; меньше 1000 он не принимает
DEEPGRAM = dict(model="nova-3", language="ru", punctuate=False, smart_format=False, filler_words=False, vad_events=True,
                utterance_end_ms=UTTERANCE_END_MS)  # настройки потока; без знаков — см. шапку
EARS_FILE = Path.home() / ".claude" / "gv.ears"
EARS = os.environ.get("GV_EARS") or (EARS_FILE.read_text().strip() if EARS_FILE.exists() else "") or "deepgram"  # чем слушать: deepgram | openai
KEYWORDS = ["Claude Code", "Claude", "Codex", "Deepgram", "devtools"]  # термины, которые OpenAI иначе слышит по-своему
OPENAI_STT = dict(model="gpt-live-transcribe", language="ru", keywords=KEYWORDS)
VAD_SILENCE_S = 0.4  # столько тишины локальный детектор речи считает концом куска для OpenAI
RETRY_S = 2  # поток Deepgram сдался — новый через столько
CONNECT = APIConnectOptions(max_retry=0, timeout=10)  # повторы — наш цикл: новый поток не тащит звук, накопленный без связи
TURN_WAIT = APIConnectOptions(max_retry=0, timeout=2)  # не ответил — реплику закроет пауза без текста
ROUTE_WAIT = APIConnectOptions(max_retry=0, timeout=5)  # не ответил — новая мысль
DECISIONS_MODEL = "gpt-6-luna"  # gpt-6-luna-decisions, что плагин берёт по умолчанию, нашему ключу недоступна
SURE = 0.7  # вероятность продолжения ниже — не поправка, а новая мысль
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}
CUT = "Claude начал отвечать, но человек заговорил снова, и ответ остановлен"
TURN = {"turn": Choice(
    "The user is talking to a voice assistant. Their words come from speech recognition, without punctuation, and may have been cut off. Decide whether "
    "the user's turn is complete. Complete means conversationally complete, not long: one word can be a complete answer, a question "
    "is complete, a correction is complete.",
    {"complete": "the user has taken their turn and the assistant should answer",
     "short": "the user stopped mid-sentence and will continue in a few seconds: the last words leave a phrase open, such as ending "
              "on a conjunction, a preposition, an article, or an unfinished list or number",
     "long": "the user needs time to think or asked the assistant to wait, or has only acknowledged the question without answering it"})}
QUESTION = {"continues": Probability(
    "Ты собираешь диктовку голосом в сообщения. Новую реплику надо дописать в то же сообщение, что и предыдущую "
    "(человек договаривает, уточняет или поправляет), а не отправить отдельным? "
    "Длина паузы перед ней — такой же сигнал, как смысл: люди договаривают почти без паузы, а замолчав надолго, обычно начинают новое.")}
EARLY = {"continues": Probability(
    "Человек диктует голосом, речь нарезана по паузам. Предыдущая реплика уже передана, а человек заговорил снова: слышно только "
    "начало новой фразы, она ещё не договорена. Это начало продолжения или поправки предыдущей реплики "
    "(договаривает, уточняет, исправляет её), а не начало отдельной мысли? "
    "Учитывай паузу перед новой репликой: меньше секунды — человек договаривает фразу; несколько секунд и больше — он уже "
    "закончил и начинает новую мысль, если начало само по себе звучит как новая мысль.")}

logging.basicConfig(level=logging.WARNING)  # в ~/.claude/gv.out — только сбои библиотек


def plain(text: str) -> str:
    """Текст как от Deepgram без знаков: строчными, без пунктуации."""
    return re.sub(r"[^\w\s-]", "", text).lower()


def patience(verdict: str, p: float) -> float:
    """Сколько секунд тишины ждать после ответа «мысль не закончена»: чем слабее уверенность, тем меньше. Закрыть фразу рано — дёшево
    (заговорит снова — ранняя проверка остановит Claude, и фраза доклеится), ждать зря — дорого каждый раз."""
    if verdict == "long":
        return LONG_MS / 1000
    sure = min(1, max(0, (p - DOUBT) / (CERTAIN - DOUBT)))
    return (QUICK_MS + (PAUSE_MS - QUICK_MS) * sure) / 1000


def env(path: str) -> dict:
    return {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / path).read_text().splitlines() if "=" in l and not l.startswith("#"))}


def words(sec: float) -> str:
    """Пауза словами: голое число классификатор читает хуже."""
    return (f"{sec} с — короткая, человек почти не останавливался" if sec < 1 else
            f"{sec} с — средняя, человек запнулся или подбирал слово" if sec < 3 else
            f"{sec} с — долгая, человек замолчал и потом заговорил снова")


def said(text: str, last, pause: float | None, cut: bool) -> str:
    """Что видит «продолжение?» по всей фразе: прошлый ask с его состоянием, пауза перед новой репликой (None — неизвестна, тега нет)
    и сама реплика — тегами."""
    d = {"до_паузы": last.text, "состояние": CUT if cut else STATE[last.state],
         **({} if pause is None else {"пауза": words(round(pause, 1))}), "после_паузы": text}
    return "\n".join(f"<{k}>{v}</{k}>" for k, v in d.items())


def state(text: str, last, pause: float | None) -> str:
    """Что видит ранняя проверка: прошлый ask с его состоянием, пауза перед новой репликой (None — неизвестна) и начало реплики."""
    gap = "" if pause is None else f"Пауза в речи перед новой репликой: {pause:.1f} с\n"
    return (f"Предыдущая реплика ({STATE[last.state]}, передана {time.monotonic() - last.at:.1f} с назад): {last.text}\n"
            f"{gap}Начало новой реплики: {text}")


class Listen:
    def __init__(self, core) -> None:
        self.core = core
        self.mic = Mic(self)
        self.turns = Turns(self)
        self.decisions = openai.DecisionModel(model=DECISIONS_MODEL, api_key=os.environ.get("OPENAI_API_KEY") or env(".config/openai.env")["OPENAI_API_KEY"])
        self.stream = None  # идущий поток распознавания (Deepgram или OpenAI)
        self.wav: wave.Wave_write | None = None
        self.audio_t0 = 0.0  # когда пошёл звук записи: от него время кусков в записи запуска
        self.order = asyncio.Lock()  # реплики решаются по одной, в порядке речи: следующая видит, куда ушла прошлая
        self.partial = self.asked = ""  # что уже слышно в идущей фразе; по какому её началу уже спросили классификатор
        self.settled = False  # ход Claude этой фразой уже остановлен
        self.checking: asyncio.Task | None = None
        self.tasks: set[asyncio.Task] = set()

    async def run(self) -> None:
        self.decisions.on("metrics_collected", lambda m: log("decide.metrics", s=round(m.duration, 2), tokens=m.input_tokens))
        self.spawn(self.keep_warm())
        self.wav_path = trace_open().with_suffix(".wav")
        openai_ears = EARS == "openai"
        # без trust_env: Deepgram — напрямую, мимо HTTPS_PROXY; OpenAI — через него
        async with aiohttp.ClientSession(proxy=os.environ.get("HTTPS_PROXY") if openai_ears else None) as http:
            if openai_ears:
                stt = openai.STT(api_key=os.environ.get("OPENAI_API_KEY") or env(".config/openai.env")["OPENAI_API_KEY"],
                                 vad=inference.VAD(model="silero", min_silence_duration=VAD_SILENCE_S), **OPENAI_STT)
                stt._session = http  # плагин берёт сессию из задания LiveKit, которого у нас нет
                same = Segments(self)
            else:
                stt = Deepgram(api_key=os.environ.get("DEEPGRAM_API_KEY") or env(".config/deepgram.env")["DEEPGRAM_API_KEY"],
                               http_session=http, **DEEPGRAM)
                same = lambda ev: ev  # noqa: E731
            self.mic.start()
            log("mic.start", ears=EARS)
            while True:
                self.stream = stream = stt.stream(conn_options=CONNECT)
                try:
                    async for ev in stream:
                        self.turns.on(same(ev))
                except Exception as ex:  # noqa: BLE001 — поток сдался (обычно APIError): ошибка на страницу, новый поток
                    self.error(f"{type(ex).__name__}: {ex}")
                finally:
                    self.stream = None
                    await stream.aclose()
                await asyncio.sleep(RETRY_S)

    def audio(self, pcm: bytes) -> None:
        """Блок звука: в WAV записи и в поток распознавания; связи нет — звук в поток пропадает, в WAV — нет."""
        if self.wav is None:
            self.audio_t0 = time.time()
            self.wav = wave.open(str(self.wav_path), "wb")
            self.wav.setnchannels(1)
            self.wav.setsampwidth(2)
            self.wav.setframerate(RATE)
            trace("audio.start", file=self.wav_path.name, rate=RATE)
        self.wav.writeframes(pcm)
        if self.stream:
            try:
                self.stream.push_frame(rtc.AudioFrame(pcm, RATE, 1, len(pcm) // 2))
            except RuntimeError:  # поток уже закрыт: новый вот-вот
                pass

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
                await self.decisions.evaluate(input="user: привет", decisions=TURN, conn_options=APIConnectOptions(max_retry=0))
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
            if await self.ask_continues(EARLY, state(text, last, pause), "route.early", last=last.id, text=text):
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
            continues = bool(last) and await self.ask_continues(QUESTION, said(text, last, pause, cut == last.id), "route",
                                                                 last=last.id, pause=pause and round(pause, 1), cut=cut == last.id)
            # Остановленный запрос ядро отправит заново: Claude снова занят им, как был до остановки. Остановка — на момент ответа
            # классификатора: пока он думал, ранний вопрос по следующей фразе мог её устроить.
            name = "ask" if continues or not (self.core.claude_busy() or self.core.cut) else "quick"
            self.core.call(name, {"heard": text, "replaces": last.id if continues else 0})

    async def ask_continues(self, question: dict, situation: str, event: str, **fields) -> bool:
        """Вероятность «продолжение» от классификатора; не ответил — нет: ход Claude не трогаем, новая мысль ничего не стирает."""
        try:
            t0 = time.monotonic()
            r = await self.decisions.evaluate(input=situation, decisions=question, conn_options=ROUTE_WAIT)
            if "continues" not in r.results:
                raise APIError(r.errors.get("continues", "нет ответа"))
            p = r.results["continues"].value
            log(event, p=round(p, 2), s=round(time.monotonic() - t0, 2), **fields)
            return p >= SURE
        except Exception as ex:  # noqa: BLE001
            log("route.error", error=f"{type(ex).__name__}: {ex}")
            return False


class Turns:
    """Реплики из потока распознавания. Черновики и окончательные куски — в раннюю проверку; «речь кончилась» — вопрос классификатору
    «мысль закончена?»: да — реплика закрыта. Пауза перед репликой — от конца прошлой до первого куска этой.
    У Deepgram кусок приходит до «речь кончилась», у OpenAI — после: тогда вопрос задаётся по куску, если человек всё ещё молчит."""

    def __init__(self, listen: Listen) -> None:
        self.listen = listen
        self.parts: list[str] = []
        self.closed: float | None = None  # когда закрыта прошлая реплика
        self.pause: float | None = None
        self.last = 0.0  # когда пришёл последний кусок текста: тишина считается от него
        self.speaking = False  # распознавание слышит речь
        self.asking: asyncio.Task | None = None
        self.waiting: asyncio.TimerHandle | None = None

    def on(self, ev) -> None:
        text = ev.alternatives[0].text.strip() if ev.alternatives else ""
        if ev.type == SpeechEventType.START_OF_SPEECH:
            self.speaking = True
        elif ev.type == SpeechEventType.INTERIM_TRANSCRIPT and text:
            self.more()
            self.listen.hearing(" ".join([*self.parts, text]), self.pause)
        elif ev.type == SpeechEventType.FINAL_TRANSCRIPT and text:
            self.more()
            self.parts.append(text)
            self.last = time.monotonic()
            self.wait(PAUSE_MS / 1000)
            self.listen.hearing(" ".join(self.parts), self.pause)
            if not self.speaking:
                self.asking = asyncio.create_task(self.ask(" ".join(self.parts)))
        elif ev.type == SpeechEventType.END_OF_SPEECH:
            self.speaking = False
            self.last = time.monotonic()  # речь кончилась только что: у OpenAI текст последнего куска ещё в пути, а тишина уже идёт
            if self.parts:
                self.asking = asyncio.create_task(self.ask(" ".join(self.parts)))

    def more(self) -> None:
        """Пришёл текст: начало реплики — запомнить паузу перед ней; продолжение — прежние ожидания конца отменить."""
        if not self.parts and self.pause is None and self.closed is not None:
            self.pause = time.monotonic() - self.closed
        for pending in (self.asking, self.waiting):
            if pending:
                pending.cancel()
        self.asking = self.waiting = None

    def wait(self, silence: float) -> None:
        """Закрыть реплику, когда от последнего куска текста пройдёт silence секунд тишины."""
        if self.waiting:
            self.waiting.cancel()
        self.waiting = asyncio.get_running_loop().call_later(max(0, self.last + silence - time.monotonic()), self.close)

    async def ask(self, text: str) -> None:
        t0 = time.monotonic()
        try:
            r = (await self.listen.decisions.evaluate(input=f"user: {text}", decisions=TURN, conn_options=TURN_WAIT)).results["turn"]
        except Exception as ex:  # noqa: BLE001 — не ответил: реплику закроет пауза без текста
            return log("turn.check.error", error=f"{type(ex).__name__}: {ex}")
        p = (r.probabilities or {}).get(r.value, 0)
        silence = None if r.value == "complete" else patience(r.value, p)
        log("turn.check", verdict=r.value, p=round(p, 2), s=round(time.monotonic() - t0, 2), wait=silence and round(silence, 1), text=text)
        if not self.parts:
            return
        if silence is None:
            self.close()
        else:
            self.wait(silence)

    def close(self) -> None:
        if self.waiting:
            self.waiting.cancel()
        text, self.parts, self.waiting = " ".join(self.parts), [], None
        if text:
            self.closed = time.monotonic()
            self.listen.heard(text, self.pause)
        self.pause = None


class Segments:
    """События OpenAI — в том же виде, что у Deepgram: текст без знаков и заглавных, в запись запуска — те же stt.interim и stt.final.
    Времени слов у OpenAI нет: кусок размечен одним «словом» от начала до конца речи по локальному детектору (точность около 0,3 с);
    конец речи приходит раньше текста куска, поэтому концы ждут свои куски в очереди. На каждый конец OpenAI отвечает либо текстом и
    счётом расхода, либо одним счётом (шум, слов нет) — тогда конец из очереди уходит без куска."""

    def __init__(self, listen: "Listen") -> None:
        self.listen = listen
        self.start = 0.0
        self.ended: deque = deque()
        self.texted = False  # у текущего ответа OpenAI был текст

    def __call__(self, ev):
        at = round(time.time() - self.listen.audio_t0, 2)
        if ev.type == SpeechEventType.START_OF_SPEECH:
            self.start = at
        elif ev.type == SpeechEventType.END_OF_SPEECH:
            self.ended.append((self.start, round(at - VAD_SILENCE_S, 2)))
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
                start, end = self.ended.popleft() if self.ended else (self.start, at)
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
