# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Devtools GV: как вопросы к Decisions делят записанную диктовку. Отдельно от живого GV: в окно Claude ничего не шлёт.

    uv run --script ~/.claude/gv/devtools/server.py      # :8791; страница — npm run dev в devtools/web (или собранная web/dist)

Источник — отрезок записи запуска (~/.claude/gv-trace/<запись>.jsonl + .wav), в трёх видах:
    punct  — текст Deepgram, как пришёл вживую (со знаками, если они тогда были);
    plain  — тот же текст без знаков и заглавных;
    socket — звук отрезка заново через поток Deepgram с настройками listen.py (платно; кнопкой, результат кешируется рядом с записью).
Прогон — та же логика, что в listen.py: «мысль закончена?» на паузах Deepgram (speech_final), закрытие реплики по ответу «complete»
или PAUSE_MS без текста; затем «продолжение?» на каждом стыке реплик — вопросом и форматом входа со страницы. Ответы кешируются в памяти.

Ручки: GET /api/config — записи, варианты вопросов, форматы входа; GET /api/source?rec&from&to&kind — куски с паузами;
POST /api/run {rec, from, to, kind, route, input} — прогон; POST /api/replay {rec, from, to} — звук заново через Deepgram.
Варианты вопросов — prompts.json рядом: правятся на странице и сохраняются туда (POST /api/prompts).
"""
import asyncio
import datetime
import hashlib
import json
import re
import sys
import wave
from pathlib import Path

import aiohttp
from aiohttp import web
from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.agents.llm import Probability

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import listen  # noqa: E402
from journal import TRACES  # noqa: E402

PORT = 8791
PROMPTS = HERE / "prompts.json"
DIST = HERE / "web" / "dist"
ASK = APIConnectOptions(max_retry=2, timeout=15)
INPUTS = {  # формат входа «продолжение?»: prev — то, что уже ушло (с прошлыми склейками), new — новая реплика, pause — секунды
    "строками": lambda prev, new, pause: (f"Предыдущая реплика (Claude работает над ней): {prev}\n"
                                          f"Пауза в речи перед новой репликой: {pause:.1f} с\nНовая реплика: {new}"),
    "json": lambda prev, new, pause: json.dumps({"до_паузы": prev, "пауза_мс": round(pause * 1000), "после_паузы": new}, ensure_ascii=False),
}
listen.trace = lambda *a, **k: None  # поток Deepgram — без записи в журнал живого GV
decisions = listen.openai.DecisionModel(model=listen.DECISIONS_MODEL, api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"])
cache: dict = {}


def plain(text: str) -> str:
    return re.sub(r"[^\w\s-]", "", text).lower()


def at(rec: str, hms: str) -> float:
    """HH:MM:SS дня записи → unix-время."""
    day = datetime.datetime.strptime(rec[:8], "%Y%m%d").date()
    return datetime.datetime.combine(day, datetime.time.fromisoformat(hms)).timestamp()


def gaps(chunks: list[dict]) -> list[dict]:
    for i, c in enumerate(chunks):
        c["gap"] = round(chunks[i + 1]["start"] - c["end"], 2) if i + 1 < len(chunks) else None
    return chunks


def live_chunks(rec: str, a: str, b: str) -> list[dict]:
    """Окончательные куски Deepgram из записи: текст, speech_final, время слов (секунды звука потока)."""
    t0, t1 = at(rec, a), at(rec, b)
    out = []
    for line in (TRACES / f"{rec}.jsonl").open():
        r = json.loads(line)
        if r["ev"] == "stt.final" and t0 <= r["ts"] <= t1 and r.get("words"):
            out.append({"text": r["text"], "sf": bool(r.get("speech_final")), "start": r["words"][0][1], "end": r["words"][-1][2]})
    return gaps(out)


def replay_path(rec: str, a: str, b: str) -> Path:
    return TRACES / f"{rec}.replay-{a.replace(':', '')}-{b.replace(':', '')}.json"


async def replay(rec: str, a: str, b: str) -> list[dict]:
    """Звук отрезка заново через поток Deepgram, как в listen.py, в реальном времени (паузы Deepgram слышит по времени звука)."""
    start = next(json.loads(l)["ts"] for l in (TRACES / f"{rec}.jsonl").open() if '"audio.start"' in l)
    with wave.open(str(TRACES / f"{rec}.wav")) as w:
        w.setpos(int((at(rec, a) - start) * listen.RATE))
        pcm = w.readframes(int((at(rec, b) - at(rec, a)) * listen.RATE))
    chunks = []
    async with aiohttp.ClientSession() as http:
        stt = listen.Deepgram(api_key=listen.env(".config/deepgram.env")["DEEPGRAM_API_KEY"], http_session=http, **listen.DEEPGRAM)
        stream = stt.stream()
        heard = stream._process_stream_event

        def tap(d: dict) -> None:
            alt = d.get("channel", {}).get("alternatives", [{}])[0]
            if d.get("type") == "Results" and d.get("is_final") and alt.get("transcript"):
                chunks.append({"text": alt["transcript"], "sf": bool(d.get("speech_final")),
                               "start": alt["words"][0]["start"], "end": alt["words"][-1]["end"]})
            heard(d)
        stream._process_stream_event = tap

        async def feed() -> None:
            step = int(listen.RATE * listen.BLOCK_S) * 2
            for i in range(0, len(pcm), step):
                stream.push_frame(rtc.AudioFrame(pcm[i:i + step], listen.RATE, 1, len(pcm[i:i + step]) // 2))
                await asyncio.sleep(listen.BLOCK_S)
            await asyncio.sleep(3)
            stream.end_input()
        feeding = asyncio.create_task(feed())
        async for _ in stream:
            pass
        await feeding
    gaps(chunks)
    replay_path(rec, a, b).write_text(json.dumps(chunks, ensure_ascii=False))
    return chunks


def source(rec: str, a: str, b: str, kind: str) -> list[dict] | None:
    if kind == "socket":
        p = replay_path(rec, a, b)
        return json.loads(p.read_text()) if p.exists() else None
    chunks = live_chunks(rec, a, b)
    return [dict(c, text=plain(c["text"])) for c in chunks] if kind == "plain" else chunks


async def once(key: str, coro):
    """Один и тот же вопрос — один запрос: ответ Decisions на тот же вход не меняется."""
    if key not in cache:
        cache[key] = await coro
    else:
        coro.close()
    return cache[key]


async def turn(text: str) -> tuple[str, float]:
    async def ask():
        r = (await decisions.evaluate(input=f"user: {text}", decisions=listen.TURN, conn_options=ASK)).results["turn"]
        return r.value, round((r.probabilities or {}).get(r.value, 0), 2)
    return await once("turn:" + text, ask())


async def route(prompt: str, inp: str) -> float | None:
    async def ask():
        r = await decisions.evaluate(input=inp, decisions={"c": Probability(prompt)}, conn_options=ASK)
        return round(r.results["c"].value, 2) if "c" in r.results else None
    return await once("route:" + hashlib.sha1((prompt + "\0" + inp).encode()).hexdigest(), ask())


async def run(chunks: list[dict], prompt: str, fmt: str) -> dict:
    """Логика listen.py по кускам: реплики по «мысль закончена?» и PAUSE_MS, затем «продолжение?» на стыках."""
    turns: list = [None] * len(chunks)
    ends, parts = [], []
    for i, c in enumerate(chunks):
        parts.append(c["text"])
        closed = c["gap"] is None or c["gap"] * 1000 >= listen.PAUSE_MS
        if c["sf"]:
            turns[i] = await turn(" ".join(parts))
            closed = closed or turns[i][0] == "complete"
        if closed:
            ends.append(i)
            parts = []
    phrases, start = [], 0
    for e in ends:
        phrases.append(" ".join(c["text"] for c in chunks[start:e + 1]))
        start = e + 1
    sent, steps = phrases[0], []
    for k in range(1, len(phrases)):
        pause = chunks[ends[k - 1]]["gap"]
        inp = INPUTS[fmt](sent, phrases[k], pause)
        p = await route(prompt, inp)
        steps.append({"at": ends[k - 1], "pause": pause, "p": p, "input": inp})
        sent = sent + " " + phrases[k] if p is not None and p >= listen.SURE else phrases[k]
    return {"turns": turns, "steps": steps}


async def config(_req):
    recs = sorted(p.stem for p in TRACES.glob("*.jsonl"))
    return web.json_response({"recs": recs, "prompts": json.loads(PROMPTS.read_text()), "inputs": list(INPUTS), "sure": listen.SURE,
                              "turn": listen.TURN["turn"].prompt, "turn_options": listen.TURN["turn"].options})


async def get_source(req):
    q = req.query
    return web.json_response({"chunks": source(q["rec"], q["from"], q["to"], q["kind"])})


async def post_run(req):
    b = await req.json()
    chunks = source(b["rec"], b["from"], b["to"], b["kind"])
    if chunks is None:
        return web.json_response({"error": "нет повторного прогона звука — нажми «звук заново»"}, status=404)
    return web.json_response({"chunks": chunks, **await run(chunks, b["route"], b["input"])})


async def post_replay(req):
    b = await req.json()
    return web.json_response({"chunks": await replay(b["rec"], b["from"], b["to"])})


async def post_prompts(req):
    PROMPTS.write_text(json.dumps(await req.json(), ensure_ascii=False, indent=2) + "\n")
    return web.json_response({"ok": True})


def main() -> None:
    app = web.Application()
    app.add_routes([web.get("/api/config", config), web.get("/api/source", get_source), web.post("/api/run", post_run),
                    web.post("/api/replay", post_replay), web.post("/api/prompts", post_prompts)])
    if DIST.exists():
        app.add_routes([web.get("/", lambda _r: web.FileResponse(DIST / "index.html")), web.static("/", DIST)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=lambda *_: print(f"devtools GV: http://127.0.0.1:{PORT}"))


if __name__ == "__main__":
    main()
