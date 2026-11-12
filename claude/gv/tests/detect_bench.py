# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Замер: как быстрее и вернее понять, что человек договорил. Записи из ~/.claude/gv-keep идут в OpenAI в реальном времени с разной
тишиной детектора; по каждой паузе собирается, что было известно в момент паузы и позже, и как ответили бы разные способы.
Правда — поведение человека на записи: после паузы молчал PAUSE_TRUE_S и дольше — договорил, иначе — продолжал.
Платно (OpenAI: распознавание и классификатор). Пишет ~/.claude/gv-keep/detect_bench.json.

    uv run --script ~/.claude/gv/tests/detect_bench.py"""
import asyncio, json, os, sys, time, wave
from pathlib import Path
import aiohttp
import numpy as np
HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HTTPS_PROXY", json.load(open(Path.home() / ".claude/settings.json"))["env"]["HTTPS_PROXY"])
import listen
from livekit import rtc
from livekit.agents import APIConnectOptions, inference
from livekit.agents.stt import SpeechEventType as T
from livekit.local_inference import EOT, EOT_MAX_SAMPLES, VAD, VAD_WINDOW_SAMPLES
from livekit.plugins import openai

KEEP = Path.home() / ".claude/gv-keep"
FILES = {"минута": ("20261112-220530", 60), "attention": ("20261112-215413", 120), "переобучение": ("20261112-214041", 100)}
SILENCES = (0.25, 0.4)
PAUSE_TRUE_S = 3.0
ASK = APIConnectOptions(max_retry=2, timeout=15)
decisions = openai.DecisionModel(model=listen.DECISIONS_MODEL, api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"])


def load(rec: str, limit: int) -> bytes:
    with wave.open(str(KEEP / f"{rec}.wav")) as w:
        return w.readframes(limit * listen.RATE)


def pauses(pcm: bytes) -> list[dict]:
    """Паузы по Silero без сети: конец речи, длина тишины после него, договорил ли человек (тишина PAUSE_TRUE_S и дольше)."""
    a = np.frombuffer(pcm, dtype=np.int16)
    vad, step = VAD(), VAD_WINDOW_SAMPLES / listen.RATE
    talk = [vad.predict(a[i:i + VAD_WINDOW_SAMPLES]) >= 0.5 for i in range(0, len(a) - VAD_WINDOW_SAMPLES, VAD_WINDOW_SAMPLES)]
    segs, start = [], None
    for i, t in enumerate(talk + [False]):
        if t and start is None:
            start = i
        elif not t and start is not None:
            if (i - start) * step >= 0.1:
                segs.append([start * step, i * step])
            start = None
    merged = []
    for s in segs:
        if merged and s[0] - merged[-1][1] < 0.2:
            merged[-1][1] = s[1]
        else:
            merged.append(s)
    total = len(a) / listen.RATE
    out = []
    for k, (s, e) in enumerate(merged):
        gap = (merged[k + 1][0] if k + 1 < len(merged) else total + PAUSE_TRUE_S) - e
        out.append({"end": round(e, 2), "gap": round(min(gap, 99), 2), "done": gap >= PAUSE_TRUE_S})
    return out


async def stream(http, name: str, pcm: bytes, silence: float) -> list[dict]:
    """Запись в OpenAI в реальном времени: события с временем по звуку."""
    stt = openai.STT(api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"],
                     vad=inference.VAD(model="silero", min_silence_duration=silence), **listen.OPENAI_STT)
    stt._session = http
    s = stt.stream()
    events, t0 = [], time.monotonic()

    async def feed():
        step = int(listen.RATE * listen.BLOCK_S) * 2
        for n, i in enumerate(range(0, len(pcm), step)):
            b = pcm[i:i + step]
            try:
                s.push_frame(rtc.AudioFrame(b, listen.RATE, 1, len(b) // 2))
            except RuntimeError:  # поток оборвался: дальше слать некуда
                return
            await asyncio.sleep(max(0, t0 + (n + 1) * listen.BLOCK_S - time.monotonic()))
        await asyncio.sleep(4)
        s.end_input()
        await asyncio.sleep(2)
        await s.aclose()
    task = asyncio.create_task(feed())
    try:
        async for ev in s:
            events.append({"t": round(time.monotonic() - t0, 2), "type": ev.type.name, "text": ev.alternatives[0].text if ev.alternatives else ""})
    except Exception as ex:  # noqa: BLE001
        print(name, silence, "поток оборвался:", type(ex).__name__, ex)
        task.cancel()
        return []
    await task
    return events


def segments(events: list[dict], silence: float) -> list[dict]:
    """По событиям — куски: когда детектор сказал «речь кончилась», какой был черновик в этот миг, когда пришёл готовый текст и
    когда черновик стал равен готовому."""
    out, waiting, draft, drafts = [], [], "", []
    texted = None
    for e in events:
        if e["type"] == "INTERIM_TRANSCRIPT":
            draft = e["text"]
            drafts.append((e["t"], listen.plain(e["text"]).strip()))
        elif e["type"] == "END_OF_SPEECH":
            waiting.append({"vad": e["t"], "end": round(e["t"] - silence, 2), "draft": listen.plain(draft).strip()})
        elif e["type"] == "FINAL_TRANSCRIPT":
            texted = e
            draft = ""
        elif e["type"] == "RECOGNITION_USAGE":
            if waiting:
                seg = waiting.pop(0)
                if texted:
                    final = listen.plain(texted["text"]).strip()
                    full = next((t for t, d in drafts if t >= seg["end"] - 1 and d == final), None)
                    out.append({**seg, "final": final, "final_at": texted["t"], "draft_full_at": full})
            texted, drafts = None, []
    return out


async def turn(text: str) -> dict:
    t0 = time.monotonic()
    r = (await decisions.evaluate(input=f"user: {text}", decisions=listen.TURN, conn_options=ASK)).results["turn"]
    return {"v": r.value, "p": round((r.probabilities or {}).get(r.value, 0), 2), "s": round(time.monotonic() - t0, 2)}


async def main() -> None:
    audio = {n: load(*f) for n, f in FILES.items()}
    truth = {n: pauses(p) for n, p in audio.items()}
    for n, ps in truth.items():
        print(n, "пауз", len(ps), "из них договорил", sum(p["done"] for p in ps))
    async with aiohttp.ClientSession(proxy=os.environ["HTTPS_PROXY"]) as http:
        jobs = [(n, s) for n in audio for s in SILENCES]
        runs = []
        for part in (jobs[::2], jobs[1::2]):  # по три потока разом: шесть через прокси рвались
            got = await asyncio.gather(*(stream(http, n, audio[n], s) for n, s in part))
            for (n, s), ev in zip(part, got):
                if not ev:  # оборвался — ещё одна попытка, одна
                    ev = await stream(http, n, audio[n], s)
                runs.append(((n, s), ev))
        runs = dict(runs)
    eot, rows, sem = EOT(), [], asyncio.Semaphore(6)
    await turn("привет")  # прогрев соединения: первый вопрос иначе медленный

    async def ask(text):
        async with sem:
            return await turn(text)
    for n in audio:
        a = np.frombuffer(audio[n], dtype=np.int16)
        for s in SILENCES:
            segs, parts = segments(runs[n, s], s), []
            for seg in segs:
                p = min(truth[n], key=lambda x: abs(x["end"] - seg["end"]))
                if abs(p["end"] - seg["end"]) > 0.6:
                    continue
                cut = int(seg["vad"] * listen.RATE)
                seg["eot"] = round(float(eot.predict(a[max(0, cut - EOT_MAX_SAMPLES):cut])), 2)
                seg.update(file=n, silence=s, gap=p["gap"], done=p["done"], true_end=p["end"],
                           ctx_final=" ".join([*parts, seg["final"]]), ctx_draft=" ".join([*parts, seg["draft"]]).strip())
                parts = [] if p["done"] else [*parts, seg["final"]]
                rows.append(seg)
    answers = await asyncio.gather(*(ask(r["ctx_final"]) for r in rows), *(ask(r["ctx_draft"] or "…") for r in rows))
    for i, r in enumerate(rows):
        r["turn_final"], r["turn_draft"] = answers[i], answers[len(rows) + i]
    (KEEP / "detect_bench.json").write_text(json.dumps({"truth": truth, "rows": rows}, ensure_ascii=False, indent=1))
    print("кусков", len(rows), "→", KEEP / "detect_bench.json")

asyncio.run(main())
