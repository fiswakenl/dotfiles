// Голос через Gemini Live ⇄ сессия («глухой телефон»).
//
// Человек говорит с Gemini; она пересказывает его слова чистым текстом (voice.py, функция say_to_claude).
// Плагин забирает текст ручкой /next и кладёт в сессию как сообщение человека; когда ход кончился,
// отдаёт ответ ручкой /answer, и Gemini озвучивает его. Пока ход идёт, сервис держит новые вопросы в очереди:
// основной ответ не перебиваем. Побочное (/btw) Gemini отвечает сама, мимо сессии.
//
// Путь по умолчанию — say_to_claude (сообщение в чат); /btw — быстрые ответы ветки, пока основной ход занят.
// Сервис сам перезапускается при правке voice.py и prompt.md (раз в 10 с); правка этого файла перезагружает плагин и сервис.
// Сервис voice.py поднимается на старте сессии и выходит сам, если плагин перестал пинговать.
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

// Работает одна сессия с плагином: новая вытесняет старую — просит прежний сервис выйти (/takeover) и занимает его порт.
const PORT = 8790;
const BASE = `http://127.0.0.1:${PORT}`;
const TRIES = 6;
const MARK = "🎙"; // голосовое сообщение: по метке система знает, что ответ зачитают вслух
const PING_MS = 20_000;
const RETRY_MS = 2_000;
const POLL_MS = 400; // пауза между короткими опросами /next
const TOKEN = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);

// Ход, начатый голосом: пока он не кончился, ответ уйдёт Gemini.
let voiceTurn = false;
let currentId = 0; // номер запроса, над которым идёт ход
let started = false;
let displaced = false;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const call = ($, path: string, init: { method?: string; body?: string } = {}) =>
  $.http.fetch(`${BASE}${path}`, { ...init, headers: { "content-type": "application/json" } });

const post = ($, path: string, body?: unknown) =>
  call($, path, { method: "POST", body: body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body) });

// Быстрый вопрос /btw: ветка разговора отвечает по тому, что в нём уже есть, пока основной ход занят; инструменты не нужны.
const BTW_PROMPT =
  "Служебный запрос голосового плагина gv, не задача из разговора. Основная работа идёт своим чередом: " +
  "не продолжай её и не меняй, инструменты не вызывай. Ответь на быстрый вопрос ниже по разговору выше, " +
  "как будто говоришь вслух: 1–3 короткие фразы простыми словами, без списков и разметки. " +
  "Если в разговоре ответа нет, так и скажи.";

async function btw($, q: { id: number; text: string }): Promise<void> {
  let text = "Не получилось ответить.";
  try {
    const reply = await $.model.fork({ prompt: `${BTW_PROMPT}\n\n<вопрос>\n${q.text}\n</вопрос>` });
    if (reply.reason === "nothing-to-fork") text = "В сессии пока нечего вспомнить: разговор только начался.";
    else if (reply.isAnswered && reply.text.trim()) text = reply.text.trim();
  } catch {}
  await post($, `/quickanswer?id=${q.id}`, text).catch(() => {});
  // В чат — строкой-пометкой: модель её не читает, ход не запускается, основная работа не страдает.
  await $.session
    .append({ message: { type: "system", content: [{ type: "text", text: `⚡ btw: ${q.text}\n← ${text}` }] } })
    .catch(() => {});
}

let healing = false;

// Хозяин сервиса записан в файле: новый экземпляр плагина (другая сессия или перезагрузка) пишет туда свой номер, старый видит чужой
// номер и молча уходит — не опрашивает и не поднимает сервис заново. Токенов в самом сервисе нет.
async function tokenFile($): Promise<string> {
  const home = await $.env.get("HOME");
  return `${home}/.claude/gv.token`;
}

async function stale($): Promise<boolean> {
  const owner = String(await $.fs.read(await tokenFile($)).catch(() => "")).trim();
  return owner !== "" && owner !== TOKEN;
}

