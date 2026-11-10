"""Журналы GV: ~/.claude/gv.log (что произошло), запись запуска для разбора (~/.claude/gv-trace/) и поток событий для страницы."""
import asyncio
import json
import os
import time
from pathlib import Path

LOG = Path.home() / ".claude" / "gv.log"
LOG_KEEP = 2000
HISTORY_KEEP = 1000
TRACES = Path.home() / ".claude" / "gv-trace"  # подробная запись запуска для разбора: события с точным временем и звук
TRACES_KEEP = 20

history: list[dict] = []  # события: новая или обновлённая страница получает их все
viewers: set = set()  # открытые страницы (websocket)
_state = {"lines": 0, "trace": None}


def trace_open() -> Path:
    """Новая запись запуска: ~/.claude/gv-trace/<время>.jsonl (+ .wav — звук микрофона); старые сверх TRACES_KEEP — прочь."""
    TRACES.mkdir(parents=True, exist_ok=True)
    for f in sorted(TRACES.glob("*.jsonl"))[:-TRACES_KEEP]:
        for g in TRACES.glob(f.stem + ".*"):
            g.unlink(missing_ok=True)
    _state["trace"] = TRACES / f"{time.strftime('%Y%m%d-%H%M%S')}.jsonl"
    return _state["trace"]


def trace(ev: str, ts: float | None = None, **data) -> None:
    """Событие в запись запуска: ts — точное время (time.time()), по нему события сводятся со звуком."""
    if f := _state["trace"]:
        try:
            with f.open("a") as h:
                h.write(json.dumps({"ts": ts or time.time(), "ev": ev, **data}, ensure_ascii=False, default=str) + "\n")
        except OSError:
            pass


def log(kind: str, **data) -> None:
    """Строка JSON: что произошло и сколько данных прошло. Звука и ключей здесь нет. Копия — в запись запуска."""
    trace(kind, **data)
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "kind": kind, **data}, ensure_ascii=False) + "\n")
        _state["lines"] += 1
        if _state["lines"] % 200 == 0 and len(lines := LOG.read_text().splitlines()) > LOG_KEEP:
            LOG.write_text("\n".join(lines[-LOG_KEEP:]) + "\n")
    except OSError:
        pass


def emit(obj: dict) -> None:
    """Событие всем страницам и в историю; отправка — в фоне, вызывающий не ждёт."""
    obj = {"t": round(time.monotonic(), 3), **obj}
    history.append(obj)
    del history[:-HISTORY_KEEP]
    for v in list(viewers):
        asyncio.get_running_loop().create_task(_send(v, obj))


async def _send(v, obj: dict) -> None:
    try:
        await v.send_json(obj)
    except Exception:  # noqa: BLE001
        viewers.discard(v)
