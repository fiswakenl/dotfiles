# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "websockets>=15", "python-socks[asyncio]"]
# ///
"""Голосовой мост Gemini Live ⇄ сессия Claude Code (плагин gv). Запускает плагин, сам не нужен.

Браузер (микрофон и динамик) ⇄ этот сервис ⇄ Gemini Live. Gemini не отвечает от себя на рабочее: пересказывает слова человека
функцией say_to_claude, ответ Claude приходит ей текстом и озвучивается.

Ручки для плагина (127.0.0.1:8790):
    GET  /next      вопрос для Claude {"id", "text"}; ждёт, пока он появится и Claude свободен; 204 — пока ничего
    POST /busy      {"busy": true|false} — Claude занят/свободен
    POST /answer    тело — ответ Claude текстом; Gemini озвучит
    POST /btw       {"text"} — вставить реплику прямо Gemini, мимо Claude (он занят — его не трогаем)
    POST /ping      плагин жив; пропал на PING_LOST секунд — сервис выходит сам

Ключ и прокси Gemini — ~/.config/gemini.env.
"""
import asyncio
import base64
import itertools
import json
import os
import time
from pathlib import Path

import websockets
from aiohttp import WSMsgType, web

HERE = Path(__file__).resolve().parent
PORT = int(os.environ.get("GV_PORT", 8790))
GEMINI_WS = "wss://generativelanguage.googleapis.com/ws/google.ai.generativelanguage.v1beta.GenerativeService.BidiGenerateContent"
MODEL = "models/gemini-3.8-live"
NEXT_WAIT = 25  # секунд ждёт /next, потом 204: плагин спрашивает снова
PING_LOST = 90  # секунд без /ping — выходим

ids = itertools.count(1)
inbox: list[dict] = []  # вопросы Gemini → Claude
S = {"busy": False, "ping": time.monotonic(), "live": None, "later": []}  # live — сокет Gemini; later — тексты, пока сессии нет

TOOLS = [{"functionDeclarations": [{
    "name": "say_to_claude",
    "description": "Передать Claude Code то, что сказал человек: вопрос, просьба или поручение, своими словами, самодостаточно и коротко",
    "parameters": {"type": "OBJECT", "properties": {"text": {"type": "STRING", "description": "Что сказал человек, ясным письменным текстом"}},
                   "required": ["text"]}}]}]


def gemini_env() -> dict:
    e = dict(l.split("=", 1) for l in (Path.home() / ".config/gemini.env").read_text().splitlines() if "=" in l and not l.startswith("#"))
    e = {k: v.strip() for k, v in e.items()}
    e.update({k: v for k, v in os.environ.items() if k.startswith("GEMINI_")})
    e["GEMINI_PROXY"] = e.get("GEMINI_PROXY") or None
    return e


def system_text() -> str:
    parts = [(HERE / "prompt.md").read_text()]
    if (HERE / "context.md").exists():
        parts.append((HERE / "context.md").read_text())
    return "\n\n".join(parts)


async def to_gemini(text: str) -> None:
    """Текст в живую сессию Gemini (запускает её ответ); нет сессии — ждёт до подключения."""
    g = S["live"]
    if g is None:
        S["later"].append(text)
        return
    await g.send(json.dumps({"realtimeInput": {"text": text}}))


