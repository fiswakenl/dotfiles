# /// script
# requires-python = ">=3.10"
# ///
"""Отрезок записи запуска GV (~/.claude/gv-trace/<время>.jsonl) → HTML-страница о вопросах к Decisions: когда какой задан,
сколько думал, что решил и что после этого стало с ходом Claude. Данные лежат в странице одним блоком JSON, рисует их скрипт
страницы: другой отрезок или другая запись — та же команда с другими числами.

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
    d = {"from": a, "to": b, "speech": [], "chunks": [], "phrases": [], "decisions": [], "asks": [], "cuts": []}
    began = heard = None  # начало идущего отрезка речи; начало идущей фразы
    for i, r in enumerate(rows):
        ev, ts, text = r["ev"], r["ts"], str(r.get("text") or "")
        if ev == "vad.start":
            began, heard = ts, heard or ts
        elif ev == "vad.stop" and began:
            d["speech"].append([began, ts])
            began = None
        elif ev in ("stt.interim", "stt.final"):
            d["chunks"].append({"t": ts, "text": text, "final": ev == "stt.final"})
        elif ev == "recognized":
            if text:
                d["phrases"].append({"a": heard or ts, "b": ts, "text": text})
            heard = None
        elif ev == "route.early":
            d["decisions"].append({"kind": "early", "t": ts, "d": r["s"], "p": r["p"], "text": text, "last": r["last"],
                                   "yes": bool(after(rows, i, "cut", id=r["last"]))})
        elif ev == "turn" and (m := VERDICT.search(text)) and "Classifier" in text:
            took = after(rows, i, "turn.metrics") or {}
            d["decisions"].append({"kind": "done", "t": ts, "d": took.get("ProcessingMetricsData", 0.2), "p": float(m[2]), "word": m[1],
                                   "text": m[3], "yes": bool(after(rows, i, "recognized"))})
        elif ev == "route":
            said = d["phrases"][-1]["text"] if d["phrases"] else ""
            d["decisions"].append({"kind": "route", "t": ts, "d": r["s"], "p": r["p"], "text": said, "last": r["last"],
                                   "pause": r.get("pause"), "yes": bool(after(rows, i, "ask", rewind=r["last"]))})
        elif ev in ("ask", "quick"):
            d["asks"].append({"t": ts, "id": r["id"], "kind": ev, "text": text, "rewind": r.get("rewind")})
        elif ev == "cut":
            d["cuts"].append({"t": ts, "id": r["id"]})
    return d


PAGE = r"""<!doctype html><meta charset="utf-8"><meta name="viewport" content="width=device-width,initial-scale=1"><meta name="frame" content="1180x__HEIGHT__">
<title>GV · Decisions · __NAME__</title>
<style>
*{box-sizing:border-box}html,body{margin:0;background:#fff;color:#111;font:16px/1.45 -apple-system,"Segoe UI",Roboto,sans-serif}
.w{padding:22px 26px;--l:150px}h1{font-size:25px;margin:0 0 4px}h2{font-size:18.5px;margin:24px 0 6px}p{margin:4px 0}
.sub,.note{color:#555;font-size:15px}code{background:#f3f4f6;padding:1px 5px;border-radius:4px;font-size:13px}
#tl{position:relative;margin-top:12px;padding-top:24px}
.row{position:relative;height:30px;margin-left:var(--l);border-bottom:1px solid #f0f0f0}.row.gap{height:10px;border:0}
.row>b{position:absolute;left:calc(-1*var(--l));top:5px;width:calc(var(--l) - 12px);font-size:14px;font-weight:600;text-align:right}
.bar{position:absolute;top:6px;height:18px;border-radius:4px;overflow:hidden;white-space:nowrap;font-size:12.5px;line-height:17px;padding:0 5px}
.ph{background:#f1f5f9;border:1px solid #94a3b8}.sp{background:#dbeafe;border:1px solid #2563eb}.cl{background:#dcfce7;border:1px solid #16a34a}
.cl.q{background:#ecfccb;border-color:#65a30d}.no{background:#fed7aa;border:1px solid #ea580c}.yes{background:#ea580c;border:1px solid #c2410c}
.dot{position:absolute;top:9px;width:11px;height:11px;border-radius:50%;margin-left:-5px;background:#fff;border:2px solid #2563eb}.dot.f{background:#2563eb}
.row.snd{height:56px}.row.snd>b{top:18px}.row.snd svg{position:absolute;left:0;top:3px;height:50px}
.vad{position:absolute;top:3px;height:50px;background:#dbeafe;border-radius:3px}
.row.txt>b{top:5px}.ck{position:absolute;font-size:12.5px;line-height:16px;white-space:nowrap;padding-left:5px;border-left:2px solid #2563eb;color:#6b7280;font-style:italic}
.ck.f{color:#111;font-style:normal;font-weight:600;border-left-width:3px}
.ck.r{padding:0 5px 0 0;border-left:0;border-right:2px solid #2563eb}.ck.f.r{border-right-width:3px}
.lab{position:absolute;top:5px;font-size:13px;white-space:nowrap;background:#fff;padding:0 3px;z-index:2}.lab.y{font-weight:700;color:#c2410c}
.v{position:absolute;top:22px;bottom:24px;width:0;border-left:2px solid #dc2626;z-index:1}.v.old{border-left:2px dashed #9ca3af}
.vt{position:absolute;top:0;font-size:13px;white-space:nowrap;transform:translateX(-50%);color:#dc2626;font-weight:600}.vt.old{color:#6b7280;font-weight:400;top:11px}
#axis{position:relative;height:24px;margin-left:var(--l);border-top:2px solid #111}
#axis i{position:absolute;top:0;height:6px;border-left:2px solid #111}#axis span{position:absolute;top:7px;font-size:12px;color:#555;transform:translateX(-50%);white-space:nowrap}
.lg{display:grid;grid-template-columns:46px 1fr 46px 1fr;gap:5px 10px;align-items:center;margin:10px 0 0;padding:10px 12px;background:#fafafa;border:1px solid #eee;border-radius:6px;font-size:14px}
.lg i{position:relative;height:22px;font-style:normal}.lg u{text-decoration:none}.lg .bar{left:0;top:2px;width:42px}.lg .dot{left:21px;top:5px}
.lg .wv{position:absolute;left:6px;top:7px;width:30px;height:8px;background:repeating-linear-gradient(90deg,#1d4ed8 0 2px,transparent 2px 4px)}
.lg .ln{position:absolute;left:20px;top:0;height:22px;border-left:2px solid #dc2626}.lg .ln.old{border-left:2px dashed #9ca3af}
#cur{position:absolute;top:22px;bottom:24px;width:0;border-left:1.5px solid #111;pointer-events:none;z-index:3;display:none}
#now{margin:8px 0 0;padding:9px 12px;min-height:96px;border:1px solid #d1d5db;border-radius:6px;font-size:14.5px;display:grid;grid-template-columns:150px 1fr;gap:3px 10px;align-content:start}
#now>b{font-weight:600;color:#555;text-align:right}#now .open{color:#6b7280;font-style:italic}#now .done{font-weight:600}#now .hint{grid-column:1/3;color:#6b7280}
.tag{display:inline-block;font-size:12px;font-style:normal;font-weight:600;border-radius:4px;padding:0 6px;margin-left:6px;vertical-align:1px}
.tag.o{background:#e0e7ff;color:#3730a3}.tag.c{background:#111;color:#fff}.tag.w{background:#f3f4f6;color:#6b7280}
.or{color:#c2410c;font-weight:600}.red{color:#dc2626;font-weight:600}.gy{color:#6b7280}
table{border-collapse:collapse;font-size:14.5px;width:100%}td,th{text-align:left;padding:3px 10px 3px 0;vertical-align:top;border-bottom:1px solid #f0f0f0}
th{font-size:13px;color:#555;font-weight:600}td.n{white-space:nowrap;font-variant-numeric:tabular-nums}
.sum{display:flex;height:30px;margin:6px 0 2px;border-radius:5px;overflow:hidden;font-size:12.5px;line-height:30px;text-align:center;white-space:nowrap}
.sum div{border-right:2px solid #fff;overflow:hidden}.wt{background:#e5e7eb}.sum .yes{color:#fff;border:0;border-right:2px solid #fff}.sum .no{border:0;border-right:2px solid #fff}
</style>
<div class="w">
<h1>Когда срабатывает Decisions</h1>
<p class="sub" id="sub"></p>
<div id="tl"></div>
<div id="now"></div>
<div class="lg">
 <i><u class="bar ph"></u></i><span><b>Фраза</b>: одна реплика целиком, от первого слова до момента, когда она закрыта</span>
 <i><u class="bar no"></u></i><span><b>Вопрос к Decisions, ответ «нет»</b>: длина полосы — сколько думал, число рядом — вероятность «да»</span>
 <i><u class="bar sp" style="background:#dbeafe;border:0"></u><u class="wv"></u></i><span><b>Звук речи</b>: тёмные столбики — громкость микрофона, светлый фон — где Silero слышит голос</span>
 <i><u class="bar yes"></u></i><span><b>Вопрос к Decisions, ответ «да»</b>: число рядом жирное</span>
 <i><u class="ck" style="left:2px;top:3px">текст</u></i><span><b>Открытый кусок текста</b> от Deepgram стоит там, где пришёл; слова ещё могут поменяться; «+» — добавка к прошлому куску</span>
 <i><u class="bar cl">№1</u></i><span><b>Claude работает</b> над запросом с этим номером</span>
 <i><u class="ck f" style="left:2px;top:3px">текст</u></i><span><b>Закрытый кусок текста</b>: слова окончательные</span>
 <i><u class="ln"></u></i><span><b>Стоп</b>: ход Claude остановлен ранним вопросом, фраза ещё звучит</span>
 <i><u class="ln" style="border-color:#111"></u></i><span><b>Курсор</b>: води мышкой по шкале — панель под ней покажет состояние на этот момент</span>
 <i><u class="ln old"></u></i><span><b>Откат</b>: здесь ход остановился бы без раннего вопроса</span>
</div>
<h2>Все вопросы к Decisions по порядку</h2>
<table id="calls"></table>
<div id="cuts"></div>
<p class="note" style="margin-top:18px">Страница собрана из записи <code>__NAME__</code>. Обновить или взять другой отрезок: <code>__COMMAND__</code></p>
</div>
<script type="application/json" id="data">__DATA__</script>
<script>
const D = JSON.parse(document.getElementById('data').textContent), T0 = D.from, LEN = D.to - D.from;
const NAME = {early: '1. Ранний', done: '2. Конец фразы', route: '3. Маршрут'};
const ASK = {early: 'это начало продолжения?', done: 'мысль закончена?', route: 'продолжает прошлую реплику?'};
const WORD = {complete: 'закончена', short: 'короткая', incomplete: 'не закончена'};
const num = (x, n = 2) => x.toFixed(n).replace('.', ',');
const clock = (t, frac) => { const d = new Date(t * 1000), p = n => String(n).padStart(2, '0');
  return `${p(d.getHours())}:${p(d.getMinutes())}:${p(d.getSeconds())}` + (frac ? ',' + p(Math.floor(t % 1 * 100)) : ''); };
const esc = s => String(s).replace(/[&<>"]/g, c => ({'&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;'}[c]));
const outcome = c => c.kind === 'early' ? (c.yes ? `да: стоп запросу №${c.last}` : 'нет: ждём следующий кусок')
  : c.kind === 'done' ? (c.yes ? 'да: фраза закрыта' : 'нет: ждём продолжения')
  : (c.yes ? `да: откат №${c.last} и склеенный текст` : 'нет: новая мысль');

function draw() {
  const tl = document.getElementById('tl'), W = tl.clientWidth - 150, S = W / LEN, x = t => (t - T0) * S;
  const row = (name, cls = '') => `<div class="row"><b class="${cls}">${name}</b>`;
  const bar = (a, b, cls, text = '', tip = text) => `<div class="bar ${cls}" title="${esc(tip)}" style="left:${x(a)}px;width:${Math.max(6, x(b) - x(a))}px">${esc(text)}</div>`;
  let h = row('Фраза') + D.phrases.map(p => bar(p.a, p.b, 'ph', p.text)).join('') + '</div>';
  // Звук: отрезки речи по Silero фоном, поверх — громкость микрофона зеркальной гистограммой.
  const L = D.level, bars = L ? L.v.map((v, i) => { const hh = Math.max(0.6, Math.sqrt(v / 100) * 24);
    return `<rect x="${x(L.from + i * L.step).toFixed(1)}" y="${(25 - hh).toFixed(1)}" width="${Math.max(1, L.step * S - 0.6).toFixed(1)}" height="${(hh * 2).toFixed(1)}"/>`; }).join('') : '';
  h += '<div class="row snd"><b>Звук речи</b>' + D.speech.map(([a, b]) => `<div class="vad" title="речь ${num(b - a)} с" style="left:${x(a)}px;width:${x(b) - x(a)}px"></div>`).join('') +
    `<svg width="${W}" height="50" fill="#1d4ed8">${bars}</svg></div>`;
  // Текст: каждый кусок стоит там, где он пришёл; подпись — только новое в нём. Строк столько, чтобы подписи не налезали.
  let prev = null; const ends = [];
  // Открытый кусок, который тут же (быстрее 0,2 с) закрылся тем же текстом, отдельной подписью не нужен.
  const cks = D.chunks.filter((c, i, all) => c.final || !(all[i + 1]?.final && all[i + 1].t - c.t < 0.2 && all[i + 1].text.startsWith(c.text))).map(c => {
    const add = prev && !prev.final && c.text.startsWith(prev.text) ? c.text.slice(prev.text.length).trim() : null, text = add ? '+ ' + add : c.text;
    prev = c;
    const left = x(c.t), width = text.length * 6.9 + 14;
    const back = left + width > W;  // у правого края подпись уходит влево от своей отметки
    let r = ends.findIndex(e => e <= (back ? left - width : left));
    if (r < 0) r = ends.length;
    ends[r] = back ? left : left + width;
    return `<span class="ck ${c.final ? 'f' : ''} ${back ? 'r' : ''}" title="${clock(c.t, true)} ${c.final ? 'закрытый' : 'открытый'}: ${esc(c.text)}" ` +
      `style="${back ? `right:${W - left}px` : `left:${left}px`};top:${4 + r * 18}px">${esc(text)}</span>`; });
  h += `<div class="row txt" style="height:${ends.length * 18 + 8}px"><b>Текст Deepgram</b>${cks.join('')}</div><div class="row gap"></div>`;
  for (const kind of ['early', 'done', 'route'])
    h += row(NAME[kind], 'or') + D.decisions.filter(c => c.kind === kind).map((c, i, all) =>
      bar(c.t - c.d, c.t, c.yes ? 'yes' : 'no', '', `«${c.text}» → ${num(c.p)}, думал ${num(c.d)} с. ${outcome(c)}`) +
      // подпись справа от полосы; следующий вопрос слишком близко — слева, чтобы не закрыть его полосу
      (all[i + 1] && x(all[i + 1].t - all[i + 1].d) - x(c.t) < 44
        ? `<span class="lab ${c.yes ? 'y' : ''}" style="right:${W - x(c.t - c.d) + 4}px">${num(c.p)}</span>`
        : `<span class="lab ${c.yes ? 'y' : ''}" style="left:${x(c.t) + 4}px">${num(c.p)}</span>`)).join('') + '</div>';
  h += '<div class="row gap"></div>' + row('Claude') + D.asks.map(a => {
    const cut = D.cuts.find(c => c.id === a.id), next = D.asks.find(n => n.rewind === a.id), end = Math.min(cut?.t ?? D.to, next?.t ?? D.to, D.to);
    return bar(a.t, end, 'cl' + (a.kind === 'quick' ? ' q' : ''), `№${a.id}`, `${a.kind} №${a.id}${a.rewind ? `, откат №${a.rewind}` : ''}: ${a.text}`); }).join('') + '</div>';
  const step = LEN > 40 ? 5 : LEN > 16 ? 2 : 1;
  let ax = '';
  for (let t = Math.ceil(T0 / step) * step; t <= D.to; t += step) ax += `<i style="left:${x(t)}px"></i><span style="left:${x(t)}px">${clock(t).slice(3)}</span>`;
  h += `<div id="axis">${ax}</div>`;
  for (const c of D.cuts) {
    h += `<div class="v" style="left:${150 + x(c.t)}px"></div><div class="vt" style="left:${150 + x(c.t)}px">стоп №${c.id}</div>`;
    const old = D.asks.find(a => a.rewind === c.id);
    if (old) h += `<div class="v old" style="left:${150 + x(old.t)}px"></div><div class="vt old" style="left:${150 + x(old.t)}px">откат</div>`;
  }
  tl.innerHTML = h + '<div id="cur"></div>';
  SCALE = S;
  tl.onmousemove = e => { const px = e.clientX - tl.getBoundingClientRect().left - 150; px >= 0 && px <= W ? at(T0 + px / S) : away(); };
  tl.onmouseleave = away;
  HOVER == null ? away() : at(HOVER);  // шкалу перерисовали (окно изменило размер) — курсор остаётся на своём моменте
}

// Состояние на момент t, как на паузе: что уже собрано из текста и закрыто ли оно, говорит ли человек, чем занят Claude.
let SCALE = 1, HOVER = null;  // пикселей в секунде; момент под курсором
function at(t) {
  const cur = document.getElementById('cur');
  HOVER = t;
  cur.style.display = 'block';
  cur.style.left = 150 + (t - T0) * SCALE + 'px';
  const phrase = D.phrases.find(p => t >= p.a && t <= p.b), last = D.phrases.filter(p => p.b < t).pop();
  const got = phrase ? D.chunks.filter(c => c.t >= phrase.a && c.t <= t) : [], tail = got[got.length - 1];
  const done = got.filter(c => c.final).map(c => c.text).join(' '), open = tail && !tail.final ? tail.text : '';
  const text = phrase
    ? (done || open ? `<span class="done">${esc(done)}</span> <span class="open">${esc(open)}</span>` +
        (open ? '<span class="tag o">открыто: слова ещё могут поменяться</span>' : '<span class="tag c">коммит: слова окончательные</span>')
      : '<span class="gy">речь идёт, текста от Deepgram ещё нет</span>')
    : last ? `<span class="done">${esc(last.text)}</span><span class="tag w">фраза закрыта ${num(t - last.b, 1)} с назад</span>` : '<span class="gy">ещё ничего не сказано</span>';
  const speaking = D.speech.some(([a, b]) => t >= a && t <= b), quiet = D.speech.filter(s => s[1] < t).pop();
  const ask = D.asks.filter(a => a.t <= t).pop(), cut = ask && D.cuts.find(c => c.id === ask.id && c.t <= t);
  const call = D.decisions.filter(c => c.t <= t).pop(), going = D.decisions.find(c => c.t - c.d <= t && c.t > t);
  document.getElementById('now').innerHTML =
    `<b>Момент</b><span>${clock(t, true)}, ${num(t - T0, 1)} с от начала отрезка</span>` +
    `<b>Собрано</b><span>${text}</span>` +
    `<b>Ты</b><span>${speaking ? 'говоришь' : quiet ? `молчишь ${num(t - quiet[1], 1)} с` : 'молчишь'}</span>` +
    `<b>Claude</b><span>${!ask ? 'свободен' : cut ? `остановлен на запросе №${ask.id}, ждёт конца фразы` : `работает над запросом №${ask.id}`}</span>` +
    `<b>Decisions</b><span>${going ? `думает: ${NAME[going.kind]}, «${esc(going.text)}»` : call ? `последний ответ: ${NAME[call.kind]} ${num(call.p)}, ${outcome(call)}` : 'вопросов ещё не было'}</span>`;
}

function away() {
  HOVER = null;
  document.getElementById('cur').style.display = 'none';
  document.getElementById('now').innerHTML = '<span class="hint">Води мышкой по шкале: здесь будет состояние на этот момент — какой текст уже собран, открыт он или закрыт, что делает Claude.</span>';
}

document.getElementById('sub').textContent = `Отрезок ${clock(D.from)}–${clock(D.to)}, ${Math.round(LEN)} с. ` +
  `Фраз: ${D.phrases.length}, вопросов к Decisions: ${D.decisions.length}, остановок хода: ${D.cuts.length}.`;
document.getElementById('calls').innerHTML = '<tr><th>когда</th><th>вопрос</th><th>что видел</th><th>ответ</th><th>думал</th><th>что решил</th></tr>' +
  D.decisions.map(c => `<tr><td class="n">${clock(c.t - c.d, true)}</td><td class="or">${NAME[c.kind]}: ${ASK[c.kind]}</td><td>«${esc(c.text)}»` +
    (c.pause != null ? ` <span class="gy">пауза перед ней ${num(c.pause, 1)} с</span>` : '') + `</td><td class="n">${num(c.p)}` +
    (c.word ? ` <span class="gy">${WORD[c.word] ?? c.word}</span>` : '') + `</td><td class="n">${num(c.d)} с</td><td>${outcome(c)}</td></tr>`).join('');
document.getElementById('cuts').innerHTML = D.cuts.map(c => {
  const phrase = D.phrases.find(p => p.b >= c.t) ?? {a: Math.max(...D.speech.map(s => s[0]).filter(a => a <= c.t), D.from)};
  const calls = D.decisions.filter(k => k.kind === 'early' && k.last === c.id && k.t <= c.t + 0.05 && k.t >= phrase.a), total = c.t - phrase.a;
  let at = phrase.a, parts = '';
  for (const k of calls) {
    const wait = k.t - k.d - at;
    if (wait > 0.01) parts += `<div class="wt" style="width:${wait / total * 100}%" title="ждём текст ${num(wait)} с">ждём текст ${num(wait)} с</div>`;
    parts += `<div class="${k.yes ? 'yes' : 'no'}" style="width:${k.d / total * 100}%" title="вопрос ${num(k.d)} с: ${num(k.p)}">${num(k.d)} с: ${k.yes ? 'да' : 'нет'}</div>`;
    at = k.t;
  }
  const old = D.asks.find(a => a.rewind === c.id), asked = calls.reduce((s, k) => s + k.d, 0);
  return `<h2>Стоп №${c.id}: ${num(total)} с от первого слова</h2><div class="sum">${parts}</div>` +
    `<p class="note">На вопросы ушло ${num(asked)} с, на ожидание текста от Deepgram ${num(total - asked)} с.` +
    (old ? ` Без ранней проверки ход остановил бы откат: на <span class="red">${num(old.t - c.t)} с позже</span>.` : '') + '</p>'; }).join('');
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
    height = 880 + 46 * len(data["decisions"]) + 140 * len(data["cuts"])  # рамка в Excalidraw: строки таблицы бывают в две строки
    page = (PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")).replace("__NAME__", name)
            .replace("__COMMAND__", command).replace("__HEIGHT__", str(height)))
    target = out or TRACES / f"{name}.decisions.html"
    target.write_text(page)
    print(target)
    if not out:
        subprocess.run(["open", str(target)], check=False)


if __name__ == "__main__":
    main()
