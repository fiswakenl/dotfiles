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
  // номер сообщения для каждого куска
  function msgs(r) {
    let n = 0
    return r.chunks.map((_, i) => { const g = i ? gap(r, i - 1) : null; if (g?.cut) n++; return n })
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
          <div class="src">
            <b>{name}</b> <span class="mute">{HINT[k]}</span>
            {#if s != null}<span class="badge" class:good={s}>{s ? '✓ делит верно' : '✗ не так'}</span>{/if}
            {#if r?.steps}<span class="mute">· {1 + r.steps.filter((x) => !glue(x.p)).length} сообщ.</span>{/if}
          </div>
          {#if r?.error}
            <button onclick={replay}>прогнать звук через Deepgram (платно, ~минута)</button>
          {:else if r?.chunks}
            {@const m = msgs(r)}
            <p class="text">
              {#each r.chunks as c, i}
                {@const g = gap(r, i)}
                <span class="chunk m{m[i] % 2}">{c.text}</span>{#if r.turns[i]}<sup class:done={r.turns[i][0] === 'complete'}>{TURN[r.turns[i][0]]}</sup>{/if}
                {#if g}
                  <button class="gap" class:joint={g.step >= 0} class:cut={g.cut} class:truth={g.truth} class:wrong={g.wrong}
                    class:sel={sel?.kind === k && sel?.i === i} onclick={() => (sel = { kind: k, i })}
                    title="пауза {sec(g.len)}">{g.truth ? '▼' : ''}{g.step >= 0 ? (g.cut ? '┃' : '⌒') : ''} {sec(g.len)}</button>
                {/if}
              {/each}
            </p>
          {:else}
            <span class="mute">…</span>
          {/if}
        </section>
      {/each}
      <div class="legend mute">
        цвет куска — какое сообщение ·
        <span class="gap">0,7 с</span> пауза Deepgram ·
        <span class="gap joint">⌒</span> стык, склеил ·
        <span class="gap joint cut">┃</span> стык, отдельно ·
        <span class="gap truth">▼</span> по разметке тут новое ·
        <span class="gap wrong">ошибка</span> не совпало с разметкой ·
        <sup>не закончена</sup>/<sup class="done">закончена</sup> — ответ «мысль закончена?»
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
  section { border: 1px solid #e5e5ea; border-radius: 10px; padding: 8px 12px; margin-bottom: 10px }
  .src { display: flex; gap: 8px; align-items: center; flex-wrap: wrap }
  .badge { font-size: 12px; font-weight: 600; padding: 0 8px; border-radius: 99px; background: #fde2e1; color: #b3261e }
  .badge.good, .cnt.good { background: #e3f6e8; color: #1e7d3a }
  .text { line-height: 2.5; margin: 4px 0 0 }
  .chunk { border-radius: 3px; padding: 1px 0 }
  .chunk.m0 { background: #e8f0fc }
  .chunk.m1 { background: #f3e8fc }
  sup { font-size: 10px; color: #8e8e93; margin-left: 1px }
  sup.done { background: #ffd9b0; color: #1d1d1f; border-radius: 3px; padding: 0 3px }
  .gap { font-size: 11px; padding: 0 5px; margin: 0 2px; border-radius: 99px; background: #f2f2f7; color: #8e8e93; border: 1px solid transparent; font-variant-numeric: tabular-nums; line-height: 17px; display: inline-block }
  .gap.joint { background: #3a7bd5; color: #fff; font-weight: 600; border-radius: 5px }
  .gap.joint.cut { background: #e08a1e }
  .gap.truth { box-shadow: inset 0 -3px 0 #2e9d57 }
  .gap.wrong { border: 2px solid #d64545 }
  .gap.sel { outline: 2px solid #1d1d1f; outline-offset: 1px }
  .legend { line-height: 2.2; margin: -2px 0 10px }
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
