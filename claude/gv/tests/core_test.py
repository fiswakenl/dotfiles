# /// script
# requires-python = ">=3.10"
# dependencies = []
# ///
"""Сценарии на ядре (core.py) без Claude, звука и сети: ненастоящий плагин держит историю голосовых сообщений Claude и отвечает
словом-маркером из текста запроса. Время ускорено в 1/SCALE раз. Запуск: uv run --script tests/core_test.py

Шаг — (когда, имя, аргументы): когда — секунды от начала или слово (шаг идёт, как только пришёл ответ с этим словом);
имя — ask, quick (реплика, как её отдаёт слух; replaces: "#2" — номер второго вызова), rewind (мост: откат до сообщения),
typed (человек набрал сообщение руками: ход Claude идёт seconds секунд), cut (по первым словам это продолжение replaces: стоп хода),
silence (фраза кончилась, а в ней ничего не распознано).
Ожидания: answers — слова пришедших ответов в любом порядке; history — голосовые сообщения в истории Claude после сценария;
not_before — ответ с этим словом не раньше, чем через столько секунд. В тексте «СБОЙ» — мост его не доставит, «sleep N» — Claude
думает N секунд.
"""
import asyncio
import re
import sys
import time
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
from core import Core  # noqa: E402

SCALE = 0.02
FAST_S = 1  # столько Claude отвечает без sleep
QUICK_S = 3  # столько думает quick


def slow(word: str, seconds: int) -> str:
    return f"выполни sleep {seconds}, потом ответь словом {word}"


SCENARIOS = [
    dict(name="вопрос → ответ",
         steps=[(0, "ask", {"text": "МАЯК"})], answers=["МАЯК"], history=["МАЯК"]),
    dict(name="продолжил, пока Claude думает → откат и склейка, ответ один",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (5, "ask", {"text": "НОЧЬ", "replaces": "#1"})],
         answers=["НОЧЬ"], history=["НОЧЬ"]),
    dict(name="продолжил после ответа → откат до него",
         steps=[(0, "ask", {"text": "МАЯК"}), ("МАЯК", "ask", {"text": "НОЧЬ", "replaces": "#1"})],
         answers=["МАЯК", "НОЧЬ"], history=["НОЧЬ"]),
    dict(name="продолжил, пока запрос в очереди → подмена без отката",
         steps=[(0, "ask", {"text": slow("МАЯК", 10)}), (4, "ask", {"text": "ЛЕС"}), (5, "ask", {"text": "ДЕНЬ", "replaces": "#2"})],
         answers=["МАЯК", "ДЕНЬ"], history=["МАЯК", "ДЕНЬ"]),
    dict(name="поправил дважды один и тот же номер → цепочка, один ответ",
         steps=[(0, "ask", {"text": slow("МАЯК", 10)}), (4, "ask", {"text": slow("ЛЕС", 10), "replaces": "#1"}),
                (8, "ask", {"text": "НОЧЬ", "replaces": "#1"})],
         answers=["НОЧЬ"], history=["НОЧЬ"]),
    dict(name="новая мысль, пока Claude думает → quick, работа идёт",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (3, "quick", {"text": "СОВА"})],
         answers=["СОВА", "МАЯК"], history=["МАЯК"]),
    dict(name="запрос не дошёл → очередь свободна",
         steps=[(0, "ask", {"text": "СБОЙ"}), (1, "ask", {"text": "НОЧЬ"})], answers=["НОЧЬ"], history=["НОЧЬ"]),
    dict(name="откат не удался → прежние ответы на месте, их можно поправить",
         steps=[(0, "ask", {"text": "МАЯК"}), ("МАЯК", "ask", {"text": "ЛЕС"}), ("ЛЕС", "ask", {"text": "СБОЙ", "replaces": "#1"}),
                (8, "ask", {"text": "НОЧЬ", "replaces": "#2"})],
         answers=["МАЯК", "ЛЕС", "НОЧЬ"], history=["МАЯК", "НОЧЬ"]),
    dict(name="Claude занят ходом, набранным руками → новое ждёт конца хода, quick идёт сразу",
         steps=[(0, "typed", {"seconds": 10}), (1, "ask", {"text": "МАЯК"}), (2, "quick", {"text": "СОВА"})],
         answers=["СОВА", "МАЯК"], not_before={"МАЯК": 10.5}, history=["МАЯК"]),
    dict(name="откат до первого сообщения",
         steps=[(0, "ask", {"text": "МАЯК"}), ("МАЯК", "ask", {"text": "НОЧЬ"}), ("НОЧЬ", "rewind", {"target": 1})],
         answers=["МАЯК", "НОЧЬ"], history=[]),
    dict(name="перебил продолжением → ход остановлен сразу, уходит склеенный текст",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (3, "cut", {"replaces": "#1"}), (5, "ask", {"text": "НОЧЬ", "replaces": "#1"})],
         answers=["НОЧЬ"], not_before={"НОЧЬ": 5}, history=["НОЧЬ"]),
    dict(name="перебил, но договорил новую мысль → прерванный запрос уходит заново, мысль — quick",
         steps=[(0, "ask", {"text": slow("МАЯК", 6)}), (2, "cut", {"replaces": "#1"}), (4, "quick", {"text": "СОВА"})],
         answers=["СОВА", "МАЯК"], not_before={"МАЯК": 10}, history=["МАЯК"]),
    dict(name="перебил, а фразы не оказалось → прерванный запрос уходит заново",
         steps=[(0, "ask", {"text": slow("МАЯК", 6)}), (2, "cut", {"replaces": "#1"}), (4, "silence", {})],
         answers=["МАЯК"], not_before={"МАЯК": 10}, history=["МАЯК"]),
    dict(name="Claude ответил раньше, чем человек заговорил → останавливать нечего, ответ на месте",
         steps=[(0, "ask", {"text": "МАЯК"}), ("МАЯК", "cut", {"replaces": "#1"}), ("МАЯК", "silence", {})],
         answers=["МАЯК"], history=["МАЯК"]),
]
WORDS = sorted({w for s in SCENARIOS for w in [*s.get("answers", []), *s.get("history", [])]})


