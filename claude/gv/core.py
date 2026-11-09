"""Логика GV без звука и сети: запросы к Claude и очередь речи.

Один вход — call(name, args): его зовут и Gemini (toolCall), и ручка /call. Плагин забирает запросы next() и отдаёт ответы answer().
Правила:
- ask — новая мысль; ask с replaces=N — дополнение или поправка N: в очереди — подмена, отправленный или отвеченный — откат до N и новый текст;
- quick — быстрый вопрос по разговору: уходит сразу, даже пока Claude занят;
- ответы звучат по одному, когда Gemini договорила и человек молчит; из ждущих первым — ответ на последний заданный вопрос
  (он свежее в голове; quick спрашивают позже основного — он и звучит первым); опоздавший quick всё равно звучит: это другая мысль;
- ответ на заменённый запрос не звучит, даже если уже ждал в очереди речи;
- ещё один ответ на отвеченный запрос (итог фоновой задачи Claude) звучит тоже.
"""
import asyncio
import itertools
import time
from dataclasses import dataclass, field

DUPLICATE_S = 30  # тот же текст ask за это время — повтор вызова, а не новый запрос
SAME_THOUGHT_S = 3  # новый ask так скоро после прошлого — Gemini переформулировала ту же мысль: это поправка
TURN_STALE_S = 10  # ход Gemini без признаков жизни столько секунд считаем законченным

READ_ALOUD = "Прочитай это человеку вслух почти дословно, от первого лица, ничего не добавляя."
HEADER = {"ask": "Ответ Claude на запрос №{id}", "quick": "Быстрый ответ Claude на вопрос №{id}"}


@dataclass
class Req:
    id: int
    kind: str  # ask | quick
    text: str
    rewind: int | None = None  # номер запроса, до которого откатить историю Claude перед отправкой
    state: str = "queued"  # queued → sent → answered; superseded — заменён поправкой; failed — не дошёл
    by: int | None = None  # чем заменён
    at: float = field(default_factory=time.monotonic)


