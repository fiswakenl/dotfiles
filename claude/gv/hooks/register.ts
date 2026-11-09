import { atom, read, update } from "claude-code";
import { decide, prompt, type Route } from "./orchestrator.ts";

// Голос ⇄ сессия Claude Code.
//
// Сервис (voice.py, логика — core.py) слушает человека через голосовой адаптер (adapters/): адаптер — настройка voice плагина
// или выбор командой /gv <имя> поверх неё. Плагин забирает у сервиса работу ручкой /next:
// - ask — сообщением в окно Claude через мост (native/bridge.mjs, команды — через /call сервиса), с откатом, если это поправка;
// - quick — веткой разговора ($.model.fork), основной ход не трогает;
// - route — реплика адаптера распознавания без решения: оркестратор ($.model.complete) решает, ask это, поправка или quick.
// Ответ — ручкой /answer; Gemini его озвучит, у адаптеров распознавания человек читает его в окне. О каждом ходе Claude, и набранном
// руками, плагин сообщает сервису (/call turn): пока ход идёт, новый ask ждёт в ядре, а quick и реплики идут своим чередом.
// Сервис поднимается на старте сессии, перезапускается при правке своих файлов и выходит сам, если плагин перестал пинговать;
// правка этого файла или настроек перезагружает плагин, а с ним и сервис. `$` через импорт не передаётся: все обращения к движку — здесь.

const ADAPTERS: Record<string, string> = { gemini: "Gemini Live", deepgram: "Deepgram", openai: "OpenAI" };
const MARK = "🎙"; // голосовое сообщение: по метке Claude знает, что оно распознано из речи
const TRIES = 6;
const PING_MS = 20_000;
const RETRY_MS = 2_000;
const ORCHESTRATOR = { model: "haiku", maxTokens: 1_000, timeoutMs: 8_000 }; // в ответе — решение и очищенный текст реплики
const TOKEN = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);

// Работает одна сессия с плагином: новая вытесняет старую — просит прежний сервис выйти (/quit) и занимает его порт.
let PORT = 8790;
let BASE = `http://127.0.0.1:${PORT}`;

let started = false;
let running = false; // голос включён в этом экземпляре плагина
let loop = 0; // номер цикла опроса: включение заводит новый, прежний видит чужой номер и выходит
let launching: Promise<boolean> | null = null; // запуск сервиса идёт: второй не начинаем, ждём этот
let epoch = ""; // номер запуска сервиса (X-GV-Epoch): номера запросов — его, перезапуск их обнуляет
let mainBusy = false; // идёт любой ход основного Claude — и голосовой, и набранный руками

// Номера запросов — номера ядра текущего запуска сервиса: новый запуск их обнуляет (adopt).
let current = 0; // запрос, над которым идёт голосовой ход; 0 — ход не голосовой
let lastVoice = 0; // последний отвеченный голосовой запрос: ход без новой реплики (итог фоновой задачи) — его продолжение
const voiceMessages = new Map<number, string>(); // номер запроса → UUID его сообщения в истории Claude
const expected = new Map<string, number>(); // текст отправленного сообщения → номер запроса: его ход ещё не начался
const quickRows = atom({ plugin: "gv", key: "quickRows" } as const, {} as Record<string, { id: number; q: string; a: string }[]>);
const quickOpen = atom({ plugin: "gv", key: "quickOpen" } as const, [] as number[]);

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));
let chosen = ""; // адаптер, выбранный командой /gv <имя> (~/.claude/gv.voice); пусто — из настройки voice
const voiceOf = (options): string => [chosen, String(options.voice)].find((v) => v in ADAPTERS) ?? "gemini";
const voiceFile = async ($): Promise<string> => `${await $.env.get("HOME")}/.claude/gv.voice`;

// ── сервис ────────────────────────────────────────────────────────────────────

// Номер запуска идёт с каждым запросом: сервис другого запуска ответит 409, и ответ на старый номер не попадёт в чужой запрос.
const call = ($, path: string, init: { method?: string; body?: string } = {}) =>
  $.http.fetch(`${BASE}${path}`, { ...init, headers: { "content-type": "application/json", ...(epoch ? { "x-gv-epoch": epoch } : {}) } });

const post = ($, path: string, body: unknown = {}) => call($, path, { method: "POST", body: JSON.stringify(body) });

const answer = ($, id: number, body: { text?: string; error?: string }) => post($, `/answer?id=${id}`, body).catch(() => {});

