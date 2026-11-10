# /// script
# requires-python = ">=3.10"
# dependencies = ["sounddevice", "numpy", "pipecat-ai[deepgram]>=1.15,<2", "httpx[http2]"]
# ///
"""Живой прогон для страницы Decisions: настоящие Deepgram, классификатор и ядро, но вместо микрофона — речь
из say, а вместо плагина и Claude — заглушка: запрос «думает» CLAUDE_S секунд, остановку принимает сразу. Сценарий — вопрос и,
пока Claude над ним работает, его продолжение. Получается обычная запись запуска в ~/.claude/gv-trace/ и страница по ней.
Платно (Deepgram и классификатор, копейки), поэтому не в наборе тестов — запускается руками:

    uv run --script ~/.claude/gv/tests/trace_demo.py [страница.html]

Без аргумента страница ложится рядом с записью и открывается в браузере."""
import asyncio
import subprocess
import sys
import tempfile
import wave
from pathlib import Path

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
import journal  # noqa: E402
import listen  # noqa: E402
from core import Core  # noqa: E402

# (что сказать, сколько молчать после)
SCRIPT = [("Расскажи подробно, что такое регуляризация и какие у неё виды.", 3.0),
          ("И ещё сравни их на примере линейной регрессии.", 5.0)]
CLAUDE_S = 8  # столько заглушка Claude «отвечает» на запрос


def speech(text: str) -> bytes:
    """PCM 16 бит, 16 кГц, моно — голосом Milena."""
    with tempfile.TemporaryDirectory() as d:
        aiff, wav = Path(d, "a.aiff"), Path(d, "a.wav")
        subprocess.run(["say", "-v", "Milena", "-o", aiff, text], check=True)
        subprocess.run(["afconvert", "-f", "WAVE", "-d", f"LEI16@{listen.RATE}", "-c", "1", aiff, wav], check=True)
        with wave.open(str(wav)) as w:
            return w.readframes(w.getnframes())


class NoMic:
    """Вместо микрофона: поток не открывается, звук даёт прогон."""

    def __init__(self, *_, **__) -> None: ...

    def start(self) -> None: ...

    def close(self) -> None: ...


async def plugin(core: Core) -> None:
    """Заглушка плагина и Claude: остановку принимает сразу, на запрос отвечает через CLAUDE_S секунд."""
    loop = asyncio.get_running_loop()
    while True:
        r = core.next()
        if not r:
            await asyncio.sleep(0.02)
        elif r["kind"] == "cancel":
            core.answer(r["id"])
        elif r["kind"] in ("ask", "quick"):
            loop.call_later(CLAUDE_S, core.answer, r["id"], "ответ")


async def main() -> None:
    listen.sd.RawInputStream = NoMic
    core = Core(emit=journal.emit, log=journal.log)
    ear = listen.Listen(core)
    journal.log("demo.start", script=[s for s, _ in SCRIPT])
    tasks = [asyncio.create_task(ear.run()), asyncio.create_task(plugin(core))]
    await asyncio.sleep(3)  # сокет Deepgram открыт, классификатор прогрет
    for text, pause in SCRIPT:
        await ear.mic.play(speech(text) + bytes(int(listen.RATE * pause) * 2))
    await asyncio.sleep(2)
    for t in tasks:
        t.cancel()
    record = journal._state["trace"]
    view = [sys.executable, str(HERE / "decisions_view.py"), record.stem] + (["-o", sys.argv[1]] if len(sys.argv) > 1 else [])
    subprocess.run(view, check=True)


if __name__ == "__main__":
    asyncio.run(main())
