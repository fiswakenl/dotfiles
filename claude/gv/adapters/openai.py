"""Адаптер OpenAI пакетно: каждая законченная реплика целиком — одним запросом распознавания.

Звук копится, пока человек говорит; после паузы — WAV в /v1/audio/transcriptions. Ключ — OPENAI_API_KEY
в ~/.config/openai.env; из России OpenAI закрыт — запрос идёт через OPENAI_PROXY или HTTPS_PROXY, если задан.
"""
import io
import os
import wave
from pathlib import Path

from adapters.mic import RATE
from adapters.recognizer import Recognizer

URL = "https://api.openai.com/v1/audio/transcriptions"
MODEL = os.environ.get("GV_OPENAI_MODEL", "gpt-transcribe")


def env() -> dict:
    """Ключ и прокси: ~/.config/openai.env, переменные окружения OPENAI_* поверх."""
    e = {k: v.strip() for k, v in (l.split("=", 1) for l in (Path.home() / ".config/openai.env").read_text().splitlines() if "=" in l and not l.startswith("#"))}
    e.update({k: v for k, v in os.environ.items() if k.startswith("OPENAI_")})
    e["OPENAI_PROXY"] = e.get("OPENAI_PROXY") or os.environ.get("HTTPS_PROXY") or None
    return e


def wav(pcm: bytes) -> bytes:
    out = io.BytesIO()
    with wave.open(out, "wb") as w:
        w.setnchannels(1)
        w.setsampwidth(2)
        w.setframerate(RATE)
        w.writeframes(pcm)
    return out.getvalue()


class OpenAIBatch(Recognizer):
    title = "OpenAI"

    def __init__(self, core) -> None:
        super().__init__(core)
        self.pcm = bytearray()

    async def begin(self) -> None:
        self.pcm.clear()

    async def audio(self, pcm: bytes) -> None:
        self.pcm.extend(pcm)

    def end(self):
        return transcribe(bytes(self.pcm))


async def transcribe(pcm: bytes) -> str:
    import aiohttp
    e = env()
    form = aiohttp.FormData()
    form.add_field("file", wav(pcm), filename="speech.wav", content_type="audio/wav")
    form.add_field("model", MODEL)
    form.add_field("language", "ru")
    async with aiohttp.ClientSession() as http:
        async with http.post(URL, data=form, headers={"Authorization": f"Bearer {e['OPENAI_API_KEY']}"}, proxy=e["OPENAI_PROXY"],
                             timeout=aiohttp.ClientTimeout(total=30)) as r:
            body = await r.json(content_type=None)
            if r.status != 200:
                raise RuntimeError(f"HTTP {r.status}: {str(body.get('error', body))[:300]}")
            return body.get("text", "")
