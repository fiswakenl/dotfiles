// Стенд замены запроса. Управление снаружи — файл ~/.claude/rp.cmd, одна JSON-строка за раз:
//   {"submit": "текст"}   отправить сообщение как от человека
//   {"abort": true}       прервать идущий ход
//   {"replace": "текст"}  прервать идущий ход и отправить поправку
// Что происходило — ~/.claude/rp.log (строка JSON на событие). Опрос файла раз в полсекунды.

const POLL_MS = 500;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let runningTurn = "";
let commandBusy = false;

const file = async ($, name: string) => `${await $.env.get("HOME")}/.claude/${name}`;

async function log($, entry: Record<string, unknown>): Promise<void> {
  try {
    const path = await file($, "rp.log");
    const had = String(await $.fs.read(path).catch(() => ""));
    const line = JSON.stringify({ at: new Date().toISOString().slice(11, 23), ...entry });
    await $.fs.write(path, had + line + "\n");
  } catch {}
}

async function run($, cmd: { native?: string; target?: string; id?: string; submit?: string; abort?: boolean; replace?: string; list?: boolean; run?: string; args?: string; fill?: string; read?: boolean; history?: boolean; api?: boolean; rewind?: number; redo?: number; compactRewind?: number; restore?: string; snapshot?: boolean; text?: string }): Promise<void> {
  if (cmd.native || cmd.rewind !== undefined || cmd.redo !== undefined) {
    if (cmd.redo !== undefined && (cmd.text === undefined || !cmd.text.trim())) throw new Error("redo needs replacement text");
    const action = cmd.redo !== undefined ? "redo" : cmd.native ?? "rewind";
    const number = cmd.redo ?? cmd.rewind;
    const target = cmd.target ?? (number === undefined ? undefined : String(number));
    const client = await file($, "rp/native-client.py");
    const out = await $.process.run(["python3", client, action, ...(target === undefined ? [] : [target]), ...(action === "redo" ? ["--", cmd.text] : [])]);
    if (out.exitCode !== 0) throw new Error(out.stderr || "Native bridge client failed");
    const result = JSON.parse(out.stdout);
    if ((action === "rewind" || action === "redo") && !result.rewound) throw new Error(result.reason ?? result.error ?? "Native Restore failed");
    await log($, { native_result: out.stdout, native_exit: out.exitCode, native_session: await $.session.id() });
    if ((action === "redo" || action === "submit") && !result.submitted) throw new Error(result.reason ?? "Restore completed but composer did not submit");
    if (action !== "redo" && cmd.text !== undefined) await $.prompt.fill({ text: cmd.text });
    return;
  }
  if (cmd.history) {
    const messages = await $.session.messages();
    await log($, { history: messages.map((m, i) => ({ i, role: m.role, text: m.text, handle: m.handle })) });
    return;
  }
  if (cmd.api) {
    await log($, { api: await $.session.messages({ as: "api" }) });
    return;
  }
  if (cmd.compactRewind !== undefined || cmd.restore || cmd.snapshot) {
    if (runningTurn) throw new Error("Wait for the running turn before rewinding");
    const commands = await $.command.list();
    if (!commands.some(c => c.name === "rp-rewind-guard")) throw new Error("rp-rewind-guard not loaded: refusing to invoke compact");
    const sessionId = await $.session.id();
    const id = String(Date.now());
    const operation = cmd.restore ? "restore" : cmd.snapshot ? "snapshot" : "rewind";
    const plan = { id, sessionId, operation, beforeUser: cmd.compactRewind, snapshotPath: cmd.restore };
    const planPath = await file($, "rp-rewind-plan.json");
    await $.fs.write(planPath, JSON.stringify(plan));
    try {
      const result = await $.session.compact({ instructions: "rp-api-rewind:" + id });
      const receipt = JSON.parse(String(await $.fs.read(await file($, "rp-rewind-receipt.json"))));
      if (receipt.id !== id || receipt.error) throw new Error(receipt.error || "Guard did not acknowledge this operation");
      if (operation !== "snapshot" && "skip" in result) throw new Error(result.skip);
      await log($, { api_rewind: receipt });
      if (cmd.text !== undefined) await $.prompt.submit({ text: cmd.text, asUser: true });
    } finally { await $.fs.write(planPath, ""); }
    return;
  }
  if (cmd.fill !== undefined) {  // вписать текст в поле ввода, не отправляя
    const out = await $.prompt.fill({ text: cmd.fill }).catch((err) => ({ error: String(err) }));
    return void (await log($, { filled: cmd.fill, result: JSON.stringify(out).slice(0, 200) }));
  }
  if (cmd.read) {  // что сейчас в поле ввода
    const box = await $.prompt.read().catch((err) => ({ error: String(err) }));
    return void (await log($, { box: JSON.stringify(box).slice(0, 300) }));
  }
  if (cmd.list) {  // какие слэш-команды видны плагину (встроенные тоже)
    const all = await $.command.list().catch((err) => [{ name: "ошибка: " + String(err) }]);
    return void (await log($, { commands: all.map((c) => c.name).join(" ") }));
  }
  if (cmd.run) {  // выполнить слэш-команду так, как будто её набрал человек
    const out = await $.command.run({ command: cmd.run, args: cmd.args ?? "" }).catch((err) => ({ error: String(err) }));
    return void (await log($, { ran: cmd.run, result: JSON.stringify(out).slice(0, 300) }));
  }
  if (cmd.abort || cmd.replace !== undefined) {
    await log($, { do: "abort", turn: runningTurn || null });
    if (runningTurn) await $.turn.abort({ turnId: runningTurn }).catch((err) => log($, { abort_failed: String(err) }));
    await sleep(400);
  }
  const text = cmd.replace ?? cmd.submit;
  if (text === undefined) return;
  await log($, { do: "submit", text });
  await $.prompt.submit({ text, asUser: true }).catch((err) => log($, { submit_failed: String(err) }));
}

