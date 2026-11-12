# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Devtools GV: как три вопроса к Decisions делят записанную диктовку. Отдельно от живого GV: в окно Claude ничего не шлёт.

    uv run --script ~/.claude/gv/devtools/server.py      # :8791; страница — npm run dev в devtools/web (или собранная web/dist)

Источник — отрезок записи запуска (~/.claude/gv-trace/<запись>.jsonl + .wav), в трёх видах:
    punct  — текст Deepgram, как пришёл вживую (со знаками, если они тогда были);
    plain  — тот же текст без знаков и заглавных;
    socket — звук отрезка заново через поток Deepgram с настройками listen.py (платно; кнопкой, результат кешируется рядом с записью).
Прогон — та же логика, что в listen.py, три вопроса (QUESTIONS), у каждого свои варианты промпта со страницы:
    turn  — «мысль закончена?» на паузах Deepgram (speech_final): реплика закрыта по ответу «complete» или тишиной — её длина по уверенности ответа (listen.patience);
    early — «начало продолжения?» по началу следующей реплики (черновики и куски), будто Claude занят прошлой: да — его ход остановлен;
    route — «продолжение?» на каждом стыке реплик: дописать к прошлому сообщению или начать новое.
Ответы кешируются в памяти.

Ручки: GET /api/config — записи, варианты вопросов, форматы входа; GET /api/source?rec&from&to&kind — куски с паузами;
POST /api/run {rec, from, to, kind, prompts: {turn, route, early}, input: {route, early: {pack, words}}} — прогон; POST /api/replay {rec, from, to} — звук заново через Deepgram;
GET /api/audio — звук отрезка (WAV); GET/POST /api/labels — где на самом деле начинается новое сообщение (секунды от начала отрезка,
размечаешь на странице; лежит рядом с записью: <запись>.labels.json). Время кусков везде — секунды от начала отрезка.
Варианты вопросов — prompts.json рядом ({вопрос: [варианты]}): правятся на странице и сохраняются туда (POST /api/prompts).
"""
import asyncio
import datetime
import hashlib
import io
import json
import re
import sys
import wave
from pathlib import Path

import aiohttp
from aiohttp import web
from livekit import rtc
from livekit.agents import APIConnectOptions
from livekit.agents.llm import Choice, Probability

HERE = Path(__file__).resolve().parent
sys.path.insert(0, str(HERE.parent))
import listen  # noqa: E402
from journal import TRACES  # noqa: E402

PORT = 8791
PROMPTS = HERE / "prompts.json"
DIST = HERE / "web" / "dist"
ASK = APIConnectOptions(max_retry=2, timeout=15)
QUESTIONS = {"turn": listen.TURN["turn"].prompt, "route": listen.QUESTION["continues"].prompt, "early": listen.EARLY["continues"].prompt}
STATE = "Claude работает над ней"  # в devtools прошлая реплика всегда в работе


def words(sec: float) -> str:
    """Пауза словами: голое число модель читает хуже."""
    return (f"{sec} с — короткая, человек почти не останавливался" if sec < 1 else
            f"{sec} с — средняя, человек запнулся или подбирал слово" if sec < 3 else
            f"{sec} с — долгая, человек замолчал и потом заговорил снова")


def fields(prev: str, new: str, pause: float, early: bool, verbal: bool) -> dict:
    """Что видит «продолжение?»: prev — то, что уже ушло (с прошлыми склейками), new — новая реплика или её начало (early);
    verbal — пауза словами, а не числом. Содержание одно на все форматы — PACK отличаются только упаковкой."""
    sec = round(pause, 1)
    return {"до_паузы": prev, "состояние": STATE, **({"пауза": words(sec)} if verbal else {"пауза_сек": sec}),
            "начало_после_паузы" if early else "после_паузы": new}


def lines(d: dict) -> str:
    new = "Начало новой реплики" if "начало_после_паузы" in d else "Новая реплика"
    return (f"Предыдущая реплика ({d['состояние']}): {d['до_паузы']}\n"
            f"Пауза в речи перед новой репликой: {d.get('пауза') or str(d['пауза_сек']) + ' с'}\n"
            f"{new}: {d.get('после_паузы', d.get('начало_после_паузы'))}")


PACK = {"строками": lines, "json": lambda d: json.dumps(d, ensure_ascii=False),
        "xml": lambda d: "\n".join(f"<{k}>{v}</{k}>" for k, v in d.items())}


def pack(fmt: dict, prev: str, new: str, pause: float, early: bool = False) -> str:
    """fmt — {pack: имя из PACK, words: пауза словами}; у «продолжение?» и ранней проверки он свой."""
    return PACK[fmt["pack"]](fields(prev, new, pause, early, bool(fmt.get("words"))))


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


def audio_start(rec: str) -> float:
    return next(json.loads(l)["ts"] for l in (TRACES / f"{rec}.jsonl").open() if '"audio.start"' in l)


def segment(rec: str, a: str, b: str) -> bytes:
    """Звук отрезка из WAV записи (время в WAV — от audio.start)."""
    with wave.open(str(TRACES / f"{rec}.wav")) as w:
        w.setpos(int((at(rec, a) - audio_start(rec)) * listen.RATE))
        return w.readframes(int((at(rec, b) - at(rec, a)) * listen.RATE))


def live_chunks(rec: str, a: str, b: str) -> list[dict]:
    """Окончательные куски Deepgram из записи: текст, speech_final, время слов — в секундах от начала отрезка (время потока
    Deepgram отсчитывается от начала звука записи; поток переподключался — сдвинется); interims — черновики, шедшие перед куском."""
    t0, t1 = at(rec, a), at(rec, b)
    shift = t0 - audio_start(rec)
    out, drafts = [], {}
    for line in (TRACES / f"{rec}.jsonl").open():
        r = json.loads(line)
        if r["ev"] == "stt.interim" and t0 <= r["ts"] <= t1:
            drafts.setdefault(r.get("dg_start"), []).append(r["text"])
        elif r["ev"] == "stt.final" and t0 <= r["ts"] <= t1 and r.get("words"):
            out.append({"text": r["text"], "sf": bool(r.get("speech_final")), "interims": drafts.pop(r.get("dg_start"), []),
                        "start": round(r["words"][0][1] - shift, 2), "end": round(r["words"][-1][2] - shift, 2)})
    return gaps(out)


def replay_path(rec: str, a: str, b: str) -> Path:
    return TRACES / f"{rec}.replay-{a.replace(':', '')}-{b.replace(':', '')}.json"


async def replay(rec: str, a: str, b: str) -> list[dict]:
    """Звук отрезка заново через поток Deepgram, как в listen.py, в реальном времени (паузы Deepgram слышит по времени звука)."""
    pcm = segment(rec, a, b)
    chunks, drafts = [], {}
    async with aiohttp.ClientSession() as http:
        stt = listen.Deepgram(api_key=listen.env(".config/deepgram.env")["DEEPGRAM_API_KEY"], http_session=http, **listen.DEEPGRAM)
        stream = stt.stream()
        heard = stream._process_stream_event

        def tap(d: dict) -> None:
            alt = d.get("channel", {}).get("alternatives", [{}])[0]
            if d.get("type") == "Results" and alt.get("transcript") and not d.get("is_final"):
                drafts.setdefault(d.get("start"), []).append(alt["transcript"])
            elif d.get("type") == "Results" and alt.get("transcript"):
                chunks.append({"text": alt["transcript"], "sf": bool(d.get("speech_final")), "interims": drafts.pop(d.get("start"), []),
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
    return [dict(c, text=plain(c["text"]), interims=[plain(t) for t in c["interims"]]) for c in chunks] if kind == "plain" else chunks


async def once(key: str, coro):
    """Один и тот же вопрос — один запрос: ответ Decisions на тот же вход не меняется."""
    if key not in cache:
        cache[key] = await coro
    else:
        coro.close()
    return cache[key]


async def turn(prompt: str, text: str) -> dict:
    inp = f"user: {text}"

    async def ask():
        r = (await decisions.evaluate(input=inp, decisions={"turn": Choice(prompt, listen.TURN["turn"].options)}, conn_options=ASK)).results["turn"]
        return {"v": r.value, "p": round((r.probabilities or {}).get(r.value, 0), 2), "input": inp}
    return await once("turn:" + hashlib.sha1((prompt + "\0" + text).encode()).hexdigest(), ask())


async def route(prompt: str, inp: str) -> float | None:
    async def ask():
        r = await decisions.evaluate(input=inp, decisions={"c": Probability(prompt)}, conn_options=ASK)
        return round(r.results["c"].value, 2) if "c" in r.results else None
    return await once("route:" + hashlib.sha1((prompt + "\0" + inp).encode()).hexdigest(), ask())


def hearing(chunks: list[dict]) -> list[str]:
    """Что listen.py слышит по ходу реплики: каждый черновик и каждый окончательный кусок — вместе с уже сказанным."""
    out, parts = [], []
    for c in chunks:
        for draft in c.get("interims", []):
            out.append(" ".join([*parts, draft]))
        parts.append(c["text"])
        out.append(" ".join(parts))
    return [t for i, t in enumerate(out) if i == 0 or t != out[i - 1]]


async def run(chunks: list[dict], prompts: dict, fmt: dict) -> dict:
    """Логика listen.py по кускам. turns[i] — ответ «мысль закончена?» после куска i (wait — сколько тишины после него ждём); ends — куски, на которых реплика закрыта;
    early и steps — по стыку реплик (at — последний кусок прошлой): ранняя проверка по началу новой и «продолжение?» по всей."""
    turns: list = [None] * len(chunks)
    ends, parts = [], []
    for i, c in enumerate(chunks):
        parts.append(c["text"])
        wait = listen.PAUSE_MS / 1000  # сколько тишины закрывает реплику без «complete»: как listen.patience
        if c["sf"]:
            t = await turn(prompts["turn"], " ".join(parts))
            wait = 0 if t["v"] == "complete" else listen.patience(t["v"], t["p"])
            turns[i] = {**t, "wait": round(wait, 1)}
        closed = c["gap"] is None or not wait or c["gap"] >= wait
        if closed:
            ends.append(i)
            parts = []
    bounds = [(0 if k == 0 else ends[k - 1] + 1, e) for k, e in enumerate(ends)]
    phrases = [" ".join(c["text"] for c in chunks[a:e + 1]) for a, e in bounds]
    sent, steps, early = phrases[0], [], []
    for k in range(1, len(phrases)):
        pause = chunks[ends[k - 1]]["gap"]
        asks = []
        for text in hearing(chunks[bounds[k][0]:bounds[k][1] + 1]):
            inp = pack(fmt["early"], sent, text, pause, early=True)
            asks.append({"text": text, "p": await route(prompts["early"], inp), "input": inp})
            if asks[-1]["p"] is not None and asks[-1]["p"] >= listen.SURE:
                break  # ход Claude остановлен — дальше не спрашиваем
        early.append({"at": ends[k - 1], "pause": pause, "asks": asks})
        inp = pack(fmt["route"], sent, phrases[k], pause)
        p = await route(prompts["route"], inp)
        steps.append({"at": ends[k - 1], "pause": pause, "p": p, "input": inp})
        sent = sent + " " + phrases[k] if p is not None and p >= listen.SURE else phrases[k]
    return {"turns": turns, "ends": ends, "steps": steps, "early": early}


def prompts() -> dict:
    """Варианты промптов по вопросам; первый у каждого — как сейчас в listen.py."""
    saved = json.loads(PROMPTS.read_text())
    saved = saved if isinstance(saved, dict) else {"route": saved}
    out = {}
    for q, text in QUESTIONS.items():
        rest = [p for p in saved.get(q, []) if p["name"] != "как в gv"]
        out[q] = [{"name": "как в gv", "text": text}, *rest]
    return out


async def config(_req):
    recs = sorted(p.stem for p in TRACES.glob("*.jsonl"))
    return web.json_response({"recs": recs, "prompts": prompts(), "inputs": list(PACK), "sure": listen.SURE,
                              "pause_ms": listen.PAUSE_MS, "quick_ms": listen.QUICK_MS, "long_ms": listen.LONG_MS, "turn_options": listen.TURN["turn"].options})


async def get_source(req):
    q = req.query
    return web.json_response({"chunks": source(q["rec"], q["from"], q["to"], q["kind"])})


async def post_run(req):
    b = await req.json()
    chunks = source(b["rec"], b["from"], b["to"], b["kind"])
    if chunks is None:
        return web.json_response({"error": "нет повторного прогона звука — нажми «звук заново»"}, status=404)
    return web.json_response({"chunks": chunks, **await run(chunks, b["prompts"], b["input"])})


async def post_replay(req):
    b = await req.json()
    return web.json_response({"chunks": await replay(b["rec"], b["from"], b["to"])})


async def get_audio(req):
    q = req.query
    pcm = segment(q["rec"], q["from"], q["to"])
    head = io.BytesIO()
    with wave.open(head, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(listen.RATE)
        w.writeframes(pcm)
    return web.Response(body=head.getvalue(), content_type="audio/wav")


def labels_path(rec: str) -> Path:
    return TRACES / f"{rec}.labels.json"


async def get_labels(req):
    q = req.query
    p = labels_path(q["rec"])
    return web.json_response((json.loads(p.read_text()) if p.exists() else {}).get(f"{q['from']}-{q['to']}", []))


async def post_labels(req):
    b = await req.json()
    p = labels_path(b["rec"])
    all_ = json.loads(p.read_text()) if p.exists() else {}
    all_[f"{b['from']}-{b['to']}"] = b["cuts"]
    p.write_text(json.dumps(all_, ensure_ascii=False))
    return web.json_response({"ok": True})


async def post_prompts(req):
    PROMPTS.write_text(json.dumps(await req.json(), ensure_ascii=False, indent=2) + "\n")
    return web.json_response({"ok": True})


def main() -> None:
    app = web.Application()
    app.add_routes([web.get("/api/config", config), web.get("/api/source", get_source), web.post("/api/run", post_run),
                    web.post("/api/replay", post_replay), web.post("/api/prompts", post_prompts), web.get("/api/audio", get_audio),
                    web.get("/api/labels", get_labels), web.post("/api/labels", post_labels)])
    if DIST.exists():
        app.add_routes([web.get("/", lambda _r: web.FileResponse(DIST / "index.html")), web.static("/", DIST)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=lambda *_: print(f"devtools GV: http://127.0.0.1:{PORT}"))


if __name__ == "__main__":
    main()
