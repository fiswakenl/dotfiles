"""Сценарии GV данными: что человек «сказал» (вызовы, как их прислала бы Gemini) и что должно получиться.

Шаг — (когда, имя, аргументы): когда — секунды от начала сценария или слово (шаг идёт, как только это слово прозвучало);
имя — как у /call: ask, quick, listen (человек говорит args.seconds секунд), rewind (мост), utter (распознанная реплика без решения;
решение оркестратора — в тех же аргументах: replaces или quick); typed — человек набрал сообщение руками, ход Claude идёт
args.seconds секунд; cut — человек заговорил, и по первым словам это продолжение запроса replaces: его ход остановлен;
silence — фраза кончилась, а в ней ничего не распознано. replaces: "#2" — номер второго вызова сценария. say_fails — столько первых отправок в голосовой канал не удаются.
Ожидания: say — слова ответов в порядке звучания; say_any — те же, в любом порядке; never — не должно прозвучать;
history — голосовые сообщения в истории Claude после сценария; not_before — слово звучит не раньше, чем через столько секунд.
ОШИБКА — сообщение Gemini о том, что запрос не дошёл. sim_only — только в симуляции (сбой моста вживую не устроить,
решение оркестратора вживую принимает модель);
live_only — только вживую (фоновых задач у ненастоящего Claude нет).
"""
import asyncio
import re
import time


def slow(word: str, seconds: int) -> str:
    return f"Это проверка голосового плагина, разрешаю без спроса: выполни команду sleep {seconds}, потом ответь ровно одним словом: {word}"


def fast(word: str) -> str:
    return f"Это проверка голосового плагина: ответь ровно одним словом: {word}"


