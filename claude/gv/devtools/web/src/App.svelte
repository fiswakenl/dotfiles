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
    <b>GV devtools</b>
    <select bind:value={rec}>{#each cfg.recs as r}<option>{r}</option>{/each}</select>
    <input bind:value={from} size="7" />–<input bind:value={to} size="7" />
    <button onclick={load}>загрузить</button>
    <button onclick={() => play(0)}>▶ отрезок</button>
    <span class="mute">{busy}</span>
    <audio bind:this={player} src={'/api/audio?' + q({ rec, from, to })} preload="auto"></audio>
  </header>

  <div class="grid">
    <main>
      {#if !cfg.labeled}
        <div class="tip">Сначала разметь, где на самом деле начинается новое сообщение: клик по паузе → «тут новое сообщение». Тогда варианты будут считаться автоматически.</div>
      {/if}
      {#each Object.entries(KINDS) as [k, name]}
        {@const r = results[key(k, vi)]}
        {@const s = score(r)}
        <section>
          <h2 title={HINT[k]}>{name}{#if s != null}<span class="mark" class:good={s}>{s ? '✓' : '✗'}</span>{/if}</h2>
          {#if r?.error}
            <button onclick={replay}>прогнать звук через Deepgram (платно, ~минута)</button>
          {:else if r?.chunks}
            <p class="msg">
              {#each r.chunks as c, i}
                {@const g = gap(r, i)}
                {c.text}{#if g}<button class="gap" class:glued={g.step >= 0 && !g.cut} class:cut={g.cut} class:good={g.cut && cfg.labeled && !g.wrong} class:wrong={g.wrong}
                    class:sel={sel?.kind === k && sel?.i === i} onclick={() => (sel = { kind: k, i })}>{g.cut ? '┃ ' : g.step >= 0 ? '⌒ ' : ''}{sec(g.len)}</button>{/if}
              {/each}
            </p>
          {:else}
            <span class="mute">…</span>
          {/if}
        </section>
      {/each}
      <div class="legend mute">
        <span class="gap">0,7 с</span><span>пауза</span>
        <span class="gap glued">⌒ 0,7 с</span><span>спросили и склеил</span>
        <span class="gap cut good">┃ 0,7 с</span><span>разрыв, как в разметке</span>
        <span class="gap cut wrong">┃ 0,7 с</span><span>лишний разрыв</span>
        <span class="gap wrong">0,7 с</span><span>разрыв пропущен</span>
        <span></span><span>клик по паузе — звук и варианты</span>
      </div>

      {#if selG}
        <section class="insp">
          <div class="src">
            <b>{KINDS[sel.kind]} · пауза {sec(selG.len)}</b>
            <button onclick={() => play(selG.from - 2, selG.to + 2)}>▶ послушать</button>
            <label><input type="checkbox" checked={selG.truth} onchange={() => toggleTruth(selG)} /> тут новое сообщение</label>
          </div>
          {#if selG.step >= 0}
            <table>
              <tbody>
                {#each cfg.prompts as p, v}
                  {@const st = results[key(sel.kind, v)]?.steps?.find((x) => x.at === sel.i)}
                  <tr class:on={v === vi} onclick={() => (vi = v)}>
                    <td>{p.name}</td>
                    <td class="bar"><i style="width:{(st?.p ?? 0) * 100}%" class:glue={glue(st?.p)}></i><b style="left:{cfg.sure * 100}%"></b></td>
                    <td>{f(st?.p)} {st ? (glue(st.p) ? '⌒ склеил' : '┃ отдельно') : ''}</td>
                  </tr>
                {/each}
              </tbody>
            </table>
            <div class="mute">вход «{cfg.prompts[vi].name}»</div>
            <pre>{selR.steps.find((x) => x.at === sel.i)?.input}</pre>
          {:else}
            <div class="mute">Здесь реплика не закрылась{selR.turns[sel.i] ? ` — «мысль закончена?»: ${TURN[selR.turns[sel.i][0]]} ${f(selR.turns[sel.i][1])}` : ' — Deepgram паузы не услышал'}, поэтому «продолжение?» тут не спрашивали.</div>
          {/if}
        </section>
      {/if}
    </main>

    <aside>
      <div class="head"><b>Вопрос «продолжение?»</b> <span class="mute">верно из 3</span></div>
      {#each cfg.prompts as p, v}
        <button class="var" class:on={v === vi} onclick={() => (vi = v)}>
          <span>{p.name}</span><span class="cnt" class:good={total(v) === 3}>{cfg.labeled ? total(v) + '/3' : ''}</span>
        </button>
      {/each}
      <div class="row"><button onclick={add}>+ копия</button><button onclick={drop}>удалить</button></div>

      <input class="name" bind:value={cfg.prompts[vi].name} oninput={() => (dirty = true)} />
      <textarea bind:value={cfg.prompts[vi].text} oninput={() => (dirty = true)} onkeydown={keys} rows="9"></textarea>
      <div class="row">
        <span class="mute">вход</span>
        {#each cfg.inputs as i}<button class:on={input === i} onclick={() => setInput(i)}>{i}</button>{/each}
      </div>
      <button class="go" onclick={run}>прогнать {dirty ? '(изменён)' : ''} ⌘↵</button>

      <details><summary class="mute">вопрос «мысль закончена?»</summary><pre>{cfg.turn}</pre></details>
    </aside>
  </div>
{/if}

<style>
  :global(body) { font: 14px/1.5 -apple-system, system-ui, sans-serif; color: #1d1d1f; margin: 0 }
  header { display: flex; gap: 8px; align-items: center; padding: 10px 16px; border-bottom: 1px solid #e5e5ea; position: sticky; top: 0; background: #fff; z-index: 1 }
  .grid { display: grid; grid-template-columns: 1fr 330px; gap: 16px; padding: 12px 16px; max-width: 1280px }
  aside { position: sticky; top: 56px; align-self: start; display: flex; flex-direction: column; gap: 6px }
  .mute { color: #6e6e73; font-size: 12px }
  button { font: inherit; font-size: 13px; border: 1px solid #d2d2d7; background: #fff; border-radius: 7px; padding: 3px 9px; cursor: pointer }
  button.on { background: #1d1d1f; color: #fff; border-color: #1d1d1f }
  .tip { background: #fff7e0; border-radius: 8px; padding: 8px 10px; margin-bottom: 10px; font-size: 13px }
  section { border: 1px solid #e5e5ea; border-radius: 12px; padding: 12px 16px 14px; margin-bottom: 12px; background: #fff }
  h2 { font-size: 11px; font-weight: 600; letter-spacing: .06em; text-transform: uppercase; color: #8e8e93; margin: 0 0 8px; padding-bottom: 8px; border-bottom: 1px solid #f0f0f3; cursor: help }
  .mark { margin-left: 6px; color: #d64545; font-size: 13px }
  .mark.good { color: #2e9d57 }
  .src { display: flex; gap: 8px; align-items: center; flex-wrap: wrap }
  .cnt.good { background: #e3f6e8; color: #1e7d3a }
  .msg { margin: 0; font-size: 15px; line-height: 1.9; max-width: 740px }
  .gap { font-size: 11px; line-height: 17px; padding: 0 7px; margin: 0 4px; border: 0; border-radius: 99px; background: #ececf0; color: #6e6e73; font-variant-numeric: tabular-nums; vertical-align: 1px }
  button.gap:hover { filter: brightness(.92) }
  .gap.glued { background: #3a7bd5; color: #fff; font-weight: 600 }
  .gap.cut { background: #e08a1e; color: #fff; font-weight: 600 }
  .gap.good { background: #2e9d57 }
  .gap.wrong { background: #d64545; color: #fff; font-weight: 600 }
  .gap.sel { background: #1d1d1f; color: #fff }
  .legend { display: grid; grid-template-columns: max-content 1fr; gap: 5px 10px; align-items: center; justify-items: start; margin: 0 0 14px }
  .legend .gap { margin: 0 }
  .insp { background: #fafafa }
  table { border-collapse: collapse; width: 100%; margin: 6px 0; font-size: 13px }
  td { padding: 3px 6px }
  tr { cursor: pointer }
  tr.on td { background: #ececf0 }
  td.bar { width: 45%; position: relative }
  td.bar i { display: block; height: 12px; border-radius: 3px; background: #e08a1e }
  td.bar i.glue { background: #3a7bd5 }
  td.bar b { position: absolute; top: 1px; bottom: 1px; width: 2px; background: #1d1d1f }
  pre { white-space: pre-wrap; font: 12px/1.5 ui-monospace, Menlo, monospace; background: #f2f2f7; border-radius: 8px; padding: 8px; margin: 4px 0 }
  .head { display: flex; justify-content: space-between; align-items: baseline }
  .var { display: flex; justify-content: space-between; text-align: left }
  .cnt { font-size: 12px; padding: 0 6px; border-radius: 99px; background: #f2f2f7 }
  .var.on .cnt { color: #1d1d1f }
  .row { display: flex; gap: 6px; align-items: center }
  .name, textarea { width: 100%; box-sizing: border-box; font: 12.5px/1.5 ui-monospace, Menlo, monospace; padding: 6px; border: 1px solid #d2d2d7; border-radius: 7px }
  .go { background: #1d1d1f; color: #fff; border-color: #1d1d1f; padding: 6px }
</style>
