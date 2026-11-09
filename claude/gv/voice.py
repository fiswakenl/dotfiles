# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "websockets>=15", "python-socks[asyncio]", "sounddevice", "numpy"]
# ///
"""Сервис GV: Gemini Live ⇄ сессия Claude Code. Поднимает плагин, сам не нужен.

Ручки (127.0.0.1:GV_PORT, по умолчанию 8790):
    POST /call   {"name", "args"} — единственный вход снаружи: ask, quick (как вызов Gemini), listen (речь вместо микрофона),
                 status, list, send, rewind (мост в окно Claude)
    GET  /next   плагин забирает запрос {"id", "kind": ask|quick, "text", "rewind"}; ждёт до ?wait= секунд, 204 — пока ничего
    POST /answer ?id=N, {"text"} или {"error"} — ответ на запрос; ask освобождает Claude
    POST /ping   плагин жив; пропал на PING_LOST секунд — сервис выходит сам
    POST /quit   сессия закрылась или пришла новая — выходим
    GET  /       страница с ходом разговора; websocket на том же адресе — поток событий

GV_TEXT_ONLY=1 — без сокета Gemini, микрофона и колонок: ответы Claude — события say (тесты таймингов).
"""
import asyncio
import json
import os
import signal
import sys
import time
from pathlib import Path

from aiohttp import web

import gemini
from core import Core
from journal import emit, history, log, viewers

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("GV_PORT", 8790))
TEXT_ONLY = os.environ.get("GV_TEXT_ONLY") == "1"
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
    log("next", id=item["id"], type=item["kind"], rewind=item["rewind"])
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
    """Страница; с заголовком Upgrade — её поток событий: сначала история и инструкция Gemini, дальше по одному."""
    if request.headers.get("Upgrade", "").lower() != "websocket":
        return web.Response(text=PAGE, content_type="text/html")
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    viewers.add(ws)
    await ws.send_json({"history": history, "prompt": gemini.system_text(), "tools": gemini.tools()[0]["functionDeclarations"]})
    try:
        async for _ in ws:
            pass
    finally:
        viewers.discard(ws)
    return ws


async def start(_: web.Application) -> None:
    global core, voice
    for sig in (signal.SIGTERM, signal.SIGHUP, signal.SIGINT):
        signal.signal(sig, lambda signum, _f: (log("exit", why=f"сигнал {signal.Signals(signum).name}"), os._exit(0)))
    core = Core(say=lambda text: voice.say(text), emit=emit, log=log)
    pause_ms = int(os.environ.get("GV_PAUSE_MS", 2500))
    voice = gemini.Text(core) if TEXT_ONLY else gemini.Live(core, os.environ.get("GV_DUPLEX", "on") == "on", pause_ms)
    log("start", port=PORT, text_only=TEXT_ONLY, pause_ms=pause_ms)
    for c in (watchdog(), *voice.start()):
        asyncio.create_task(c)


async def watchdog() -> None:
    """Раз в секунду: очередь речи (ход Gemini мог зависнуть); раз в 10 с — пинг плагина и правка кода (перезапуск)."""
    watched = [*HERE.glob("*.py"), HERE / "prompt.md", HERE / "context.md"]
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


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Gemini ⇄ Claude</title>
<style>body{font:15px ui-monospace,monospace;max-width:720px;margin:1.5rem auto;padding:0 1rem}#st{color:#888}
details{margin:.6rem 0;color:#444}pre{white-space:pre-wrap;font:inherit;background:#f4f4f4;padding:.6rem;border-radius:6px}#log div{margin:.35rem 0}.me{color:#06c}.ai{color:#222}.ask{color:#a50;font-style:italic}.err{color:#c00}.state{color:#888}</style>
<div id=st>жду сессию Claude…</div>
<details><summary>Инструкция и функции Gemini</summary><pre id=prompt></pre></details><div id=log></div>
<script>
const log=document.getElementById('log'),st=document.getElementById('st'),texts={};let cur={};
function line(k,pre,t){let d=cur[k];if(!d||!d.isConnected){d=document.createElement('div');d.className=k;d.textContent=pre;log.append(d)}
 cur={};cur[k]=d;d.textContent+=t;scrollTo(0,document.body.scrollHeight)}
function add(k,t){cur={};const d=document.createElement('div');d.className=k;d.textContent=t;log.append(d);scrollTo(0,document.body.scrollHeight)}
const handle=m=>{
 if(m.history){log.innerHTML='';cur={};m.history.forEach(handle);
  document.getElementById('prompt').textContent=m.prompt+'\\n\\n— Функции —\\n'+m.tools.map(t=>t.name+': '+t.description).join('\\n');return}
 if(m.me)line('me','Вы: ',m.me);if(m.ai)line('ai','Gemini: ',m.ai);
 if(m.ask){texts[m.id]=m.ask;add('ask',(m.kind==='quick'?'⚡ №':'→ Claude №')+m.id+(m.rewind?' (откат до №'+m.rewind+')':'')+': '+m.ask)}
 if(m.answer)add('ask','← №'+m.id+': '+m.answer);if(m.superseded)add('state','· №'+m.superseded+' заменён');
 if(m.say)add('state','· Gemini читает ответ №'+m.say);if(m.dropped)add('state','· ответ на №'+m.dropped+' отброшен');
 if(m.error)add('err',m.error);if(m.state)add('state','· '+m.state)};
function connect(){
 const ws=new WebSocket('ws://'+location.host+'/');
 ws.onopen=()=>{st.textContent='журнал подключён · звук и микрофон — на компьютере'};
 ws.onmessage=ev=>handle(JSON.parse(ev.data));
 ws.onclose=()=>{st.textContent='сессия закрыта, жду новую…';setTimeout(connect,2000)};}
connect();
</script>"""


def main() -> None:
    app = web.Application()
    app.on_startup.append(start)
    app.add_routes([web.get("/", index), web.post("/call", call), web.get("/next", nxt), web.post("/answer", answer),
                    web.post("/ping", ping), web.post("/quit", quit_)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


core: Core
voice: gemini.Text | gemini.Live

if __name__ == "__main__":
    main()
