import { test, expect } from "claude-code/testing";
import { buildContext, buildSystem } from "./clean.ts";

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
