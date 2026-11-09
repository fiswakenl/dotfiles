import { expect, mock, test } from "claude-code/testing";

// Плагин (hooks/register.ts) вокруг ненастоящего сервиса: /next отдаёт то, что положил тест, остальные ручки записываются.
// Сервис, как настоящий, шлёт номер запуска (X-GV-Epoch) и отвечает 409 на /call и /answer с чужим номером.

const HOME = "/home/gv";
const TOKEN = `${HOME}/.claude/gv.token`;
const wait = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

type Sent = { path: string; body: any; epoch?: string };

function world(on) {
  const files = new Map<string, string>();
  const svc = { epoch: "A", up: false, next: [] as unknown[], sent: [] as Sent[] };
  mock.env(on, { HOME, GV_NATIVE_SOCKET: "/tmp/gv.sock" });
  mock.clock(on);
  // Вызовы на $ (не события) отвечают { value }.
  on("fs.read", async (_$, e) => {
    if (!files.has(e.path)) throw new Error("ENOENT");
    return { value: files.get(e.path) };
  });
  on("fs.write", async (_$, e) => (files.set(e.path, e.text), { value: undefined }));
  on("process.run", async () => ((svc.up = true), { value: { exitCode: 0, stdout: "", stderr: "" } }));
  on("session.start", async (_$, e) => ({ cwd: e.cwd }));
  on("session.end", async (_$, e) => ({ sessionId: e.sessionId }));
  on("turn.start", async (_$, e) => ({ turnId: e.turnId }));
  on("turn.complete", async (_$, e) => ({ text: e.answer }));
  on("command.register", async () => ({ value: {} }));
  on("ui.status", async () => ({ value: undefined }));
  on("ui.log", async () => ({ value: undefined }));
  on("session.messages", async () => ({ value: [] }));
  on("model.fork", async () => ({ value: { isAnswered: true, reason: "answer", text: "Пять." } }));
  on("http.fetch", async (_$, e) => {
    if (!svc.up) throw new Error("ECONNREFUSED");
    const path = e.url.replace(/^https?:\/\/[^/]+/, "");
    const body = e.init?.body ? JSON.parse(e.init.body) : undefined;
    const epoch = e.init?.headers?.["x-gv-epoch"];
    svc.sent.push({ path, body, epoch });
    const res = (status: number, data?: unknown) =>
      ({ value: { status, ok: status < 300, headers: { "x-gv-epoch": svc.epoch }, text: data === undefined ? "" : JSON.stringify(data) } });
    if (path.startsWith("/next")) {
      await wait(5);
      const item = svc.next.shift();
      return item ? res(200, item) : res(204);
    }
    if ((path.startsWith("/call") || path.startsWith("/answer")) && epoch && epoch !== svc.epoch) return res(409, { error: "чужой запуск" });
    if (path === "/quit") return (svc.up = false), res(200);
    if (path === "/call" && body.name === "send") return res(200, { result: { uuid: `u-${body.args.text}` } });
    return res(200, { result: "ok" });
  });
  const until = async (what: () => unknown, ms = 3000) => {
    for (const end = Date.now() + ms; !what(); await wait(10)) if (Date.now() > end) throw new Error("не дождались");
  };
  const calls = (name: string) => svc.sent.filter((s) => s.path === "/call" && s.body?.name === name);
  return { files, svc, until, calls };
}

const startSession = ($) => $.session.start({ cwd: "/work", surface: "terminal", isInteractive: true });

// Голос выключен — цикл опроса плагина останавливается, пока тест не кончился.
const stop = async ($) => {
  await $.command.run({ command: "gv", args: "" });
  await wait(50);
};

test("сервис перезапущен посреди хода → ответ на прежний номер не уходит новому запуску", { timeoutMs: 10_000 }, async ($, on) => {
  const w = world(on);
  await startSession($);
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next")));
  w.svc.next.push({ id: 1, kind: "ask", text: "привет", rewind: null });
  await w.until(() => w.calls("send").length);
  await $.turn.start({ text: "🎙 привет", turnId: "t1" });
  w.svc.epoch = "B"; // перезапуск: номера запросов начались заново
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next") && s.epoch === "B"));
  await $.turn.complete({ turnId: "t1", answer: "Привет!", reason: "answer", durationMs: 1, isAborted: false });
  expect(w.svc.sent.filter((s) => s.path.startsWith("/answer"))).toEqual([]);
  await stop($);
});

test("поправка к запросу прежнего запуска → новое сообщение, без отката чужой истории", { timeoutMs: 10_000 }, async ($, on) => {
  const w = world(on);
  await startSession($);
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next")));
  w.svc.next.push({ id: 1, kind: "ask", text: "первое", rewind: null });
  await w.until(() => w.calls("send").length === 1);
  w.svc.epoch = "B";
  w.svc.next.push({ id: 2, kind: "ask", text: "второе", rewind: 1 }); // №1 нового запуска — не то сообщение, что ушло как №1
  await w.until(() => w.calls("send").length === 2);
  expect(w.calls("rewind")).toEqual([]);
  await stop($);
});

test("идёт ход, набранный руками → сервис знает, quick отвечает сразу", { timeoutMs: 10_000 }, async ($, on) => {
  const w = world(on);
  await startSession($);
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next")));
  await $.turn.start({ text: "набрал руками", turnId: "t1" });
  await w.until(() => w.calls("turn").some((c) => c.body.args.working === true));
  w.svc.next.push({ id: 1, kind: "ask", text: "новая мысль", rewind: null }, { id: 2, kind: "quick", text: "сколько времени" });
  await w.until(() => w.svc.sent.some((s) => s.path === "/answer?id=2"));
  await $.turn.complete({ turnId: "t1", answer: "готово", reason: "answer", durationMs: 1, isAborted: false });
  await w.until(() => w.calls("turn").some((c) => c.body.args.working === false));
  await stop($);
});

test("ход голосового запроса упал → сервис получает ошибку, а не пустой ответ", { timeoutMs: 10_000 }, async ($, on) => {
  const w = world(on);
  await startSession($);
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next")));
  w.svc.next.push({ id: 1, kind: "ask", text: "привет", rewind: null });
  await w.until(() => w.calls("send").length);
  await $.turn.start({ text: "🎙 привет", turnId: "t1" });
  await $.turn.complete({ turnId: "t1", answer: "", reason: "error", durationMs: 1, isAborted: false });
  const sent = w.svc.sent.find((s) => s.path === "/answer?id=1");
  expect(typeof sent?.body.error).toBe("string");
  await stop($);
});

test("голос забрала другая сессия → прежняя не пингует и не гасит её сервис", { timeoutMs: 10_000 }, async ($, on) => {
  const w = world(on);
  await startSession($);
  await w.until(() => w.svc.sent.some((s) => s.path.startsWith("/next")));
  w.files.set(TOKEN, "другая сессия");
  await wait(100); // опрос видит чужого хозяина и уходит
  w.svc.sent.length = 0;
  await wait(100);
  expect(w.svc.sent).toEqual([]);
  await $.session.end({ reason: "other", sessionId: "s", resume: { id: "s" } });
  expect(w.svc.sent.filter((s) => s.path === "/quit")).toEqual([]);
});