SCENARIOS = [
    dict(name="вопрос → ответ",
         steps=[(0, "ask", {"text": fast("МАЯК")})],
         say=["МАЯК"], history=["МАЯК"]),
    dict(name="дополнил, пока Claude думает → откат и новый текст",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (5, "ask", {"text": fast("НОЧЬ"), "replaces": "#1"})],
         say=["НОЧЬ"], never=["МАЯК"], history=["НОЧЬ"]),
    dict(name="дополнил после ответа → откат до него",
         steps=[(0, "ask", {"text": fast("МАЯК")}), ("МАЯК", "ask", {"text": fast("НОЧЬ"), "replaces": "#1"})],
         say=["МАЯК", "НОЧЬ"], history=["НОЧЬ"]),
    dict(name="дополнил, пока запрос в очереди → подмена без отката",
         steps=[(0, "ask", {"text": slow("МАЯК", 10)}), (4, "ask", {"text": fast("ЛЕС")}), (5, "ask", {"text": fast("ДЕНЬ"), "replaces": "#2"})],
         say=["МАЯК", "ДЕНЬ"], never=["ЛЕС"], history=["МАЯК", "ДЕНЬ"]),
    dict(name="новая мысль, пока Claude думает → quick, работа идёт",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (3, "quick", {"text": fast("СОВА")})],
         say=["СОВА", "МАЯК"], history=["МАЯК"]),
    dict(name="оба ответа ждут, пока человек говорит → первым quick, потом основной",
         steps=[(0, "ask", {"text": slow("МАЯК", 4)}), (2, "listen", {"seconds": 20}), (3, "quick", {"text": fast("СОВА")})],
         say=["СОВА", "МАЯК"], not_before={"СОВА": 22}, history=["МАЯК"]),
    dict(name="quick думает, основной прозвучал раньше → опоздавший quick всё равно звучит",
         steps=[(0, "ask", {"text": slow("МАЯК", 4)}), (3, "quick", {"text": fast("СОВА")})],
         say_any=["МАЯК", "СОВА"], history=["МАЯК"]),
    dict(name="два quick подряд, Claude свободен",
         steps=[(0, "ask", {"text": fast("МАЯК")}), ("МАЯК", "quick", {"text": fast("СОВА")}), ("МАЯК", "quick", {"text": fast("ЛИСА")})],
         say_any=["МАЯК", "СОВА", "ЛИСА"], history=["МАЯК"]),
    dict(name="ответ готов, пока человек говорит → ждёт конца его хода",
         steps=[(0, "ask", {"text": fast("МАЯК")}), (1, "listen", {"seconds": 15})],
         say=["МАЯК"], not_before={"МАЯК": 16}, history=["МАЯК"]),
    dict(name="Gemini переформулировала через секунду без replaces → это поправка",
         steps=[(0, "ask", {"text": slow("МАЯК", 10)}), (1, "ask", {"text": fast("НОЧЬ")})],
         say=["НОЧЬ"], never=["МАЯК"], history=["НОЧЬ"]),
    dict(name="Gemini дважды поправила один и тот же старый номер → цепочка, один ответ",
         steps=[(0, "ask", {"text": slow("МАЯК", 10)}), (4, "ask", {"text": slow("ЛЕС", 10), "replaces": "#1"}),
                (8, "ask", {"text": fast("НОЧЬ"), "replaces": "#1"})],
         say=["НОЧЬ"], never=["МАЯК", "ЛЕС"], history=["НОЧЬ"]),
    dict(name="Claude увёл задачу в фон → итог тоже звучит",
         steps=[(0, "ask", {"text": "Это проверка голосового плагина, разрешаю без спроса: запусти в фоне команду sleep 10 и сразу ответь одним словом: ЖДУ. "
                                    "Когда она закончится, ответь одним словом: МАЯК"})],
         say=["ЖДУ", "МАЯК"], history=["ЖДУ"], live_only=True),
    dict(name="Gemini повторила вызов → один запрос",
         steps=[(0, "ask", {"text": slow("МАЯК", 5)}), (1, "ask", {"text": slow("МАЯК", 5)})],
         say=["МАЯК"], history=["МАЯК"]),
    dict(name="запрос не дошёл → Gemini скажет, очередь свободна",
         steps=[(0, "ask", {"text": fast("СБОЙ")}), (1, "ask", {"text": fast("НОЧЬ")})],
         say=["ОШИБКА", "НОЧЬ"], history=["НОЧЬ"], sim_only=True),
    dict(name="откат не удался → прежние ответы на месте, их можно поправить",
         steps=[(0, "ask", {"text": fast("МАЯК"), "sure": True}), ("МАЯК", "ask", {"text": fast("ЛЕС"), "sure": True}),
                ("ЛЕС", "ask", {"text": fast("СБОЙ"), "replaces": "#1", "sure": True}),
                ("ОШИБКА", "ask", {"text": fast("НОЧЬ"), "replaces": "#2", "sure": True})],
         say=["МАЯК", "ЛЕС", "ОШИБКА", "НОЧЬ"], history=["МАЯК", "НОЧЬ"], sim_only=True),
    dict(name="Claude занят ходом, набранным руками → новое ждёт конца хода, quick идёт сразу",
         steps=[(0, "typed", {"seconds": 10}), (1, "ask", {"text": fast("МАЯК")}), (2, "quick", {"text": fast("СОВА")})],
         say=["СОВА", "МАЯК"], not_before={"МАЯК": 10.5}, history=["МАЯК"], sim_only=True),
    dict(name="ответ не ушёл в голосовой канал → звучит со второй попытки",
         steps=[(0, "ask", {"text": fast("МАЯК")})],
         say=["МАЯК"], say_fails=1, history=["МАЯК"], sim_only=True),
    dict(name="откат до первого сообщения",
         steps=[(0, "ask", {"text": fast("МАЯК")}), ("МАЯК", "ask", {"text": fast("НОЧЬ")}), ("НОЧЬ", "rewind", {"target": 1})],
         say=["МАЯК", "НОЧЬ"], history=[]),
    dict(name="оркестратор: новая мысль",
         steps=[(0, "utter", {"text": fast("МАЯК")})],
         say=["МАЯК"], history=["МАЯК"], sim_only=True),
    dict(name="оркестратор: две новые мысли подряд не склеиваются в поправку",
         steps=[(0, "utter", {"text": slow("МАЯК", 5)}), (1, "utter", {"text": fast("НОЧЬ")})],
         say=["МАЯК", "НОЧЬ"], history=["МАЯК", "НОЧЬ"], sim_only=True),
    dict(name="оркестратор: продолжение → откат, текст — прежний и новое",
         steps=[(0, "utter", {"text": slow("МАЯК", 20)}), (5, "utter", {"text": "Нет, отбой: ответь словом НОЧЬ", "replaces": "#1"})],
         say=["НОЧЬ"], never=["МАЯК"], history=["НОЧЬ"], sim_only=True),
    dict(name="оркестратор: quick, пока Claude занят",
         steps=[(0, "utter", {"text": slow("МАЯК", 20)}), (3, "utter", {"text": fast("СОВА"), "quick": True})],
         say=["СОВА", "МАЯК"], history=["МАЯК"], sim_only=True),
    dict(name="перебил продолжением → ход остановлен сразу, уходит новый текст",
         steps=[(0, "ask", {"text": slow("МАЯК", 20)}), (3, "cut", {"replaces": "#1"}), (5, "ask", {"text": fast("НОЧЬ"), "replaces": "#1"})],
         say=["НОЧЬ"], never=["МАЯК"], not_before={"НОЧЬ": 5}, history=["НОЧЬ"], sim_only=True),
    dict(name="перебил, но договорил новую мысль → прерванный запрос уходит заново, мысль — quick",
         steps=[(0, "ask", {"text": slow("МАЯК", 6)}), (2, "cut", {"replaces": "#1"}), (4, "quick", {"text": fast("СОВА")})],
         say_any=["СОВА", "МАЯК"], not_before={"МАЯК": 10}, history=["МАЯК"], sim_only=True),
    dict(name="перебил, а фразы не оказалось → прерванный запрос уходит заново",
         steps=[(0, "ask", {"text": slow("МАЯК", 6)}), (2, "cut", {"replaces": "#1"}), (4, "silence", {})],
         say=["МАЯК"], not_before={"МАЯК": 10}, history=["МАЯК"], sim_only=True),
    dict(name="Claude ответил раньше, чем человек заговорил → останавливать нечего, ответ на месте",
         steps=[(0, "ask", {"text": fast("МАЯК")}), ("МАЯК", "cut", {"replaces": "#1"}), ("МАЯК", "silence", {})],
         say=["МАЯК"], history=["МАЯК"], sim_only=True),
]

