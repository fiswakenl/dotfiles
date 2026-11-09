"""Куда отправить законченную реплику адаптера распознавания — решает классификатор (OpenAI Decisions), без переписывания текста.

Вопрос один, да или нет: продолжает ли новая реплика последнюю переданную (договаривает, уточняет, исправляет). Да — ask
с replaces=N: ядро склеит текст и откатит №N. Нет — новая мысль: пока Claude занят — quick, свободен — ask.
Выбор из нескольких прошлых реплик проверяли: вероятность размазывается по вариантам, и обрывок «транскрибация.» после
«…есть websocket» уходил новой мыслью; да-нет про последнюю на тех же фразах разделяет чётко (0,8–0,96 против 0,0).
Неуверенное «да» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.
"""
import time
from dataclasses import dataclass

from pipecat.classifiers.base_classifier import BaseClassifier, YesNoQuestion

from journal import log

SURE = 0.7  # вероятность продолжения ниже — не поправка, а новая мысль
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}
QUESTION = {"continues": YesNoQuestion(instructions=(
    "Человек диктует голосом, речь нарезана по паузам. Новая реплика — продолжение или поправка предыдущей "
    "(договаривает, уточняет, исправляет её), а не отдельная законченная мысль?"))}


@dataclass
class Decision:
    name: str  # ask | quick
    replaces: int  # 0 — новая мысль


def state(text: str, last, now: float) -> str:
    return f"Предыдущая реплика ({STATE[last.state]}, {now - last.at:.1f} с назад): {last.text}\nНовая реплика: {text}"


async def decide(classifier: BaseClassifier, core, text: str) -> Decision:
    last = next((r for r in reversed(core.recent(10)) if r.kind == "ask" and r.state in STATE), None)
    if last:
        try:
            t0 = time.monotonic()
            p = (await classifier.yes_no(state(text, last, time.monotonic()), QUESTION))["continues"].probability
            log("route", last=last.id, p=round(p, 2), s=round(time.monotonic() - t0, 2))
            if p >= SURE:
                return Decision("ask", last.id)
        except Exception as ex:  # noqa: BLE001 — классификатор не ответил: новая мысль ничего не стирает
            log("route.error", error=f"{type(ex).__name__}: {ex}")
    return Decision("quick" if core.claude_busy() else "ask", 0)
