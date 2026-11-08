// Голос через Gemini Live ⇄ сессия («глухой телефон»).
//
// Человек говорит с Gemini; она пересказывает его слова чистым текстом (voice.py, функция say_to_claude).
// Плагин забирает текст ручкой /next и кладёт в сессию как сообщение человека; когда ход кончился,
// отдаёт ответ ручкой /answer, и Gemini озвучивает его. Пока ход идёт, сервис держит новые вопросы в очереди:
// основной ответ не перебиваем. Побочное (/btw) Gemini отвечает сама, мимо сессии.
//
// Сервис voice.py поднимается на старте сессии и выходит сам, если плагин перестал пинговать.
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

const PORT = 8790;
const BASE = `http://127.0.0.1:${PORT}`;
const MARK = "🎙"; // голосовое сообщение: по метке система знает, что ответ зачитают вслух
const PING_MS = 20_000;
const RETRY_MS = 2_000;

// Ход, начатый голосом: пока он не кончился, ответ уйдёт Gemini.
let voiceTurn = false;
let started = false;

const post = ($, path: string, body?: unknown) =>
  $.http.fetch(`${BASE}${path}`, {
    method: "POST",
    headers: { "content-type": "application/json" },
    body: body === undefined ? "" : typeof body === "string" ? body : JSON.stringify(body),
  });

export function register(on, options) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    if (!e.isInteractive || started) return result;
    started = true;

    // Сервис живёт, пока живёт цикл; закончился сам (нет ключа, порт занят) — не мешаем сессии.
    void (async () => {
      try {
        const server = $.process.spawn({ argv: ["uv", "run", "-q", "--script", `${$.plugin.root}/voice.py`] });
        for await (const { stream, text } of server) {
          if (stream === "stderr") $.ui.log(`gv: ${text.trim().slice(0, 300)}`, { to: "debug" });
        }
      } catch (err) {
        $.ui.log(`gv: сервис не запустился: ${String(err)}`, { to: "debug" });
      }
    })();

    // Пинг: пока он идёт, сервис живёт.
    $.clock.every(PING_MS, () => void post($, "/ping").catch(() => {}));

    // Цикл забора вопросов: /next ждёт до 25 с и отдаёт 204, если вопросов нет.
    void (async () => {
      for (;;) {
        try {
          const r = await $.http.fetch(`${BASE}/next`);
          if (r.status === 200 && r.text) {
            const { text } = JSON.parse(r.text);
            if (text) {
              voiceTurn = true;
              await $.prompt.submit({ text: `${MARK} ${text}`, asUser: true });
            }
          } else if (r.status !== 204) {
            await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
          }
        } catch {
          await new Promise((resolve) => setTimeout(resolve, RETRY_MS));
        }
      }
    })();

    if (options.open === "on") {
      $.clock.after(3000, () => void $.process.run(["open", BASE]).catch(() => {}));
    } else {
      $.ui.status(`gv: ${BASE}`);
    }
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