class Core:
    def __init__(self, say, emit, log) -> None:
        self.say, self.emit, self.log = say, emit, log  # say(text) — отдать Gemini; emit(event) — журнал и страница
        self.ids = itertools.count(1)
        self.reqs: dict[int, Req] = {}
        self.busy: int | None = None  # ask, над которым сейчас работает Claude
        self.speech: list[tuple[int, str]] = []  # (номер запроса, что сказать) — ждут, пока голосовой канал свободен
        self.listening = False  # человек говорит
        self.turn: float | None = None  # ждём конца хода Gemini (с этого времени)
        self.alive = time.monotonic()  # последний признак жизни от Gemini
        self.changed = asyncio.Event()  # появился запрос для плагина

    # ── Gemini и /call ──────────────────────────────────────────────────────────
    def call(self, name: str, args: dict) -> str:
        text = str(args.get("text") or "").strip()
        if not text:
            return "Пустой текст: ничего не передано."
        if name == "quick":
            r = self._add("quick", text)
            return f"Вопрос №{r.id} передан."
        if dup := self._duplicate(text):
            self.log("ask.duplicate", id=dup.id)
            return f"Запрос №{dup.id} уже передан."
        old = self.reqs.get(int(args.get("replaces") or 0)) or self._just_asked()
        while old and old.by:  # Gemini поправила уже заменённый — значит, его свежую замену
            old = self.reqs[old.by]
        if old and (old.kind != "ask" or old.state == "failed"):
            old = None
        # Поправка ещё не отправленного запроса наследует его откат: история Claude та же, что ждала его.
        rewind = None if not old else old.rewind if old.state == "queued" else old.id
        r = self._add("ask", text, rewind)
        if old:
            self._supersede(old, r.id)
        return f"Запрос №{r.id} передан" + (f" вместо №{old.id}." if old else ".")

    # ── плагин ──────────────────────────────────────────────────────────────────
    def next(self) -> dict | None:
        """Что отдать плагину: quick и поправки — сразу, новый ask — когда Claude свободен."""
        for r in self.reqs.values():
            if r.state == "queued" and (r.kind == "quick" or r.rewind or self.busy is None):
                r.state = "sent"
                if r.kind == "ask":
                    if r.rewind:  # откат стирает из истории всё после цели: те ходы больше не ответят
                        for o in self.reqs.values():
                            if o.kind == "ask" and o.id > r.rewind and o.id != r.id and o.state in ("sent", "answered"):
                                self._supersede(o, r.id)
                    self.busy = r.id
                self.emit({"sent": r.id})
                return {"id": r.id, "kind": r.kind, "text": r.text, "rewind": r.rewind}
        return None

    def answer(self, id: int, text: str = "", error: str = "") -> None:
        """Ответ плагина на запрос: текст (пустой — ход кончился без ответа) или ошибка доставки."""
        r = self.reqs.get(id)
        if not r or r.state not in ("sent", "answered"):
            self.log("answer.dropped", id=id, state=r.state if r else None)
            return self.emit({"dropped": id})
        if self.busy == id:
            self.busy = None
        r.state = "failed" if error else "answered"
        if error:
            self.emit({"error": f"Запрос №{id} не дошёл: {error}", "id": id})
            self.speech.append((id, f"[Запрос №{id} не дошёл до Claude: {error}] Скажи человеку одной фразой, что не получилось."))
        else:
            self.emit({"answer": text, "id": id})
            if text:
                self.speech.append((id, f"[{HEADER[r.kind].format(id=id)}] {text}\n\n{READ_ALOUD}"))
        self.changed.set()  # Claude освободился — следующий ask может идти
        self.pump()

    # ── голосовой канал ─────────────────────────────────────────────────────────
    def hear(self, talking: bool) -> None:
        """Человек начал или кончил говорить; кончил — Gemini ответит, ждём её хода."""
        self.listening = talking
        if not talking:
            self.turn = time.monotonic()
        self.pump()

    def heartbeat(self) -> None:
        self.alive = time.monotonic()

    def turn_end(self) -> None:
        self.turn = None
        self.pump()

    def pump(self) -> None:
        """Следующий ответ — Gemini, если голосовой канал свободен."""
        stale = self.turn is not None and time.monotonic() - max(self.turn, self.alive) > TURN_STALE_S
        if not self.speech or self.listening or (self.turn is not None and not stale):
            return
        id, text = self.speech.pop(max(range(len(self.speech)), key=lambda i: self.speech[i][0]))  # последний заданный вопрос
        self.turn = time.monotonic()
        self.emit({"say": id})
        asyncio.get_running_loop().create_task(self.say(text))

    def idle(self) -> bool:
        """Ничего не ждём: ни запросов, ни ответов, ни речи."""
        return not self.speech and self.turn is None and not any(r.state in ("queued", "sent") for r in self.reqs.values())

    # ── внутреннее ──────────────────────────────────────────────────────────────
    def _add(self, kind: str, text: str, rewind: int | None = None) -> Req:
        r = Req(next(self.ids), kind, text, rewind)
        self.reqs[r.id] = r
        self.log(kind, id=r.id, text=text, rewind=rewind)
        self.emit({"ask": text, "id": r.id, "kind": kind, "rewind": rewind})
        self.changed.set()
        return r

    def _supersede(self, r: Req, by: int) -> None:
        r.state, r.by = "superseded", by
        if self.busy == r.id:
            self.busy = None
        self.speech = [s for s in self.speech if s[0] != r.id]
        self.emit({"superseded": r.id})

    def _just_asked(self) -> Req | None:
        last = next((r for r in reversed(self.reqs.values()) if r.kind == "ask"), None)
        return last if last and time.monotonic() - last.at < SAME_THOUGHT_S else None

    def _duplicate(self, text: str) -> Req | None:
        key, now = " ".join(text.lower().split()), time.monotonic()
        return next((r for r in self.reqs.values() if r.kind == "ask" and r.state in ("queued", "sent") and now - r.at < DUPLICATE_S
                     and " ".join(r.text.lower().split()) == key), None)