// Сервис сообщает ядру, идёт ли ход Claude.
const turn = ($) => (running ? post($, "/call", { name: "turn", args: { working: mainBusy } }).catch(() => {}) : undefined);

// Новый запуск сервиса — новое ядро: прежние номера запросов ничего не значат, а идёт ли сейчас ход Claude, ядро должно узнать.
function adopt($, next: string): void {
  if (next === epoch) return;
  epoch = next;
  current = lastVoice = 0;
  voiceMessages.clear();
  expected.clear();
  void turn($);
}

// Хозяин сервиса записан в файле: новый экземпляр плагина (другая сессия или перезагрузка) пишет туда свой номер, старый видит
// чужой номер и молча уходит — не опрашивает, не пингует и не гасит сервис.
async function tokenFile($): Promise<string> {
  return `${await $.env.get("HOME")}/.claude/gv${PORT === 8790 ? "" : "-" + PORT}.token`;
}

async function mine($): Promise<boolean> {
  return String(await $.fs.read(await tokenFile($)).catch(() => "")).trim() === TOKEN;
}

// Поднять сервис отдельным процессом (вывод — ~/.claude/gv.out); true — отвечает. Запуски не накладываются: пока идёт один,
// остальные получают его же результат.
function launch($, options): Promise<boolean> {
  return (launching ??= start($, options).finally(() => (launching = null)));
}

async function start($, options): Promise<boolean> {
  await $.fs.write(await tokenFile($), TOKEN).catch(() => {});
  await post($, "/quit").catch(() => {});
  for (let i = 0; i < 20; i++) {
    if (!(await $.http.fetch(`${BASE}/`).catch(() => null))) break;
    await sleep(250);
  }
  const env = [`GV_PORT=${PORT}`, `GV_VOICE=${voiceOf(options)}`, `GV_DUPLEX=${options.duplex ?? "on"}`, `GV_PAUSE_MS=${String(options.pause ?? "2500")}`];
  const pass = {
    GV_NATIVE_SOCKET: await $.env.get("GV_NATIVE_SOCKET"),
    GV_TEXT_ONLY: await $.env.get("GV_TEXT_ONLY"),
    GV_SAY_MS: await $.env.get("GV_SAY_MS"),
  }; // сервису: мост и тестовый режим
  for (const [key, value] of Object.entries(pass)) if (value) env.push(`${key}='${value}'`);
  await $.process
    .run(["sh", "-c", `${env.join(" ")} nohup uv run -q --script '${$.plugin.root}/voice.py' >>"$HOME/.claude/gv.out" 2>&1 &`])
    .catch((err) => $.ui.log(`gv: запуск не удался: ${String(err)}`, { to: "debug" }));
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if ((await post($, "/ping").catch(() => null))?.ok) return true;
  }
  return false;
}

// Цикл работы от сервиса: ask и route — по одному, quick — сразу в фоне. Сервис не отвечает несколько раз подряд — поднимаем заново.
async function poll($, options, me: number): Promise<void> {
  let down = 0;
  const alive = () => running && me === loop;
  while (alive()) {
    if (!(await mine($))) return void ((running = false), $.ui.status("голос в другой сессии")); // сервис теперь у другого экземпляра
    let r;
    try {
      r = await call($, "/next?wait=1");
      down = 0;
    } catch {
      await sleep(RETRY_MS);
      if (alive() && ++down >= 3) {
        down = 0;
        await launch($, options).catch(() => false);
      }
      continue;
    }
    if (r.headers["x-gv-epoch"]) adopt($, r.headers["x-gv-epoch"]);
    if (r.status !== 200 || !r.text) {
      if (r.status !== 204) await sleep(RETRY_MS);
      continue;
    }
    const item = JSON.parse(r.text);
    if (item.kind === "quick") {
      void quick($, item);
      continue;
    }
    try {
      await (item.kind === "route" ? route($, item) : deliver($, item));
    } catch (error) {
      if (item.kind === "ask") await answer($, item.id, { error: String(error) });
      $.ui.status(String(error));
    }
  }
}

// ── работа ────────────────────────────────────────────────────────────────────

// Мост в окно Claude — через сервис, теми же именами: send, rewind, list.
async function bridge($, name: string, args: Record<string, unknown>): Promise<any> {
  const r = await post($, "/call", { name, args });
  const result = JSON.parse(r.text || "{}").result ?? {};
  if (!r.ok || result.error) throw new Error(result.error || `мост не ответил: HTTP ${r.status}`);
  return result;
}

