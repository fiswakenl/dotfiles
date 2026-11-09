# /// script
# requires-python = ">=3.10"
# ///
"""Запись запуска GV (~/.claude/gv-trace/<время>.jsonl + .wav) → HTML-таймлайн рядом с ней, и открыть в браузере.

    uv run --script ~/.claude/gv/trace_view.py            # последняя запись
    uv run --script ~/.claude/gv/trace_view.py 20261109-230249

Дорожки: речь по Silero, текст Deepgram (промежуточный серым, окончательный чёрным), Smart Turn, классификатор конца
реплики, маршрут и отправки в Claude. Клик по событию — звук с этого места; колесо с Ctrl или ползунок — масштаб.
"""
import json
import subprocess
import sys
from pathlib import Path

TRACES = Path.home() / ".claude" / "gv-trace"


def lane(e: dict) -> tuple[str, str] | None:
    """Событие → (дорожка, подпись); служебное — мимо."""
    ev, text = e["ev"], str(e.get("text") or "")
    if ev in ("vad.start", "vad.stop", "user.start", "user.stop"):
        return "речь", ev
    if ev == "stt.interim":
        return "Deepgram", text
    if ev == "stt.final":
        flags = " ".join(k for k in ("is_final", "speech_final") if e.get(k))
        return "Deepgram", f"■ {text}" + (f"  [{flags}]" if flags else "")
    if ev == "turn" and "End of Turn result" in text:
        return "Smart Turn", text.rsplit(".", 1)[-1]
    if ev == "turn" and "ClassifierUserTurnStopStrategy" in text:
        return "конец реплики", text.split(": ", 1)[-1]
    if ev == "turn.metrics":
        return "конец реплики", f"{e.get('ProcessingMetricsData', 0):.2f} с"
    if ev == "turn" and ("did not continue" in text):
        return "конец реплики", text.split(": ", 1)[-1]
    if ev == "recognized":
        return "реплика", text
    if ev == "route":
        return "маршрут", f"продолжает №{e.get('last')}? p={e.get('p')}"
    if ev in ("ask", "quick"):
        return "Claude", f"→ {ev} №{e.get('id')}" + (f" откат до №{e['rewind']}" if e.get("rewind") else "") + f": {text}"
    if ev.startswith("answer"):
        return "Claude", f"← {ev} №{e.get('id')}"
    if ev == "turn" and "Deepgram" in text or ev == "turn" and "Websocket" in text:
        return "соединение", text
    if ev in ("error", "task.error", "route.error", "turn.warm.error"):
        return "ошибки", str(e.get("error"))
    return None


LANES = ["речь", "Deepgram", "Smart Turn", "конец реплики", "реплика", "маршрут", "Claude", "соединение", "ошибки"]

PAGE = """<!doctype html><meta charset=utf-8><title>GV · __NAME__</title>
<style>
body{font:13px -apple-system,system-ui,sans-serif;margin:0;background:#fafafa;color:#111}
header{position:sticky;top:0;background:#fff;border-bottom:1px solid #ddd;padding:8px 12px;display:flex;gap:12px;align-items:center;z-index:2}
#wrap{overflow-x:auto;position:relative}#tl{position:relative}
.lane{position:relative;height:46px;border-bottom:1px solid #eee}.lane b{position:sticky;left:0;background:#fafafaee;padding:2px 6px;font-size:11px;color:#666;z-index:1}
.e{position:absolute;top:16px;white-space:nowrap;font-size:11px;padding:1px 4px;border-left:2px solid #333;cursor:pointer;background:#fff;max-width:340px;overflow:hidden;text-overflow:ellipsis}
.e:hover{max-width:none;z-index:3;box-shadow:0 1px 4px #0003}
.seg{position:absolute;top:14px;height:20px;background:#cffafe;border:1px solid #0891b2;border-radius:3px}
.gray{color:#999;border-color:#ccc}.warn{border-color:#ea580c}.ok{border-color:#16a34a}
#ph{position:absolute;top:0;bottom:0;width:1px;background:#e11d48;pointer-events:none;z-index:4}
.tick{position:absolute;top:0;font-size:10px;color:#999;border-left:1px solid #eee;height:100%;padding-left:2px}
</style>
<header><b>GV · __NAME__</b><audio id=a controls src="__WAV__"></audio>
<label>масштаб <input id=z type=range min=20 max=600 value=120></label><span id=t></span></header>
<div id=wrap><div id=tl></div></div>
<script>
const E=__DATA__,LANES=__LANES__,T0=__T0__,END=__END__;
const a=document.getElementById('a'),tl=document.getElementById('tl'),z=document.getElementById('z');
function draw(){const px=+z.value;tl.innerHTML='';tl.style.width=(END-T0)*px+200+'px';
 for(let s=0;s<END-T0;s+=1){const k=document.createElement('div');k.className='tick';k.style.left=s*px+90+'px';if(s%5==0)k.textContent=s+' с';tl.append(k)}
 for(const L of LANES){const d=document.createElement('div');d.className='lane';d.innerHTML='<b>'+L+'</b>';tl.append(d);
  const es=E.filter(e=>e.lane==L);
  if(L=='речь'){let st=null;for(const e of es){if(e.label=='vad.start')st=e.t;if(e.label=='vad.stop'&&st!=null){const s=document.createElement('div');s.className='seg';
   s.style.left=st*px+90+'px';s.style.width=Math.max(2,(e.t-st)*px)+'px';s.title=(e.t-st).toFixed(2)+' с';s.onclick=()=>{a.currentTime=st;a.play()};d.append(s);st=null}}continue}
  for(const e of es){const x=document.createElement('div');x.className='e'+(e.cls?' '+e.cls:'');x.style.left=e.t*px+90+'px';
   x.textContent=e.label;x.title=e.t.toFixed(2)+' с · '+e.label;x.onclick=()=>{a.currentTime=Math.max(0,e.t-1);a.play()};d.append(x)}}
 const p=document.createElement('div');p.id='ph';tl.append(p)}
z.oninput=draw;draw();
setInterval(()=>{const p=document.getElementById('ph');if(p)p.style.left=a.currentTime*z.value+90+'px';document.getElementById('t').textContent=a.currentTime.toFixed(1)+' с'},50);
</script>"""


def main() -> None:
    name = sys.argv[1] if len(sys.argv) > 1 else sorted(TRACES.glob("*.jsonl"))[-1].stem
    src = TRACES / f"{name}.jsonl"
    events = [json.loads(l) for l in src.read_text().splitlines() if l.strip()]
    t0 = next((e["ts"] for e in events if e["ev"] == "audio.start"), events[0]["ts"])  # ноль — начало звука
    data = []
    for e in events:
        if got := lane(e):
            cls = "gray" if e["ev"] == "stt.interim" else "warn" if "INCOMPLETE" in got[1] or "short" in got[1] else ""
            data.append({"t": round(e["ts"] - t0, 3), "lane": got[0], "label": got[1], "cls": cls})
    end = max([d["t"] for d in data] + [1]) + 2
    html = (PAGE.replace("__NAME__", name).replace("__WAV__", f"{name}.wav").replace("__DATA__", json.dumps(data, ensure_ascii=False))
            .replace("__LANES__", json.dumps(LANES, ensure_ascii=False)).replace("__T0__", "0").replace("__END__", str(end)))
    out = src.with_suffix(".html")
    out.write_text(html)
    print(out)
    subprocess.run(["open", str(out)], check=False)


if __name__ == "__main__":
    main()
