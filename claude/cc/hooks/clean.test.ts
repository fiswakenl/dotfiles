import { test, expect } from "claude-code/testing";
import { buildContext, buildSystem, parseAnswer } from "./clean.ts";

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

test("обёртка реплики снимается, пустой ответ — null", () => {
  expect(parseAnswer("<реплика>\nПочини тест.\n</реплика>")).toBe("Почини тест.");
  expect(parseAnswer("  \n")).toBeNull();
});
