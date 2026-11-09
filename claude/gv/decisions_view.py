# /// script
# requires-python = ">=3.10"
# ///
"""Отрезок записи запуска GV (~/.claude/gv-trace/<время>.jsonl) → HTML-страница о вопросах к Decisions: когда какой задан,
сколько думал, что решил и что после этого стало с ходом Claude. На странице только шкала и строки под ней: ведёшь мышкой
по шкале — строки показывают состояние на этот момент (собранный текст, открытая часть выделена; человек; Claude; Decisions).
Дорожки: громкость и фон там, где Silero слышит голос; ромбы Smart Turn (закрашен — «договорил»); отметки кусков текста Deepgram
(толстая — закрытый); три вопроса Decisions (полоса — время ответа, тёмная — «да», рядом вероятность); запросы Claude;
красная линия — стоп, пунктир — откат. Данные лежат в странице одним блоком JSON, рисует их скрипт страницы: другой отрезок
или другая запись — та же команда с другими числами.

    uv run --script ~/.claude/gv/decisions_view.py                                  # последняя запись, 25 с до последнего запроса
    uv run --script ~/.claude/gv/decisions_view.py 20261110-001556 00:16:17 00:16:39 -o страница.html

Без -o страница ложится рядом с записью и открывается в браузере. Весь запуск по событиям, со звуком, — trace_view.py.
"""
import json
import re
import subprocess
import sys
import time
import wave
from array import array
from pathlib import Path

TRACES = Path.home() / ".claude" / "gv-trace"
TAIL_S = 25  # отрезок по умолчанию: столько секунд до последнего запроса
NEAR_S = 0.05  # события одного решения пишутся подряд: ближе этого — следствие
LEVEL_S = 0.04  # шаг громкости на странице
VERDICT = re.compile(r": (\w+) \(([\d.]+)\) for \[(.*)\]$")


def clock(ts: float) -> str:
    return time.strftime("%H:%M:%S", time.localtime(ts))


def seconds(hms: str) -> int:
    h, m, s = (int(x) for x in hms.split(":"))
    return h * 3600 + m * 60 + s


def span(rows: list[dict], start: str | None, end: str | None) -> tuple[float, float]:
    """Отрезок записи: с первого события не раньше start до end; без них — TAIL_S секунд до последнего запроса."""
    if start:
        a = next(r["ts"] for r in rows if clock(r["ts"]) >= start)
        a -= a % 1
        return a, a + ((seconds(end) - seconds(start)) % 86400 if end else TAIL_S)
    last = next((r["ts"] for r in reversed(rows) if r["ev"] in ("ask", "quick")), rows[-1]["ts"])
    return last - TAIL_S, last + 1.5


def after(rows: list[dict], i: int, ev: str, **same) -> dict | None:
    """Событие ev сразу за i-м (не дальше NEAR_S) с теми же полями — следствие этого решения."""
    for r in rows[i + 1:]:
        if r["ts"] - rows[i]["ts"] > NEAR_S:
            return None
        if r["ev"] == ev and all(r.get(k) == v for k, v in same.items()):
            return r
    return None


def level(rows: list[dict], sound: Path, a: float, b: float) -> dict | None:
    """Громкость микрофона на отрезке: среднеквадратичная по шагам LEVEL_S, в долях самого громкого шага (0–100).
    Звук запуска пишется подряд с события audio.start, поэтому время — это место в файле."""
    start = next((r["ts"] for r in rows if r["ev"] == "audio.start"), None)
    if start is None or not sound.exists():
        return None
    with wave.open(str(sound)) as w:
        rate, size = w.getframerate(), round(w.getframerate() * LEVEL_S)
        w.setpos(min(w.getnframes(), max(0, round((a - start) * rate))))
        pcm = array("h", w.readframes(round((b - a) * rate)))
    rms = [(sum(x * x for x in pcm[i:i + size]) / size) ** 0.5 for i in range(0, len(pcm) - size + 1, size)]
    top = max(rms, default=0) or 1
    return {"step": LEVEL_S, "from": max(a, start), "v": [round(x / top * 100) for x in rms]}


