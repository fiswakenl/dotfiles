# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp"]
# ///
"""Сценарии на живом Claude без голоса: ccg в tmux (сессия gv-e2e), /gv, сокет Gemini не открывается (GV_TEXT_ONLY=1).
Шаги идут в /call, что прозвучало бы — события say. Перед каждым сценарием история Claude откатывается до начала.

    uv run --script tests/run.py [часть имени сценария…]   # --keep — не закрывать tmux в конце
"""
import asyncio
import subprocess
import sys
import tempfile
import time
from pathlib import Path

import aiohttp

sys.path.insert(0, str(Path(__file__).resolve().parent))
from scenarios import SCENARIOS, check, play, word_of  # noqa: E402

PORT = 18790
BASE = f"http://127.0.0.1:{PORT}"
TMUX = "gv-e2e"
SAY_MS = 1500
MARK = "🎙"


def tmux(*args: str) -> str:
    return subprocess.run(["tmux", *args], capture_output=True, text=True).stdout


async def start_claude(http: aiohttp.ClientSession) -> None:
    """ccg в пустой временной папке, голос выключен до /gv; доверие к папке подтверждаем сами."""
    work = Path(tempfile.mkdtemp(prefix="gv-e2e-"))
    (work / "state").write_text("off")
    env = f"GV_TEXT_ONLY=1 GV_SAY_MS={SAY_MS} GV_PORT={PORT} GV_STATE_FILE={work}/state"
    tmux("kill-session", "-t", TMUX)
    tmux("new", "-d", "-s", TMUX, "-x", "200", "-y", "50", "-c", str(work), f"{env} zsh -ic \"ccg --allowedTools 'Bash(sleep:*)'\"")
    for _ in range(120):
        await asyncio.sleep(0.5)
        screen = tmux("capture-pane", "-t", TMUX, "-p")
        if "I trust this folder" in screen:  # по умолчанию стоит «No, exit»
            tmux("send-keys", "-t", TMUX, "Down", "Enter")
        elif "выключен, /gv" in screen:
            break
    else:
        raise SystemExit("Claude не поднялся:\n" + tmux("capture-pane", "-t", TMUX, "-p"))
    tmux("send-keys", "-t", TMUX, "/gv", "Enter")
    for _ in range(60):
        await asyncio.sleep(0.5)
        try:
            if (await http.post(f"{BASE}/ping")).ok:
                return
        except aiohttp.ClientError:
            pass
    raise SystemExit("сервис GV не поднялся:\n" + tmux("capture-pane", "-t", TMUX, "-p"))


class Events:
    """Поток событий сервиса: запросы, ответы, что прозвучало — с временем прихода."""

    def __init__(self) -> None:
        self.all: list[tuple[float, dict]] = []

    async def follow(self, http: aiohttp.ClientSession) -> None:
        async with http.ws_connect(f"{BASE}/") as ws:
            async for msg in ws:
                e = msg.json()
                if "history" not in e:
                    self.all.append((time.monotonic(), e))

    def since(self, t0: float) -> list[tuple[float, dict]]:
        return [(t, e) for t, e in self.all if t >= t0]

    def says(self, t0: float) -> list[tuple[float, str]]:
        ev = self.since(t0)
        texts = {e["id"]: e["answer"] for _, e in ev if "answer" in e}
        failed = {e["id"] for _, e in ev if "error" in e and "id" in e}
        return [(t - t0, "ОШИБКА" if e["say"] in failed else word_of(texts.get(e["say"], "?"))) for t, e in ev if "say" in e]

    def settled(self, t0: float) -> bool:
        """Каждый запрос сценария ответил, заменён или упал, каждый непустой ответ и ошибка прозвучали, и 3 с тишина."""
        ev = [e for _, e in self.since(t0)]
        of = lambda key: {e["id"] for e in ev if key in e and "id" in e}  # ask, answer, error — номер в поле id
        val = lambda key: {e[key] for e in ev if key in e}  # sent, say, superseded, dropped — номер и есть значение
        closed = of("answer") | of("error") | val("superseded") | val("dropped")
        to_say = ({e["id"] for e in ev if e.get("answer")} | of("error")) - val("superseded")
        quiet = bool(ev) and time.monotonic() - self.all[-1][0] > 3 + SAY_MS / 1000
        return of("ask") <= closed and to_say <= val("say") and quiet

    def timings(self, t0: float) -> str:
        first: dict[int, dict[str, float]] = {}
        for t, e in self.since(t0):
            for key in ("ask", "sent", "answer", "say"):
                if key in e:
                    first.setdefault(e["id"] if key in ("ask", "answer") else e[key], {}).setdefault(key, t - t0)
        return "; ".join(f"№{i}: " + " → ".join(f"{k} {v:.1f}" for k, v in st.items()) for i, st in first.items())


async def main() -> None:
    names = [a for a in sys.argv[1:] if not a.startswith("--")]
    chosen = [s for s in SCENARIOS if not s.get("sim_only") and (not names or any(n in s["name"] for n in names))]
    async with aiohttp.ClientSession() as http:
        async def do(name: str, args: dict):
            body = await (await http.post(f"{BASE}/call", json={"name": name, "args": args})).json()
            return body.get("result", body)

        await start_claude(http)
        events = Events()
        follower = asyncio.create_task(events.follow(http))
        await asyncio.sleep(1)
        failed = 0
        for s in chosen:
            if (await do("list", {})).get("messages"):
                await do("rewind", {"target": 1})
                await asyncio.sleep(2)
            t0 = time.monotonic()
            problems: list[str] = []
            try:
                await play(s, do, lambda: [w for _, w in events.says(t0)])
                deadline = time.monotonic() + 150
                while not events.settled(t0) and time.monotonic() < deadline:
                    await asyncio.sleep(0.5)
            except TimeoutError as ex:
                problems.append(str(ex))
            history = [word_of(m["text"]) for m in (await do("list", {}))["messages"] if m["text"].startswith(MARK)]
            problems += check(s, events.says(t0), history, SAY_MS / 1000)
            failed += bool(problems)
            print(f"{'✗' if problems else '✓'} {s['name']}  ({time.monotonic() - t0:.0f} с)")
            print(f"    {events.timings(t0)}")
            for p in problems:
                print(f"    ! {p}")
        follower.cancel()
        if "--keep" not in sys.argv:
            await http.post(f"{BASE}/quit")
            tmux("kill-session", "-t", TMUX)
        print(f"\nпрошло {len(chosen) - failed} из {len(chosen)}")


if __name__ == "__main__":
    asyncio.run(main())