def word_of(text: str) -> str:
    """Слово-маркер в тексте; в склеенном тексте «прежний и новое» — последнее."""
    return max((w for w in WORDS if w in text), key=text.rfind, default=text[:40])


class Sim:
    """Ненастоящий плагин и Claude вокруг настоящего ядра."""

    def __init__(self) -> None:
        self.t0 = time.monotonic()
        self.answers: list[tuple[float, str]] = []  # (секунды от начала, слово) — ответы, которые ядро приняло
        self.history: list[tuple[int, str]] = []  # (номер запроса, текст) — голосовые сообщения Claude
        self.texts: dict[int, str] = {}
        self.turn: asyncio.Task | None = None
        self.core = Core(emit=self.emit, log=lambda *a, **k: None)

    def emit(self, e: dict) -> None:
        if "ask" in e:
            self.texts[e["id"]] = e["ask"]
        if e.get("answer"):
            self.answers.append(((time.monotonic() - self.t0) / SCALE, word_of(e["answer"])))

    async def plugin(self) -> None:
        while True:
            r = self.core.next()
            if not r:
                await asyncio.sleep(0.01 * SCALE)
            elif r["kind"] == "cancel":  # как плагин: ход остановлен, Claude свободен — ответ пустой
                if self.turn:
                    self.turn.cancel()
                self.core.answer(r["id"])
            elif r["kind"] == "quick":
                asyncio.create_task(self.reply(r["id"], QUICK_S))
            elif "СБОЙ" in r["text"]:
                self.core.answer(r["id"], error="мост не ответил")
            else:
                if r["rewind"]:  # как плагин: откат, затем ядру — чьи сообщения стёрты
                    at = next(i for i, (rid, _) in enumerate(self.history) if rid == r["rewind"])
                    removed = [rid for rid, _ in self.history[at:]]
                    self.rewind(at + 1)
                    self.core.rewound(r["id"], removed)
                self.history.append((r["id"], r["text"]))
                m = re.search(r"sleep (\d+), потом ответь словом \w+$", r["text"])  # думает, если «sleep» в последнем куске
                self.turn = asyncio.create_task(self.reply(r["id"], FAST_S + (int(m.group(1)) if m else 0)))

    def rewind(self, target: int) -> None:
        if self.turn:
            self.turn.cancel()  # откат прерывает текущий ход, как в настоящем мосте
        del self.history[target - 1:]

    async def reply(self, id: int, seconds: float) -> None:
        await asyncio.sleep(seconds * SCALE)
        self.core.answer(id, word_of(self.texts[id]))

    async def do(self, name: str, args: dict, ids: list) -> int | None:
        if "replaces" in args:
            args = {**args, "replaces": ids[int(args["replaces"][1:]) - 1]}
        if name == "rewind":
            return self.rewind(args["target"])
        if name == "typed":
            self.core.claude(True)
            asyncio.get_running_loop().call_later(args["seconds"] * SCALE, self.core.claude, False)
        elif name == "cut":
            self.core.interrupt(args["replaces"])
        elif name == "silence":
            self.core.resume()
        else:
            return int(re.search(r"№(\d+)", self.core.call(name, {"heard": args["text"], "replaces": args.get("replaces")})).group(1))
        return None

    async def play(self, steps: list) -> None:
        ids: list = []
        for when, name, args in steps:
            if isinstance(when, str):
                while when not in [w for _, w in self.answers]:
                    await asyncio.sleep(0.05 * SCALE)
                await asyncio.sleep(0.5 * SCALE)
            else:
                await asyncio.sleep(max(0.0, self.t0 + when * SCALE - time.monotonic()))
            ids.append(await self.do(name, dict(args), ids))


class Scenarios(unittest.IsolatedAsyncioTestCase):
    async def test_all(self):
        for s in SCENARIOS:
            with self.subTest(s["name"]):
                sim = Sim()
                task = asyncio.create_task(sim.plugin())
                await asyncio.wait_for(sim.play(s["steps"]), 60 * SCALE)
                deadline = time.monotonic() + 60 * SCALE
                while not sim.core.idle() and time.monotonic() < deadline:
                    await asyncio.sleep(0.05 * SCALE)
                await asyncio.sleep(3 * SCALE)
                task.cancel()
                self.assertEqual(sorted(w for _, w in sim.answers), sorted(s["answers"]), "ответы")
                self.assertEqual([word_of(t) for _, t in sim.history], s["history"], "история Claude")
                for w, at in s.get("not_before", {}).items():
                    self.assertTrue(all(t >= at for t, x in sim.answers if x == w), f"{w} раньше {at} с: {sim.answers}")


if __name__ == "__main__":
    unittest.main()
