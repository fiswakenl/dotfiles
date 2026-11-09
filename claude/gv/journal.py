"""Журналы GV: ~/.claude/gv.log (что произошло), файл на подключение Gemini (весь диалог) и поток событий для страницы."""
import asyncio
import json
import os
import time
from pathlib import Path

LOG = Path.home() / ".claude" / "gv.log"
LOG_KEEP = 2000
SESSIONS = Path.home() / ".claude" / "gv-sessions"
SESSIONS_KEEP = 200
HISTORY_KEEP = 1000

history: list[dict] = []  # события: новая или обновлённая страница получает их все
viewers: set = set()  # открытые страницы (websocket)
_state = {"lines": 0, "rec": None, "t0": 0.0}


def log(kind: str, **data) -> None:
    """Строка JSON: что произошло и сколько данных прошло. Звука и ключей здесь нет."""
    try:
        with LOG.open("a") as f:
            f.write(json.dumps({"at": time.strftime("%Y-%m-%dT%H:%M:%S"), "pid": os.getpid(), "kind": kind, **data}, ensure_ascii=False) + "\n")
        _state["lines"] += 1
        if _state["lines"] % 200 == 0 and len(lines := LOG.read_text().splitlines()) > LOG_KEEP:
            LOG.write_text("\n".join(lines[-LOG_KEEP:]) + "\n")
    except OSError:
        pass


def rec(obj: dict) -> None:
    if f := _state["rec"]:
        try:
            with f.open("a") as h:
                h.write(json.dumps({"at": time.strftime("%H:%M:%S"), **obj}, ensure_ascii=False) + "\n")
        except OSError:
            pass


def rec_open(**head) -> None:
    """Новый файл диалога — на каждое подключение сокета Gemini."""
    try:
        SESSIONS.mkdir(parents=True, exist_ok=True)
        for f in sorted(SESSIONS.glob("*.jsonl"))[:-SESSIONS_KEEP]:
            f.unlink()
        _state.update(rec=SESSIONS / f"{time.strftime('%Y%m%d-%H%M%S')}-{os.getpid()}.jsonl", t0=time.monotonic())
        rec({"connect": time.strftime("%Y-%m-%dT%H:%M:%S"), **head})
    except OSError:
        _state["rec"] = None


def rec_close(**tail) -> None:
    rec({**tail, "seconds": round(time.monotonic() - _state["t0"])})
    _state["rec"] = None


def emit(obj: dict) -> None:
    """Событие всем страницам и в историю; отправка — в фоне, вызывающий не ждёт."""
    obj = {"t": round(time.monotonic(), 3), **obj}
    history.append(obj)
    del history[:-HISTORY_KEEP]
    rec(obj)
    for v in list(viewers):
        asyncio.get_running_loop().create_task(_send(v, obj))


async def _send(v, obj: dict) -> None:
    try:
        await v.send_json(obj)
    except Exception:  # noqa: BLE001
        viewers.discard(v)
