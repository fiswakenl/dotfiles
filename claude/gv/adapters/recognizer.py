"""Общее у адаптеров распознавания: микрофон по репликам, текст реплики — как распознан, решение — за оркестратором плагина.

Наследник даёт три шага одной реплики: begin() — человек заговорил, audio(pcm) — звук, end() — замолчал;
end() возвращает корутину с распознанным текстом. Реплики уходят в ядро в порядке речи, даже если распознались вразнобой.
"""
import asyncio
import base64

from adapters import Adapter
from adapters.mic import BLOCK, Mic
from journal import emit, log


class Recognizer(Adapter):
    def __init__(self, core) -> None:
        super().__init__(core)
        self.tail: asyncio.Task | None = None  # последняя реплика в распознавании: следующая ждёт её

    def start(self) -> list:
        return [self.mic()]

    async def begin(self) -> None: ...

    async def audio(self, pcm: bytes) -> None: ...

    def end(self):
        raise NotImplementedError

    async def mic(self) -> None:
        mic = Mic(self.pause_ms)
        log("mic.start", adapter=self.title)
        async for event, pcm in mic.blocks():
            try:
                await self.step(event, pcm)
            except Exception as ex:  # noqa: BLE001
                mic.stop()
                self.error(ex)
                await asyncio.sleep(2)

    async def step(self, event: str, pcm: bytes) -> None:
        if event == "start":
            await self.begin()
        elif event == "audio":
            await self.audio(pcm)
        else:
            emit({"state": "распознаю"})
            self.finish(self.end())

    def finish(self, recognized) -> None:
        """Текст реплики — в ядро, после предыдущей."""
        before = self.tail

        async def run() -> None:
            try:
                text = (await recognized or "").strip()
            except Exception as ex:  # noqa: BLE001
                text = ""
                self.error(ex)
            if before:
                await before
            log("recognized", adapter=self.title, text=text)
            if text:
                emit({"me": text})
                self.core.utter(text)
            else:
                emit({"state": "ничего не распознано"})

        self.tail = asyncio.create_task(run())

    def error(self, ex: Exception) -> None:
        why = f"{self.title}: {type(ex).__name__}: {ex}"
        log("recognize.error", error=why[:500])
        emit({"error": why})

    async def listen(self, args: dict) -> str:
        """Речь вместо микрофона: args.pcm — base64 PCM 16 бит, 16 кГц, моно, одной репликой."""
        pcm = base64.b64decode(args.get("pcm", ""))
        log("audio.inject", bytes=len(pcm))
        await self.step("start", b"")
        for i in range(0, len(pcm), BLOCK * 2):  # 100 мс, как в реальном времени
            await self.step("audio", pcm[i:i + BLOCK * 2])
            await asyncio.sleep(0.1)
        await self.step("end", b"")
        await self.tail
        return f"ok {len(pcm) / 32000:.1f} с"