def collect(rows: list[dict], a: float, b: float) -> dict:
    rows = [r for r in rows if a <= r["ts"] <= b]
    d = {"from": a, "to": b, "speech": [], "chunks": [], "phrases": [], "decisions": [], "asks": [], "cuts": [], "local": [], "asks_dg": [], "replies": []}
    began = heard = None  # начало идущего отрезка речи; начало идущей фразы
    for i, r in enumerate(rows):
        ev, ts, text = r["ev"], r["ts"], str(r.get("text") or "")
        if ev == "vad.start":
            began, heard = ts, heard or ts
        elif ev == "vad.stop" and began:
            d["speech"].append([began, ts])
            began = None
        elif ev in ("stt.interim", "stt.final"):
            d["chunks"].append({"t": ts, "text": text, "final": ev == "stt.final", "by_us": bool(r.get("finalized")), "pause": bool(r.get("speech_final"))})
        elif ev == "dg.finalize":
            d["asks_dg"].append({"t": ts, "why": r.get("why", "")})
        elif ev == "dg.finalize.reply":
            d["replies"].append({"t": ts, "text": text})
        elif ev == "recognized":
            if text:
                d["phrases"].append({"a": heard or ts, "b": ts, "text": text})
            heard = None
        elif ev == "turn" and "End of Turn result" in text:  # Smart Turn, локальная модель: договорил ли по интонации
            d["local"].append({"t": ts, "yes": text.endswith("COMPLETE") and not text.endswith("INCOMPLETE")})
        elif ev == "route.early":
            d["decisions"].append({"kind": "early", "t": ts, "d": r["s"], "p": r["p"], "text": text, "last": r["last"],
                                   "yes": bool(after(rows, i, "cut", id=r["last"]))})
        elif ev == "turn" and (m := VERDICT.search(text)) and "Classifier" in text:
            took = after(rows, i, "turn.metrics") or {}
            d["decisions"].append({"kind": "done", "t": ts, "d": took.get("ProcessingMetricsData", 0.2), "p": float(m[2]), "text": m[3], "yes": bool(after(rows, i, "recognized"))})
        elif ev == "route":
            said = d["phrases"][-1]["text"] if d["phrases"] else ""
            d["decisions"].append({"kind": "route", "t": ts, "d": r["s"], "p": r["p"], "text": said, "last": r["last"],
                                   "pause": r.get("pause"), "yes": bool(after(rows, i, "ask", rewind=r["last"]))})
        elif ev in ("ask", "quick"):
            d["asks"].append({"t": ts, "id": r["id"], "kind": ev, "text": text, "rewind": r.get("rewind")})
        elif ev == "cut":
            d["cuts"].append({"t": ts, "id": r["id"]})
    if not d["asks_dg"]:  # запись старше записи «верни»: Pipecat шлёт его на каждый конец речи по Silero, пока сокет открыт
        d["asks_dg"] = [{"t": b2, "why": "silero: пауза", "derived": True} for _, b2 in d["speech"]]
    return d