async def talk(request: web.Request) -> web.WebSocketResponse:
    """Браузер ⇄ Gemini Live: речь (PCM 16 кГц) туда, голос (PCM 24 кГц) и расшифровки обратно. Звук приходит только пока человек говорит."""
    ws = web.WebSocketResponse()
    await ws.prepare(request)
    env = gemini_env()
    setup = {"model": MODEL, "generationConfig": {"responseModalities": ["AUDIO"], "speechConfig": {"languageCode": "ru-RU"}},
             "systemInstruction": {"parts": [{"text": system_text()}]}, "tools": TOOLS,
             "inputAudioTranscription": {}, "outputAudioTranscription": {}}
    async with websockets.connect(GEMINI_WS, additional_headers={"x-goog-api-key": env["GEMINI_API_KEY"]},
                                  proxy=env["GEMINI_PROXY"], max_size=None) as g:
        await g.send(json.dumps({"setup": setup}))
        S["live"] = g
        for t in S["later"]:
            await to_gemini(t)
        S["later"].clear()

        async def up() -> None:
            async for m in ws:
                if m.type == WSMsgType.BINARY:
                    await g.send(json.dumps({"realtimeInput": {"audio": {"mimeType": "audio/pcm;rate=16000", "data": base64.b64encode(m.data).decode()}}}))
                elif m.type == WSMsgType.TEXT and m.data == "end":  # человек замолчал: тишину не шлём, сообщаем конец речи
                    await g.send(json.dumps({"realtimeInput": {"audioStreamEnd": True}}))

        async def down() -> None:
            async for raw in g:
                e = json.loads(raw)
                if "error" in e:
                    await ws.send_json({"error": str(e["error"])})
                for fc in e.get("toolCall", {}).get("functionCalls", []):
                    text = fc.get("args", {}).get("text", "")
                    inbox.append({"id": next(ids), "text": text})
                    await ws.send_json({"ask": text})
                    if S["busy"]:
                        res = ("Claude сейчас занят прошлым запросом, этот в очереди и уйдёт, когда он закончит: его не перебиваем. "
                               "Скажи это одной фразой и дальше поддерживай разговор сама; ответ придёт сообщением «[Ответ Claude]».")
                    else:
                        res = "Передано Claude, он работает. Скажи одной фразой, что передала; ответ придёт сообщением «[Ответ Claude]»."
                    await g.send(json.dumps({"toolResponse": {"functionResponses": [{"id": fc["id"], "name": fc["name"], "response": {"result": res}}]}}))
                c = e.get("serverContent", {})
                for p in c.get("modelTurn", {}).get("parts", []):
                    if "inlineData" in p:
                        await ws.send_bytes(base64.b64decode(p["inlineData"]["data"]))
                if t := c.get("inputTranscription", {}).get("text"):
                    await ws.send_json({"me": t})
                if t := c.get("outputTranscription", {}).get("text"):
                    await ws.send_json({"ai": t})
                if c.get("interrupted"):
                    await ws.send_json({"interrupted": 1})

        tasks = [asyncio.create_task(up()), asyncio.create_task(down())]
        try:
            await asyncio.wait(tasks, return_when=asyncio.FIRST_COMPLETED)
        finally:
            S["live"] = None
            for t in tasks:
                t.cancel()
    return ws


async def nxt(_: web.Request) -> web.Response:
    """Плагин забирает следующий вопрос; пока Claude занят, вопросы ждут — его не пинаем."""
    end = time.monotonic() + NEXT_WAIT
    while time.monotonic() < end:
        if inbox and not S["busy"]:
            S["busy"] = True  # с этой минуты Claude занят, не ждём /busy от плагина
            return web.json_response(inbox.pop(0))
        await asyncio.sleep(0.2)
    return web.Response(status=204)


async def busy(request: web.Request) -> web.Response:
    S["busy"] = bool((await request.json()).get("busy"))
    return web.Response(text="ok")


async def answer(request: web.Request) -> web.Response:
    text = (await request.read()).decode().strip()
    if text:
        await to_gemini(f"[Ответ Claude] {text}\n\nПерескажи это человеку голосом своими словами, коротко и без списков.")
    return web.Response(text="ok")


async def btw(request: web.Request) -> web.Response:
    text = (await request.json()).get("text", "").strip()
    if text:
        await to_gemini(f"[Побочная реплика, Claude не трогаем] {text}")
    return web.Response(text="ok")


async def ping(_: web.Request) -> web.Response:
    S["ping"] = time.monotonic()
    return web.Response(text="ok")


async def watchdog(_: web.Application) -> None:
    async def run() -> None:
        while True:
            await asyncio.sleep(10)
            if time.monotonic() - S["ping"] > PING_LOST:
                os._exit(0)
    asyncio.create_task(run())


