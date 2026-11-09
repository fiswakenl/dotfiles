"""Голосовые адаптеры: что слышит человека и как реплика попадает в ядро. Один файл — один адаптер, выбор — по имени.

Адаптер даёт ядру одно из двух:
- сам решает, что с репликой, и зовёт core.call(ask | quick, …) — так делает Gemini Live; он же читает ответы Claude вслух;
- только распознаёт и отдаёт законченную реплику core.utter(text) — решает оркестратор плагина, ответы человек читает в окне Claude.
"""
import importlib
import os
from pathlib import Path

HERE = Path(__file__).resolve().parent
PROMPTS = HERE.parent / "prompts"

# имя в настройках плагина → модуль и класс
ADAPTERS = {"gemini": "gemini:Live", "deepgram": "deepgram:Deepgram", "openai": "openai:OpenAIBatch"}


class Adapter:
    """Общий интерфейс. Без голоса ответов: say() сразу освобождает голосовой канал — ответ Claude уже в его окне."""

    title = ""
    speaks = False  # читает ответы Claude вслух

    def __init__(self, core) -> None:
        self.core = core
        self.pause_ms = int(os.environ.get("GV_PAUSE_MS", 2500))  # столько тишины — реплика закончена

    def start(self) -> list:
        """Фоновые задачи адаптера (микрофон, сон сокета)."""
        return []

    async def say(self, text: str) -> None:
        self.core.turn_end()

    async def listen(self, args: dict) -> str:
        """Речь вместо микрофона (проверки): args.pcm — base64 PCM 16 бит, 16 кГц, моно."""
        raise NotImplementedError

    def about(self) -> str:
        """Как устроен канал — для страницы журнала."""
        return f"{self.title}: распознаёт законченную реплику; ask, поправку или quick решает оркестратор плагина.\n\n" + \
            (PROMPTS / "orchestrator.md").read_text()


def make(name: str, core) -> Adapter:
    """Адаптер по имени; GV_TEXT_ONLY=1 — текстовая замена Gemini без звука и сети (тесты)."""
    if os.environ.get("GV_TEXT_ONLY") == "1":
        from adapters.text import Text
        return Text(core)
    module, cls = ADAPTERS.get(name, ADAPTERS["gemini"]).split(":")
    return getattr(importlib.import_module(f"adapters.{module}"), cls)(core)
