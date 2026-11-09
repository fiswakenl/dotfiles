import { expect, test } from "claude-code/testing";
import { context, decide, prompt, type Route } from "../hooks/orchestrator.ts";

const item = (over: Partial<Route> = {}): Route => ({
  kind: "route",
  text: "и ещё проверь тесты",
  busy: true,
  recent: [
    { id: 1, kind: "ask", state: "answered", text: "что в todo", answer: "Три задачи." },
    { id: 2, kind: "ask", state: "sent", text: "почини сборку", answer: "" },
    { id: 3, kind: "quick", state: "answered", text: "сколько времени", answer: "Пять." },
  ],
  ...over,
});

test("разговор: последние непустые, служебное мимо, длинное режется", () => {
  const ctx = context([
    { role: "user", text: "первое" },
    { role: "user", text: "<command-name>/gv</command-name>" },
    { role: "assistant", text: "" },
    { role: "assistant", text: "а".repeat(20) + " <system-reminder>шум</system-reminder>" },
    { role: "user", text: "свежее" },
  ], 2, 10);
  expect(ctx).toBe("assistant: ааааааааа…\nuser: свежее");
});

test("промпт: реплики с номерами и состоянием, занятость, новая реплика последней", () => {
  const p = prompt([{ role: "user", text: "привет" }], item());
  expect(p).toContain("<разговор>\nuser: привет\n</разговор>");
  expect(p).toContain("№2 (ask, Claude над ней работает): почини сборку");
  expect(p).toContain("№1 (ask, Claude ответил): что в todo\n  ответ Claude: Три задачи.");
  expect(p).toContain("Claude сейчас занят");
  expect(p.endsWith("<новая реплика>\nи ещё проверь тесты\n</новая реплика>")).toBe(true);
});

test("промпт без истории и реплик", () => {
  const p = prompt([], item({ recent: [], busy: false }));
  expect(p).toContain("пока пусто");
  expect(p).toContain("пока ни одной");
  expect(p).toContain("Claude свободен.");
});

test("решение: поправка переданного ask", () => {
  expect(decide('{"call": "ask", "replaces": 2}', item())).toEqual({ name: "ask", replaces: 2, text: item().text });
});

test("решение: quick", () => {
  expect(decide('Вот: {"call":"quick"}', item())).toEqual({ name: "quick", replaces: 0, text: item().text });
});

test("решение: новая мысль", () => {
  expect(decide('{"call": "ask"}', item())).toEqual({ name: "ask", replaces: 0, text: item().text });
});

test("поправить можно только переданный ask: quick и неизвестный номер — новая мысль", () => {
  expect(decide('{"call": "ask", "replaces": 3}', item())).toEqual({ name: "ask", replaces: 0, text: item().text });
  expect(decide('{"call": "ask", "replaces": 9}', item())).toEqual({ name: "ask", replaces: 0, text: item().text });
});

test("непонятный ответ или его нет — новая мысль", () => {
  expect(decide("не знаю", item())).toEqual({ name: "ask", replaces: 0, text: item().text });
  expect(decide("{сломано", item())).toEqual({ name: "ask", replaces: 0, text: item().text });
  expect(decide("", item())).toEqual({ name: "ask", replaces: 0, text: item().text });
});

test("текст: очищенный, если не сжат до пересказа; иначе расшифровка", () => {
  const long = item({ text: "ну типа проверь, короче, тесты в Cloud MD" });
  expect(decide('{"call": "ask", "text": "проверь тесты в CLAUDE.md"}', long).text).toBe("проверь тесты в CLAUDE.md");
  expect(decide('{"call": "ask", "text": "тесты"}', long).text).toBe(long.text);
  expect(decide('{"call": "ask"}', long).text).toBe(long.text);
});
