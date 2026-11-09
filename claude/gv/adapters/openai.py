"""Адаптер OpenAI пакетно: распознавание Pipecat (OpenAISTTService) — каждый отрезок речи между паузами по Silero VAD целиком
одним запросом; реплика — все отрезки до её конца по Smart Turn.

Ключ — OPENAI_API_KEY в ~/.config/openai.env; из России OpenAI закрыт — запрос идёт через OPENAI_PROXY или HTTPS_PROXY, если задан.
"""
import os
from pathlib import Path

from openai import DefaultAsyncHttpxClient
from pipecat.services.openai.stt import OpenAISTTService
from pipecat.transcriptions.language import Language

from adapters.recognizer import Recognizer

MODEL = os.environ.get("GV_OPENAI_MODEL", "gpt-transcribe")


def env() -> dict:
    """Ключ и прокси: ~/.config/openai.env, переменные окружения OPENAI_* поверх."""
    e = {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / ".config/openai.env").read_text().splitlines() if "=" in l and not l.startswith("#"))}
    e.update({k: v for k, v in os.environ.items() if k.startswith("OPENAI_")})
    e["OPENAI_PROXY"] = e.get("OPENAI_PROXY") or os.environ.get("HTTPS_PROXY") or None
    return e


class OpenAIBatch(Recognizer):
    title = "OpenAI"

    def stt(self):
        e = env()
        return OpenAISTTService(api_key=e["OPENAI_API_KEY"], http_client=DefaultAsyncHttpxClient(proxy=e["OPENAI_PROXY"]),
                                settings=OpenAISTTService.Settings(model=MODEL, language=Language.RU))
