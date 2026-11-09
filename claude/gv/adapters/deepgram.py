"""Адаптер Deepgram: поток по вебсокету, сокет на реплику — открывается, когда человек заговорил, закрывается, когда замолчал.

Звук идёт, пока человек говорит (тарифицируется отправленный звук); после паузы — CloseStream: Deepgram досылает
окончательные куски и закрывает сокет сам, их текст — реплика. Ключ — DEEPGRAM_API_KEY в ~/.config/deepgram.env.
"""
import asyncio
import json
import os
from pathlib import Path

from adapters.recognizer import Recognizer
from journal import log

URL = ("wss://api.deepgram.com/v1/listen?model=nova-3&language=ru&encoding=linear16&sample_rate=16000&channels=1"
       "&punctuate=true&smart_format=true")


def key() -> str:
    """Переменная окружения DEEPGRAM_API_KEY поверх ~/.config/deepgram.env."""
    if k := os.environ.get("DEEPGRAM_API_KEY"):
        return k
    for line in (Path.home() / ".config/deepgram.env").read_text().splitlines():
        if line.startswith("DEEPGRAM_API_KEY="):
            return line.split("=", 1)[1].strip()
    raise RuntimeError("нет DEEPGRAM_API_KEY в ~/.config/deepgram.env")


class Deepgram(Recognizer):
    title = "Deepgram"

    def __init__(self, core) -> None:
        super().__init__(core)
        self.ws = None
        self.text: asyncio.Task | None = None  # окончательные куски текущей реплики

    async def begin(self) -> None:
        import websockets
        if self.ws:  # прошлая реплика оборвалась сбоем: её сокет больше не нужен
            await self.ws.close()
        self.ws = await websockets.connect(URL, additional_headers={"Authorization": f"Token {key()}"}, proxy=None, max_size=None)
        self.text = asyncio.create_task(self.read(self.ws))
        log("deepgram.open")

    async def audio(self, pcm: bytes) -> None:
        await self.ws.send(pcm)

    def end(self):
        ws, text, self.ws = self.ws, self.text, None

        async def close() -> str:
            await ws.send(json.dumps({"type": "CloseStream"}))
            return await asyncio.wait_for(text, 15)

        return close()

    async def read(self, ws) -> str:
        parts = []
        async for raw in ws:
            m = json.loads(raw)
            if m.get("type") == "Results" and m.get("is_final"):
                if t := m["channel"]["alternatives"][0]["transcript"].strip():
                    parts.append(t)
        log("deepgram.close", code=ws.close_code, parts=len(parts))
        return " ".join(parts)
