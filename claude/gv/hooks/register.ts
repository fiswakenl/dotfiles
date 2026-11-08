// Голос через Gemini Live ⇄ сессия («глухой телефон»).
//
// Человек говорит с Gemini; она пересказывает его слова чистым текстом (voice.py, функция say_to_claude).
// Плагин забирает текст ручкой /next и кладёт в сессию как сообщение человека; когда ход кончился,
// отдаёт ответ ручкой /answer, и Gemini озвучивает его. Пока ход идёт, сервис держит новые вопросы в очереди:
// основной ответ не перебиваем. Побочное (/btw) Gemini отвечает сама, мимо сессии.
//
// Путь по умолчанию — say_to_claude (сообщение в чат); /btw — быстрые ответы ветки, пока основной ход занят.
// Сервис voice.py поднимается на старте сессии и выходит сам, если плагин перестал пинговать.
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

// Работает одна сессия с плагином: новая вытесняет старую — просит прежний сервис выйти (/takeover) и занимает его порт.
// Ручки сервиса открыты только по токену сессии: вытесненная сессия получает 403 и замолкает, чужие вопросы не забирает.
const PORT = 8790;
const BASE = `http://127.0.0.1:${PORT}`;
const TRIES = 6;
const MARK = "🎙"; // голосовое сообщение: по метке система знает, что ответ зачитают вслух
const PING_MS = 20_000;
const RETRY_MS = 2_000;
const TOKEN = Math.random().toString(36).slice(2) + Math.random().toString(36).slice(2);

// Ход, начатый голосом: пока он не кончился, ответ уйдёт Gemini.
let voiceTurn = false;
let started = false;
let displaced = false;

const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

const call = ($, path: string, init: { method?: string; body?: string } = {}) =>
  $.http.fetch(`${BASE}${path}`, { ...init, headers: { "x-gv-token": TOKEN, "content-type": "application/json" } });

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

export function register(on, options) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    if (!e.isInteractive || started) return result;
    started = true;

    // Сервис живёт, пока живёт цикл. Порт занят прежней сессией — просим её сервис выйти и ждём, пока порт освободится.
    // Не поднялся — пробуем снова несколько раз; поработал и вышел (нас вытеснили или сессия кончилась) — не поднимаем.
    void (async () => {
      let opened = false;
      for (let attempt = 0; attempt < TRIES; attempt++) {
        await $.http.fetch(`${BASE}/takeover`, { method: "POST" }).catch(() => {});
        for (let i = 0; i < 20; i++) {
          if (!(await $.http.fetch(`${BASE}/`).catch(() => null))) break;
          await sleep(250);
        }
        const startedAt = Date.now();
        // Страницу открываем, когда сервис отвечает.
        void (async () => {
          for (let i = 0; i < 40 && !opened; i++) {
            await sleep(500);
            if ((await post($, "/ping").catch(() => null))?.ok) {
              opened = true;
              if (options.open === "on") void $.process.run(["open", BASE]).catch(() => {});
              else $.ui.status(`gv: ${BASE}`);
            }
          }
        })();
        try {
          const server = $.process.spawn({
            argv: ["uv", "run", "-q", "--script", `${$.plugin.root}/voice.py`],
            env: { GV_PORT: String(PORT), GV_TOKEN: TOKEN, GV_DUPLEX: options.duplex === "off" ? "0" : "1" },
          });
          for await (const { stream, text } of server) {
            if (stream === "stderr") $.ui.log(`gv: ${text.trim().slice(0, 300)}`, { to: "debug" });
          }
        } catch (err) {
          $.ui.log(`gv: сервис не запустился: ${String(err)}`, { to: "debug" });
        }
        if (Date.now() - startedAt > 15_000) return;
        await sleep(1000);
      }
      $.ui.status("gv: сервис не поднялся");
    })();

    // Пинг: пока он идёт, сервис живёт.
    $.clock.every(PING_MS, () => void post($, "/ping").catch(() => {}));

    // Цикл забора вопросов: /next ждёт до 25 с и отдаёт 204, если вопросов нет. 403 — нас вытеснила другая сессия.
    void (async () => {
      while (!displaced) {
        try {
          const r = await call($, "/next");
          if (r.status === 403) {
            displaced = true;
            $.ui.status("gv: голос взяла другая сессия");
          } else if (r.status === 200 && r.text) {
            const { text } = JSON.parse(r.text);
            if (text) {
              voiceTurn = true;
              await $.prompt.submit({ text: `${MARK} ${text}`, asUser: true });
            }
          } else if (r.status !== 204) {
            await sleep(RETRY_MS);
          }
        } catch {
          await sleep(RETRY_MS);
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
      await post($, "/answer", e.answer.trim()).catch(() => {});
    }
    return next(e);
  });
}
