<script>
  // Как три вопроса к Decisions делят записанную диктовку. Слева выбираешь вопрос — справа в тексте видно, где он сработал и что
  // ответил. Три вида текста одного отрезка — друг под другом; правильное деление размечаешь сам (клик по паузе → «тут новое
  // сообщение»), и каждый вариант промпта считается по нему.
  const KINDS = { punct: 'как есть', plain: 'без знаков', socket: 'звук заново' }
  const HINT = { punct: 'текст Deepgram, как пришёл вживую', plain: 'тот же текст без знаков и заглавных', socket: 'звук отрезка заново через Deepgram (как сейчас в listen.py)' }
  const TURN = { complete: 'закончена', short: 'не закончена', long: 'подождать' }
  // join — модель считает, что сообщение то же (⌒); split — что тут граница (┃)
  const QS = {
    turn: { name: 'Мысль закончена?', when: 'замолчал', join: 'ждёт', split: 'закрыл фразу', extra: 'закрыл посреди сообщения', miss: 'не закрыл, хотя сообщение кончилось',
      hint: 'Deepgram услышал конец речи. Модель решает: фраза закончена — закрыть её, или человек договорит — подождать.' },
    route: { name: 'Продолжение?', when: 'фраза закрыта', join: 'склеил', split: 'новое сообщение', extra: 'разорвал одно сообщение', miss: 'склеил два разных сообщения',
      hint: 'Фраза закрыта. Модель решает: дописать её к прошлому сообщению или отправить отдельным.' },
    early: { name: 'Начало продолжения?', when: 'Claude занят', join: 'остановил Claude', split: 'не тронул', extra: 'не остановил, хотя это продолжение', miss: 'остановил, хотя это новое сообщение',
      hint: 'Claude работает над прошлой фразой, а человек заговорил снова. По первым словам модель решает: это продолжение — остановить Claude сразу, или новая мысль — не трогать.' },
  }
  const NEAR = 0.5 // метка разметки относится к паузе, если лежит в ней или рядом (секунды)

  let cfg = $state(null)
  let rec = $state('20261110-103304'), from = $state('10:36:20'), to = $state('10:37:20')
  let input = $state('строками')
  let qn = $state('route') // выбранный вопрос
  let vs = $state({ turn: 0, route: 0, early: 0 }) // выбранный вариант промпта у каждого вопроса
  let results = $state({}) // ключ → {chunks, turns, ends, steps, early} | {error}
  let cuts = $state([]) // разметка: секунды от начала отрезка, где начинается новое сообщение
  let sel = $state(null) // {kind, i} — выбранная пауза: после куска i
  let busy = $state(''), dirty = $state(false)
  let player = $state()
  const asked = {} // прогоны, которые уже идут

  // прогон зависит от варианта каждого из трёх вопросов: у вопроса q берём вариант v, у остальных — выбранные
  const pick = (q, v) => ({ ...vs, [q]: v })
  const key = (k, q = qn, v = vs[q]) => { const t = pick(q, v); return `${rec}|${from}|${to}|${k}|${t.turn}|${t.route}|${t.early}|${input}` }
  const f = (x) => (x == null ? '—' : x.toFixed(2).replace('.', ','))
  const sec = (x) => (x == null ? '' : Math.max(0, x).toFixed(1).replace('.', ',') + ' с')
  const glue = (p) => p != null && p >= cfg.sure
  const qs = (o) => new URLSearchParams(o).toString()
  const top = (asks) => asks.reduce((m, a) => ((a.p ?? 0) >= (m.p ?? 0) ? a : m), asks[0])

  // что вопрос q ответил на паузе после куска i: asked — спрашивали ли, split — решил, что тут граница
  function answer(r, i, q = qn) {
    if (q === 'turn') {
      const t = r.turns[i], closed = r.ends.includes(i)
      if (!t && !closed) return { asked: false }
      const timer = closed && t?.v !== 'complete'
      return { asked: true, split: closed, p: t?.p, timer, input: t?.input,
        text: (t ? `${TURN[t.v]} ${f(t.p)}` : 'Deepgram паузы не услышал') + (timer ? ` · закрыл таймер ${sec(cfg.pause_ms / 1000)}` : '') }
    }
    if (q === 'route') {
      const s = r.steps.find((x) => x.at === i)
      return s ? { asked: true, split: !glue(s.p), p: s.p, input: s.input, text: f(s.p) } : { asked: false }
    }
    const e = r.early.find((x) => x.at === i)
    if (!e) return { asked: false }
    const best = top(e.asks)
    return { asked: true, split: !glue(best.p), p: best.p, input: best.input, asks: e.asks, text: `${f(best.p)} на «${best.text}»` }
  }
  // пауза после куска i: где она в отрезке, что ответил вопрос и совпало ли с разметкой
  function gap(r, i, q = qn) {
    const a = r.chunks[i], b = r.chunks[i + 1]
    if (!b) return null
    const truth = cuts.some((t) => t >= a.end - NEAR && t <= b.start + NEAR)
    const ans = answer(r, i, q)
    return { from: a.end, to: b.start, len: a.gap, truth, ...ans, wrong: cfg.labeled && (ans.asked ? ans.split !== truth : truth) }
  }
  // вариант делит этот вид текста ровно по разметке?
  function score(r, q = qn) {
    if (!r?.chunks || !cfg.labeled) return null
    return r.chunks.every((_, i) => !gap(r, i, q)?.wrong)
  }
  const total = (q, v) => Object.keys(KINDS).filter((k) => score(results[key(k, q, v)], q)).length

  async function api(path, body) {
    const r = await fetch('/api/' + path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {})
    return r.json()
  }
  // досчитать всё, что нужно экрану: каждый вариант каждого вопроса на каждом виде текста
  async function fill() {
    const jobs = []
    for (const k of Object.keys(KINDS)) for (const q of Object.keys(QS)) cfg.prompts[q].forEach((_, v) => {
      const id = key(k, q, v), t = pick(q, v)
      if (results[id] || asked[id]) return
      const prompts = Object.fromEntries(Object.keys(QS).map((x) => [x, cfg.prompts[x][t[x]].text]))
      jobs.push((asked[id] = api('run', { rec, from, to, kind: k, prompts, input }).then((r) => { results[id] = r; delete asked[id] })))
    })
    if (!jobs.length) return
    busy = 'гоняю Decisions…'
    await Promise.all(jobs)
    busy = ''
  }
  async function load() {
    sel = null
    results = {}
    cuts = await api('labels?' + qs({ rec, from, to }))
    cfg.labeled = cuts.length > 0
    await fill()
  }
  async function run() {
    await api('prompts', cfg.prompts)
    dirty = false
    results = {}
    await fill()
  }
  const choose = (v, q = qn) => { vs[q] = v; fill() }
  const setInput = (i) => { input = i; fill() }
  async function replay() {
    busy = 'звук заново через Deepgram, ~минута…'
    await api('replay', { rec, from, to })
    results = {}
    await fill()
  }
  function add() {
    const list = cfg.prompts[qn]
    list.push({ name: 'вариант ' + list.length, text: list[vs[qn]].text })
    vs[qn] = list.length - 1
    dirty = true
  }
  async function drop() {
    if (!vs[qn]) return
    cfg.prompts[qn].splice(vs[qn], 1)
    vs[qn] -= 1
    await run()
  }
  async function toggleTruth(g) {
    const near = cuts.filter((t) => t >= g.from - NEAR && t <= g.to + NEAR)
    cuts = near.length ? cuts.filter((t) => !near.includes(t)) : [...cuts, Math.round(((g.from + g.to) / 2) * 100) / 100]
    cfg.labeled = cuts.length > 0
    await api('labels', { rec, from, to, cuts })
  }
  let stopAt = null
  function play(t0, t1) {
    player.currentTime = Math.max(0, t0)
    player.play()
    clearTimeout(stopAt)
    if (t1) stopAt = setTimeout(() => player.pause(), (t1 - t0) * 1000)
  }
  const keys = (e) => { if (e.key === 'Enter' && (e.metaKey || e.ctrlKey)) { e.preventDefault(); run() } }

  $effect(() => { api('config').then((c) => { cfg = c; load() }) })

  let Q = $derived(QS[qn])
  let list = $derived(cfg?.prompts[qn])
  let cur = $derived(list?.[vs[qn]])
  let selR = $derived(sel && results[key(sel.kind)])
  let selG = $derived(selR?.chunks && gap(selR, sel.i))
