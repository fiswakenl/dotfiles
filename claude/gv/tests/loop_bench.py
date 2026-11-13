# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Сравнение: нынешний слух (listen.Turns как есть) против готового цикла LiveKit Agents (AgentSession) на одних и тех же паузах.

Три шага, каждый — своя команда:

    record   записи идут в распознавание один раз (Deepgram напрямую; OpenAI через прокси и с отсечкой тишины), события с временем
             по звуку ложатся в ~/.claude/gv-keep/loop_bench_events.json. Платно, повторно не шлёт то, что уже записано.
    run      те же события проигрываются в реальном времени в обе системы: в Turns — как из потока, в цикл LiveKit — через узел
             stt_node рядом со звуком записи (детектор речи и модель конца цикла слушают настоящий звук). Распознавание не
             платится; платится только классификатор (TURN). Настройка цикла — строкой: "имя det=mini thr=0.4 min=0.8 max=2.5".
    report   правда из поведения человека (после паузы молчал PAUSE_TRUE_S и дольше — договорил), три числа на систему и
             распознавание, склейка нашим вопросом QUESTION одинаково для всех → ~/.claude/gv-keep/loop_bench.json.

det — чем цикл решает конец реплики:
    mini     локальная модель по звуку из пакета (turn-detector-v1-mini), порог thr
    audio    та же модель, но вызванная нами в момент «речь кончилась» распознавания (для OpenAI: ждёт запоздавший текст, hold=1)
    text     наш вопрос TURN: complete → min, иначе max; patience=1 — ждать как listen.patience (наша логика в гнезде цикла)
    vad      только тишина min секунд

    uv run --script tests/loop_bench.py record
    uv run --script tests/loop_bench.py run "turns" "lk-mini det=mini thr=0.4 min=0.8 max=2.5"
    uv run --script tests/loop_bench.py report"""
import asyncio
import contextvars
import json
import logging
import os
import sys
import time
import wave
from collections import deque
from pathlib import Path
from types import SimpleNamespace

import aiohttp
import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HTTPS_PROXY", json.load(open(Path.home() / ".claude/settings.json"))["env"]["HTTPS_PROXY"])
import listen  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import Agent, AgentSession, StopResponse, inference, stt as lkstt  # noqa: E402
from livekit.agents.language import LanguageCode  # noqa: E402
from livekit.agents.stt import SpeechEventType as T  # noqa: E402
from livekit.agents.vad import VADEventType  # noqa: E402
from livekit.agents.voice import io  # noqa: E402
from livekit.local_inference import EOT, EOT_MAX_SAMPLES, VAD, VAD_WINDOW_SAMPLES  # noqa: E402
from livekit.plugins import openai  # noqa: E402

KEEP = Path.home() / ".claude/gv-keep"
EVENTS, OUT = KEEP / "loop_bench_events.json", KEEP / "loop_bench.json"
TRACE = Path.home() / ".claude/gv-trace"  # записи живых запусков: только читаем
# имя: (запись, с какой секунды, сколько секунд); первые три — те же отрезки, что в detect_bench.py, на них подбирались настройки;
# остальные в подборе не участвовали: хвост той же записи и две другие диктовки
FILES = {"минута": (KEEP / "20261112-220530.wav", 0, 60), "attention": (KEEP / "20261112-215413.wav", 0, 120),
         "переобучение": (KEEP / "20261112-214041.wav", 0, 100),
         "хвост-1": (KEEP / "20261112-214041.wav", 415, 80), "хвост-2": (KEEP / "20261112-214041.wav", 740, 70),
         "ночь-1": (TRACE / "20261112-235402.wav", 0, 300), "ночь-2": (TRACE / "20261113-000946.wav", 25, 400)}
HOLDOUT = {"хвост-1", "хвост-2", "ночь-1", "ночь-2"}
STTS = ("deepgram", "openai")
PAUSE_TRUE_S = 3.0
MIN_SPEECH_S = 0.25  # короче — не речь, а щелчок: иначе шум после фразы сдвигает её конец
TAIL_S = 6  # тишина после отрезка: последняя реплика успевает закрыться (LONG_MS — 4 с)
RUN: contextvars.ContextVar = contextvars.ContextVar("run", default=None)
DEC = None  # классификатор, один на все прогоны


def load(name: str) -> bytes:
    path, start, seconds = FILES[name]
    with wave.open(str(path)) as w:
        w.setpos(start * listen.RATE)
        return w.readframes(seconds * listen.RATE)


def frames(pcm: bytes):
    step = int(listen.RATE * listen.BLOCK_S) * 2
    for i in range(0, len(pcm), step):
        yield pcm[i:i + step]


def pauses(pcm: bytes) -> list[dict]:
    """Правда, как в detect_bench.py: паузы по Silero без сети — конец речи, тишина после, договорил ли человек."""
    a = np.frombuffer(pcm, dtype=np.int16)
    vad, step = VAD(), VAD_WINDOW_SAMPLES / listen.RATE
    talk = [vad.predict(a[i:i + VAD_WINDOW_SAMPLES]) >= 0.5 for i in range(0, len(a) - VAD_WINDOW_SAMPLES, VAD_WINDOW_SAMPLES)]
    segs, start = [], None
    for i, t in enumerate(talk + [False]):
        if t and start is None:
            start = i
        elif not t and start is not None:
            segs.append([start * step, i * step])
            start = None
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < 0.2:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    merged = [s for s in merged if s[1] - s[0] >= MIN_SPEECH_S]
    total = len(a) / listen.RATE
    out = []
    for k, (s, e) in enumerate(merged):
        gap = (merged[k + 1][0] if k + 1 < len(merged) else total + PAUSE_TRUE_S) - e
        out.append({"start": round(s, 2), "end": round(e, 2), "gap": round(min(gap, 99), 2), "done": gap >= PAUSE_TRUE_S})
    return out


# ── record: записи → события распознавания ──────────────────────────────────────
async def record(http, name: str, ears: str) -> list[dict]:
    pcm = load(name) + bytes(TAIL_S * listen.RATE * 2)
    if ears == "openai":
        model = openai.STT(api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"],
                           vad=inference.VAD(model="silero", min_silence_duration=listen.VAD_SILENCE_S), **listen.OPENAI_STT)
        model._session = http
    else:
        model = listen.Deepgram(api_key=listen.env(".config/deepgram.env")["DEEPGRAM_API_KEY"], http_session=http, **listen.DEEPGRAM)
    s = model.stream(conn_options=listen.CONNECT)
    events, t0, sent = [], time.monotonic(), 0

    def send(b: bytes) -> None:
        nonlocal sent
        sent += 1
        s.push_frame(rtc.AudioFrame(b, listen.RATE, 1, len(b) // 2))

    async def feed() -> None:
        # отсечка тишины для OpenAI — то же, что listen.Gate
        gate = inference.VAD(model="silero", min_silence_duration=listen.GATE_AFTER_S).stream() if ears == "openai" else None
        before, state = deque(maxlen=int(listen.GATE_BEFORE_S / listen.BLOCK_S)), {"open": False}

        async def watch() -> None:
            async for ev in gate:
                if ev.type == VADEventType.START_OF_SPEECH:
                    state["open"] = True
                    while before:
                        send(before.popleft())
                elif ev.type == VADEventType.END_OF_SPEECH:
                    state["open"] = False
        w = asyncio.create_task(watch()) if gate else None
        for n, b in enumerate(frames(pcm)):
            try:
                if gate is None:
                    send(b)
                else:
                    gate.push_frame(rtc.AudioFrame(b, listen.RATE, 1, len(b) // 2))
                    send(b) if state["open"] else before.append(b)
            except RuntimeError:
                break
            await asyncio.sleep(max(0, t0 + (n + 1) * listen.BLOCK_S - time.monotonic()))
        s.end_input()
        await asyncio.sleep(2)
        if w:
            w.cancel()
        await s.aclose()
    task = asyncio.create_task(feed())
    try:
        async for ev in s:
            alt = ev.alternatives[0] if ev.alternatives else None
            events.append({"t": round(time.monotonic() - t0, 2), "type": ev.type.name, "text": alt.text if alt else "",
                           "start": alt.start_time if alt else 0, "end": alt.end_time if alt else 0, "conf": alt.confidence if alt else 0})
    except Exception as ex:  # noqa: BLE001
        print(name, ears, "поток оборвался:", type(ex).__name__, ex)
        task.cancel()
        return []
    await task
    print(name, ears, "событий", len(events), "в распознавание ушло", round(sent * listen.BLOCK_S), "с")
    return events + [{"t": 0, "type": "SENT", "text": str(round(sent * listen.BLOCK_S))}]


async def cmd_record() -> None:
    got = json.loads(EVENTS.read_text()) if EVENTS.exists() else {}

    async def one(http, name, ears):
        for _ in range(2):  # оборвался — ещё одна попытка
            if ev := await record(http, name, ears):
                got[f"{name}/{ears}"] = ev
                EVENTS.write_text(json.dumps(got, ensure_ascii=False))
                return

    async def chain(http, ears, batches):
        for batch in batches:
            await asyncio.gather(*(one(http, n, ears) for n in batch if f"{n}/{ears}" not in got))
    names = list(FILES)
    async with aiohttp.ClientSession() as direct, aiohttp.ClientSession(proxy=os.environ["HTTPS_PROXY"]) as proxied:
        await asyncio.gather(chain(direct, "deepgram", [names]), chain(proxied, "openai", [names[i:i + 2] for i in range(0, len(names), 2)]))  # через прокси — по два


# ── run: события → система ──────────────────────────────────────────────────────
def speech_event(e: dict, clean: bool):
    if e["type"] in ("INTERIM_TRANSCRIPT", "FINAL_TRANSCRIPT"):
        text = listen.plain(e["text"]) if clean else e["text"]  # OpenAI ставит знаки и заглавные: в listen.py их снимает Segments
        return lkstt.SpeechEvent(type=T[e["type"]], alternatives=[lkstt.SpeechData(language=LanguageCode("ru"), text=text,
                                 start_time=e["start"], end_time=e["end"], confidence=e["conf"])])
    return lkstt.SpeechEvent(type=T[e["type"]])


def turn_events(events: list[dict]) -> list[dict]:
    return [e for e in events if e["type"] in ("START_OF_SPEECH", "END_OF_SPEECH", "INTERIM_TRANSCRIPT", "FINAL_TRANSCRIPT", "RECOGNITION_USAGE")]


async def run_turns(run: dict, events: list[dict], total: float, clean: bool) -> None:
    """Нынешний слух: listen.Turns как есть, события приходят в те же моменты, что из потока."""
    RUN.set(run)
    t0 = run["t0"] = time.monotonic()
    ear = SimpleNamespace(decisions=DEC, hearing=lambda *_: None,
                          heard=lambda text, pause: run["closes"].append({"t": round(time.monotonic() - t0, 2), "text": text}))
    turns = listen.Turns(ear)
    for e in turn_events(events):
        await asyncio.sleep(max(0, t0 + e["t"] - time.monotonic()))
        if e["type"] != "RECOGNITION_USAGE":
            turns.on(speech_event(e, clean))
    await asyncio.sleep(max(0, t0 + total + TAIL_S - time.monotonic()))


class Player(io.AudioInput):
    """Источник звука цикла вместо комнаты: отрезок записи в реальном времени."""

    def __init__(self, pcm: bytes, run: dict) -> None:
        super().__init__(label="запись")
        self.frames, self.n, self.run = list(frames(pcm)), 0, run

    async def __anext__(self) -> rtc.AudioFrame:
        if self.n >= len(self.frames):
            raise StopAsyncIteration
        if not self.n:
            self.run["t0"] = time.monotonic()
        await asyncio.sleep(max(0, self.run["t0"] + self.n * listen.BLOCK_S - time.monotonic()))
        b = self.frames[self.n]
        self.n += 1
        return rtc.AudioFrame(b, listen.RATE, 1, len(b) // 2)


class Replayed(lkstt.STT):
    """Распознавание-заглушка: цикл требует объект, а события даёт узел stt_node из записанных."""

    def __init__(self) -> None:
        super().__init__(capabilities=lkstt.STTCapabilities(streaming=True, interim_results=True))

    async def _recognize_impl(self, *_, **__):
        raise NotImplementedError


class Ear(Agent):
    """Агент без модели и голоса. Узел распознавания отдаёт записанные события в их моменты по звуку."""

    def __init__(self, run: dict, events: list[dict], clean: bool, cfg: dict) -> None:
        super().__init__(instructions="")
        self.run, self.events, self.clean, self.cfg = run, turn_events(events), clean, cfg
        self.pending = 0  # сколько «речь кончилась» распознавания ещё без своего текста (OpenAI отдаёт текст позже)
        self.audio_p: asyncio.Future | None = None
        self.eot = EOT() if cfg["det"] == "audio" else None

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        self.run["closes"].append({"t": round(time.monotonic() - self.run["t0"], 2), "text": new_message.text_content or ""})
        raise StopResponse

    async def stt_node(self, audio, model_settings):
        i, n, last = 0, 0, deque(maxlen=int(EOT_MAX_SAMPLES / listen.RATE / listen.BLOCK_S))
        async for frame in audio:
            n += 1
            last.append(frame)
            while i < len(self.events) and self.events[i]["t"] <= n * listen.BLOCK_S:
                e = self.events[i]
                i += 1
                if e["type"] == "END_OF_SPEECH":
                    self.pending += 1
                    if self.eot:
                        pcm = np.frombuffer(b"".join(bytes(f.data) for f in last), dtype=np.int16)[-EOT_MAX_SAMPLES:]
                        self.audio_p = asyncio.ensure_future(asyncio.to_thread(self.eot.predict, pcm))
                elif e["type"] == "RECOGNITION_USAGE":  # ответ OpenAI на конец речи пришёл целиком: с текстом или без
                    self.pending = max(0, self.pending - 1)
                    continue
                yield speech_event(e, self.clean)

    async def settled(self) -> None:
        """Ждать текст на каждый услышанный конец речи; цикл сам перезапустит вопрос, когда текст придёт."""
        t0 = time.monotonic()
        while self.cfg["hold"] and self.pending and time.monotonic() - t0 < 3:
            await asyncio.sleep(0.02)


class TextTurn:
    """Наш вопрос TURN как детектор цикла. Не ответил — «договорит» (max), как PAUSE_MS в listen.py."""

    model, provider = listen.DECISIONS_MODEL, "openai"

    def __init__(self, ear: Ear) -> None:
        self.ear = ear

    async def unlikely_threshold(self, language) -> float:
        return 0.5

    async def supports_language(self, language) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx, *, timeout=None) -> float:
        t0 = time.monotonic()
        await self.ear.settled()
        text = listen.plain(chat_ctx.items[-1].text_content or "")
        try:
            r = (await DEC.evaluate(input=f"user: {text}", decisions=listen.TURN, conn_options=listen.TURN_WAIT)).results["turn"]
        except Exception:  # noqa: BLE001
            return 0.0
        p = (r.probabilities or {}).get(r.value, 0)
        note("turn.check", verdict=r.value, p=round(p, 2), s=round(time.monotonic() - t0, 2), text=text[-60:])
        if r.value == "complete":
            return 1.0
        if self.ear.cfg["patience"]:  # ждём сами столько, сколько ждал бы Turns; цикл после этого закрывает сразу (max мал)
            await asyncio.sleep(max(0, t0 - 0.3 + listen.patience(r.value, p) - time.monotonic()))
        return 0.0


class AudioTurn:
    """Локальная модель по звуку, вызванная в момент «речь кончилась» распознавания; ждёт запоздавший текст (hold)."""

    model, provider = "turn-detector-v1-mini", "local"

    def __init__(self, ear: Ear) -> None:
        self.ear = ear

    async def unlikely_threshold(self, language) -> float:
        return self.ear.cfg["thr"]

    async def supports_language(self, language) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx, *, timeout=None) -> float:
        await self.ear.settled()
        p = float(await self.ear.audio_p) if self.ear.audio_p else 1.0
        note("audio.check", p=round(p, 2))
        return p


def note(kind: str, **data) -> None:
    if run := RUN.get():
        run["checks"].append({"t": round(time.monotonic() - run.get("t0", time.monotonic()), 2), "kind": kind, **data})


class Verdicts(logging.Handler):
    """Ответ детектора цикла есть только в отладочном журнале библиотеки (см. livekit_loop_probe.py)."""

    def emit(self, r: logging.LogRecord) -> None:
        if r.getMessage() == "eot prediction" and getattr(r, "probability", None) is not None:
            note("lk.eot", p=round(r.probability, 2), wait=r.endpointing_delay, by=r.trigger)
        elif r.levelno >= logging.WARNING:
            note("lk.warning", text=r.getMessage()[:160])


async def run_loop(run: dict, events: list[dict], pcm: bytes, clean: bool, cfg: dict) -> None:
    """Готовый цикл LiveKit: AgentSession без комнаты, звук — запись, распознавание — записанные события."""
    RUN.set(run)
    ear = Ear(run, events, clean, cfg)
    det = {"mini": lambda: inference.TurnDetector(version="v1-mini", unlikely_threshold=cfg["thr"]), "audio": lambda: AudioTurn(ear),
           "text": lambda: TextTurn(ear), "vad": lambda: "vad"}[cfg["det"]]()
    session = AgentSession(stt=Replayed(), vad=inference.VAD(model="silero", min_silence_duration=cfg["vadsil"]), user_away_timeout=None,
                           turn_handling={"turn_detection": det, "endpointing": {"min_delay": cfg["min"], "max_delay": cfg["max"]}})
    player = Player(pcm + bytes(TAIL_S * listen.RATE * 2), run)
    session.input.audio = player
    await session.start(ear)
    while player.n < len(player.frames):
        await asyncio.sleep(0.2)
    await asyncio.sleep(0.5)
    await session.aclose()


def parse(spec: str) -> tuple[str, dict]:
    label, *pairs = spec.split()
    cfg = {"det": "turns", "thr": 0.4, "min": 0.7, "max": 2.5, "vadsil": 0.25, "hold": 0, "patience": 0, "stt": ",".join(STTS), "files": ",".join(FILES)}
    for k, v in (p.split("=", 1) for p in pairs):
        cfg[k] = v if k in ("det", "stt", "files") else float(v)
    return label, cfg


async def cmd_run(specs: list[str]) -> None:
    global DEC
    events = json.loads(EVENTS.read_text())
    out = json.loads(OUT.read_text()) if OUT.exists() else {"runs": {}}
    DEC = openai.DecisionModel(model=listen.DECISIONS_MODEL, api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"])
    listen.log = note  # журнал Turns — в прогон, а не в ~/.claude/gv.log живого сервиса
    lk = logging.getLogger("livekit.agents")
    lk.setLevel(logging.DEBUG)
    lk.propagate = False
    lk.addHandler(Verdicts())
    await DEC.evaluate(input="user: привет", decisions=listen.TURN)  # прогрев, как keep_warm
    audio = {n: load(n) for n in FILES}
    jobs = []
    for spec in specs:
        label, cfg = parse(spec)
        out["runs"][label] = {"cfg": cfg, "data": out["runs"].get(label, {}).get("data", {})}
        for name in cfg["files"].split(","):
            for ears in cfg["stt"].split(","):
                run = out["runs"][label]["data"][f"{name}/{ears}"] = {"closes": [], "checks": []}
                ev, clean = events[f"{name}/{ears}"], ears == "openai"
                jobs.append(run_turns(run, ev, len(audio[name]) / 2 / listen.RATE, clean) if cfg["det"] == "turns"
                            else run_loop(run, ev, audio[name], clean, cfg))
    print("прогонов разом:", len(jobs))
    for r in await asyncio.gather(*jobs, return_exceptions=True):
        if isinstance(r, Exception):
            print("прогон упал:", type(r).__name__, r)
    for label in (parse(s)[0] for s in specs):
        for run in out["runs"][label]["data"].values():
            run.pop("t0", None)
            run.pop("glued", None)
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))


# ── report: правда, три числа, склейка ──────────────────────────────────────────
def score(truth: list[dict], closes: list[dict]) -> dict:
    """Каждое закрытие относится к последней паузе, что началась до него: зря закрытая посреди мысли фраза часто закрывается
    уже под следующие слова."""
    hit: dict[int, list[float]] = {}
    for c in closes:
        k = max((i for i, p in enumerate(truth) if p["end"] <= c["t"] - 0.05), default=None)
        if k is not None:
            hit.setdefault(k, []).append(c["t"])
    ends = [i for i, p in enumerate(truth) if p["done"]]
    return {"ends": len(ends), "closed": sum(i in hit for i in ends), "delays": [round(hit[i][0] - truth[i]["end"], 2) for i in ends if i in hit],
            "mid": len(truth) - len(ends), "false": sum(i in hit for i in range(len(truth)) if not truth[i]["done"]), "messages": len(closes)}


async def glue(events: list[dict], closes: list[dict]) -> int:
    """Сколько сообщений останется после нашего вопроса QUESTION: одинаково для обеих систем, пауза — как её считает Turns
    (от закрытия прошлой реплики до первого текста этой)."""
    texts = [e["t"] for e in events if e["type"] in ("INTERIM_TRANSCRIPT", "FINAL_TRANSCRIPT") and e["text"].strip()]
    last, n = None, 0
    for k, c in enumerate(closes):
        if not c["text"].strip():
            continue
        if last is not None:
            prev = closes[k - 1]["t"]
            first = next((t for t in texts if t > prev - 1.5), c["t"])  # текст, пришедший чуть раньше закрытия, — уже этой реплики
            try:
                r = await DEC.evaluate(input=listen.said(listen.plain(c["text"]), last, round(max(0, first - prev), 1), False),
                                       decisions=listen.QUESTION, conn_options=listen.ROUTE_WAIT)
                p = r.results["continues"].value
            except Exception:  # noqa: BLE001
                p = 0
            c["continues"] = round(p, 2)
            if p >= listen.SURE:
                last.text += " " + listen.plain(c["text"])
                continue
        last, n = SimpleNamespace(text=listen.plain(c["text"]), state="sent", answer="", id=1), n + 1
    return n


async def cmd_report() -> None:
    global DEC
    DEC = openai.DecisionModel(model=listen.DECISIONS_MODEL, api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"])
    events, out = json.loads(EVENTS.read_text()), json.loads(OUT.read_text())
    truth = out["truth"] = {n: pauses(load(n)) for n in FILES}
    for n, ps in truth.items():
        print(f"{n}: пауз {len(ps)}, из них договорил {sum(p['done'] for p in ps)}")
    todo = [(key, run) for r in out["runs"].values() for key, run in r["data"].items() if "glued" not in run]
    for (key, run), n in zip(todo, await asyncio.gather(*(glue(events[key], run["closes"]) for key, run in todo))):
        run["glued"] = n
    table = out["table"] = {}
    print(f"\n{'система':<22}{'слух':<9}{'отрезки':<9}{'концы: закрыто':<16}{'через, с (медиана / худшее)':<29}{'зря посреди мысли':<19}{'сообщений → после склейки / ждали'}")
    for label, r in out["runs"].items():
        for ears in STTS:
            for part, names in (("подбор", [n for n in FILES if n not in HOLDOUT]), ("отлож.", sorted(HOLDOUT))):
                rows = [(score(truth[n], r["data"][f"{n}/{ears}"]["closes"]), r["data"][f"{n}/{ears}"]["glued"]) for n in names if f"{n}/{ears}" in r["data"]]
                if not rows:
                    continue
                s = {k: sum((x[k] for x, _ in rows), [] if k == "delays" else 0) for k in rows[0][0]}
                d = sorted(s["delays"]) or [0]
                s.update(median=d[len(d) // 2], worst=d[-1], glued=sum(g for _, g in rows))
                table[f"{label}/{ears}/{part}"] = s
                print(f"{label:<22}{ears:<9}{part:<9}{str(s['closed']) + ' из ' + str(s['ends']):<16}{str(s['median']) + ' / ' + str(s['worst']):<29}"
                      f"{str(s['false']) + ' из ' + str(s['mid']):<19}{s['messages']} → {s['glued']} / {s['ends']}")
    for wait in (1.0, 1.5, 2.5):  # для сравнения, без прогона: закрывать по одной тишине, считая от правды
        for part, names in (("подбор", [n for n in FILES if n not in HOLDOUT]), ("отлож.", sorted(HOLDOUT))):
            rows = [score(truth[n], [{"t": q["end"] + wait} for q in truth[n] if q["gap"] > wait]) for n in names]
            s = {k: sum(x[k] for x in rows) for k in ("ends", "closed", "mid", "false", "messages")}
            table[f"тишина {wait} с (расчёт)/-/{part}"] = s
            print(f"{'тишина ' + str(wait) + ' с (расчёт)':<22}{'-':<9}{part:<9}{str(s['closed']) + ' из ' + str(s['ends']):<16}{str(wait):<29}"
                  f"{str(s['false']) + ' из ' + str(s['mid']):<19}{s['messages']} → - / {s['ends']}")
    OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))


if __name__ == "__main__":
    cmd = sys.argv[1] if len(sys.argv) > 1 else "report"
    asyncio.run({"record": cmd_record, "report": cmd_report}[cmd]() if cmd != "run" else cmd_run(sys.argv[2:]))