// ask: в окно Claude как набранное руками; поправка — откат до сообщения прежнего запроса и новый текст. Новое сообщение ядро
// отдаёт, только когда Claude свободен; поправка откатывает идущий ход сама. Чей ход, узнаём по тексту на его старте (turn.start).
async function deliver($, r: { id: number; text: string; rewind: number | null }): Promise<void> {
  const text = `${MARK} ${r.text}`;
  const target = r.rewind ? voiceMessages.get(r.rewind) : undefined;
  expected.set(text, r.id);
  // Сообщения, которое поправляем, в истории уже нет (откат руками) или оно из прошлого запуска сервиса — реплика уходит новой.
  const result = await (target
    ? bridge($, "rewind", { target, text }).catch((err) => (/target_not_found/.test(String(err)) ? bridge($, "send", { text }) : Promise.reject(err)))
    : bridge($, "send", { text })
  ).catch((err) => Promise.reject((expected.delete(text), err)));
  if (result.removed?.length) { // ядру — какие запросы стёрты: их ходы больше не ответят
    const removed = [...voiceMessages].filter(([, uuid]) => result.removed.includes(uuid)).map(([id]) => id);
    for (const id of removed) voiceMessages.delete(id);
    await post($, "/call", { name: "rewound", args: { id: r.id, removed } }).catch(() => {});
  }
  if (result.uuid) voiceMessages.set(r.id, result.uuid);
}

// Быстрый вопрос: ветка разговора отвечает по тому, что в нём уже есть; основной ход не трогаем, инструменты не нужны.
const QUICK_PROMPT =
  "Служебный запрос голосового плагина gv, не задача из разговора. Основная работа идёт своим чередом: " +
  "не продолжай её и не меняй, инструменты не вызывай. Ответь на быстрый вопрос ниже по разговору выше, " +
  "как будто говоришь вслух: 1–3 короткие фразы простыми словами, без списков и разметки. " +
  "Если в разговоре ответа нет, так и скажи.";

async function quick($, r: { id: number; text: string }): Promise<void> {
  let text = "Не получилось ответить.";
  try {
    const reply = await $.model.fork({ prompt: `${QUICK_PROMPT}\n\n<вопрос>\n${r.text}\n</вопрос>` });
    if (reply.reason === "nothing-to-fork") text = "В сессии пока нечего вспомнить: разговор только начался.";
    else if (reply.isAnswered && reply.text.trim()) text = reply.text.trim();
  } catch {}
  await answer($, r.id, { text });
  // Человеку — раскрывающейся строкой под голосовым сообщением, во время которого спросил (см. ui.render UserMessage);
  // нет такого — тусклой строкой в чате. Claude — репликой без показа, чтобы знал, что спросили мимо него.
  const line = `⚡ quick №${r.id}: ${r.text}\n← ${text}`;
  const under = voiceMessages.get(current || lastVoice);
  if (under) await update($, quickRows, (rows) => ({ ...rows, [under]: [...(rows[under] ?? []), { id: r.id, q: r.text, a: text }] }));
  else $.ui.log(line);
  await $.session.append({ message: { type: "user", content: [{ type: "text", text: line }] } }).catch(() => {});
}

// Реплика адаптера распознавания: оркестратор — один короткий вызов модели без форка и прогрева чата, видит конец разговора
// и переданные реплики. Решение — обратно в сервис тем же входом, что у Gemini; текст — расшифровка, очищенная оркестратором.
async function route($, item: Route): Promise<void> {
  $.ui.status(`решаю: «${item.text.slice(0, 40)}${item.text.length > 40 ? "…" : ""}»`); // видно, что реплика услышана и ждёт решения
  const [rules, messages] = await Promise.all([
    $.fs.read(`${$.plugin.root}/prompts/orchestrator.md`).then(String).catch(() => ""),
    $.session.messages().catch(() => []),
  ]);
  const reply = await $.model.complete({ ...ORCHESTRATOR, system: rules, prompt: prompt(messages, item) }).catch(() => null);
  if (!reply?.isAnswered) $.ui.log(`gv: оркестратор не ответил (${reply?.reason ?? "ошибка"}), реплика — новой мыслью`, { to: "debug" });
  const d = decide(reply?.isAnswered ? reply.text : "", item);
  const sent = await post($, "/call", { name: d.name, args: { heard: d.text, replaces: d.replaces, sure: true } }).catch(() => null);
  const n = /№(\d+)/.exec(JSON.parse(sent?.text || "{}").result ?? "")?.[1] ?? "?";
  $.ui.status(d.name === "quick" ? `⚡ quick №${n}` : d.replaces ? `↺ поправка №${d.replaces} → №${n}` : `→ новое №${n}`);
}

