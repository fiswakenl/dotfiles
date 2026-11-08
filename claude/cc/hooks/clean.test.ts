import { test, expect } from "claude-code/testing";
import { buildContext, buildForkPrompt, buildRules, buildSystem, parseAnswer, parseCheck } from "./clean.ts";

const msg = (role, text, toolUses = []) => ({ role, text, toolUses });

test("берёт последние реплики и подписывает роли", () => {
  const ctx = buildContext([
    msg("user", "первое"),
    msg("assistant", "второе"),
  ]);
  expect(ctx).toBe("user: первое\nassistant: второе");
});

test("набирает limit непустых, а не первые limit подряд", () => {
  const ctx = buildContext([
    msg("user", "важное"),
    msg("user", "<command-name>/sync</command-name>"),
    msg("assistant", ""),
    msg("user", "свежее"),
  ], 2);
  expect(ctx).toBe("user: важное\nuser: свежее");
});

test("режет длинное и схлопывает пробелы", () => {
  const ctx = buildContext([msg("user", "а".repeat(50) + "\n\n  хвост")], 20, 10);
  expect(ctx).toBe("user: ааааааааа…");
});

test("инструменты становятся адресами", () => {
  const ctx = buildContext([
    msg("assistant", "смотрю", [{ tool: "Read", input: { file_path: "hooks/clean.ts" } }]),
  ]);
  expect(ctx).toBe("assistant: смотрю [Read: hooks/clean.ts]");
});

test("system-reminder вырезается", () => {
  const ctx = buildContext([msg("user", "текст <system-reminder>шум</system-reminder>")]);
  expect(ctx).toBe("user: текст");
});

test("контекст попадает в системный промпт, язык остаётся последней строкой", () => {
  const system = buildSystem("почини тот файл", "user: привет", "Причеши реплику.");
  expect(system).toContain("<контекст разговора>\nuser: привет\n</контекст разговора>");
  expect(system.trimEnd().endsWith("идёт разговор в контексте.")).toBe(true);
});

test("без контекста блока нет", () => {
  expect(buildSystem("fix that file", "", "Причеши реплику.")).not.toContain("<контекст разговора>");
});

// Промпт приходит только файлом. Нет файла — пусто, и register.ts отдаёт
// исходную реплику: второй копии текста в коде нет.
test("без файла промпта система пустая", () => {
  expect(buildSystem("почини тот файл", "user: привет")).toBe("");
});

test("просьба в fork: правила, язык, реплика последней", () => {
  const ask = buildForkPrompt("глянь второй тоже", "Причеши реплику.");
  expect(ask.startsWith("Служебный запрос плагина cc")).toBe(true);
  expect(ask).toContain("Причеши реплику.");
  expect(ask).toContain("на русском языке");
  expect(ask.endsWith("<реплика>\nглянь второй тоже\n</реплика>")).toBe(true);
  expect(buildForkPrompt("глянь второй тоже")).toBe("");
});

test("правила: проверка подмешивается, найденное идёт вторым заходом", () => {
  expect(buildRules("", "Проверка.")).toBe("");
  expect(buildRules("Причеши.")).toBe("Причеши.");
  expect(buildRules("Причеши.", "Проверка.")).toBe("Причеши.\n\nПроверка.");
  const again = buildRules("Причеши.", "", "keywatch: ~/.config/keywatch");
  expect(again).toContain("<найдено>\nkeywatch: ~/.config/keywatch\n</найдено>");
  expect(again).toContain("проверку больше не проси");
});

test("просьба о проверке читается, обычный ответ — нет", () => {
  expect(parseCheck("<проверить>утилита keywatch: где конфиг</проверить>")).toBe("утилита keywatch: где конфиг");
  expect(parseCheck("Посмотри и второй тест.")).toBeNull();
  expect(parseCheck("<проверить> </проверить>")).toBeNull();
});

test("обёртка реплики снимается, пустой ответ — null", () => {
  expect(parseAnswer("<реплика>\nПочини тест.\n</реплика>")).toBe("Почини тест.");
  expect(parseAnswer("  \n")).toBeNull();
});

// Транскрипт, как у большого диалога: много реплик, каждая огромная.
const fat = (n: number) =>
  Array.from({ length: n }, (_, i) => ({
    role: i % 2 ? "assistant" : "user",
    text: `реплика ${i} ` + "текст ".repeat(2000),   // ~12 КБ каждая
    toolUses: [{ tool: "Bash", input: { command: "x".repeat(5000) } }],
  }));

for (const n of [5, 100, 1000, 5000]) {
  test(`${n} сообщений в диалоге`, () => {
    const ctx = buildContext(fat(n));
    const system = buildSystem("почини тот файл", ctx, "Причеши реплику.");
    const turns = ctx ? ctx.split("\n").length : 0;
    console.log(`  диалог ${String(n).padStart(4)} реплик (~${Math.round(n * 17 / 1024)} МБ) → `
      + `контекст ${turns} строк, ${ctx.length} знаков; системный промпт ${system.length} знаков`);
    expect(turns).toBe(Math.min(n, 20));
    // 400 знаков на реплику плюс префикс роли ("assistant: " — 11).
    expect(ctx.length).toBeLessThan(20 * (400 + 12));
  });
}

test("берутся последние, а не первые", () => {
  const ctx = buildContext(fat(1000));
  expect(ctx).toContain("реплика 999");
  expect(ctx).not.toContain("реплика 1 ");
});
