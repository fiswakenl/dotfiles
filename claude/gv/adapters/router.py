"""Куда отправить законченную реплику адаптера распознавания — решает классификатор (OpenAI Decisions), без переписывания текста.

Классификатору — переданные реплики с состоянием и давностью и новая реплика; он выбирает: новая мысль или продолжение №N.
Новая мысль, пока Claude занят, — quick; свободен — ask. Продолжение — ask с replaces=N: ядро склеит текст и откатит №N.
Маловероятное «продолжение» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.
"""
import time
from dataclasses import dataclass

from pipecat.classifiers.base_classifier import BaseClassifier, ChoiceQuestion

from journal import log

RECENT = 3  # столько последних запросов можно продолжить
SURE = 0.55  # вероятность продолжения ниже — не поправка, а новая мысль
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}

INSTRUCTIONS = ("Человек говорит с Claude голосом; речь распознана в текст и нарезана по паузам. "
                "Новая реплика — продолжение переданной, если без неё непонятна, обрывает или поправляет её; "
                "законченная мысль, понятная сама по себе, — новая, даже на ту же тему. "
                "Короткая реплика через секунду-две после переданной — скорее её продолжение: человек договаривал после паузы.")


@dataclass
class Decision:
    name: str  # ask | quick
    replaces: int  # 0 — новая мысль


def question(text: str, recent: list, now: float) -> tuple[str, dict[str, ChoiceQuestion]]:
    """Что спросить: переданные реплики и новая — текстом, варианты — новая мысль или продолжение каждой из переданных."""
    lines = [f"№{r.id} ({STATE.get(r.state, r.state)}, {now - r.at:.1f} с назад): {r.text}" for r in recent]
    state = "\n".join(["Переданные реплики:", *lines, "", f"Новая реплика: {text}"])
    options = {"new": "новая мысль", **{f"continue_{r.id}": f"продолжение или поправка №{r.id}" for r in recent}}
    return state, {"route": ChoiceQuestion(instructions=INSTRUCTIONS, options=options)}


async def decide(classifier: BaseClassifier, core, text: str) -> Decision:
    recent = [r for r in core.recent(10) if r.kind == "ask" and r.state in STATE][-RECENT:]
    replaces = 0
    if recent:
        state, questions = question(text, recent, time.monotonic())
        try:
            t0 = time.monotonic()
            r = (await classifier.choice(state, questions))["route"]
            p = r.probabilities.get(r.choice, 0.0)  # confidence у Decisions — отрыв от второго варианта, не вероятность
            log("route", choice=r.choice, p=round(p, 2), s=round(time.monotonic() - t0, 2))
            if r.choice.startswith("continue_") and p >= SURE:
                replaces = int(r.choice.removeprefix("continue_"))
        except Exception as ex:  # noqa: BLE001 — классификатор не ответил: новая мысль ничего не стирает
            log("route.error", error=f"{type(ex).__name__}: {ex}")
    if replaces:
        return Decision("ask", replaces)
    return Decision("quick" if core.claude_busy() else "ask", 0)