PAGE = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="frame" content="1180x720">
<title>gv · decisions · __NAME__</title>
<!-- обновить или взять другой отрезок: __COMMAND__ -->
<style>
*{box-sizing:border-box}html,body{margin:0;background:#fff;color:#111;font:15px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif}
.w{padding:18px 24px;--l:110px}.mono{font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280}
#tl{position:relative;margin-top:8px;cursor:crosshair}
.row{position:relative;height:28px;margin-left:var(--l);border-bottom:1px solid #f3f4f6}.row.gap{height:8px;border:0}
.row>b{position:absolute;left:calc(-1*var(--l));top:5px;width:calc(var(--l) - 12px);text-align:right;font:12.5px/1.6 ui-monospace,SFMono-Regular,Menlo,monospace;color:#6b7280;font-weight:400}
.bar{position:absolute;top:5px;height:18px;border-radius:4px;overflow:hidden;white-space:nowrap;font-size:12px;line-height:17px;padding:0 5px}
.cl{background:#dcfce7;border:1px solid #16a34a}.cl.q{background:#ecfccb;border-color:#65a30d}
.no{background:#fed7aa;border:1px solid #ea580c}.yes{background:#ea580c;border:1px solid #c2410c}
.row.snd{height:56px}.row.snd>b{top:18px}.row.snd svg{position:absolute;left:0;top:3px;height:50px}
.vad{position:absolute;top:3px;height:50px;background:#dbeafe;border-radius:3px}
.tk{position:absolute;top:6px;height:16px;border-left:2px solid #93c5fd}.tk.f{top:3px;height:22px;border-left:4px solid #1d4ed8;margin-left:-1px}
.fz{position:absolute;top:-2px;font-size:12px;line-height:12px;margin-left:-5px;color:#7c3aed}
.rh{position:absolute;top:8px;width:12px;height:12px;margin-left:-6px;transform:rotate(45deg);background:#fff;border:2px solid #7c3aed}.rh.y{background:#7c3aed}
.lab{position:absolute;top:4px;font-size:12.5px;white-space:nowrap;background:#fff;padding:0 3px;z-index:2;color:#6b7280}.lab.y{font-weight:700;color:#c2410c}
.v{position:absolute;top:0;bottom:22px;width:0;border-left:2px solid #dc2626;z-index:1}.v.old{border-left:2px dashed #9ca3af}
#axis{position:relative;height:22px;margin-left:var(--l);border-top:1px solid #9ca3af}
#axis i{position:absolute;top:0;height:5px;border-left:1px solid #9ca3af}#axis span{position:absolute;top:5px;transform:translateX(-50%);white-space:nowrap}
#cur{position:absolute;top:0;bottom:22px;width:0;border-left:1.5px solid #111;pointer-events:none;z-index:3}
#cap{margin:12px 0 6px var(--l);display:flex;gap:14px;align-items:baseline}#cap b{font:13px ui-monospace,SFMono-Regular,Menlo,monospace}
#cap .text{font-size:17px}
#now{margin-left:var(--l);display:grid;grid-template-columns:1fr 1fr;gap:0 22px}
.tile{display:grid;grid-template-columns:38px 108px 1fr 52px;align-items:center;gap:0 6px;height:25px;padding:0 6px;border-radius:4px;font-size:13px;cursor:default}
.tile.on{background:#fef3c7}.tile i{position:relative;height:22px;font-style:normal}.tile u{text-decoration:none}
.tile .nm{color:#6b7280;font-size:12.5px;white-space:nowrap}.tile.on .nm{color:#b45309;font-weight:600}
.tile .val{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}.tile .ago{text-align:right;color:#9ca3af;font:11.5px ui-monospace,SFMono-Regular,Menlo,monospace}
.tile .bar{left:0;top:2px}.tile .rh{top:5px;margin-left:0}.tile .tk{top:3px}.tile .tk.f{top:0}.tile .fz{top:5px}
.tile .ln{position:absolute;top:0;height:22px;border-left:2px solid #dc2626}.tile .ln.old{border-left:2px dashed #9ca3af}
.tile .wv{position:absolute;left:0;top:7px;width:34px;height:8px;background:repeating-linear-gradient(90deg,#1d4ed8 0 2px,#dbeafe 2px 4px)}
.open{background:#bfdbfe;border-radius:2px;padding:1px 2px;box-decoration-break:clone;-webkit-box-decoration-break:clone}
.hit{filter:drop-shadow(0 0 2px #f59e0b) drop-shadow(0 0 5px #f59e0b);z-index:4}.vad.hit{filter:none;box-shadow:inset 0 0 0 2px #f59e0b}
.v.hit{border-left-width:3px}.row.hot>b{color:#b45309;font-weight:600}
#log{margin:12px 0 0 var(--l);font:12.5px/1.7 ui-monospace,SFMono-Regular,Menlo,monospace;color:#374151;min-height:170px}
#log .hd{color:#9ca3af;text-transform:uppercase;letter-spacing:.04em;font-size:11.5px}
#log div{white-space:nowrap;overflow:hidden;text-overflow:ellipsis}#log .dt{display:inline-block;width:70px;text-align:right;color:#9ca3af;margin-right:12px}
#log .who{display:inline-block;width:150px;color:#6b7280}#log .near{background:#fef3c7}#log .fut{opacity:.4}#log .near .dt{color:#b45309}
.dim{color:#9ca3af}.key{font-weight:600}.red{color:#dc2626;font-weight:600}.past{color:#6b7280}#cap>b.t0{color:#111}
</style>
<div class="w">
<div class="mono" id="head"></div>
<div id="tl"></div>
<div id="cap"></div>
<div id="now"></div>
<div id="log"></div>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent), T0 = D.from, LEN = D.to - D.from, LEFT = 110;
const NAME = {early: 'ранний', done: 'конец фразы', route: 'маршрут'};
const ASK = {early: 'начало продолжения?', done: 'мысль закончена?', route: 'продолжает прошлую?'};
const num = (x, n = 2) => x.toFixed(n).replace('.', ',');
const clock = (t, frac) => { const d = new Date(t * 1000), p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` + (frac ? ',' + p(Math.floor(t % 1 * 100)) : ''); };
const dim = s => `<span class="dim">${s}</span>`;
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const outcome = c => c.kind === 'early' ? (c.yes ? `стоп №${c.last}` : 'ждём следующий кусок')
  : c.kind === 'done' ? (c.yes ? 'фраза закрыта' : 'ждём продолжения')
  : (c.yes ? `продолжение №${c.last}: откат и склейка` : 'новая мысль');
// Все события отрезка словами протокола: кто кому что прислал. Показываются рядом с курсором — понятно, что за чем пришло.
const flag = c => `is_final=${c.final}` + (c.final ? `, speech_final=${c.pause}, from_finalize=${c.by_us}` : '');
const EVENTS = [
  ...D.speech.flatMap(([a, b]) => [[a, 'Silero', 'голос начался · VADUserStartedSpeaking'], [b, 'Silero', 'тишина → голос кончился · VADUserStoppedSpeaking']]),
  ...D.asks_dg.map(a => [a.t, 'мы → Deepgram', 'Finalize («верни»)' + (a.derived ? ' · по коду Pipecat, в записи не было' : '')]),
  ...D.replies.map(r => [r.t, 'Deepgram → мы', `ответ на Finalize, from_finalize=true «${r.text}»`]),
  ...D.chunks.map(c => [c.t, 'Deepgram → мы', `Results ${flag(c)} «${c.text}»`]),
  ...D.local.map(m => [m.t, 'Smart Turn', `EndOfTurnState.${m.yes ? 'COMPLETE' : 'INCOMPLETE'} · по интонации ${m.yes ? 'договорил' : 'не договорил'}`]),
  ...D.decisions.flatMap(c => [[c.t - c.d, 'мы → Decisions', `${NAME[c.kind]}: «${ASK[c.kind]}» по «${c.text}»`],
                               [c.t, 'Decisions → мы', `p=${num(c.p)} за ${num(c.d)} с → ${outcome(c)}`]]),
  ...D.cuts.map(c => [c.t, 'мы → плагин', `cancel №${c.id}: остановить ход Claude`]),
  ...D.asks.map(a => [a.t, 'мы → плагин', `${a.kind} №${a.id}` + (a.rewind ? `, rewind №${a.rewind}` : '') + ` «${a.text}»`]),
].sort((a, b) => a[0] - b[0]);
const LOG_N = 7;  // строк в логе: ближайшие к курсору

let SCALE = 1;  // пикселей в секунде
let NOW = D.cuts[0]?.t ?? D.chunks[0]?.t ?? D.from;  // момент под курсором; сначала — первый стоп

function draw() {
  const tl = document.getElementById('tl'), W = tl.clientWidth - LEFT, S = SCALE = W / LEN, x = t => (t - T0) * S;
  const row = name => `<div class="row"><b>${name}</b>`;
  const bar = (a, b, cls, text = '', on = '') => `<div class="bar ${cls}" ${on} style="left:${x(a)}px;width:${Math.max(6, x(b) - x(a))}px">${esc(text)}</div>`;
  // Срабатывание отметки: data-at — мгновения (их может быть несколько), data-span — отрезок; data-k — её строка в панели.
  const at_ = (k, ...ts) => `data-k="${k}" data-at="${ts.join(' ')}"`, span = (k, a, b) => `data-k="${k}" data-span="${a} ${b}"`;
  // Звук: отрезки речи по Silero фоном, поверх — громкость микрофона зеркальной гистограммой.
  const L = D.level, bars = L ? L.v.map((v, i) => { const hh = Math.max(0.6, Math.sqrt(v / 100) * 24);
    return `<rect x="${x(L.from + i * L.step).toFixed(1)}" y="${(25 - hh).toFixed(1)}" width="${Math.max(1, L.step * S - 0.6).toFixed(1)}" height="${(hh * 2).toFixed(1)}"/>`; }).join('') : '';
  let h = '<div class="row snd"><b>звук</b>' + D.speech.map(([a, b]) => `<div class="vad" ${at_('vad', a, b)} style="left:${x(a)}px;width:${x(b) - x(a)}px"></div>`).join('') +
    `<svg width="${W}" height="50" fill="#1d4ed8">${bars}</svg></div>`;
  // Локальная модель конца реплики: вердикт на каждой паузе в звуке; закрашен — «договорил».
  h += row('smart turn') + D.local.map(m => `<div class="rh ${m.yes ? 'y' : ''}" ${at_('turn', m.t)} style="left:${x(m.t)}px"></div>`).join('') + '</div>';
  // Текст: отметка там, где кусок пришёл; толстая — закрытый. Сам текст — под шкалой, по курсору.
  h += row('deepgram') + D.asks_dg.map(a => `<span class="fz" ${at_('fin', a.t)} style="left:${x(a.t)}px">▼</span>`).join('') +
    D.chunks.map(c => `<div class="tk ${c.final ? 'f' : ''}" ${at_(c.final ? 'final' : 'open', c.t)} style="left:${x(c.t)}px"></div>`).join('') + '</div><div class="row gap"></div>';
  for (const kind of ['early', 'done', 'route'])
    h += row(NAME[kind]) + D.decisions.filter(c => c.kind === kind).map((c, i, all) =>
      bar(c.t - c.d, c.t, c.yes ? 'yes' : 'no', '', span(c.kind, c.t - c.d, c.t)) +
      // вероятность справа от полосы; следующий вопрос слишком близко — слева, чтобы не закрыть его полосу
      (all[i + 1] && x(all[i + 1].t - all[i + 1].d) - x(c.t) < 44
        ? `<span class="lab ${c.yes ? 'y' : ''}" style="right:${W - x(c.t - c.d) + 4}px">${num(c.p)}</span>`
        : `<span class="lab ${c.yes ? 'y' : ''}" style="left:${x(c.t) + 4}px">${num(c.p)}</span>`)).join('') + '</div>';
  h += '<div class="row gap"></div>' + row('claude') + D.asks.map(a => {
    const cut = D.cuts.find(c => c.id === a.id), next = D.asks.find(n => n.rewind === a.id), end = Math.min(cut?.t ?? D.to, next?.t ?? D.to, D.to);
    return bar(a.t, end, 'cl' + (a.kind === 'quick' ? ' q' : ''), `№${a.id}`, at_('ask', a.t)); }).join('') + '</div>';
  const step = LEN > 40 ? 5 : LEN > 16 ? 2 : 1;
  let ax = '';
  for (let t = Math.ceil(T0 / step) * step; t <= D.to; t += step) ax += `<i style="left:${x(t)}px"></i><span class="mono" style="left:${x(t)}px">${clock(t).slice(3)}</span>`;
  h += `<div id="axis">${ax}</div>`;
  for (const c of D.cuts) {  // красная — ход остановлен; пунктир — откат, где он остановился бы без раннего вопроса
    h += `<div class="v" ${at_('cut', c.t)} style="left:${LEFT + x(c.t)}px"></div>`;
    const old = D.asks.find(a => a.rewind === c.id);
    if (old) h += `<div class="v old" ${at_('cut', old.t)} style="left:${LEFT + x(old.t)}px"></div>`;
  }
  tl.innerHTML = h + '<div id="cur"></div>';
  tl.onmousemove = e => { const px = e.clientX - tl.getBoundingClientRect().left - LEFT; if (px >= 0 && px <= W) at(T0 + px / S); };
  at(NOW);
}

// Состояние на момент t, как на паузе: над легендой — собранный текст (открытая часть выделена); легенда — по строке на каждое
// обозначение шкалы: значок, имя, его последнее значение к этому моменту и сколько назад. Сработавшие рядом с курсором — подсвечены.
const lastOf = (list, t) => list.filter(e => (e.t ?? e) <= t).pop();
// [ключ отметки, значок, имя, t → [значение, момент события или null]]
const TILES = [
  ['vad', '<u class="wv"></u>', 'Silero', t => {
    const s = D.speech.find(([a, b]) => t >= a && t <= b), q = D.speech.filter(s => s[1] < t).pop(), v = D.level ? D.level.v[Math.round((t - D.level.from) / D.level.step)] : null;
    return [(s ? '<b>голос</b>' : 'тишина') + (v != null ? dim(` · ${v}%`) : ''), s ? s[0] : q ? q[1] : null]; }],
  ['turn', '<u class="rh" style="left:4px"></u><u class="rh y" style="left:20px"></u>', 'Smart Turn', t => {
    const m = lastOf(D.local, t);
    return m ? [`<b>${m.yes ? 'COMPLETE' : 'INCOMPLETE'}</b>`, m.t] : ['—', null]; }],
  ['open', '<u class="tk" style="left:16px"></u>', 'is_final=false', t => {
    const c = lastOf(D.chunks.filter(c => !c.final), t);
    return c ? [`«${esc(c.text)}»`, c.t] : ['—', null]; }],
  ['final', '<u class="tk f" style="left:16px"></u>', 'is_final=true', t => {
    const c = lastOf(D.chunks.filter(c => c.final), t);
    return c ? [`«${esc(c.text)}» ` + dim(`speech_final=${c.pause}, from_finalize=${c.by_us}`), c.t] : ['—', null]; }],
  ['fin', '<u class="fz" style="left:12px">▼</u>', 'Finalize', t => {
    const a = lastOf(D.asks_dg, t);
    if (!a) return ['—', null];
    const got = D.chunks.find(c => c.final && c.t >= a.t && c.t <= t), rep = D.replies.find(r => r.t >= a.t && r.t <= t);
    return [rep ? `ответ «${esc(rep.text)}»` : got ? (got.by_us ? 'ответ с текстом' : 'Deepgram закрыл сам раньше') : '<b>ждём ответ</b>', a.t]; }],
  ...['early', 'done', 'route'].map(kind => [kind, `<u class="bar no" style="width:14px"></u><u class="bar yes" style="left:18px;width:14px"></u>`, NAME[kind], t => {
    const going = D.decisions.find(c => c.kind === kind && c.t - c.d <= t && c.t > t), c = going ?? lastOf(D.decisions.filter(c => c.kind === kind), t);
    if (!c) return ['—', null];
    return [going ? '<b>думает…</b>' : `<b>${num(c.p)}</b> · <span class="${c.yes ? 'key' : ''}">${outcome(c)}</span>`, going ? c.t - c.d : c.t]; }]),
  ['ask', '<u class="bar cl" style="width:32px">№1</u>', 'Claude', t => {
    const a = lastOf(D.asks, t);
    if (!a) return ['—', null];
    return [`№${a.id} <b>${D.cuts.some(c => c.id === a.id && c.t <= t) ? 'остановлен' : 'работает'}</b>` + (a.rewind ? dim(` · вместо №${a.rewind}`) : ''), a.t]; }],
  ['cut', '<u class="ln" style="left:8px"></u><u class="ln old" style="left:22px"></u>', 'стоп · откат', t => {
    const c = lastOf(D.cuts, t);
    return c ? [`№${c.id}${stopped(c)}`, c.t] : ['—', null]; }],
];
// Подсказка при наведении на строку легенды: что значит обозначение.
const ABOUT = {vad: 'Silero VAD: голос или тишина; громкость микрофона', turn: 'Smart Turn: договорил ли по интонации (EndOfTurnState)',
  open: 'Deepgram → мы: открытый кусок, is_final=false, слова ещё поменяются', final: 'Deepgram → мы: закрытый кусок, is_final=true; speech_final — сам услышал паузу; from_finalize — ответ на наш Finalize',
  fin: 'мы → Deepgram: Finalize («верни»), шлём по паузе Silero', early: 'мы → Decisions: «начало продолжения?» — по первым словам, пока Claude работает',
  done: 'мы → Decisions: «мысль закончена?» — на паузе', route: 'мы → Decisions: «продолжает прошлую реплику?» — когда фраза закрыта',
  ask: 'мы → плагин: запрос Claude', cut: 'мы → плагин: cancel — стоп хода; пунктир — где был бы откат без раннего вопроса'};

function at(t) {
  NOW = t;
  document.getElementById('cur').style.left = LEFT + (t - T0) * SCALE + 'px';
  const hot = light(t);
  log(t);
  const phrase = D.phrases.find(p => t >= p.a && t <= p.b), last = D.phrases.filter(p => p.b < t).pop();
  const got = phrase ? D.chunks.filter(c => c.t >= phrase.a && c.t <= t) : [], tail = got[got.length - 1];
  const done = got.filter(c => c.final).map(c => c.text).join(' '), open = tail && !tail.final ? tail.text : '';
  const text = phrase ? `${esc(done)} ${open ? `<span class="open">${esc(open)}</span>` : ''}`.trim() : last ? `<span class="past">${esc(last.text)}</span>` : '';
  document.getElementById('cap').innerHTML = `<b class="t0">${clock(t, true)}</b><span class="text">${text || dim('—')}</span>`;
  document.getElementById('now').innerHTML = TILES.map(([k, glyph, name, value]) => {
    const [v, when] = value(t);
    return `<div class="tile ${hot.has(k) ? 'on' : ''}" title="${esc(ABOUT[k])}"><i>${glyph}</i><span class="nm">${name}</span>` +
      `<span class="val">${v}</span><span class="ago">${when == null ? '' : '−' + num(t - when) + ' с'}</span></div>`; }).join('');
}

// Лог: LOG_N событий вокруг курсора, время — относительно курсора. Выделено последнее уже случившееся — то же, что в легенде;
// будущие — бледнее.
function log(t) {
  const i = Math.max(0, EVENTS.findLastIndex(e => e[0] <= t));
  const from = Math.max(0, Math.min(i - (LOG_N >> 1), EVENTS.length - LOG_N));
  document.getElementById('log').innerHTML = '<div class="hd">события рядом с курсором</div>' + EVENTS.slice(from, from + LOG_N).map(([at, who, what], j) =>
    `<div class="${from + j === i ? 'near' : at > t ? 'fut' : ''}"><span class="dt">${at - t >= 0 ? '+' : '−'}${num(Math.abs(at - t))} с</span><span class="who">${who}</span>${esc(what)}</div>`).join('');
}

// Подсветить отметки, сработавшие рядом с t (в пределах HIT_PX пикселей при текущем масштабе), и их дорожки; вернуть их строки панели.
const HIT_PX = 5;
function light(t) {
  const tol = HIT_PX / SCALE, hot = new Set(), tl = document.getElementById('tl');
  tl.querySelectorAll('.row.hot').forEach(r => r.classList.remove('hot'));
  for (const el of tl.querySelectorAll('[data-k]')) {
    const [a, b] = (el.dataset.span ?? '').split(' ').map(Number);
    const hit = el.dataset.span ? t >= a - tol && t <= b + tol : el.dataset.at.split(' ').some(m => Math.abs(t - m) <= tol);
    el.classList.toggle('hit', hit);
    if (hit) { hot.add(el.dataset.k); el.closest('.row')?.classList.add('hot'); }
  }
  return hot;
}

// Сколько прошло от первого слова фразы до стопа и на сколько позже ход остановил бы откат.
function stopped(c) {
  const phrase = D.phrases.find(p => p.a <= c.t && p.b >= c.t), old = D.asks.find(a => a.rewind === c.id);
  return (phrase ? ` через ${num(c.t - phrase.a)} с` : '') + (old ? dim(' · откат ') + `<span class="red">+${num(old.t - c.t)} с</span>` : '');
}

document.getElementById('head').textContent = `gv · decisions · __NAME__ · ${clock(D.from)}–${clock(D.to)}`;
draw();
addEventListener('resize', draw);
</script>
"""


def main() -> None:
    args, out = sys.argv[1:], None
    if "-o" in args:
        i = args.index("-o")
        out, args = Path(args[i + 1]).expanduser(), args[:i] + args[i + 2:]
    name = args[0] if args else sorted(TRACES.glob("*.jsonl"))[-1].stem
    rows = [json.loads(line) for line in (TRACES / f"{name}.jsonl").read_text().splitlines() if line.strip()]
    data = collect(rows, *span(rows, *(args[1:3] + [None, None])[:2]))
    data["level"] = level(rows, TRACES / f"{name}.wav", data["from"], data["to"])
    command = f"uv run --script ~/.claude/gv/decisions_view.py {name} {clock(data['from'])} {clock(data['to'])}" + (f' -o "{out}"' if out else "")
    page = (PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")).replace("__NAME__", name)
            .replace("__COMMAND__", command))
    target = out or TRACES / f"{name}.decisions.html"
    target.write_text(page)
    print(target)
    if not out:
        subprocess.run(["open", str(target)], check=False)


if __name__ == "__main__":
    main()