export function register(on) {
  on("session.start", async ($, e, next) => {
    const result = await next(e);
    await $.command.register({ name: "rp", description: "Стенд замены запроса: статус" });
    await $.fs.write(await file($, "rp.cmd"), "").catch(() => {});
    $.clock.every(POLL_MS, async () => {
      if (commandBusy) return;
      const path = await file($, "rp.cmd");
      const raw = String(await $.fs.read(path).catch(() => "")).trim();
      if (!raw) return;
      await $.fs.write(path, "").catch(() => {});
      commandBusy = true;
      try {
        const command = JSON.parse(raw);
        await run($, command);
        await log($, { command_done: command.id ?? null });
      } catch (err) {
        await log($, { bad_command: raw.slice(0, 200), error: String(err) });
      } finally { commandBusy = false; }
    });
    await log($, { event: "session.start" });
    return result;
  });

  on("command.run", { command: "rp" }, async () => ({ text: `rp: идущий ход ${runningTurn || "нет"}. Команды — в ~/.claude/rp.cmd.` }));

  // Всё, что движок дописывает в разговор: по этому видно, что происходит при откате.
  on("session.append", async ($, e, next) => {
    const m = e.message ?? {};
    const text = Array.isArray(m.content) ? m.content.map((b) => b.text ?? b.type).join(" ") : String(m.content ?? "");
    await log($, { event: "append", door: e.door, type: m.type, role: m.role, text: text.slice(0, 60) });
    return next(e);
  });

  on("prompt.submit", async ($, e, next) => {
    await log($, { event: "prompt.submit", origin: e.origin?.kind, text: String(e.text).slice(0, 60) });
    return next(e);
  });

  on("session.end", async ($, e, next) => {
    await log($, { event: "session.end", reason: e.reason });
    return next(e);
  });

  on("turn.start", async ($, e, next) => {
    if (!e.agentId) {
      runningTurn = e.turnId;
      await log($, { event: "turn.start", turn: e.turnId });
    }
    return next(e);
  });

  on("turn.complete", async ($, e, next) => {
    if (!e.agentId) {
      runningTurn = "";
      await log($, { event: "turn.complete", reason: e.reason, answer: e.answer.slice(0, 80) });
    }
    return next(e);
  });
}