// Ход упал на ошибке API или Claude отказался — человек узнаёт об этом, как о любом сбое запроса.
function failure(e): string {
  if (e.reason === "refusal") return `Claude отказался отвечать${e.refusal?.explanation ? `: ${e.refusal.explanation}` : ""}`;
  return "ход Claude оборвался ошибкой";
}

// ── включение и выбор адаптера ────────────────────────────────────────────────

// Файл ~/.claude/gv.state ("on" / "off"): включён ли голос. Переживает перезагрузку плагина и перезапуск сессии; меняет его команда /gv.
async function stateFile($): Promise<string> {
  return (await $.env.get("GV_STATE_FILE")) || `${await $.env.get("HOME")}/.claude/gv.state`;
}

// Включить: стать хозяином, запустить цикл, поднять сервис.
async function turnOn($, options): Promise<void> {
  if (running) return;
  running = true;
  await $.fs.write(await stateFile($), "on").catch(() => {});
  // Хозяином становимся сразу: иначе собственный опрос успеет прочитать чужой номер в файле и решит, что его вытеснили.
  await $.fs.write(await tokenFile($), TOKEN).catch(() => {});
  $.ui.status(undefined);
  void poll($, options, ++loop);
  let up = false;
  for (let attempt = 0; attempt < TRIES && !up && running; attempt++) up = await launch($, options);
  if (!running) return;
  if (!up) return void $.ui.status("сервис не поднялся");
  $.ui.status(`${ADAPTERS[voiceOf(options)]} · ${BASE}`); // страница журнала — по адресу из статуса
}

// Выключить: цикл стоит, сервис и сокеты закрыты, выключенность запомнена.
async function turnOff($): Promise<void> {
  running = false;
  await $.fs.write(await stateFile($), "off").catch(() => {});
  await post($, "/quit").catch(() => {});
  $.ui.status("выкл");
}

// Другой адаптер: выбор запомнен в ~/.claude/gv.voice поверх настройки voice, сервис перезапускается с ним, сессия остаётся.
// Выбрать адаптер — заодно включить голос.
async function choose($, options, name: string): Promise<string> {
  const same = voiceOf(options) === name;
  chosen = name;
  await $.fs.write(await voiceFile($), name).catch(() => {});
  if (!running) return void turnOn($, options), `вкл · ${ADAPTERS[name]}`;
  if (same) return `уже ${ADAPTERS[name]}`;
  void (async () => {
    await launching; // идёт запуск со старым адаптером — новый после него
    $.ui.status((await launch($, options)) ? `${ADAPTERS[name]} · ${BASE}` : "сервис не поднялся");
  })();
  return `→ ${ADAPTERS[name]}`;
}

// ── правила голосовых сообщений для Claude ────────────────────────────────────

const HEARD = `Сообщения, которые начинаются с ${MARK}, пришли голосом: распознанная речь человека почти дословно, возможны ошибки распознавания.`;
const ALOUD = "Твой ответ зачитают вслух, поэтому: 1–3 короткие фразы простыми словами, без списков, таблиц, кода и разметки; сначала суть.";
const CONSENT =
  "Если сказано поручить работу (править файлы, запускать команды, что-то делать в проекте), не начинай без спроса: одной фразой " +
  "скажи, что собираешься сделать, и спроси «делаю?». Вопросы, объяснения и чтение файлов можно сразу.";

