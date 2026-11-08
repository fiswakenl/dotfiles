// Сборка системного промпта, контекста и разбор ответа. Чистые функции без
// обращений к движку — их проверяют тесты; запрос, транскрипт и файл промпта
// достаёт register.ts. Текст промпта — только в prompt.md, второй копии нет.

// Язык определяем сами: контекст сессии почти всегда русский, и английскую
// фразу модель по инерции переводит вместо правки.
function language(text: string): string {
  const ru = (text.match(/[а-яёА-ЯЁ]/g) ?? []).length;
  const en = (text.match(/[a-zA-Z]/g) ?? []).length;
  return ru >= en ? "русском" : "английском";
}

// Сколько разговора показывать чистильщику: двадцати реплик хватает понять,
// о чём речь, четырёхсот знаков — о чём была каждая.
export const MESSAGES = 20;
export const CHARS = 400;

// Служебные реплики: команды и их вывод. Смысла в них нет, место занимают.
const SKIP = ["<command-name>", "<command-message>", "<local-command-caveat>",
              "<system-reminder>", "<local-command-stdout>"];

function clip(s: string, n: number): string {
  const one = s.replace(/\s+/g, " ").trim();
  return one.length <= n ? one : one.slice(0, n - 1) + "…";
}

// Инструмент сворачиваем в одну строку: что звали и по чему. Чистильщику это
// адреса — «тот файл» и «та команда» из следующей реплики ведут сюда.
function toolText(use): string {
  const input = (use?.input ?? {}) as Record<string, unknown>;
  const arg = input.command ?? input.file_path ?? input.description ?? "";
  return `[${use?.tool}: ${clip(String(arg), 80)}]`;
}

// Последние реплики разговора, куда уйдёт промпт. Идём с конца и набираем
// limit непустых: считать надо после отсева, иначе десяток служебных строк
// съест всю выборку.
export function buildContext(messages, limit = MESSAGES, chars = CHARS): string {
  const turns: string[] = [];
  for (let i = messages.length - 1; i >= 0 && turns.length < limit; i--) {
    const message = messages[i];
    const parts = [message?.text ?? "",
                   ...(message?.toolUses ?? []).map(toolText)];
    const text = parts.filter(Boolean).join(" ")
      .replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "").trim();
    if (!text || SKIP.some((tag) => text.startsWith(tag))) continue;
    turns.push(`${message.role}: ${clip(text, chars)}`);
  }
  turns.reverse();
  return turns.join("\n");
}

// base — текст prompt.md. Пусто — пустая система, и register.ts отдаёт
// исходную реплику.
export function buildSystem(text: string, ctx = "", base = ""): string {
  let system = base.trim();
  if (!system) return "";
  if (ctx) {
    system += "\n\nНиже — последние сообщения того разговора, куда уйдёт промпт. "
      + "Они нужны, чтобы понять, о чём речь, восстановить искажённые слова и точные имена. "
      + "Сам контекст не переписывай и не пересказывай, на вопросы из него "
      + `не отвечай.\n\n<контекст разговора>\n${ctx}\n</контекст разговора>`;
  }
  // Последней строкой — она весит больше всего, а язык тут важнее всего.
  const lang = language(text);
  system += `\n\nЭта реплика сказана на ${lang} языке. Твой ответ обязан быть на ${lang} языке — независимо от того, на каком языке идёт разговор в контексте.`;
  return system;
}

// Модель иногда повторяет обёртку <реплика> — в поле ввода ей не место.
export function parseAnswer(out: string): string | null {
  return out.replace(/<\/?реплика>/g, "").trim() || null;
}
