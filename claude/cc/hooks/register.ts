// Чистка надиктованного промпта перед отправкой.
//
// Настройки — userConfig в plugin.json: там значения, дефолты и описания. Движок
// подставляет дефолты и отбрасывает значения не из списка, поэтому здесь options
// читаются как есть. Хранятся в ~/.claude/settings.json → pluginConfigs."cc@inline".
// Промпт чистильщика — prompt.md в корне плагина. Читается на каждой чистке:
// правку видно сразу. Нет файла — реплика уходит как есть.
//
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

import { buildContext, buildSystem, parseAnswer } from "./clean.ts";

// Своих имён для сочетаний завести нельзя — занимаем чужое, из списка движка.
// Свой чорд вешаешь на него в ~/.claude/keybindings.json.
const ACTION = "app:cycleDiffBase";

// Чистим только то, что человек набрал сам: с клавиатуры или с телефона.
const TYPED = ["composer", "bridge"];

// Крутилка в поле, пока идёт чистка: несколько штук со сдвигом — крутятся вразнобой.
const FRAMES = [..."⠙⠼⠇⠹⠏⠴⠸⠋⠦⠙⠧⠼⠏⠹⠇⠴⠋⠸⠦⠧"];
const SPINNERS = 3;
const TICK_MS = 120;

// По умолчанию движок режет ответ на 256 токенах — задача обрывается на полуслове.
const MAX_TOKENS = 2000;

// Журнал правок: «было» и «стало» по строке на чистку. Без него исходной реплики
// нет нигде — в транскрипт уходит переписанное. Лежит вне плагина: это личные реплики.
const LOG_KEEP = 500;

export function register(on, options) {
  on("prompt.submit", async ($, e, next) => {
    if (!e.text.trim() || !TYPED.includes(e.origin?.kind ?? "composer")) return next(e);
    if (options.mode !== "auto") return next(e);

    // Поле после Enter пустое — крутим в нём, пока думает чистильщик, и убираем
    // за собой. Начал печатать следующий промпт — не мешаем.
    const cleaning = clean($, e.text, options);
    const untouched = await spin($, cleaning);
    const text = await cleaning;
    if (untouched) await $.prompt.fill({ text: "" });
    if (!text || text === e.text) return next(e);
    if (options.delivery === "rewrite") return next({ ...e, text });

    // resend: remote control рисует то, что отправил сам, и подмену не видит.
    // Сырой промпт роняем, чистый отправляем от плагина. Сразу из хука submit не
    // проходит — движок ещё занят этим промптом, поэтому чуть позже.
    $.clock.after(300, () => {
      $.prompt.submit({ text, asUser: true })
        .catch((err) => log($, { resubmit: "failed", error: String(err) }));
    });
    return { drop: "причёсано, отправлено заново" };
  });

  // Кнопка над полем — только в input: в auto чистит сам Enter, в off не чистит никто.
  // Матчер обязателен — без него хук на компонент не подписывается.
  on("ui.render", { component: "AbovePrompt" }, async ($, e, next) => {
    if (options.mode !== "input") return next(e);
    const { Button } = $.ui.resolve(e);
    return Button({
      key: "clean",
      label: "почистить ⏎",
      action: ACTION,
      plain: true,
      color: "warning",
      onPress: () => rephrase($, options),
    });
  });
}

// Кнопка: черновик из поля — чистка — обратно в поле. Ничего не отправляет.
async function rephrase($, options): Promise<void> {
  const { text } = await $.prompt.read();
  if (!text.trim()) return;
  const cleaning = clean($, text, options);
  const untouched = await spin($, cleaning);
  const fixed = await cleaning;
  if (untouched) await $.prompt.fill({ text: fixed || text });
}

// Крутим, пока не доедет чистка. false — поле тронули руками.
async function spin($, until: Promise<unknown>): Promise<boolean> {
  let done = false;
  until.then(() => (done = true), () => (done = true));
  for (let i = 0; !done; i++) {
    const shown = Array.from({ length: SPINNERS },
      (_, n) => FRAMES[(i + n * 3) % FRAMES.length]).join("");
    await $.prompt.fill({ text: shown });
    if ((await $.prompt.read()).text !== shown) return false;
    await new Promise((resolve) => setTimeout(resolve, TICK_MS));
  }
  return true;
}

// Одно завершение прямо из плагина: без сессии, CLAUDE.md, скиллов и инструментов —
// иначе чистильщик отвечает по существу вместо правки. Разговор он видит данными
// в системном промпте: без него «убери тот скилл» не раскрыть.
// Любая осечка — null, и уходит исходный текст: текст дороже чистки.
async function clean($, text: string, options): Promise<string | null> {
  const who = options.model;
  try {
    const reply = await $.model.complete({
      model: who,
      system: buildSystem(text, await context($), await prompt($)),
      prompt: `<реплика>\n${text}\n</реплика>`,
      maxTokens: MAX_TOKENS,
    });
    const raw = replyText(reply);
    const cleaned = raw === null ? null : parseAnswer(raw);
    await log($, { model: who, before: text, after: cleaned,
                   ...(raw === null ? { reply: JSON.stringify(reply)?.slice(0, 500) } : {}) });
    return cleaned;
  } catch (error) {
    await log($, { model: who, before: text, after: null, error: String(error) });
    return null;
  }
}

async function prompt($): Promise<string> {
  return String(await $.fs.read(`${$.plugin.root}/prompt.md`).catch(() => ""));
}

async function context($): Promise<string> {
  return buildContext(await $.session.messages().catch(() => []));
}

// Ответ модели бывает строкой, { text }, { content: [...] } или массивом блоков.
// String() от объекта дал бы «[object Object]» в чат. Текста нет — null.
function replyText(reply): string | null {
  if (typeof reply === "string") return reply;
  if (Array.isArray(reply)) {
    const parts = reply.map(replyText).filter((part) => part !== null);
    return parts.length ? parts.join("") : null;
  }
  if (reply && typeof reply === "object") {
    if (typeof reply.text === "string") return reply.text;
    if (reply.content !== undefined) return replyText(reply.content);
    if (reply.message !== undefined) return replyText(reply.message);
  }
  return null;
}

// $.fs умеет только read и write: перечитываем, дописываем, режем хвост.
// Не записался — чистку это не касается.
async function log($, entry): Promise<void> {
  try {
    const home = await $.env.get("HOME");
    if (!home) return;
    const file = `${home}/.claude/cc-clean.jsonl`;
    const had = String(await $.fs.read(file).catch(() => ""));
    const lines = [...had.split("\n").filter(Boolean),
                   JSON.stringify({ at: new Date().toISOString(), ...entry })];
    await $.fs.write(file, lines.slice(-LOG_KEEP).join("\n") + "\n");
  } catch {}
}