export function register(on, options) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    if (!e.isInteractive || started) return result;
    started = true;
    PORT = Number(await $.env.get("GV_PORT")) || 8790;
    BASE = `http://127.0.0.1:${PORT}`;
    chosen = String(await $.fs.read(await voiceFile($)).catch(() => "")).trim();
    await $.command.register({
      name: "gv",
      description: `Голос: без имени — включить или выключить; с именем — сменить адаптер (${Object.keys(ADAPTERS).join(", ")})`,
      argumentHint: `[${Object.keys(ADAPTERS).join("|")}]`,
    });
    // Пока идёт пинг, сервис живёт; пингует только хозяин.
    $.clock.every(PING_MS, () => void (running && mine($).then((my) => my && post($, "/ping")).catch(() => {})));
    const off = String(await $.fs.read(await stateFile($)).catch(() => "")).trim() === "off";
    if (off) $.ui.status("выкл");
    else void turnOn($, options);
    return result;
  });

  // /gv — переключатель; /gv <адаптер> — другой адаптер. Голос ушёл в другую сессию — /gv здесь забирает его обратно.
  on("command.run", { command: "gv" }, async ($, e) => {
    if (running && !(await mine($))) running = false;
    const name = e.args.trim().toLowerCase();
    if (name) return { text: name in ADAPTERS ? await choose($, options, name) : `нет «${name}»: ${Object.keys(ADAPTERS).join(" · ")}` };
    if (running) {
      await turnOff($);
      return { text: "выкл" };
    }
    void turnOn($, options);
    return { text: `вкл · ${ADAPTERS[voiceOf(options)]}` };
  });

  // Сессия закрылась — свой сервис уходит сразу, не ждёт потерянных пингов; чужой не трогаем.
  on("session.end", async ($, e, next) => {
    if (running && (await mine($))) await post($, "/quit").catch(() => {});
    return next(e);
  });

  // Правила для голосовых сообщений: распознанная речь, поручения — только после согласия; у Gemini ответ ещё и звучит.
  on("prompt.compose", async ($, e, next) => {
    const base = await next(e);
    const text = [HEARD, ...(voiceOf(options) === "gemini" ? [ALOUD] : []), CONSENT].join(" ");
    return { sections: [...base.sections, { id: "gv:voice", scope: "session" as const, text }] };
  });

  // Ход отправленного голосового сообщения — ход его запроса (узнаём по тексту: поле ввода могло поставить сообщение в очередь).
  // Ход без новой реплики сразу после голосового (продолжение или итог фоновой задачи, <task-notification>) — тот же запрос;
  // набранное руками обрывает цепочку.
  on("turn.start", async ($, e, next) => {
    if (e.agentId) return next(e);
    mainBusy = true;
    void turn($);
    const id = expected.get(e.text) ?? (e.text.startsWith(MARK) ? expected.values().next().value : undefined);
    if (id) {
      for (const [text, v] of expected) if (v === id) expected.delete(text);
      current = id;
      if (!voiceMessages.has(id)) { // ушло очередью: UUID — из истории
        void bridge($, "list", {}).then((list) => {
          const uuid = list.messages?.findLast((m: { text: string }) => m.text === e.text)?.uuid;
          if (uuid && !voiceMessages.has(id)) voiceMessages.set(id, uuid);
        }).catch(() => {});
      }
      return next(e);
    }
    const continued = e.text === "" || e.text.startsWith("<task-notification>");
    if (continued && !current && lastVoice) current = lastVoice;
    else if (!continued) lastVoice = 0;
    return next(e);
  });

  // Под голосовым сообщением — его quick: «▸ ⚡ quick №N», нажатие раскрывает вопрос и ответ.
  on("ui.render", { component: "UserMessage" }, async ($, e, next) => {
    const rows = (await read($, quickRows))[e.requestId];
    if (!rows?.length) return next(e);
    const { Box, Text, Button } = $.ui.resolve(e);
    const open = await read($, quickOpen);
    const toggle = (id: number) => () => void update($, quickOpen, (ids) => (ids.includes(id) ? ids.filter((x) => x !== id) : [...ids, id]));
    return h(Box, { flexDirection: "column" }, await next(e), ...rows.map((q) =>
      h(Box, { flexDirection: "column", marginLeft: 2 },
        h(Button, { onPress: toggle(q.id) }, `${open.includes(q.id) ? "▾" : "▸"} ⚡ quick №${q.id}`),
        ...(open.includes(q.id) ? [h(Text, { dimColor: true }, `→ ${q.q}\n← ${q.a}`)] : []))));
  });

  // Голосовой ход кончился — ответ сервису: текст; пустой — ход прерван или без текста (Claude всё равно свободен); ошибка — ход
  // упал или Claude отказался. Затем, после любого хода, — что Claude свободен. Побочные ходы (агенты) не трогаем.
  on("turn.complete", async ($, e, next) => {
    if (e.agentId) return next(e);
    mainBusy = false;
    const id = current;
    current = 0;
    if (id) {
      lastVoice = id;
      const failed = e.reason === "error" || e.reason === "refusal";
      if (failed) $.ui.status(`✗ №${id}: ${failure(e)}`);
      await answer($, id, failed ? { error: failure(e) } : { text: e.reason === "answer" ? e.answer.trim() : "" });
    }
    await turn($);
    return next(e);
  });
}
