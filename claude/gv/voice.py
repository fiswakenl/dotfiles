# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "websockets>=15", "python-socks[asyncio]", "sounddevice", "numpy"]
# ///
"""Сервис GV: голосовой адаптер ⇄ сессия Claude Code. Поднимает плагин, сам не нужен.

Адаптер — GV_VOICE (gemini, deepgram, openai; adapters/), его выбирает плагин из своих настроек.

Ручки (127.0.0.1:GV_PORT, по умолчанию 8790):
    POST /call   {"name", "args"} — единственный вход снаружи: ask, quick (как вызов Gemini; с "sure" — решение оркестратора),
                 utter {"text"} (законченная реплика на решение оркестратору), listen (речь вместо микрофона),
                 status, list, send, rewind (мост в окно Claude)
    GET  /next   плагин забирает запрос {"id", "kind": ask|quick, "text", "rewind"} или реплику на решение
                 {"kind": "route", "text", "busy", "recent"}; ждёт до ?wait= секунд, 204 — пока ничего
    POST /answer ?id=N, {"text"} или {"error"} — ответ на запрос; ask освобождает Claude
    POST /ping   плагин жив; пропал на PING_LOST секунд — сервис выходит сам
    POST /quit   сессия закрылась или пришла новая — выходим
    GET  /       страница с ходом разговора; websocket на том же адресе — поток событий

GV_TEXT_ONLY=1 — вместо адаптера Gemini без сокета, микрофона и колонок: ответы Claude — события say (тесты таймингов).
"""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from aiohttp import web

import adapters
from core import Core
from journal import emit, history, log, viewers

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("GV_PORT", 8790))
VOICE = os.environ.get("GV_VOICE", "gemini")
NEXT_WAIT = 25  # дольше /next не держим
PING_LOST = 90
BRIDGE = {"status", "list", "send", "rewind"}
S = {"ping": time.monotonic()}


async def bridge(name: str, args: dict) -> dict:
    """Команда моста в окне Claude (native/bridge.mjs): строка JSON туда, строка обратно."""
    path = os.environ.get("GV_NATIVE_SOCKET")
    if not path:
        return {"error": "мост не подключён: запустите Claude через ccg"}
    reader, writer = await asyncio.open_unix_connection(path)
    writer.write((json.dumps({"name": name, "args": args}) + "\n").encode())
    await writer.drain()
    line = await asyncio.wait_for(reader.readline(), 15)
    writer.close()
    return json.loads(line)


async def call(request: web.Request) -> web.Response:
    body = await request.json()
    name, args = body.get("name", ""), body.get("args") or {}
    if name in ("ask", "quick"):
        result = core.call(name, args)
    elif name == "utter":
        core.utter(str(args.get("text") or ""))
        result = "ok"
    elif name == "listen":  # речь идёт в своём темпе, вызов возвращается сразу
        asyncio.create_task(voice.listen(args))
        result = "ok"
    elif name in BRIDGE:
        result = await bridge(name, args)
    else:
        return web.json_response({"error": f"нет такого имени: {name}"}, status=404)
    return web.json_response({"result": result})


async def nxt(request: web.Request) -> web.Response:
    end = time.monotonic() + min(float(request.query.get("wait", NEXT_WAIT)), NEXT_WAIT)
    while not (item := core.next()):
        core.changed.clear()
        if (left := end - time.monotonic()) <= 0:
            return web.Response(status=204)
        try:
            await asyncio.wait_for(core.changed.wait(), left)
        except TimeoutError:
            pass
    log("next", id=item.get("id"), type=item["kind"], rewind=item.get("rewind"))
    return web.json_response(item)


async def answer(request: web.Request) -> web.Response:
    body = await request.json()
    core.answer(int(request.query["id"]), str(body.get("text", "")).strip(), str(body.get("error", "")))
    return web.Response(text="ok")


async def ping(_: web.Request) -> web.Response:
    S["ping"] = time.monotonic()
    return web.Response(text="ok")


async def quit_(_: web.Request) -> web.Response:
    log("exit", why="/quit")
    asyncio.get_running_loop().call_later(0.3, lambda: os._exit(0))
    return web.Response(text="ok")


async def index(request: web.Request) -> web.StreamResponse:
    """Страница; с заголовком Upgrade — её поток событий: сначала история и устройство канала, дальше по одному."""
    if request.headers.get("Upgrade", "").lower() != "websocket":
        return web.FileResponse(HERE / "page.html")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    viewers.add(ws)
    await ws.send_json({"history": history, "title": voice.title, "speaks": voice.speaks, "about": voice.about()})
    try:
        async for _ in ws:
            pass
    finally:
        viewers.discard(ws)
    return ws


