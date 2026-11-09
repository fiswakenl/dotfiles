# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Все сценарии на ядре (core.py) без Claude и Gemini: ненастоящий плагин отвечает словом-маркером, речь длится SAY_S,
оркестратор решает так, как сказано в шаге сценария.
Время ускорено в 1/SCALE раз; запуск: uv run --script tests/sim_test.py"""
import asyncio
import re
import sys
import time
import unittest
from pathlib import Path

sys.path[:0] = [str(Path(__file__).resolve().parents[1]), str(Path(__file__).resolve().parent)]
import core  # noqa: E402
from core import Core  # noqa: E402
from scenarios import SCENARIOS, check, play, word_of  # noqa: E402

SCALE = 0.02
SAY_S = 1.5  # столько звучит ответ
FAST_S = 1  # столько Claude отвечает без sleep
QUICK_S = 3  # столько думает quick
core.SAME_THOUGHT_S *= SCALE  # пороги ядра — в том же ускоренном времени
core.DUPLICATE_S *= SCALE


class Sim:
    """Ненастоящие плагин, Claude и голосовой канал вокруг настоящего ядра."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.core = Core(say=self.say, emit=self.emit, log=lambda *a, **k: None)
        self.history: list[tuple[int, str]] = []  # (номер запроса, текст) — голосовые сообщения Claude
        self.texts: dict[int, str] = {}
        self.says: list[tuple[float, str]] = []
        self.turn: asyncio.Task | None = None
        self.decisions: dict[str, dict] = {}  # текст реплики → решение ненастоящего оркестратора

    def emit(self, e: dict) -> None:
        if "ask" in e:
            self.texts[e["id"]] = e["ask"]
        if "say" in e:
            r = self.core.reqs[e["say"]]
            self.says.append(((time.monotonic() - self.t0) / SCALE, "ОШИБКА" if r.state == "failed" else word_of(r.text)))

    async def say(self, _text: str) -> None:
        await asyncio.sleep(SAY_S * SCALE)
        self.core.turn_end()

    async def listen(self, args: dict) -> str:
        self.core.hear(True)
        await asyncio.sleep(args["seconds"])
        self.core.hear(False)
        await asyncio.sleep(SAY_S * SCALE)
        self.core.turn_end()
        return "ok"

    async def plugin(self) -> None:
        while True:
            r = self.core.next()
            if not r:
                await asyncio.sleep(0.01 * SCALE)
                continue
            if r["kind"] == "route":
                d = self.decisions[r["text"]]
                self.core.call("quick" if d.get("quick") else "ask", {"heard": r["text"], "replaces": d.get("replaces") or 0, "sure": True})
                continue
            if r["kind"] == "quick":
                asyncio.create_task(self.reply(r["id"], QUICK_S))
                continue
            if "СБОЙ" in r["text"]:
                self.core.answer(r["id"], error="мост не ответил")
                continue
            if r["rewind"]:
                self.rewind(next(i for i, (rid, _) in enumerate(self.history) if rid == r["rewind"]) + 1)
            self.history.append((r["id"], r["text"]))
            m = re.search(r"sleep (\d+)", r["text"])
            self.turn = asyncio.create_task(self.reply(r["id"], FAST_S + (int(m.group(1)) if m else 0)))

    def rewind(self, target: int) -> dict:
        if self.turn:
            self.turn.cancel()  # откат прерывает текущий ход, как в настоящем мосте
        del self.history[target - 1:]
        return {"removed": True}

    async def reply(self, id: int, seconds: float) -> None:
        await asyncio.sleep(seconds * SCALE)
        self.core.answer(id, word_of(self.texts[id]))

    async def do(self, name: str, args: dict):
        if name == "listen":
            asyncio.create_task(self.listen(args))  # человек говорит в фоне, шаги идут дальше
            return "ok"
        if name == "rewind":
            return self.rewind(args["target"])
        if name == "utter":  # реплика уходит оркестратору; номер — когда он решит
            self.decisions[args["text"]] = args
            self.core.utter(args["text"])
            while not (r := next((r for r in self.core.reqs.values() if r.text.endswith(args["text"])), None)):
                await asyncio.sleep(0.01 * SCALE)
            return f"№{r.id}"
        return self.core.call(name, args)


class Scenarios(unittest.IsolatedAsyncioTestCase):
    async def test_all(self):
        for s in (s for s in SCENARIOS if not s.get("live_only")):
            with self.subTest(s["name"]):
                sim = Sim()
                task = asyncio.create_task(sim.plugin())
                await play(s, sim.do, lambda: [w for _, w in sim.says], scale=SCALE)
                deadline = time.monotonic() + 60 * SCALE
                while not sim.core.idle() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05 * SCALE)
                await asyncio.sleep(3 * SCALE)
                task.cancel()
                problems = check(s, sim.says, [word_of(t) for _, t in sim.history], SAY_S)
                self.assertEqual(problems, [], s["name"])


if __name__ == "__main__":
    unittest.main()
