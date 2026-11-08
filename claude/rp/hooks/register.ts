// Стенд замены запроса. Управление снаружи — файл ~/.claude/rp.cmd, одна JSON-строка за раз:
//   {"submit": "текст"}   отправить сообщение как от человека
//   {"abort": true}       прервать идущий ход
//   {"replace": "текст"}  прервать идущий ход и отправить поправку
// Что происходило — ~/.claude/rp.log (строка JSON на событие). Опрос файла раз в полсекунды.

const POLL_MS = 500;
const sleep = (ms: number) => new Promise((resolve) => setTimeout(resolve, ms));

let runningTurn = "";

const file = async ($, name: string) => `${await $.env.get("HOME")}/.claude/${name}`;

async function log($, entry: Record<string, unknown>): Promise<void> {
  try {
    const path = await file($, "rp.log");
    const had = String(await $.fs.read(path).catch(() => ""));
    const line = JSON.stringify({ at: new Date().toISOString().slice(11, 23), ...entry });
    await $.fs.write(path, had + line + "\n");
  } catch {}
}

async function run($, cmd: { submit?: string; abort?: boolean; replace?: string; list?: boolean; run?: string; args?: string }): Promise<void> {
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
      const path = await file($, "rp.cmd");
      const raw = String(await $.fs.read(path).catch(() => "")).trim();
      if (!raw) return;
      await $.fs.write(path, "").catch(() => {});
      try {
        await run($, JSON.parse(raw));
      } catch (err) {
        await log($, { bad_command: raw.slice(0, 200), error: String(err) });
      }
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