def step_events(row: dict) -> list[dict]:
    """Строка истории Claude → шаги для страницы: мысли, текст, вызовы инструментов, сообщения пользователя."""
    m = row.get("message") or {}
    content = m.get("content")
    if row.get("type") == "user":
        text = content if isinstance(content, str) else next((b.get("text", "") for b in content or [] if b.get("type") == "text"), "")
        return [{"step": "user", "text": text[:300]}] if text and not row.get("isMeta") else []
    if row.get("type") != "assistant" or not isinstance(content, list):
        return []
    out = []
    for b in content:
        if b.get("type") == "thinking":
            out.append({"step": "think", "text": (b.get("thinking") or "")[:200]})
        elif b.get("type") == "text" and b.get("text", "").strip():
            out.append({"step": "text", "text": b["text"][:400]})
        elif b.get("type") == "tool_use":
            args = b.get("input") or {}
            brief = next((str(args[k]) for k in ("description", "command", "file_path", "pattern", "url", "query", "prompt") if args.get(k)), "")
            out.append({"step": "tool", "text": f"{b.get('name')}: {brief[:160]}"})
    return out


OVERLAY = HERE / "overlay.swift"
OVERLAY_BIN = HERE / ".build" / "overlay"
OVERLAY_PID = Path(os.environ.get("TMPDIR", "/tmp")) / f"gv-overlay-{os.getuid()}.pid"


def overlay(session: Path) -> None:
    """Плашка с последними сообщениями сессии поверх экрана (только мак): собирает overlay.swift, если он новее сборки,
    гасит прежнюю плашку и поднимает новую на файл сессии. Выходит сама, когда пропал этот процесс."""
    if sys.platform != "darwin":
        return
    import subprocess
    try:
        if not OVERLAY_BIN.exists() or OVERLAY_BIN.stat().st_mtime < OVERLAY.stat().st_mtime:
            OVERLAY_BIN.parent.mkdir(exist_ok=True)
            subprocess.run(["swiftc", "-O", str(OVERLAY), "-o", str(OVERLAY_BIN)], check=True, capture_output=True, timeout=180)
        try:
            os.kill(int(OVERLAY_PID.read_text()), signal.SIGTERM)
        except (OSError, ValueError):
            pass
        OVERLAY_PID.write_text(str(subprocess.Popen([str(OVERLAY_BIN), str(session)]).pid))
        log("overlay", session=session.name)
    except Exception as ex:  # noqa: BLE001 — без плашки голос работает как прежде
        log("overlay.error", error=f"{type(ex).__name__}: {ex}")


async def claude_steps() -> None:
    """Шаги сессии Claude — из её файла истории (~/.claude/projects/*/<сессия>.jsonl): номер сессии даёт мост."""
    path, pos = None, 0
    while True:
        await asyncio.sleep(1)
        try:
            if path is None:
                sid = (await bridge("status", {})).get("sessionId")
                found = sorted(Path.home().glob(f".claude/projects/*/{sid}.jsonl")) if sid else []
                if not found:
                    await asyncio.sleep(9)
                    continue
                path, pos = found[0], found[0].stat().st_size  # только новое: старое уже в чате
                await asyncio.to_thread(overlay, path)
            if path.stat().st_size < pos:
                pos = 0
            with path.open() as f:
                f.seek(pos)
                chunk = f.read()
            done = chunk.rfind("\n") + 1  # строку, что ещё пишется, дочитаем в следующий раз
            pos += len(chunk[:done].encode())
            for line in chunk[:done].splitlines():
                for e in step_events(json.loads(line)):
                    emit(e)
        except Exception as ex:  # noqa: BLE001 — сессия сменилась или мост не готов: найдём заново
            log("steps.error", error=f"{type(ex).__name__}: {ex}")
            path = None
            await asyncio.sleep(5)


async def start(_: web.Application) -> None:
    global core, voice
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda signum, _f: (log("exit", why=f"сигнал {signal.Signals(signum).name}"), os._exit(0)))
    core = Core(say=lambda text: voice.say(text), emit=emit, log=log)
    voice = adapters.make(VOICE, core)
    log("start", port=PORT, voice=VOICE, adapter=voice.title, pause_ms=voice.pause_ms)
    for c in (watchdog(), claude_steps(), *voice.start()):
        asyncio.create_task(c)


async def watchdog() -> None:
    """Раз в секунду: очередь речи (ход Gemini мог зависнуть); раз в 10 с — пинг плагина и правка кода (перезапуск)."""
    watched = [*HERE.glob("*.py"), *HERE.glob("adapters/*.py"), *HERE.glob("prompts/*.md"), HERE / "prompts/context.md", HERE / "page.html", OVERLAY]
    stamp = {f: f.stat().st_mtime for f in watched if f.exists()}
    for tick in iter(int, 1):
        await asyncio.sleep(1)
        core.pump()
        if tick % 10:
            continue
        if time.monotonic() - S["ping"] > PING_LOST:
            log("exit", why=f"плагин не пинговал {PING_LOST} с")
            os._exit(0)
        if any(f.exists() and f.stat().st_mtime != stamp.get(f) for f in watched):
            log("reload")
            os.execv(sys.executable, [sys.executable, *sys.argv])


def main() -> None:
    app = web.Application()
    app.on_startup.append(start)
    app.add_routes([web.get("/", index), web.post("/call", call), web.get("/next", nxt), web.post("/answer", answer),
                    web.post("/ping", ping), web.post("/quit", quit_)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


core: Core
voice: adapters.Adapter

if __name__ == "__main__":
    main()
