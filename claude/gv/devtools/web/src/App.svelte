<script>
  // Как вопросы к Decisions делят записанную диктовку. Три вида текста одного отрезка — друг под другом; правильное деление
  // размечаешь сам (клик по паузе → «тут новое сообщение»), и каждый вариант вопроса считается по нему.
  const KINDS = { punct: 'как есть', plain: 'без знаков', socket: 'звук заново' }
  const HINT = { punct: 'текст Deepgram, как пришёл вживую', plain: 'тот же текст без знаков и заглавных', socket: 'звук отрезка заново через Deepgram (как сейчас в listen.py)' }
  const TURN = { complete: 'закончена', short: 'не закончена', long: 'подождать' }
  const NEAR = 0.5 // метка разметки относится к паузе, если лежит в ней или рядом (секунды)

  let cfg = $state(null)
  let rec = $state('20261110-103304'), from = $state('10:36:20'), to = $state('10:37:20')
  let input = $state('строками'), vi = $state(1)
  let results = $state({}) // ключ → {chunks, turns, steps} | {error}
  let cuts = $state([]) // разметка: секунды от начала отрезка, где начинается новое сообщение
  let sel = $state(null) // {kind, i} — выбранная пауза: после куска i
  let busy = $state(''), dirty = $state(false)
  let player = $state()

  const key = (k, v, i = input) => `${rec}|${from}|${to}|${k}|${v}|${i}`
  const f = (x) => (x == null ? '—' : x.toFixed(2).replace('.', ','))
  const sec = (x) => (x == null ? '' : Math.max(0, x).toFixed(1).replace('.', ',') + ' с')
  const glue = (p) => p != null && p >= cfg.sure
  const q = (o) => new URLSearchParams(o).toString()

  // пауза после куска i: где она в отрезке и что про неё решено
  function gap(r, i) {
    const a = r.chunks[i], b = r.chunks[i + 1]
    if (!b) return null
    const step = r.steps.findIndex((s) => s.at === i)
    const truth = cuts.some((t) => t >= a.end - NEAR && t <= b.start + NEAR)
    const cut = step >= 0 && !glue(r.steps[step].p)
    return { from: a.end, to: b.start, len: a.gap, step, truth, cut, wrong: cfg.labeled && (step >= 0 ? cut !== truth : truth) }
  }
  // вариант делит этот вид текста ровно по разметке?
  function score(r) {
    if (!r?.chunks || !cfg.labeled) return null
    return r.chunks.every((_, i) => !gap(r, i)?.wrong)
  }
  const total = (v) => Object.keys(KINDS).filter((k) => score(results[key(k, v)])).length

  async function api(path, body) {
    const r = await fetch('/api/' + path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {})
    return r.json()
  }
  const runOne = async (k, v, i = input) => (results[key(k, v, i)] = await api('run', { rec, from, to, kind: k, route: cfg.prompts[v].text, input: i }))

  async function load() {
    busy = 'гоняю Decisions…'
    sel = null
    cuts = await api('labels?' + q({ rec, from, to }))
    cfg.labeled = cuts.length > 0
    await Promise.all(Object.keys(KINDS).flatMap((k) => cfg.prompts.map((_, v) => runOne(k, v))))
    busy = ''
  }
  async function run() {
    busy = 'гоняю Decisions…'
    await api('prompts', cfg.prompts)
    dirty = false
    await Promise.all(Object.keys(KINDS).map((k) => runOne(k, vi)))
    busy = ''
  }
  async function setInput(i) {
    input = i
    busy = 'гоняю Decisions…'
    await Promise.all(Object.keys(KINDS).flatMap((k) => cfg.prompts.map((_, v) => results[key(k, v)] ?? runOne(k, v))))
    busy = ''
  }
  async function replay() {
    busy = 'звук заново через Deepgram, ~минута…'
    await api('replay', { rec, from, to })
    await Promise.all(cfg.prompts.map((_, v) => runOne('socket', v)))
    busy = ''
  }
  function add() {
    cfg.prompts.push({ name: 'вариант ' + (cfg.prompts.length + 1), text: cfg.prompts[vi].text })
    vi = cfg.prompts.length - 1
    dirty = true
  }
  async function drop() {
    if (cfg.prompts.length < 2) return
    cfg.prompts.splice(vi, 1)
    vi = Math.max(0, vi - 1)
    results = {}
    await api('prompts', cfg.prompts)
    await load()
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

  let selR = $derived(sel && results[key(sel.kind, vi)])
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
    <button class="ghost" onclick={() => play(0)}>▶ Отрезок</button>
    <button class="primary" onclick={run}>Прогнать ⌘↵</button>
    <audio bind:this={player} src={'/api/audio?' + q({ rec, from, to })} preload="auto"></audio>
  </header>

  <div class="grid">
    <aside>
      <div class="pane">
        <div class="pane-h">
          <h2 title="Промпт спрашивает классификатор на каждой паузе: новая реплика — продолжение прошлой или новое сообщение?">Варианты промпта</h2>
          <button class="mini" onclick={add} title="Новый вариант на основе выбранного">+ новый</button>
        </div>
        {#each cfg.prompts as p, v}
          <button class="item" class:on={v === vi} onclick={() => (vi = v)}>
            <span>{p.name}</span>
            {#if cfg.labeled}<span class="cnt" class:good={total(v) === 3} title="Делит как в разметке в {total(v)} из 3 текстов справа">{total(v)}/3</span>{/if}
          </button>
        {/each}
      </div>

      <div class="pane">
        <div class="pane-h"><h2>Промпт</h2><button class="mini danger" onclick={drop}>удалить</button></div>
        <input class="name" bind:value={cfg.prompts[vi].name} oninput={() => (dirty = true)} placeholder="Название варианта" />
        <textarea bind:value={cfg.prompts[vi].text} oninput={() => (dirty = true)} onkeydown={keys} rows="9" placeholder="Текст промпта"></textarea>
        <div class="kv"><span>Реплики подаются</span>
          <div class="seg">{#each cfg.inputs as i}<button class:on={input === i} onclick={() => setInput(i)}>{i}</button>{/each}</div>
        </div>
      </div>

      <div class="pane">
        <details><summary>Вопрос «мысль закончена?»</summary><pre>{cfg.turn}</pre></details>
      </div>
    </aside>

    <main>
      <div class="legend">
        <span><i class="gap">0,7 с</i> пауза</span>
        <span><i class="gap glued">⌒ 0,7 с</i> склеил</span>
        <span><i class="gap cut good">┃ 0,7 с</i> разрыв верный</span>
        <span><i class="gap cut wrong">┃ 0,7 с</i> лишний</span>
        <span><i class="gap wrong">0,7 с</i> пропущен</span>
      </div>
      {#if !cfg.labeled}
        <div class="tip">Сначала разметь, где начинается новое сообщение: клик по паузе → «тут новое сообщение».</div>
      {/if}
      {#each Object.entries(KINDS) as [k, name]}
        {@const r = results[key(k, vi)]}
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
                {c.text}{#if g}<button class="gap" class:glued={g.step >= 0 && !g.cut} class:cut={g.cut} class:good={g.cut && cfg.labeled && !g.wrong} class:wrong={g.wrong}
                    class:sel={sel?.kind === k && sel?.i === i} onclick={() => (sel = { kind: k, i })}>{g.cut ? '┃ ' : g.step >= 0 ? '⌒ ' : ''}{sec(g.len)}</button>{/if}
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
            <h2>Пауза {sec(selG.len)}</h2><span class="dim">{KINDS[sel.kind]}</span><span class="grow"></span>
            <label class="check"><input type="checkbox" checked={selG.truth} onchange={() => toggleTruth(selG)} /> тут новое сообщение</label>
            <button onclick={() => play(selG.from - 2, selG.to + 2)}>▶ Послушать</button>
            <button class="ghost" onclick={() => (sel = null)} title="Закрыть">✕</button>
          </div>
          {#if selG.step >= 0}
            <table>
              <tbody>
                {#each cfg.prompts as p, v}
                  {@const st = results[key(sel.kind, v)]?.steps?.find((x) => x.at === sel.i)}
                  <tr class:on={v === vi} onclick={() => (vi = v)}>
                    <td>{p.name}</td>
                    <td class="bar"><i style="width:{(st?.p ?? 0) * 100}%" class:glue={glue(st?.p)}></i><b style="left:{cfg.sure * 100}%"></b></td>
                    <td class="dim">{f(st?.p)} {st ? (glue(st.p) ? '⌒ склеил' : '┃ отдельно') : ''}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <details><summary class="dim small">Что ушло классификатору — «{cfg.prompts[vi].name}»</summary><pre>{selR.steps.find((x) => x.at === sel.i)?.input}</pre></details>
          {:else}
            <div class="dim">Здесь реплика не закрылась{selR.turns[sel.i] ? ` — «мысль закончена?»: ${TURN[selR.turns[sel.i][0]]} ${f(selR.turns[sel.i][1])}` : ' — Deepgram паузы не услышал'}, поэтому «продолжение?» тут не спрашивали.</div>
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
  .cnt { font-size: 12px; font-weight: 500; padding: 1px 7px; border-radius: 6px; background: var(--chip); color: var(--dim); font-variant-numeric: tabular-nums }
  .cnt.good { background: rgba(26, 155, 99, .22); color: #5fd39a }
  .name, textarea { display: block; width: 100%; padding: 8px 10px; margin-bottom: 8px }
  textarea { resize: vertical; line-height: 1.5 }
  .kv { display: flex; justify-content: space-between; align-items: center; color: var(--dim); padding-left: 8px }
  .seg { display: flex; gap: 2px; padding: 2px; border: 1px solid var(--line2); border-radius: 8px }
  .seg button { border: 0; border-radius: 6px; padding: 3px 12px; color: var(--dim) }
  .seg button.on { background: var(--hover); color: var(--text) }
  summary { cursor: pointer; font-weight: 600; padding-left: 8px }
  .drawer summary { font-weight: 400; padding: 0 }

  main { padding: 14px 20px 0; min-height: calc(100vh - 52px); box-sizing: border-box; display: flex; flex-direction: column }
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
  .legend { display: flex; flex-wrap: wrap; gap: 4px 18px; color: var(--dim); font-size: 12px; margin: 0 2px 12px }
  .legend .gap { margin: 0 4px 0 0; font-style: normal; display: inline-block }

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
