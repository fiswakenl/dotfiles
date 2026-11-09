"""Замена Gemini без звука и сокета (GV_TEXT_ONLY=1): то, что ушло бы Gemini, — событие say; речь длится GV_SAY_MS."""
import asyncio
import os

from adapters import Adapter, gemini
from journal import log


class Text(Adapter):
    title = "Gemini без звука"
    speaks = True

    def __init__(self, core) -> None:
        super().__init__(core)
        self.say_s = int(os.environ.get("GV_SAY_MS", 1500)) / 1000

    async def say(self, text: str) -> None:
        log("say", chars=len(text))
        await asyncio.sleep(self.say_s)
        self.core.turn_end()

    async def listen(self, args: dict) -> str:
        """Человек говорит args.seconds секунд, затем Gemini отвечает ему (GV_SAY_MS)."""
        self.core.hear(True)
        await asyncio.sleep(float(args.get("seconds", 3)))
        self.core.hear(False)
        await asyncio.sleep(self.say_s)
        self.core.turn_end()
        return "ok"

    def about(self) -> str:
        return gemini.about()
