<script>
  // Как вопросы к Decisions делят диктовку: три вида текста × варианты вопроса «продолжение?» × формат входа.
  const KINDS = { punct: 'как есть', plain: 'без знаков и заглавных', socket: 'звук заново через Deepgram' }
  const TURN = { complete: 'закончена', short: 'не закончена', long: 'подождать' }

  let cfg = $state(null)
  let rec = $state('20261110-103304'), from = $state('10:36:20'), to = $state('10:37:20')
  let kind = $state('punct'), vi = $state(1), input = $state('строками')
  let results = $state({})          // ключ прогона → {chunks, turns, steps} | {error}
  let pick = $state(null)           // выбранный стык: индекс в steps
  let busy = $state(''), dirty = $state(false)

  const key = (k, v, i) => `${rec}|${from}|${to}|${k}|${v}|${i}`
  const f = (x) => (x == null ? '—' : x.toFixed(2).replace('.', ','))
  const sec = (x) => (x == null ? '' : (x < 0 ? 0 : x).toString().replace('.', ',') + ' с')
  const glue = (p) => p != null && p >= cfg.sure
  const count = (r) => (r?.steps ? 1 + r.steps.filter((s) => !glue(s.p)).length : null)

  let cur = $derived(results[key(kind, vi, input)])
  let variant = $derived(cfg?.prompts[vi])
  let step = $derived(cur?.steps?.[pick])
  // реплика, куда попал кусок: перед ней стык k — она склеена с прошлым сообщением или ушла отдельно
  let fate = $derived(cur?.chunks?.map((_, i) => { const k = cur.steps.filter((s) => s.at < i).length - 1; return k < 0 ? '' : glue(cur.steps[k].p) ? 'g' : 'n' }) ?? [])

  async function api(path, body) {
    const r = await fetch('/api/' + path, body ? { method: 'POST', headers: { 'content-type': 'application/json' }, body: JSON.stringify(body) } : {})
    return r.json()
  }

  async function runOne(k, v, i) {
    results[key(k, v, i)] = await api('run', { rec, from, to, kind: k, route: cfg.prompts[v].text, input: i })
  }

  async function runAll() {
    busy = 'гоняю Decisions…'
    await Promise.all(Object.keys(KINDS).flatMap((k) => cfg.prompts.flatMap((_, v) => cfg.inputs.map((i) => runOne(k, v, i)))))
    busy = ''
  }

  async function rerunVariant() {
    busy = 'гоняю Decisions…'
    await Promise.all(Object.keys(KINDS).flatMap((k) => cfg.inputs.map((i) => runOne(k, vi, i))))
    busy = ''
  }

  async function save() {
    await api('prompts', cfg.prompts)
    dirty = false
    await rerunVariant()
  }

  function add() {
    cfg.prompts.push({ name: 'новый ' + (cfg.prompts.length + 1), text: variant.text })
    vi = cfg.prompts.length - 1
    dirty = true
  }

  async function replay() {
    busy = 'звук заново через Deepgram, ~минута…'
    await api('replay', { rec, from, to })
    await Promise.all(cfg.prompts.flatMap((_, v) => cfg.inputs.map((i) => runOne('socket', v, i))))
    busy = ''
  }

  $effect(() => { api('config').then((c) => { cfg = c; runAll() }) })
</script>

