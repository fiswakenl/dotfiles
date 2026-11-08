// Чистка надиктованного промпта перед отправкой.
//
// Настройки — userConfig в plugin.json: там значения, дефолты и описания. Движок
// подставляет дефолты и отбрасывает значения не из списка, поэтому здесь options
// читаются как есть. Хранятся в ~/.claude/settings.json → pluginConfigs."cc@inline".
// Промпт чистильщика — prompt.md в корне плагина. Читается на каждой чистке:
// правку видно сразу. Нет файла — реплика уходит как есть. Правила проверки
// агентом — check.md рядом, подмешиваются, только когда она включена.
//
// Все обращения к движку — только здесь: `$` через импорт не передаётся.

import { buildContext, buildForkPrompt, buildRules, buildSystem, parseAnswer, parseCheck } from "./clean.ts";

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

// Реплики, которые ждут проверки агентом: id агента → сырой текст.
// Перезагрузка плагина их теряет — сырой текст тогда остаётся только в журнале.
const waiting = new Map<string, string>();

export function register(on, options) {
  on("prompt.submit", async ($, e, next) => {
    if (!e.text.trim() || !TYPED.includes(e.origin.kind)) return next(e);
    if (options.mode !== "auto") return next(e);

    // Поле после Enter пустое — крутим в нём, пока думает чистильщик, и убираем
    // за собой. Начал печатать следующий промпт — не мешаем.
    const cleaning = clean($, e.text, options);
    const untouched = await spin($, cleaning);
    const text = await cleaning;
    if (untouched) await $.prompt.fill({ text: "" });
    const question = text ? parseCheck(text) : null;
    if (question) return options.check === "on" ? verify($, e, next, question) : next(e);
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

  // Агент проверки ответил: дочищаем с тем, что он нашёл, и отправляем.
  // Не ответил — уходит сырой текст: текст дороже чистки.
  on("turn.complete", async ($, e, next) => {
    const raw = e.agentId ? waiting.get(e.agentId) : undefined;
    if (raw === undefined) return next(e);
    waiting.delete(e.agentId);
    $.ui.status(undefined);
    await log($, { check: e.reason, agentId: e.agentId, found: e.answer.slice(0, 1000) });
    const text = e.reason === "answer" ? await clean($, raw, options, e.answer) : null;
    const final = text && !parseCheck(text) ? text : raw;
    $.prompt.submit({ text: final, asUser: true })
      .catch((err) => log($, { resubmit: "failed", error: String(err) }));
    return next(e);
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

// Чистильщик просит проверку: агент ищет в фоне, сырую реплику роняем — поле
// свободно. Чистый текст уйдёт из turn.complete агента. Отказ движка — сырой текст.
async function verify($, e, next, question: string) {
  const { agentId, deny } = await $.agent.spawn({
    description: "cc: проверка для чистки",
    subagentType: "Explore",
    prompt: `Найди в файлах проекта: ${question}\n\nОтветь коротко фактами: точные имена и пути. Ничего не меняй.`,
  });
  if (deny || !agentId) {
    await log($, { check: "denied", question, error: deny ?? "no agentId" });
    return next(e);
  }
  waiting.set(agentId, e.text);
  $.ui.status(`cc: проверяю — ${question}`);
  await log($, { check: "spawned", agentId, question, before: e.text });
  return { drop: "cc: проверяю, отправлю после" };
}

// Кнопка: черновик из поля — чистка — обратно в поле. Ничего не отправляет.
async function rephrase($, options): Promise<void> {
  const { text } = await $.prompt.read();
  if (!text.trim()) return;
  const cleaning = clean($, text, options);
  const untouched = await spin($, cleaning);
  const cleaned = await cleaning;
  const fixed = cleaned && !parseCheck(cleaned) ? cleaned : null;
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

// Два пути, выбор — options.context.
// fork — ветка от последнего запроса сессии: весь разговор из кэша, модель
// и системный промпт сессии; правила и реплика — одним сообщением в конце.
// complete — отдельный вызов без сессии, CLAUDE.md и инструментов: свой системный
// промпт с правилами и последними репликами разговора, своя модель.
// В новой сессии ответвляться не от чего — тогда complete.
// Нет prompt.md или любая осечка — null, и уходит исходный текст: текст дороже чистки.
// found — что нашёл агент проверки: второй заход, просить проверку снова нельзя.
async function clean($, text: string, options, found = ""): Promise<string | null> {
  const check = options.check === "on" && !found ? await prompt($, "check.md") : "";
  const rules = buildRules(await prompt($, "prompt.md"), check, found);
  if (!rules) return null;
  const started = Date.now();
  let via = options.context;
  let model = options.model;
  try {
    let reply = via === "fork"
      ? await $.model.fork({ prompt: buildForkPrompt(text, rules) })
      : null;
    const forked = reply !== null && reply.reason !== "nothing-to-fork";
    if (forked) {
      model = await $.session.model();
    } else {
      via = "complete";
      reply = await $.model.complete({
        model,
        system: buildSystem(text, await context($), rules),
        prompt: `<реплика>\n${text}\n</реплика>`,
        maxTokens: MAX_TOKENS,
      });
    }
    const cleaned = reply.isAnswered ? parseAnswer(reply.text) : null;
    await log($, { via, model, ms: Date.now() - started, before: text, after: cleaned,
                   ...(reply.isAnswered ? {} : { reason: reply.reason }) });
    return cleaned;
  } catch (error) {
    await log($, { via, model, before: text, after: null, error: String(error) });
    return null;
  }
}

async function prompt($, name: string): Promise<string> {
  return String(await $.fs.read(`${$.plugin.root}/${name}`).catch(() => ""));
}

async function context($): Promise<string> {
  return buildContext(await $.session.messages().catch(() => []));
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
