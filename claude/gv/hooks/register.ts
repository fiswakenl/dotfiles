// Голос через Gemini Live ⇄ сессия Claude Code.
//
// Человек говорит с Gemini; она пересказывает его слова вызовами ask и quick (voice.py, логика — core.py).
// Плагин забирает запросы ручкой /next: ask — сообщением в окно Claude через мост (native/bridge.mjs), с откатом, если это поправка;
// quick — веткой разговора ($.model.fork), основной ход не трогает. Ответ — ручкой /answer, Gemini его озвучит.
// Сервис сам перезапускается при правке своих файлов (раз в 10 с); правка этого файла перезагружает плагин и сервис.
// Сервис поднимается на старте сессии и выходит сам, если плагин перестал пинговать.
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

// Работает одна сессия с плагином: новая вытесняет старую — просит прежний сервис выйти (/quit) и занимает его порт.
let PORT = 8790;
let BASE = `http://127.0.0.1:${PORT}`;
const TRIES = 6;
const MARK = "🎙"; // голосовое сообщение: по метке система знает, что ответ зачитают вслух
const PING_MS = 20_000;
const RETRY_MS = 2_000;
const TOKEN = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);

let current = 0; // запрос, над которым идёт голосовой ход; 0 — ход не голосовой
let started = false;
let running = false; // голос включён в этом экземпляре плагина
let healing = false;
const voiceMessages = new Map<number, string>(); // номер запроса → UUID его сообщения в истории Claude

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const call = ($, path: string, init: { method?: string; body?: string } = {}) =>
  $.http.fetch(`${BASE}${path}`, { ...init, headers: { "content-type": "application/json" } });

const post = ($, path: string, body: unknown = {}) => call($, path, { method: "POST", body: JSON.stringify(body) });

const answer = ($, id: number, body: { text?: string; error?: string }) => post($, `/answer?id=${id}`, body).catch(() => {});

// Мост в окно Claude теми же именами, что у /call: send, rewind.
async function bridge($, name: string, args: Record<string, unknown>): Promise<any> {
  const socket = await $.env.get("GV_NATIVE_SOCKET");
  if (!socket) throw new Error("мост не подключён: запустите Claude через ccg");
  const out = await $.process.run(["python3", `${$.plugin.root}/native/client.py`, "--socket", socket, name, JSON.stringify(args)]);
  const result = JSON.parse(out.stdout || "{}");
  if (out.exitCode !== 0 || result.error) throw new Error(result.error || out.stderr || "мост не ответил");
  return result;
}

// ask: в окно Claude как набранное руками; поправка — откат до сообщения прежнего запроса и новый текст.
async function deliver($, r: { id: number; text: string; rewind: number | null }): Promise<void> {
  const text = `${MARK} ${r.text}`;
  const target = r.rewind ? voiceMessages.get(r.rewind) : undefined;
  if (r.rewind && !target) throw new Error(`сообщение запроса №${r.rewind} не найдено в истории`);
  current = 0; // завершение отменяемого хода не выдаём за ответ
  const result = await bridge($, target ? "rewind" : "send", target ? { target, text } : { text });
  for (const [id, uuid] of voiceMessages) if (result.removed?.includes(uuid)) voiceMessages.delete(id);
  voiceMessages.set(r.id, result.uuid);
  current = r.id;
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
  // В чат — строкой-пометкой: модель её не читает, ход не запускается.
  await $.session.append({ message: { type: "system", content: [{ type: "text", text: `⚡ ${r.text}\n← ${text}` }] } }).catch(() => {});
}

// Хозяин сервиса записан в файле: новый экземпляр плагина (другая сессия или перезагрузка) пишет туда свой номер, старый видит чужой
// номер и молча уходит — не опрашивает и не поднимает сервис заново.
async function tokenFile($): Promise<string> {
  return `${await $.env.get("HOME")}/.claude/gv${PORT === 8790 ? "" : "-" + PORT}.token`;
}

async function stale($): Promise<boolean> {
  const owner = String(await $.fs.read(await tokenFile($)).catch(() => "")).trim();
  return owner !== "" && owner !== TOKEN;
}

// Поднять сервис отдельным процессом (вывод — ~/.claude/gv.out); true — отвечает.
async function launch($, options): Promise<boolean> {
  await $.fs.write(await tokenFile($), TOKEN).catch(() => {});
  await post($, "/quit").catch(() => {});
  for (let i = 0; i < 20; i++) {
    if (!(await $.http.fetch(`${BASE}/`).catch(() => null))) break;
    await sleep(250);
  }
  const env = [`GV_PORT=${PORT}`, `GV_DUPLEX=${options.duplex === "off" ? "0" : "1"}`, `GV_PAUSE_MS=${String(options.pause ?? "2500")}`];
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

// Цикл запросов: ask — по одному, quick — сразу в фоне. Сервис не отвечает несколько раз подряд — поднимаем его заново.
async function poll($, options): Promise<void> {
  let down = 0;
  while (running) {
    if (await stale($)) return; // сервис теперь у другого экземпляра плагина или остановлен: молча уходим
    let r;
    try {
      r = await call($, "/next?wait=1");
      down = 0;
    } catch {
      await sleep(RETRY_MS);
      if (!running || (await stale($))) return;
      if (++down >= 3 && !healing) {
        healing = true;
        down = 0;
        await launch($, options).catch(() => false);
        healing = false;
      }
      continue;
    }
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
      await deliver($, item);
    } catch (error) {
      await answer($, item.id, { error: String(error) });
      $.ui.status(`gv: ${String(error)}`);
    }
  }
}

