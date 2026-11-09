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
from pathlib import Path

TRACES = Path.home() / ".claude" / "gv-trace"
TAIL_S = 25  # отрезок по умолчанию: столько секунд до последнего запроса
NEAR_S = 0.05  # события одного решения пишутся подряд: ближе этого — следствие
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
.lab{position:absolute;top:5px;font-size:13px;white-space:nowrap;background:#fff;padding:0 3px;z-index:2}.lab.y{font-weight:700;color:#c2410c}
.v{position:absolute;top:22px;bottom:24px;width:0;border-left:2px solid #dc2626;z-index:1}.v.old{border-left:2px dashed #9ca3af}
.vt{position:absolute;top:0;font-size:13px;white-space:nowrap;transform:translateX(-50%);color:#dc2626;font-weight:600}.vt.old{color:#6b7280;font-weight:400;top:11px}
#axis{position:relative;height:24px;margin-left:var(--l);border-top:2px solid #111}
#axis i{position:absolute;top:0;height:6px;border-left:2px solid #111}#axis span{position:absolute;top:7px;font-size:12px;color:#555;transform:translateX(-50%);white-space:nowrap}
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
<p class="note">Кружок с дыркой: открытый кусок текста от Deepgram, он ещё может поменяться; закрашенный: закрытый. Оранжевая полоса: вопрос к Decisions, её длина — время ответа; тёмная — ответ «да». Красная линия: ход Claude остановлен. Серый пунктир: откат, где он остановился бы без ранней проверки. Наведи на любой элемент — покажет текст целиком.</p>
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
  h += row('Звук речи') + D.speech.map(([a, b]) => bar(a, b, 'sp', '', `речь ${num(b - a)} с`)).join('') + '</div>';
  h += row('Текст Deepgram') + D.chunks.map(c => `<div class="dot ${c.final ? 'f' : ''}" title="${esc(c.text)}" style="left:${x(c.t)}px"></div>`).join('') + '</div><div class="row gap"></div>';
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
  tl.innerHTML = h;
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
    command = f"uv run --script ~/.claude/gv/decisions_view.py {name} {clock(data['from'])} {clock(data['to'])}" + (f' -o "{out}"' if out else "")
    height = 500 + 46 * len(data["decisions"]) + 140 * len(data["cuts"])  # рамка в Excalidraw: строки таблицы бывают в две строки
    page = (PAGE.replace("__DATA__", json.dumps(data, ensure_ascii=False).replace("</", "<\\/")).replace("__NAME__", name)
            .replace("__COMMAND__", command).replace("__HEIGHT__", str(height)))
    target = out or TRACES / f"{name}.decisions.html"
    target.write_text(page)
    print(target)
    if not out:
        subprocess.run(["open", str(target)], check=False)


if __name__ == "__main__":
    main()
