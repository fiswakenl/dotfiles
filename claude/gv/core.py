"""Логика GV без звука и сети: запросы к Claude.

Слух отдаёт законченную реплику call(ask | quick, {heard, replaces}); плагин забирает работу next() и отдаёт ответы answer().
О ходах Claude плагин сообщает claude(working): пока идёт любой ход, и набранный руками, новый ask ждёт; о стёртом откатом — rewound().
Правила:
- ask — новая мысль; ask с replaces=N — продолжение или поправка N: в очереди — подмена, отправленный или отвеченный — откат до N
  и текст N вместе с новым; поздние запросы заменены, только когда откат удался и плагин назвал, что стёрто;
- quick — быстрый вопрос по разговору: уходит сразу, даже пока Claude занят;
- человек заговорил, пока Claude работает над последней репликой, и по первым словам это её продолжение — interrupt(): ход Claude
  останавливается сразу, не дожидаясь конца фразы; фраза договорена — решение заново по всему тексту: продолжение — откат и
  склеенный текст, как у любой поправки; не продолжение (или ничего не распознано) — прерванный запрос уходит заново.
"""
import asyncio
import itertools
import time
from dataclasses import dataclass, field


@dataclass
class Req:
    id: int
    kind: str  # ask | quick
    text: str
    rewind: int | None = None  # номер запроса, до которого откатить историю Claude перед отправкой
    state: str = "queued"  # queued → sent → answered; superseded — заменён поправкой; failed — не дошёл
    by: int | None = None  # чем заменён
    answer: str = ""  # что ответил Claude
    at: float = field(default_factory=time.monotonic)


class Core:
    def __init__(self, emit, log) -> None:
        self.emit, self.log = emit, log  # emit(event) — страница журнала; log — журнал
        self.ids = itertools.count(1)
        self.reqs: dict[int, Req] = {}
        self.busy: int | None = None  # ask, над которым сейчас работает Claude
        self.working = False  # идёт ход основного Claude — любой, и набранный руками; сообщает плагин
        self.cut: int | None = None  # ask, чей ход остановлен на первых словах новой фразы: ждёт решения по ней целиком
        self.cancels: list[int] = []  # остановки хода — ждут плагина
        self.changed = asyncio.Event()  # появилась работа для плагина

    # ── слух и /call ─────────────────────────────────────────────────────────────
    def call(self, name: str, args: dict) -> str:
        """args.heard — распознанная реплика; args.replaces — какой ask она продолжает."""
        text = str(args.get("heard") or args.get("text") or "").strip()
        if not text:
            return "Пустой текст: ничего не передано."
        if name == "quick":
            self.resume()
            return f"Вопрос №{self._add('quick', text).id} передан."
        old = self.reqs.get(int(args.get("replaces") or 0))
        while old and old.by:  # продолжение уже заменённого — значит, его свежей замены
            old = self.reqs[old.by]
        if old and (old.kind != "ask" or old.state == "failed"):
            old = None
        self.resume(keep=old)
        if old:
            text = f"{old.text} {text}"
        # Поправка ещё не отправленного запроса наследует его откат: история Claude та же, что ждала его.
        r = self._add("ask", text, None if not old else old.rewind if old.state == "queued" else old.id)
        if old:
            self._supersede(old, r.id)
        return f"Запрос №{r.id} передан" + (f" вместо №{old.id}." if old else ".")

    def working_on(self) -> Req | None:
        """Последняя переданная реплика, если Claude сейчас работает именно над ней: только её ход можно остановить на полуслове."""
        last = self.last_ask()
        return last if last and last.state == "sent" and self.busy == last.id else None

    def interrupt(self, id: int) -> bool:
        """Остановить ход Claude над запросом id: человек заговорил снова, и это его продолжение. Запрос ждёт решения по всей фразе."""
        r = self.working_on()
        if not r or r.id != id:
            return False  # Claude успел ответить или работает уже над другим
        self.cut = id
        self.cancels.append(id)
        self.log("cut", id=id)
        self.emit({"cut": id})
        self.changed.set()
        return True

    def resume(self, keep: Req | None = None) -> None:
        """Фраза, остановившая ход, договорена. Она не продолжила прерванный запрос (keep — кого она поправляет) — он уходит заново
        тем же текстом; успел ответить до остановки — возвращать нечего."""
        r, self.cut = self.reqs.get(self.cut or 0), None
        if r and r is not keep and r.state in ("sent", "answered") and not r.answer:
            self._supersede(r, self._add("ask", r.text, r.id).id)

    # ── плагин ──────────────────────────────────────────────────────────────────
    def next(self) -> dict | None:
        """Что отдать плагину: остановка хода — первой, quick и поправки — сразу, новый ask — когда Claude свободен."""
        if self.cancels:
            return {"kind": "cancel", "id": (id := self.cancels.pop(0)), "text": self.reqs[id].text}
        for r in self.reqs.values():
            if r.state == "queued" and (r.kind == "quick" or r.rewind or (self.busy is None and not self.working)):
                r.state = "sent"
                if r.kind == "ask":
                    self.busy = r.id
                self.emit({"sent": r.id})
                return {"id": r.id, "kind": r.kind, "text": r.text, "rewind": r.rewind}
        return None

    def claude(self, working: bool) -> None:
        """Начался или кончился ход основного Claude — любой: пока он идёт, новое сообщение в окно не отправляем."""
        self.working = working
        self.changed.set()

    def rewound(self, id: int, removed: list[int]) -> None:
        """Откат ради запроса id удался и стёр из истории Claude сообщения этих запросов: их ходы больше не ответят."""
        for o in (self.reqs.get(i) for i in removed):
            if o and o.kind == "ask" and o.id != id and o.state in ("sent", "answered"):
                self._supersede(o, id)

    def answer(self, id: int, text: str = "", error: str = "") -> None:
        """Ответ плагина на запрос: текст (пустой — ход кончился без ответа) или ошибка — не дошёл или ход Claude упал."""
        r = self.reqs.get(id)
        if not r or r.state not in ("sent", "answered"):
            self.log("answer.dropped", id=id, state=r.state if r else None)
            return self.emit({"dropped": id})
        if self.busy == id:
            self.busy = None
        r.state = "failed" if error else "answered"
        if error:
            self.emit({"error": f"Запрос №{id}: {error}", "id": id})
        else:
            r.answer = f"{r.answer}\n{text}".strip()
            self.emit({"answer": text, "id": id})
        self.changed.set()  # Claude освободился — следующий ask может идти

    def claude_busy(self) -> bool:
        """Claude занят: идёт ход или ask ждёт отправки либо ответа."""
        return self.working or any(r.kind == "ask" and r.state in ("queued", "sent") for r in self.reqs.values())

    def last_ask(self) -> Req | None:
        """Последний живой ask — его может продолжить следующая реплика."""
        return next((r for r in reversed(self.reqs.values()) if r.kind == "ask" and r.state in ("queued", "sent", "answered")), None)

    def idle(self) -> bool:
        """Ничего не ждём: ни остановок, ни запросов, ни ответов."""
        return not self.cancels and not any(r.state in ("queued", "sent") for r in self.reqs.values())

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
        self.emit({"superseded": r.id})
