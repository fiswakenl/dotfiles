"""Логика GV без звука и сети: запросы к Claude и очередь речи.

Один вход — call(name, args): его зовут голосовой канал, оркестратор плагина и ручка /call. Плагин забирает запросы next() и отдаёт ответы answer().
Реплику без решения (распознавание без модели) канал отдаёт utter(text): плагин заберёт её next() как route, решит оркестратором
и вернёт сюда же call() с sure — решение точное, догадки за Gemini (повтор, переформулировка) не нужны.
О ходах Claude плагин сообщает claude(working): пока идёт любой ход, и набранный руками, новый ask ждёт; о стёртом откатом — rewound().
Правила:
- ask — новая мысль; ask с replaces=N — дополнение или поправка N: в очереди — подмена, отправленный или отвеченный — откат до N и новый текст;
  поздние запросы заменены, только когда откат удался и плагин назвал, что стёрто;
- quick — быстрый вопрос по разговору: уходит сразу, даже пока Claude занят;
- человек заговорил, пока Claude работает над последней репликой, и по первым словам это её продолжение — interrupt(): ход Claude
  останавливается сразу, не дожидаясь конца фразы; фраза договорена — решение заново по всему тексту: продолжение — откат и
  склеенный текст, как у любой поправки; не продолжение (или ничего не распознано) — прерванный запрос уходит заново;
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
SAY_RETRY_S = 5  # ответ не ушёл в голосовой канал — снова не раньше чем через столько секунд
SAY_TRIES = 3  # столько неудачных попыток — и ответ больше не озвучиваем

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
    answer: str = ""  # что ответил Claude (для истории нового сокета Gemini)
    at: float = field(default_factory=time.monotonic)


class Core:
    def __init__(self, say, emit, log) -> None:
        self.say, self.emit, self.log = say, emit, log  # say(text) — отдать Gemini; emit(event) — журнал и страница
        self.ids = itertools.count(1)
        self.reqs: dict[int, Req] = {}
        self.busy: int | None = None  # ask, над которым сейчас работает Claude
        self.working = False  # идёт ход основного Claude — любой, и набранный руками; сообщает плагин
        self.speech: list[tuple[int, str]] = []  # (номер запроса, что сказать) — ждут, пока голосовой канал свободен
        self.utterances: list[str] = []  # реплики без решения — ждут оркестратора плагина
        self.cut: int | None = None  # ask, чей ход остановлен на первых словах новой фразы: ждёт решения по ней целиком
        self.cancels: list[int] = []  # остановки хода — ждут плагина
        self.listening = False  # человек говорит
        self.turn: float | None = None  # ждём конца хода Gemini (с этого времени)
        self.alive = time.monotonic()  # последний признак жизни от Gemini
        self.hold = 0.0  # до этого времени не озвучиваем: прошлая отправка в голосовой канал не удалась
        self.tries: dict[int, int] = {}  # номер запроса → неудачных отправок его ответа
        self.changed = asyncio.Event()  # появился запрос для плагина

    # ── голосовой канал, оркестратор и /call ────────────────────────────────────
    def call(self, name: str, args: dict) -> str:
        """args.heard — расшифровка речи после прошлого вызова (её даёт голосовой канал): точнее текста модели, берём её.
        args.sure — решение принял оркестратор: replaces ровно как сказано, без догадок."""
        heard = str(args.get("heard") or "").strip()
        text = heard or str(args.get("text") or "").strip()
        sure = bool(args.get("sure"))
        if not text:
            return "Пустой текст: ничего не передано."
        if name == "quick":
            self.resume()
            r = self._add("quick", text)
            return f"Вопрос №{r.id} передан."
        if not sure and (dup := self._duplicate(text)):
            self.log("ask.duplicate", id=dup.id)
            return f"Запрос №{dup.id} уже передан."
        old = self.reqs.get(int(args.get("replaces") or 0)) or (None if sure else self._just_asked())
        while old and old.by:  # Gemini поправила уже заменённый — значит, его свежую замену
            old = self.reqs[old.by]
        if old and (old.kind != "ask" or old.state == "failed"):
            old = None
        self.resume(keep=old)
        if old and heard:  # расшифровка — только новое; полный текст — прежний плюс новое
            text = f"{old.text} {heard}"
        # Поправка ещё не отправленного запроса наследует его откат: история Claude та же, что ждала его.
        rewind = None if not old else old.rewind if old.state == "queued" else old.id
        r = self._add("ask", text, rewind)
        if old:
            self._supersede(old, r.id)
        return f"Запрос №{r.id} передан" + (f" вместо №{old.id}." if old else ".")

    def utter(self, text: str) -> None:
        """Законченная реплика без решения: ask, поправка или quick — решит оркестратор плагина."""
        if text := text.strip():
            self.utterances.append(text)
            self.log("utter", text=text)
            self.changed.set()

    def working_on(self) -> Req | None:
        """Последняя переданная реплика, если Claude сейчас работает именно над ней: только её ход можно остановить на полуслове."""
        last = next((r for r in reversed(self.reqs.values()) if r.kind == "ask" and r.state in ("queued", "sent", "answered")), None)
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
        """Что отдать плагину: остановка хода — первой, затем реплики на решение, quick и поправки — сразу, новый ask — когда
        Claude свободен."""
        if self.cancels:
            return {"kind": "cancel", "id": self.cancels.pop(0)}
        if self.utterances:
            return {"kind": "route", "text": self.utterances.pop(0), "busy": self.claude_busy(),
                    "recent": [{"id": r.id, "kind": r.kind, "state": r.state, "text": r.text, "answer": r.answer[:300]} for r in self.recent(10)]}
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
            self.speech.append((id, f"[Запрос №{id} не выполнен: {error}] Скажи человеку одной фразой, что не получилось."))
        else:
            r.answer = f"{r.answer}\n{text}".strip()
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
        now = time.monotonic()
        stale = self.turn is not None and now - max(self.turn, self.alive) > TURN_STALE_S
        if not self.speech or self.listening or now < self.hold or (self.turn is not None and not stale):
            return
        id, text = self.speech.pop(max(range(len(self.speech)), key=lambda i: self.speech[i][0]))  # последний заданный вопрос
        self.turn = now
        self.emit({"say": id})
        asyncio.get_running_loop().create_task(self._say(id, text))

    async def _say(self, id: int, text: str) -> None:
        """say() вернул False — в голосовой канал не ушло: ответ возвращается в очередь и ждёт SAY_RETRY_S."""
        if await self.say(text) is not False:
            self.tries.pop(id, None)
            return
        self.turn = None
        self.tries[id] = self.tries.get(id, 0) + 1
        if self.tries[id] >= SAY_TRIES:
            self.log("say.dropped", id=id)
            return self.emit({"error": f"Ответ на №{id} не удалось озвучить", "id": id})
        if self.reqs[id].state != "superseded":
            self.speech.append((id, text))
            self.hold = time.monotonic() + SAY_RETRY_S
            asyncio.get_running_loop().call_later(SAY_RETRY_S, self.pump)

    def claude_busy(self) -> bool:
        """Claude занят: идёт ход или ask ждёт отправки либо ответа."""
        return bool(self.working) or any(r.kind == "ask" and r.state in ("queued", "sent") for r in self.reqs.values())

    def recent(self, n: int = 6) -> list[Req]:
        """Последние живые реплики — история для нового сокета Gemini: по ней она узнаёт, что можно поправить."""
        return [r for r in self.reqs.values() if r.state in ("queued", "sent", "answered")][-n:]

    def quiet(self) -> bool:
        """Голосовой канал свободен: никто не говорит, нечего сказать, Gemini не в ходе."""
        return not self.listening and not self.speech and self.turn is None

    def idle(self) -> bool:
        """Ничего не ждём: ни реплик, ни запросов, ни ответов, ни речи."""
        return not self.utterances and not self.cancels and not self.speech and self.turn is None and not any(r.state in ("queued", "sent") for r in self.reqs.values())

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