</script>

{#if cfg}
  <header>
    <h1>GV devtools</h1>
    <select bind:value={rec}>{#each cfg.recs as r}<option>{r}</option>{/each}</select>
    <input bind:value={from} size="7" /><span class="dim">–</span><input bind:value={to} size="7" />
    <button onclick={load}>Загрузить</button>
    <span class="status">{busy || (dirty ? 'Несохранённые правки' : '')}</span>
    <span class="grow"></span>
    <span class="help" tabindex="0">?<div class="legend">
        <span><i class="gap">0,7 с</i> не спрашивали</span>
        <span><i class="gap glued">⌒ 0,7 с</i> {Q.join}</span>
        <span><i class="gap cut good">┃ 0,7 с</i> {Q.split} — верно</span>
        <span><i class="gap cut wrong">┃ 0,7 с</i> {Q.extra}</span>
        <span><i class="gap wrong">⌒ 0,7 с</i> {Q.miss}</span>
      </div></span>
    <button class="ghost" onclick={() => play(0)}>▶ Отрезок</button>
    <button class="primary" onclick={run}>Прогнать ⌘↵</button>
    <audio bind:this={player} src={'/api/audio?' + qs({ rec, from, to })} preload="auto"></audio>
  </header>

  <div class="grid">
    <aside>
      <div class="pane">
        <div class="pane-h"><h2 title="Три момента, когда GV спрашивает модель. Выбери — справа в тексте видно, где вопрос сработал и что ответил.">Вопрос к модели</h2></div>
        {#each Object.entries(QS) as [q, m], n}
          <button class="item" class:on={q === qn} onclick={() => (qn = q)} title={m.hint}>
            <span><b class="num">{n + 1}</b>{m.name}</span><span class="when">{m.when}</span>
          </button>
        {/each}
      </div>

      <div class="pane">
        <div class="pane-h">
          <h2 title={Q.hint}>Варианты промпта</h2>
          <button class="mini" onclick={add} title="Новый вариант на основе выбранного">+ новый</button>
        </div>
        {#each list as p, v}
          <button class="item" class:on={v === vs[qn]} onclick={() => choose(v)}>
            <span>{p.name}</span>
            {#if cfg.labeled}<span class="cnt" class:good={total(qn, v) === 3} title="Совпал с разметкой в {total(qn, v)} из 3 текстов справа">{total(qn, v)}/3</span>{/if}
          </button>
        {/each}
      </div>

      <div class="pane">
        {#if vs[qn]}
          <div class="pane-h"><h2>Промпт</h2><button class="mini danger" onclick={drop}>удалить</button></div>
          <input class="name" bind:value={cur.name} oninput={() => (dirty = true)} placeholder="Название варианта" />
          <textarea bind:value={cur.text} oninput={() => (dirty = true)} onkeydown={keys} rows="7" placeholder="Текст промпта"></textarea>
        {:else}
          <div class="pane-h"><h2 title="Этот промпт сейчас стоит в listen.py. Чтобы менять — «+ новый» сделает копию.">Промпт — как в gv</h2><button class="mini" onclick={add}>править копию</button></div>
          <textarea value={cur.text} readonly rows="7"></textarea>
        {/if}
        {#if qn === 'turn'}
          <details><summary class="dim small">Варианты ответа</summary><pre>{Object.entries(cfg.turn_options).map(([k, v]) => `${TURN[k]} — ${v}`).join('\n\n')}</pre></details>
        {:else}
          <div class="kv"><span>Реплики подаются</span>
            <div class="seg">{#each cfg.inputs as i}<button class:on={input === i} onclick={() => setInput(i)}>{i}</button>{/each}</div>
          </div>
        {/if}
      </div>
    </aside>

    <main>
      {#if !cfg.labeled}
        <div class="tip">Сначала разметь, где начинается новое сообщение: клик по паузе → «тут новое сообщение».</div>
      {/if}
      {#each Object.entries(KINDS) as [k, name]}
        {@const r = results[key(k)]}
        {@const s = score(r)}
        <section>
          <div class="sec-h">
            <h2 title={HINT[k]}>{name}</h2>
            {#if s != null}<span class="mark" class:good={s}>{s ? '✓' : '✗'}</span>{/if}
          </div>
          {#if r?.error}
            <button onclick={replay}>Прогнать звук через Deepgram (платно, ~минута)</button>
          {:else if r?.chunks}
            <p class="msg">
              {#each r.chunks as c, i}
                {@const g = gap(r, i)}
                {c.text}{#if g}<button class="gap" class:glued={g.asked && !g.split} class:cut={g.split} class:good={g.split && cfg.labeled && !g.wrong} class:wrong={g.wrong}
                    class:sel={sel?.kind === k && sel?.i === i} onclick={() => (sel = { kind: k, i })}>{g.split ? '┃ ' : g.asked ? '⌒ ' : ''}{sec(g.len)}</button>{/if}
              {/each}
            </p>
          {:else}
            <span class="dim">…</span>
          {/if}
        </section>
      {/each}

      {#if selG}
        <div class="drawer">
          <div class="sec-h">
            <h2>Пауза {sec(selG.len)}</h2><span class="dim">{KINDS[sel.kind]} · {Q.name}</span><span class="grow"></span>
            <label class="check"><input type="checkbox" checked={selG.truth} onchange={() => toggleTruth(selG)} /> тут новое сообщение</label>
            <button onclick={() => play(selG.from - 2, selG.to + 2)}>▶ Послушать</button>
            <button class="ghost" onclick={() => (sel = null)} title="Закрыть">✕</button>
          </div>
          {#if selG.asked}
            <table>
              <tbody>
                {#each list as p, v}
                  {@const rv = results[key(sel.kind, qn, v)]}
                  {@const a = rv?.chunks ? answer(rv, sel.i) : null}
                  <tr class:on={v === vs[qn]} onclick={() => choose(v)}>
                    <td>{p.name}</td>
                    <td class="bar"><i style="width:{(a?.p ?? 0) * 100}%" class:glue={a?.asked && !a.split}></i>{#if qn !== 'turn'}<b style="left:{cfg.sure * 100}%"></b>{/if}</td>
                    <td class="dim">{#if a?.asked}{a.split ? '┃ ' + Q.split : '⌒ ' + Q.join} · {a.text}{:else if a}не спрашивали{/if}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <details><summary class="dim small">Что ушло модели — «{cur.name}»</summary>
              {#if selG.asks}<pre>{selG.asks.map((a) => `${f(a.p)}  ${a.text}`).join('\n')}</pre>{/if}
              <pre>{selG.input ?? 'Модель не спрашивали: фразу закрыл таймер.'}</pre>
            </details>
          {:else if qn === 'turn'}
            <div class="dim">Deepgram тут конца речи не услышал — вопрос не задавали, фраза идёт дальше.</div>
          {:else}
            {@const t = answer(selR, sel.i, 'turn')}
            <div class="dim">Фраза тут не закрылась{t.asked ? ` («мысль закончена?»: ${t.text})` : ' (Deepgram конца речи не услышал)'}, поэтому этот вопрос не задавали.</div>
          {/if}
        </div>
      {/if}
    </main>
  </div>
{/if}

<style>
  :global(:root) { color-scheme: dark; --bg: #212121; --deep: #171717; --hover: #2f2f2f; --chip: #333; --line: rgba(255, 255, 255, .1); --line2: rgba(255, 255, 255, .2);
    --text: #f3f3f3; --dim: #afafaf; --blue: #0285ff; --green: #1a9b63; --red: #e5484d; --orange: #d9822b }
  :global(body) { font: 14px/1.5 ui-sans-serif, -apple-system, 'Helvetica Neue', system-ui, sans-serif; color: var(--text); background: var(--bg); margin: 0; -webkit-font-smoothing: antialiased }
  header { display: flex; gap: 8px; align-items: center; height: 52px; box-sizing: border-box; padding: 0 20px 0 24px; border-bottom: 1px solid var(--line); position: sticky; top: 0; background: var(--bg); z-index: 2 }
  h1 { font-size: 18px; font-weight: 600; margin: 0 8px 0 0 }
  h2 { font-size: 14px; font-weight: 600; margin: 0 }
  .grow { flex: 1 }
  .dim { color: var(--dim) }
  .small { font-size: 12px }
  .status { color: var(--dim); font-size: 12px; margin-left: 6px }

  button, input, select, textarea { font: inherit; color: var(--text); background: transparent; border: 1px solid var(--line2); border-radius: 8px; padding: 5px 12px; box-sizing: border-box }
  button { cursor: pointer; font-weight: 500 }
  button:hover { background: var(--hover) }
  input:focus, select:focus, textarea:focus { outline: none; border-color: var(--text) }
  header input { padding: 5px 8px; text-align: center }
  .ghost { border-color: transparent }
  .primary, .primary:hover { background: #fff; color: #0d0d0d; border-color: #fff; font-weight: 600 }
  .primary:hover { opacity: .88 }
  .danger { color: #ff8a8a }

  .grid { display: grid; grid-template-columns: 340px 1fr }
  aside { position: sticky; top: 52px; height: calc(100vh - 52px); overflow: auto; border-right: 1px solid var(--line) }
  .pane { padding: 14px 16px; border-bottom: 1px solid var(--line) }
  .pane:last-child { border-bottom: 0 }
  .pane-h { display: flex; justify-content: space-between; align-items: center; margin: 0 0 8px 8px }
  .mini { border: 0; padding: 2px 8px; font-size: 12px; color: var(--dim) }
  .note { font-size: 12px; line-height: 1.45; color: var(--dim); margin: 0 0 10px }
  .item { display: flex; justify-content: space-between; align-items: center; gap: 8px; width: 100%; text-align: left; border-color: transparent; padding: 7px 10px; margin-bottom: 2px }
  .item.on { background: var(--hover) }
  .num { display: inline-block; width: 18px; color: var(--dim); font-weight: 500 }
  .when { font-size: 12px; color: var(--dim); text-align: right }
  textarea[readonly] { color: var(--dim) }
  .cnt { font-size: 12px; font-weight: 500; padding: 1px 7px; border-radius: 6px; background: var(--chip); color: var(--dim); font-variant-numeric: tabular-nums }
  .cnt.good { background: rgba(26, 155, 99, .22); color: #5fd39a }
  .name, textarea { display: block; width: 100%; padding: 8px 10px; margin-bottom: 8px }
  textarea { resize: vertical; line-height: 1.5 }
  .kv { display: flex; justify-content: space-between; align-items: center; color: var(--dim); padding-left: 8px }
  .seg { display: flex; gap: 2px; padding: 2px; border: 1px solid var(--line2); border-radius: 8px }
  .seg button { border: 0; border-radius: 6px; padding: 3px 12px; color: var(--dim) }
  .seg button.on { background: var(--hover); color: var(--text) }
  summary { cursor: pointer; font-weight: 600; padding-left: 8px }
  .drawer summary, .pane details summary { font-weight: 400; padding: 0 }
  .pane details summary { padding-left: 8px }

  main { padding: 16px 20px 0; min-height: calc(100vh - 52px); box-sizing: border-box; display: flex; flex-direction: column }
  .tip { border: 1px solid var(--line2); border-radius: 10px; padding: 10px 12px; margin-bottom: 14px; color: var(--dim) }
  section { border: 1px solid var(--line); border-radius: 12px; padding: 10px 16px 12px; margin-bottom: 10px }
  .sec-h { display: flex; align-items: center; gap: 10px; margin-bottom: 4px }
  .mark { color: var(--red); font-weight: 700 }
  .mark.good { color: #5fd39a }
  .drawer { position: sticky; bottom: 0; margin: auto -20px 0; padding: 12px 20px 14px; background: var(--deep); border-top: 1px solid var(--line2); max-height: 50vh; overflow: auto }
  .drawer .sec-h { margin-bottom: 8px }
  .sec-h .dim { font-size: 13px }
  .check { display: flex; align-items: center; gap: 6px; color: var(--dim) }
  .msg { margin: 0; font-size: 14.5px; line-height: 1.85 }
  .gap { font-size: 11px; line-height: 18px; font-weight: 500; padding: 0 7px; margin: 0 4px; border: 0; border-radius: 6px; background: var(--chip); color: var(--dim); font-variant-numeric: tabular-nums; vertical-align: 1px }
  button.gap:hover { background: var(--chip); filter: brightness(1.25) }
  .gap.glued { background: var(--blue); color: #fff }
  .gap.cut { background: var(--orange); color: #fff }
  .gap.good { background: var(--green) }
  .gap.wrong { background: var(--red); color: #fff }
  .gap.sel, button.gap.sel:hover { background: #fff; color: #0d0d0d; filter: none }
  .help { position: relative; width: 24px; height: 24px; line-height: 24px; text-align: center; border: 1px solid var(--line2); border-radius: 50%; color: var(--dim); font-size: 12px; font-weight: 600; cursor: help }
  .help:hover, .help:focus { color: var(--text); border-color: var(--text); outline: none }
  .legend { display: none; position: absolute; top: 32px; right: 0; z-index: 3; flex-direction: column; gap: 8px; padding: 12px 14px; background: var(--deep); border: 1px solid var(--line2); border-radius: 10px;
    color: var(--dim); font-size: 12px; font-weight: 400; line-height: 18px; text-align: left; white-space: nowrap; cursor: default; box-shadow: 0 8px 24px rgba(0, 0, 0, .4) }
  .help:hover .legend, .help:focus .legend { display: flex }
  .legend .gap { margin: 0 8px 0 0; font-style: normal; display: inline-block; min-width: 52px; text-align: center; box-sizing: border-box }

  table { border-collapse: collapse; width: 100%; margin: 0 0 8px }
  td { padding: 4px 10px }
  tr { cursor: pointer }
  tr td:first-child { border-radius: 8px 0 0 8px }
  tr td:last-child { border-radius: 0 8px 8px 0; white-space: nowrap }
  tr.on td { background: var(--hover) }
  td.bar { width: 45%; position: relative }
  td.bar i { display: block; height: 8px; border-radius: 4px; background: var(--orange) }
  td.bar i.glue { background: var(--blue) }
  td.bar b { position: absolute; top: 4px; bottom: 4px; width: 2px; background: var(--text) }
  pre { white-space: pre-wrap; font: 12.5px/1.55 ui-monospace, Menlo, monospace; background: var(--deep); border: 1px solid var(--line); border-radius: 8px; padding: 10px 12px; margin: 6px 0 0 }
</style>