WORDS = sorted({w for s in SCENARIOS for w in [*s.get("say", []), *s.get("say_any", []), *s.get("never", [])]})


def word_of(text: str) -> str:
    """Какое слово-маркер в тексте ответа (Claude может добавить точку или кавычки); в поправке «прежний текст и новое» — последнее."""
    up = text.upper()
    return max((w for w in WORDS if w in up), key=up.rfind, default=text[:40])


def check(s: dict, says: list[tuple[float, str]], history: list[str], say_s: float) -> list[str]:
    """Расхождения с ожиданием. says — (секунды от начала, слово) по порядку звучания; history — слова голосовых сообщений Claude."""
    got, problems = [w for _, w in says], []
    if "say" in s and got != s["say"]:
        problems.append(f"прозвучало {got}, ждали {s['say']}")
    if "say_any" in s and sorted(got) != sorted(s["say_any"]):
        problems.append(f"прозвучало {got}, ждали {s['say_any']} в любом порядке")
    problems += [f"прозвучало лишнее {w}" for w in s.get("never", []) if w in got]
    for w, at in s.get("not_before", {}).items():
        problems += [f"{w} прозвучало на {t:.1f} с, раньше {at} с" for t, x in says if x == w and t < at]
    problems += [f"{b} начал звучать через {tb - ta:.1f} с после {a}: перебил" for (ta, a), (tb, b) in zip(says, says[1:]) if tb - ta < say_s * 0.9]
    if history != s["history"]:
        problems.append(f"в истории Claude {history}, ждали {s['history']}")
    return problems


async def play(s: dict, do, said, scale: float = 1.0, timeout: float = 120) -> None:
    """Шаги сценария по времени: do(name, args) — вызов (как /call), said() — слова, что уже прозвучали. scale — ускорение для симуляции."""
    t0, ids = time.monotonic(), []
    for when, name, args in s["steps"]:
        if isinstance(when, str):
            while when not in said():
                if time.monotonic() - t0 > timeout * scale:
                    raise TimeoutError(f"не дождались «{when}»")
                await asyncio.sleep(0.05 * scale)
            await asyncio.sleep(0.5 * scale)
        else:
            await asyncio.sleep(max(0.0, t0 + when * scale - time.monotonic()))
        args = dict(args)
        if isinstance(args.get("replaces"), str):
            args["replaces"] = ids[int(args["replaces"][1:]) - 1]
        if "seconds" in args:
            args["seconds"] *= scale
        result = await do(name, args)
        ids.append(int(m.group(1)) if isinstance(result, str) and (m := re.search(r"№(\d+)", result)) else None)