PAGE = """<!doctype html><meta charset=utf-8><meta name=viewport content="width=device-width,initial-scale=1">
<title>Gemini ⇄ Claude</title>
<style>body{font:16px system-ui;max-width:640px;margin:2rem auto;padding:0 1rem}button{font-size:1.1rem;padding:.6rem 1.2rem}
#log div{margin:.4rem 0}.me{color:#06c}.ai{color:#222}.ask{color:#a50;font-style:italic}.err{color:#c00}</style>
<button id=go>Начать разговор</button> <span id=st></span><div id=log></div>
<script>
const log=document.getElementById('log'),st=document.getElementById('st');
let cur={};
function line(k,pre,t){let d=cur[k];if(!d||!d.isConnected){d=document.createElement('div');d.className=k;d.textContent=pre;log.append(d);cur[k]=d}
 d.textContent+=t;scrollTo(0,document.body.scrollHeight)}
function add(k,t){cur={};const d=document.createElement('div');d.className=k;d.textContent=t;log.append(d)}
go.onclick=async()=>{
 go.disabled=true;st.textContent='подключаюсь…';
 const ws=new WebSocket('ws://'+location.host+'/ws');ws.binaryType='arraybuffer';
 const out=new AudioContext({sampleRate:24000});let next=0,srcs=[];
 ws.onopen=async()=>{
  const inp=new AudioContext({sampleRate:16000});
  const mic=await navigator.mediaDevices.getUserMedia({audio:{echoCancellation:true,noiseSuppression:true,channelCount:1}});
  const sp=inp.createScriptProcessor(2048,1,1);
  // шлём звук только пока есть речь: тишина стоит как речь
  const TH=0.015,HANG=8,PRE=3;let talking=false,quiet=0,pre=[];
  sp.onaudioprocess=e=>{const f=e.inputBuffer.getChannelData(0);let s=0;for(let i=0;i<f.length;i++)s+=f[i]*f[i];
   const loud=Math.sqrt(s/f.length)>TH,b=new Int16Array(f.length);
   for(let i=0;i<f.length;i++)b[i]=Math.max(-1,Math.min(1,f[i]))*32767;
   if(ws.readyState!=1)return;
   if(loud){quiet=0;if(!talking){talking=true;st.textContent='слышу вас';pre.forEach(p=>ws.send(p));pre=[]}ws.send(b.buffer)}
   else if(talking){ws.send(b.buffer);if(++quiet>HANG){talking=false;ws.send('end');st.textContent='говорите (лучше в наушниках)'}}
   else{pre.push(b.buffer);if(pre.length>PRE)pre.shift()}};
  inp.createMediaStreamSource(mic).connect(sp);sp.connect(inp.destination);st.textContent='говорите (лучше в наушниках)';};
 ws.onmessage=ev=>{
  if(typeof ev.data!=='string'){const i=new Int16Array(ev.data),f=new Float32Array(i.length);
   for(let k=0;k<i.length;k++)f[k]=i[k]/32768;const b=out.createBuffer(1,f.length,24000);b.copyToChannel(f,0);
   const s=out.createBufferSource();s.buffer=b;s.connect(out.destination);next=Math.max(next,out.currentTime);s.start(next);next+=b.duration;
   srcs.push(s);s.onended=()=>srcs=srcs.filter(x=>x!==s);return}
  const m=JSON.parse(ev.data);
  if(m.me)line('me','Вы: ',m.me);
  if(m.ai)line('ai','Gemini: ',m.ai);
  if(m.ask)add('ask','→ Claude: '+m.ask);
  if(m.error)add('err',m.error);
  if(m.interrupted){srcs.forEach(s=>{try{s.stop()}catch(e){}});srcs=[];next=0}};
 ws.onclose=()=>{st.textContent='разговор закончен';go.disabled=false};
};
</script>"""


async def index(_: web.Request) -> web.Response:
    return web.Response(text=PAGE, content_type="text/html")


def main() -> None:
    app = web.Application()
    app.on_startup.append(watchdog)
    app.add_routes([web.get("/", index), web.get("/ws", talk), web.get("/next", nxt), web.post("/busy", busy),
                    web.post("/answer", answer), web.post("/btw", btw), web.post("/ping", ping)])
    web.run_app(app, host="127.0.0.1", port=PORT, print=None)


if __name__ == "__main__":
    main()
