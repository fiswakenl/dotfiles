// Оркестратор адаптеров распознавания: законченная реплика — новая мысль (ask), поправка №N (ask с replaces) или quick.
// Чистые функции без обращений к движку — их проверяют тесты: промпт из разговора и переданных реплик, разбор ответа.
// Вызов модели — в register.ts, правила — prompts/orchestrator.md.

// Сколько разговора показывать: двадцати сообщений хватает понять, о чём речь, трёхсот знаков — о чём каждое.
export const MESSAGES = 20;
export const CHARS = 300;

export type Recent = { id: number; kind: "ask" | "quick"; state: "queued" | "sent" | "answered"; text: string; answer: string };
export type Route = { kind: "route"; text: string; busy: boolean; recent: Recent[] };
export type Decision = { name: "ask" | "quick"; replaces: number }; // replaces 0 — новая мысль

const STATE = { queued: "ждёт, пока Claude освободится", sent: "Claude над ней работает", answered: "Claude ответил" };
const SERVICE = ["<command-name>", "<command-message>", "<local-command-", "<task-notification>"];

const clip = (s: string, n: number) => {
  const one = s.replace(/<system-reminder>[\s\S]*?<\/system-reminder>/g, "").replace(/\s+/g, " ").trim();
  return one.length <= n ? one : one.slice(0, n - 1) + "…";
};

// Последние limit непустых сообщений разговора, служебные (команды, их вывод) — мимо.
export function context(messages: readonly { role: string; text: string }[], limit = MESSAGES, chars = CHARS): string {
  const lines: string[] = [];
  for (let i = messages.length - 1; i >= 0 && lines.length < limit; i--) {
    const m = messages[i]!;
    const text = clip(m.text ?? "", chars);
    if (text && !SERVICE.some((tag) => text.startsWith(tag))) lines.push(`${m.role}: ${text}`);
  }
  return lines.reverse().join("\n");
}

export function prompt(messages: readonly { role: string; text: string }[], item: Route): string {
  const sent = item.recent.map((r) =>
    `№${r.id} (${r.kind}, ${STATE[r.state]}): ${clip(r.text, CHARS)}` + (r.answer ? `\n  ответ Claude: ${clip(r.answer, CHARS)}` : ""));
  return [
    `<разговор>\n${context(messages) || "пока пусто"}\n</разговор>`,
    `<переданные реплики>\n${sent.join("\n") || "пока ни одной"}\n</переданные реплики>`,
    item.busy ? "Claude сейчас занят: ещё не ответил на последнюю реплику." : "Claude свободен.",
    `<новая реплика>\n${item.text}\n</новая реплика>`,
  ].join("\n\n");
}

// Ответ модели → решение. Поправить можно только переданный ask; непонятный ответ — новая мысль: она ничего не стирает.
export function decide(reply: string, item: Route): Decision {
  let parsed: { call?: unknown; replaces?: unknown } = {};
  try {
    parsed = JSON.parse(reply.match(/\{[\s\S]*\}/)?.[0] ?? "{}");
  } catch {}
  if (parsed.call === "quick") return { name: "quick", replaces: 0 };
  const target = Number(parsed.replaces);
  const known = item.recent.some((r) => r.kind === "ask" && r.id === target);
  return { name: "ask", replaces: known ? target : 0 };
}
