"""Куда отправить законченную реплику адаптера распознавания — решает классификатор (OpenAI Decisions), без переписывания текста.

Вопрос один, да или нет: продолжает ли новая реплика последнюю переданную (договаривает, уточняет, исправляет). Да — ask
с replaces=N: ядро склеит текст и откатит №N. Нет — новая мысль: пока Claude занят — quick, свободен — ask.
Выбор из нескольких прошлых реплик проверяли: вероятность размазывается по вариантам, и обрывок «транскрибация.» после
«…есть websocket» уходил новой мыслью; да-нет про последнюю на тех же фразах разделяет чётко (0,8–0,96 против 0,0).
Неуверенное «да» — новая мысль: ошибочная поправка стёрла бы работу Claude, лишнее сообщение — нет.

Тот же вопрос задаётся дважды. Рано — по первым словам новой фразы, пока Claude работает над последней репликой (early): «да»
останавливает его ход сразу, не дожидаясь конца фразы; слов мало — вопрос повторяется на каждом новом куске распознанного.
И в конце фразы — по всему тексту (decide): оно и решает, куда уйдёт реплика, раннее «да» его не предрешает.
Оба видят паузу в речи перед новой фразой: договаривают сразу, к новой мысли переходят после паузы.
"""
import time
from dataclasses import dataclass

from pipecat.classifiers.base_classifier import BaseClassifier, YesNoQuestion

from journal import log

SURE = 0.7  # вероятность продолжения ниже — не поправка, а новая мысль
STATE = {"queued": "ждёт отправки", "sent": "Claude работает над ней", "answered": "Claude ответил"}
CUT = "Claude начал отвечать, но человек заговорил снова, и ответ остановлен"
QUESTION = {"continues": YesNoQuestion(instructions=(
    "Человек диктует голосом, речь нарезана по паузам. Новая реплика — продолжение или поправка предыдущей "
    "(договаривает, уточняет, исправляет её), а не отдельная законченная мысль?"))}
EARLY = {"continues": YesNoQuestion(instructions=(
    "Человек диктует голосом, речь нарезана по паузам. Предыдущая реплика уже передана, а человек заговорил снова: слышно только "
    "начало новой фразы, она ещё не договорена. Это начало продолжения или поправки предыдущей реплики "
    "(договаривает, уточняет, исправляет её), а не начало отдельной мысли?"))}


@dataclass
class Decision:
    name: str  # ask | quick
    replaces: int  # 0 — новая мысль


def state(text: str, last, pause: float | None, cut: bool = False, new: str = "Новая реплика") -> str:
    """pause — сколько человек молчал между концом прошлой речи и началом этой; None — неизвестно."""
    gap = "" if pause is None else f"Пауза в речи перед новой репликой: {pause:.1f} с\n"
    return (f"Предыдущая реплика ({CUT if cut else STATE[last.state]}, передана {time.monotonic() - last.at:.1f} с назад): {last.text}\n"
            f"{gap}{new}: {text}")


async def early(classifier: BaseClassifier, core, text: str, pause: float | None):
    """Начало фразы, пока Claude работает над последней репликой: продолжение — вернуть её запрос, чтобы остановить ход."""
    if not (last := core.working_on()):
        return None
    try:
        t0 = time.monotonic()
        p = (await classifier.yes_no(state(text, last, pause, new="Начало новой реплики"), EARLY))["continues"].probability
        log("route.early", last=last.id, p=round(p, 2), s=round(time.monotonic() - t0, 2), text=text)
        return last if p >= SURE else None
    except Exception as ex:  # noqa: BLE001 — классификатор не ответил: ход Claude не трогаем
        log("route.error", error=f"{type(ex).__name__}: {ex}")
        return None


async def decide(classifier: BaseClassifier, core, text: str, pause: float | None = None) -> Decision:
    last = next((r for r in reversed(core.recent(10)) if r.kind == "ask" and r.state in STATE), None)
    cut = bool(last) and core.cut == last.id
    if last:
        try:
            t0 = time.monotonic()
            p = (await classifier.yes_no(state(text, last, pause, cut), QUESTION))["continues"].probability
            log("route", last=last.id, p=round(p, 2), s=round(time.monotonic() - t0, 2), pause=pause and round(pause, 1), cut=cut)
            if p >= SURE:
                return Decision("ask", last.id)
        except Exception as ex:  # noqa: BLE001 — классификатор не ответил: новая мысль ничего не стирает
            log("route.error", error=f"{type(ex).__name__}: {ex}")
    # Остановленный запрос ядро отправит заново: Claude снова занят им, как был до остановки.
    return Decision("quick" if core.claude_busy() or core.cut else "ask", 0)