// Файл ~/.claude/gv.state ("on" / "off"): включён ли голос. Переживает перезагрузку плагина и перезапуск сессии; меняет его команда /gv.
async function stateFile($): Promise<string> {
  return (await $.env.get("GV_STATE_FILE")) || `${await $.env.get("HOME")}/.claude/gv.state`;
}

// Включить: стать хозяином, поднять сервис, открыть страницу, запустить цикл запросов.
async function turnOn($, options): Promise<void> {
  if (running) return;
  running = true;
  await $.fs.write(await stateFile($), "on").catch(() => {});
  // Хозяином становимся сразу: иначе собственный опрос успеет прочитать чужой номер в файле и решит, что его вытеснили.
  await $.fs.write(await tokenFile($), TOKEN).catch(() => {});
  $.ui.status(undefined);
  void poll($, options);
  let up = false;
  for (let attempt = 0; attempt < TRIES && !up && running; attempt++) up = await launch($, options);
  if (!running) return;
  if (!up) return void $.ui.status("gv: сервис не поднялся");
  if (options.open === "on" && (await $.env.get("GV_TEXT_ONLY")) !== "1") void $.process.run(["open", BASE]).catch(() => {});
  else $.ui.status(`gv: ${BASE}`);
}

// Выключить: цикл стоит, сервис и сокет Gemini закрыты, выключенность запомнена.
async function turnOff($): Promise<void> {
  running = false;
  await $.fs.write(await stateFile($), "off").catch(() => {});
  await post($, "/quit").catch(() => {});
  $.ui.status("gv: выключен, /gv включает");
}

export function register(on, options) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    if (!e.isInteractive || started) return result;
    started = true;
    PORT = Number(await $.env.get("GV_PORT")) || 8790;
    BASE = `http://127.0.0.1:${PORT}`;
    await $.command.register({ name: "gv", description: "Включить или выключить голос (сервис Gemini и сокеты)" });
    $.clock.every(PING_MS, () => void post($, "/ping").catch(() => {})); // пока идёт пинг, сервис живёт
    const off = String(await $.fs.read(await stateFile($)).catch(() => "")).trim() === "off";
    if (off) $.ui.status("gv: выключен, /gv включает");
    else void turnOn($, options);
    return result;
  });

  // /gv — переключатель: включён → выключить, выключен → включить.
  on("command.run", { command: "gv" }, async ($) => {
    if (running) {
      await turnOff($);
      return { text: "gv выключен: сервис и сокеты остановлены. /gv — включить." };
    }
    void turnOn($, options);
    return { text: "gv включается: сервис поднимается, страница откроется сама." };
  });

  // Сессия закрылась — сервис с сокетом Gemini уходит сразу, не ждёт потерянных пингов.
  on("session.end", async ($, e, next) => {
    await post($, "/quit").catch(() => {});
    return next(e);
  });

  // Правила для голосовых сообщений: коротко, зачитывается вслух, поручения — только после согласия.
  on("prompt.compose", async ($, e, next) => {
    const base = await next(e);
    return {
      sections: [
        ...base.sections,
        {
          id: "gv:voice",
          scope: "session" as const,
          text:
            `Сообщения, которые начинаются с ${MARK}, пришли голосом через посредника: человек говорил, ` +
            "посредник привёл слова в порядок. Твой ответ зачитают вслух, поэтому: 1–3 короткие фразы простыми " +
            "словами, без списков, таблиц, кода и разметки; сначала суть. Если сказано поручить работу " +
            "(править файлы, запускать команды, что-то делать в проекте), не начинай без спроса: одной фразой " +
            "скажи, что собираешься сделать, и спроси «делаю?». Вопросы, объяснения и чтение файлов можно сразу.",
        },
      ],
    };
  });

  // Голосовой ход кончился — ответ сервису (пустой, если ответа нет: Claude всё равно свободен). Побочные ходы (агенты) не трогаем.
  on("turn.complete", async ($, e, next) => {
    if (e.agentId || !current) return next(e);
    const id = current;
    current = 0;
    await answer($, id, { text: e.reason === "answer" ? e.answer.trim() : "" });
    return next(e);
  });
}