// Поднять сервис отдельным процессом (лог — ~/.claude/gv.out); true — отвечает.
async function launch($, options): Promise<boolean> {
  await $.fs.write(await tokenFile($), TOKEN).catch(() => {});
  await $.http.fetch(`${BASE}/takeover`, { method: "POST" }).catch(() => {});
  for (let i = 0; i < 20; i++) {
    if (!(await $.http.fetch(`${BASE}/`).catch(() => null))) break;
    await sleep(250);
  }
  const env =
    `GV_PORT=${PORT} GV_DUPLEX=${options.duplex === "off" ? "0" : "1"} GV_PAUSE_MS=${String(options.pause ?? "2500")}`;
  await $.process
    .run(["sh", "-c", `${env} nohup uv run -q --script '${$.plugin.root}/voice.py' >>"$HOME/.claude/gv.out" 2>&1 &`])
    .catch((err) => $.ui.log(`gv: запуск не удался: ${String(err)}`, { to: "debug" }));
  for (let i = 0; i < 40; i++) {
    await sleep(500);
    if ((await post($, "/ping").catch(() => null))?.ok) return true;
  }
  return false;
}

export function register(on, options) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    if (!e.isInteractive || started) return result;
    started = true;
    $.ui.status(undefined);
    // Хозяином становимся сразу и до всего остального: иначе собственный опрос успеет прочитать чужой номер в файле и решит, что его вытеснили.
    await $.fs.write(await tokenFile($), TOKEN).catch(() => {});

    // Сервис — отдельный процесс, не дочерний: перезагрузка плагина его не убивает. Занятый порт — просим прежний сервис выйти.
    void (async () => {
      let up = false;
      for (let attempt = 0; attempt < TRIES && !up; attempt++) up = await launch($, options);
      if (!up) return void $.ui.status("gv: сервис не поднялся");
      $.ui.status(undefined); // убрать устаревшие предупреждения прежних экземпляров
      if (options.open === "on") void $.process.run(["open", BASE]).catch(() => {});
      else $.ui.status(`gv: ${BASE}`);
    })();

    // Пинг: пока он идёт, сервис живёт.
    $.clock.every(PING_MS, () => void post($, "/ping").catch(() => {}));

    // Цикл забора вопросов: /next ждёт до 25 с и отдаёт 204, если вопросов нет. 403 — нас вытеснила другая сессия.
    // Сервис не отвечает несколько раз подряд — поднимаем его заново.
    void (async () => {
      let down = 0;
      while (!displaced) {
        if (await stale($)) break; // сервис теперь у другого экземпляра плагина или остановлен: молча уходим
        try {
          const r = await call($, "/next?wait=1");
          down = 0;
          if (r.status === 200 && r.text) {
            const { id, text, replaces } = JSON.parse(r.text);
            if (text) {
              voiceTurn = true;
              currentId = id;
              const fix = replaces ? "(поправка к прежнему запросу, новый текст полный) " : "";
              await $.prompt.submit({ text: `${MARK} ${fix}${text}`, asUser: true });
            }
          } else if (r.status === 204) {
            await sleep(POLL_MS);
          } else {
            await sleep(RETRY_MS);
          }
        } catch {
          await sleep(RETRY_MS);
          if (await stale($)) {
            displaced = true;
            return;
          }
          if (++down >= 3 && !healing && !displaced) {
            healing = true;
            down = 0;
            await launch($, options).catch(() => false);
            healing = false;
          }
        }
      }
    })();

    // Быстрые вопросы /btw: свой цикл, не ждёт основного хода.
    void (async () => {
      while (!displaced) {
        try {
          const r = await call($, "/quick");
          if (r.status === 200 && r.text) void btw($, JSON.parse(r.text));
          else if (r.status !== 204) await sleep(RETRY_MS);
        } catch {
          await sleep(RETRY_MS);
        }
      }
    })();

    return result;
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

  // Ход начался голосом — сообщаем сервису, что Claude занят (очередь не пускает новых).
  on("turn.start", async ($, e, next) => {
    if (voiceTurn && !e.agentId) void post($, "/busy", { busy: true }).catch(() => {});
    return next(e);
  });

  // Ход кончился: ответ — Gemini, Claude свободен. Побочные ходы (агенты) не трогаем.
  on("turn.complete", async ($, e, next) => {
    if (e.agentId || !voiceTurn) return next(e);
    voiceTurn = false;
    await post($, "/busy", { busy: false }).catch(() => {});
    if (e.reason === "answer" && e.answer.trim()) {
      await post($, `/answer?id=${currentId}`, e.answer.trim()).catch(() => {});
    }
    return next(e);
  });
}