{#if cfg}
  <h1>GV devtools — как Decisions делит диктовку</h1>
  <div class="row">
    <select bind:value={rec}>{#each cfg.recs as r}<option>{r}</option>{/each}</select>
    <input bind:value={from} size="8" /> – <input bind:value={to} size="8" />
    <button onclick={runAll}>прогнать</button>
    <button onclick={replay} title="платно: Deepgram">звук заново</button>
    <span class="mute">{busy}</span>
  </div>

  <div class="row">
    <span class="lbl">текст</span>
    <div class="seg">{#each Object.entries(KINDS) as [k, name]}<button class:on={kind === k} onclick={() => (kind = k)}>{name}</button>{/each}</div>
  </div>
  <div class="row">
    <span class="lbl">вопрос</span>
    <div class="seg">{#each cfg.prompts as p, v}<button class:on={vi === v} onclick={() => (vi = v)}>{p.name}</button>{/each}</div>
    <button class="ghost" onclick={add}>+</button>
  </div>
  <div class="row">
    <span class="lbl">вход</span>
    <div class="seg">{#each cfg.inputs as i}<button class:on={input === i} onclick={() => (input = i)}>{i}</button>{/each}</div>
  </div>

  <div class="card">
    {#if cur?.error}
      <span class="mute">{cur.error}</span>
    {:else if cur}
      <b>{count(cur) === 2 ? '✓ ' : ''}{count(cur)} сообщ. в Claude</b> <span class="mute">— <span class="chunk g">голубой</span> кусок склеен с прошлым сообщением, <span class="chunk n">оранжевый</span> ушёл отдельным; серые числа — паузы Deepgram</span>
      <div class="text">
        {#each cur.chunks as c, i}
          {@const s = cur.steps.findIndex((x) => x.at === i)}
          <span class="chunk {fate[i]}">{c.text}</span>{#if cur.turns[i]}<sup class:done={cur.turns[i][0] === 'complete'}>{TURN[cur.turns[i][0]]} {f(cur.turns[i][1])}</sup>{/if}
          {#if s >= 0}
            <button class="cut" class:glue={glue(cur.steps[s].p)} class:sel={pick === s} onclick={() => (pick = s)}>
              {glue(cur.steps[s].p) ? '⌒ склеил' : '| отдельно'} · {sec(c.gap)} · {f(cur.steps[s].p)}
            </button>{#if !glue(cur.steps[s].p)}<br />{/if}
          {:else if c.gap != null}
            <span class="pz" class:big={c.gap > 5}>{sec(c.gap)}</span>
          {/if}
        {/each}
      </div>
    {:else}
      <span class="mute">…</span>
    {/if}
  </div>

  <div class="card">
    <b>prompt «продолжение?» — {variant.name}</b>
    <input class="name" bind:value={variant.name} oninput={() => (dirty = true)} />
    <textarea bind:value={variant.text} oninput={() => (dirty = true)} rows="4"></textarea>
    <button onclick={save} disabled={!dirty}>сохранить и прогнать</button>
    {#if step}
      <div class="mute" style="margin-top:8px">вход на выбранном стыке (пауза {sec(step.pause)} → {f(step.p)})</div>
      <pre>{step.input}</pre>
    {/if}
  </div>

  <div class="card">
    <b>«продолжение?» на каждом стыке — все варианты</b> <span class="mute">— {KINDS[kind]}, вход {input}; в шапке — пауза перед репликой, как её услышал Deepgram; клик — открыть</span>
    <table>
      <thead><tr><th></th>{#each cur?.steps ?? [] as s, k}<th>стык {k + 1}<br />пауза {sec(s.pause)}</th>{/each}<th>сообщ.</th></tr></thead>
      <tbody>
      {#each cfg.prompts as p, v}
        {@const r = results[key(kind, v, input)]}
        <tr>
          <td>{p.name}</td>
          {#each r?.steps ?? [] as s, k}
            <td class="p" class:on={v === vi && k === pick} style="background:{glue(s.p) ? '#3a7bd5' : '#e08a1e'}" onclick={() => { vi = v; pick = k }}>
              {glue(s.p) ? '⌒' : '|'} {f(s.p)}
            </td>
          {/each}
          <td class:ok={count(r) === 2}>{count(r) ?? '—'}</td>
        </tr>
      {/each}
      </tbody>
    </table>
  </div>

  <div class="card">
    <b>сколько сообщений</b> <span class="mute">— правильно 2; клик — открыть</span>
    <table>
      <thead><tr><th></th>{#each Object.values(KINDS) as n}{#each cfg.inputs as i}<th>{n}<br />{i}</th>{/each}{/each}</tr></thead>
      <tbody>
      {#each cfg.prompts as p, v}
        <tr>
          <td>{p.name}</td>
          {#each Object.keys(KINDS) as k}{#each cfg.inputs as i}
            {@const n = count(results[key(k, v, i)])}
            <td class:ok={n === 2} class:on={k === kind && v === vi && i === input} onclick={() => { kind = k; vi = v; input = i }}>{n ?? '—'}</td>
          {/each}{/each}
        </tr>
      {/each}
      </tbody>
    </table>
  </div>

  <details class="card"><summary>prompt «мысль закончена?»</summary><pre>{cfg.turn}</pre><pre>{JSON.stringify(cfg.turn_options, null, 1)}</pre></details>
{/if}

<style>
  :global(body) { font: 14px/1.5 -apple-system, system-ui, sans-serif; color: #1d1d1f; margin: 16px; max-width: 900px }
  h1 { font-size: 17px; margin: 0 0 8px }
  .row { display: flex; gap: 8px; align-items: center; flex-wrap: wrap; margin: 6px 0 }
  .lbl { width: 52px; font-size: 12px; color: #6e6e73 }
  .mute { color: #6e6e73; font-size: 12px }
  .seg { display: inline-flex; flex-wrap: wrap; border: 1px solid #d2d2d7; border-radius: 8px; overflow: hidden }
  .seg button { border: 0; border-right: 1px solid #d2d2d7; background: #fff; padding: 4px 10px; font: inherit; font-size: 13px; cursor: pointer }
  .seg button:last-child { border-right: 0 }
  .seg button.on { background: #1d1d1f; color: #fff }
  .card { border: 1px solid #e5e5ea; border-radius: 10px; padding: 10px 12px; margin: 10px 0 }
  .text { line-height: 2.4; margin-top: 6px }
  .chunk.g { background: #dce9fb; border-radius: 3px }
  .chunk.n { background: #fde8cf; border-radius: 3px }
  sup { font-size: 10px; color: #6e6e73; margin-left: 2px }
  sup.done { background: #ffd9b0; color: #1d1d1f; border-radius: 4px; padding: 0 3px }
  .pz { font-size: 11px; background: #e5e5ea; border-radius: 99px; padding: 0 6px; font-variant-numeric: tabular-nums }
  .pz.big { background: #1d1d1f; color: #fff; font-weight: 700 }
  .cut { border: 0; color: #fff; background: #e08a1e; font-size: 11px; font-weight: 600; padding: 1px 7px; border-radius: 6px; cursor: pointer; font-variant-numeric: tabular-nums }
  .cut.glue { background: #3a7bd5 }
  .cut.sel { outline: 2px solid #1d1d1f; outline-offset: 1px }
  textarea, .name { width: 100%; box-sizing: border-box; font: 13px/1.5 ui-monospace, Menlo, monospace; margin: 6px 0; padding: 6px; border: 1px solid #d2d2d7; border-radius: 6px }
  pre { white-space: pre-wrap; font: 12.5px/1.55 ui-monospace, Menlo, monospace; background: #f7f7f9; border-radius: 8px; padding: 8px 10px; margin: 4px 0 }
  table { border-collapse: collapse; margin-top: 6px; font-size: 12px }
  th { color: #6e6e73; font-weight: 600; padding: 2px 8px }
  td { padding: 3px 8px; text-align: center }
  td:first-child { text-align: left }
  td:not(:first-child) { cursor: pointer; border-radius: 4px }
  td.ok { background: #e3f6e8; font-weight: 700 }
  td.p { color: #fff; font-weight: 600; font-variant-numeric: tabular-nums }
  td.on { outline: 2px solid #1d1d1f }
  .ghost { border: 1px solid #d2d2d7; background: #fff; border-radius: 8px; padding: 3px 9px; cursor: pointer }
</style>
