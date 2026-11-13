# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Замер слуха: настоящий listen.py на записях диктовки — сколько концов сообщения закрыто и как быстро, сколько раз фраза
закрыта зря посреди мысли, сколько сообщений ушло в ядро. По этому замеру подобраны END_SURE, QUICK_MS и PAUSE_MS.

    record   записи идут в распознавание один раз (Deepgram напрямую; OpenAI через прокси и с отсечкой тишины), события с временем
             по звуку ложатся в ~/.claude/gv-keep/loop_bench_events.json. Платно, повторно не шлёт то, что уже записано.
    run      listen.Listen целиком на каждом отрезке с обоими распознаваниями: звук записи в реальном времени, вместо сети
             распознавания — записанные события в их моменты, ядро настоящее, вместо плагина и Claude — заглушка.
             Платится только классификатор. Имя прогона — аргументом (по умолчанию listen.py).
    report   правда из поведения человека (после паузы молчал PAUSE_TRUE_S и дольше — договорил) и таблица по всем прогонам из
             ~/.claude/gv-keep/loop_bench.json; в нём же прогоны прежнего слуха (turns) и вариантов цикла (lk-*), по которым решали.
    live     один отрезок через настоящее распознавание (GV_EARS), запись запуска — во временную папку: что услышано, что ушло
             в ядро, сколько звука ушло в OpenAI. --silence N — N секунд тишины перед отрезком. Платно.

    uv run --script tests/loop_bench.py record
    uv run --script tests/loop_bench.py run && uv run --script tests/loop_bench.py report
    GV_EARS=openai uv run --script tests/loop_bench.py live минута --silence 150"""
import asyncio
import contextvars
import json
import os
import sys
import time
import wave
from collections import deque
from pathlib import Path
import tempfile

import aiohttp
import numpy as np

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HTTPS_PROXY", json.load(open(Path.home() / ".claude/settings.json"))["env"]["HTTPS_PROXY"])
import listen  # noqa: E402
from core import Core  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import inference, stt as lkstt  # noqa: E402
from livekit.agents.language import LanguageCode  # noqa: E402
from livekit.agents.stt import SpeechEventType as T  # noqa: E402
from livekit.agents.vad import VADEventType  # noqa: E402
from livekit.local_inference import VAD, VAD_WINDOW_SAMPLES  # noqa: E402
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
TAIL_S = 6  # тишина после отрезка: последняя реплика успевает закрыться
CLAUDE_S = 8  # столько заглушка Claude «отвечает» на запрос
RUN: contextvars.ContextVar = contextvars.ContextVar("run", default=None)


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


# ── run и live: настоящий listen.py ─────────────────────────────────────────────
class NoMic:
    """Вместо микрофона: поток не открывается, звук даёт прогон."""

    def __init__(self, *_, **__) -> None: ...

    def start(self) -> None: ...


def note(kind: str, **data) -> None:
    """Журнал слуха и ядра — в прогон, а не в ~/.claude/gv.log живого сервиса."""
    if run := RUN.get():
        run["log"].append({"t": round(time.monotonic() - run.get("t0", time.monotonic()), 2), "kind": kind, **data})


class Replayed(lkstt.STT):
    """Распознавание без сети: поток отдаёт записанные события в их моменты по звуку прогона."""

    def __init__(self, events: list[dict], run: dict) -> None:
        super().__init__(capabilities=lkstt.STTCapabilities(streaming=True, interim_results=True))
        self.events, self.run = [e for e in events if e["type"] != "SENT"], run

    async def _recognize_impl(self, *_, **__):
        raise NotImplementedError

    def stream(self, *, conn_options=listen.CONNECT, **_) -> "Replay":
        return Replay(stt=self, conn_options=conn_options)


class Replay(lkstt.RecognizeStream):
    async def _run(self) -> None:
        async def drain() -> None:
            async for _ in self._input_ch:
                pass
        draining = asyncio.create_task(drain())
        while "t0" not in self._stt.run:
            await asyncio.sleep(0.01)
        for e in self._stt.events:
            await asyncio.sleep(max(0, self._stt.run["t0"] + e["t"] - time.monotonic()))
            alt = [lkstt.SpeechData(language=LanguageCode("ru"), text=e["text"], start_time=e["start"], end_time=e["end"], confidence=e["conf"])]
            self._event_ch.send_nowait(lkstt.SpeechEvent(type=T[e["type"]], alternatives=alt if e["type"].endswith("TRANSCRIPT") else [],
                                                         recognition_usage=lkstt.RecognitionUsage(audio_duration=0) if e["type"] == "RECOGNITION_USAGE" else None))
        await draining


async def plugin(core: Core) -> None:
    """Заглушка плагина и Claude, как в trace_demo.py: остановку принимает сразу, на запрос отвечает через CLAUDE_S секунд."""
    loop = asyncio.get_running_loop()
    while True:
        r = core.next()
        if not r:
            await asyncio.sleep(0.02)
        elif r["kind"] == "cancel":
            core.answer(r["id"])
        elif r["kind"] in ("ask", "quick"):
            loop.call_later(CLAUDE_S, core.answer, r["id"], "ответ")


async def hear(run: dict, pcm: bytes, events: list[dict] | None, emit=lambda *_: None, log=note) -> None:
    """Отрезок через listen.Listen и ядро. events — записанные события вместо сети распознавания; None — настоящее распознавание."""
    RUN.set(run)
    core = Core(emit=emit, log=log)
    ear = listen.Listen(core)
    if events is not None:
        ear.ears = lambda http: Replayed(events, run)
    tasks = [asyncio.create_task(ear.run()), asyncio.create_task(plugin(core))]
    while ear.sound is None:
        await asyncio.sleep(0.05)
    await asyncio.sleep(3)  # сокет распознавания открыт, классификатор прогрет
    t0 = run["t0"] = time.monotonic()
    for n, b in enumerate(frames(pcm + bytes(TAIL_S * listen.RATE * 2))):
        ear.audio(b)
        await asyncio.sleep(max(0, t0 + (n + 1) * listen.BLOCK_S - time.monotonic()))
    await asyncio.sleep(CLAUDE_S)
    for t in tasks:
        t.cancel()
    run["closes"] = [{"t": x["t"], "text": x["text"]} for x in run["log"] if x["kind"] == "recognized"]
    run["sent"] = [{"id": r.id, "kind": r.kind, "state": r.state, "text": r.text} for r in core.reqs.values()]
    run["glued"] = sum(r.state != "superseded" for r in core.reqs.values())  # сообщений осталось в разговоре с Claude
    run.pop("t0")


async def cmd_run(label: str = "listen.py") -> None:
    events = json.loads(EVENTS.read_text())
    out = json.loads(OUT.read_text()) if OUT.exists() else {"runs": {}}
    listen.log, listen.trace, listen.emit, listen.sd.RawInputStream = note, note, lambda *_: None, NoMic
    tmp, count = Path(tempfile.mkdtemp(prefix="gv-bench-")), iter(range(10**6))
    listen.trace_open = lambda: tmp / f"{next(count)}.jsonl"  # звук прогонов — во временную папку, мимо ~/.claude/gv-trace
    data = out["runs"].setdefault(label, {"cfg": {"det": "listen.py"}, "data": {}})["data"]
    for ears in STTS:  # слух читает выбор распознавания при запуске: по одному за проход
        listen.EARS = ears
        runs = {n: {"log": []} for n in FILES}
        got = await asyncio.gather(*(hear(runs[n], load(n), events[f"{n}/{ears}"]) for n in FILES), return_exceptions=True)
        for n, r in zip(FILES, got):
            data[f"{n}/{ears}"] = runs[n]
            print(ears, n, "упал: " + repr(r) if isinstance(r, Exception) else f"реплик {len(runs[n]['closes'])}, сообщений {runs[n]['glued']}")
        OUT.write_text(json.dumps(out, ensure_ascii=False, indent=1))


async def cmd_live(name: str, silence: int) -> None:
    import journal
    journal.TRACES = Path(tempfile.mkdtemp(prefix="gv-live-"))  # запись запуска и журнал — настоящие, но не живого сервиса
    journal.LOG = journal.TRACES / "gv.log"
    listen.sd.RawInputStream = NoMic
    run = {"log": []}

    def log(kind: str, **data) -> None:
        note(kind, **data)
        journal.log(kind, **data)
    listen.log = log
    await hear(run, bytes(silence * listen.RATE * 2) + load(name), None, emit=journal.emit, log=log)
    rows = [json.loads(l) for l in journal._state["trace"].read_text().splitlines()]
    for x in run["log"]:
        if x["kind"] in ("recognized", "turn.check", "route", "route.early", "cut", "ask", "quick", "recognize.error", "route.error"):
            print(f"{x['t']:7.2f}  {x['kind']:<12} " + " ".join(f"{k}={str(v)[:150]}" for k, v in x.items() if k not in ("t", "kind")))
    print(f"\nраспознавание {listen.EARS}; запись запуска: {journal._state['trace']}; события: {sorted({r['ev'] for r in rows})}")
    if gates := [r["s"] for r in rows if r["ev"] == "gate.sent"]:
        print(f"в распознавание ушло {sum(gates):.1f} с звука из {silence + FILES[name][2] + TAIL_S} с, отрезков речи {len(gates)}")
    print("сообщений в разговоре с Claude:", run["glued"])
    for r in run["sent"]:
        print(f"  №{r['id']} {r['kind']} ({r['state']}): {r['text']}")


# ── report: правда и три числа ──────────────────────────────────────────
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


async def cmd_report() -> None:
    out = json.loads(OUT.read_text())
    truth = out["truth"] = {n: pauses(load(n)) for n in FILES}
    for n, ps in truth.items():
        print(f"{n}: пауз {len(ps)}, из них договорил {sum(p['done'] for p in ps)}")
    table = out["table"] = {}
    print(f"\n{'система':<22}{'слух':<9}{'отрезки':<9}{'концы: закрыто':<16}{'через, с (медиана / худшее)':<29}{'зря посреди мысли':<19}{'реплик → сообщений / ждали'}")
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
    cmd, args = (sys.argv[1] if len(sys.argv) > 1 else "report"), sys.argv[2:]
    silence = int(args.pop(args.index("--silence") + 1)) if "--silence" in args else 0
    args = [a for a in args if a != "--silence"]
    asyncio.run({"record": cmd_record, "report": cmd_report, "run": lambda: cmd_run(*args[:1]),
                 "live": lambda: cmd_live(args[0] if args else "минута", silence)}[cmd]())
