"""Микрофон компьютера по репликам для Gemini Live: громкий блок начинает речь, pause_ms тишины её кончают."""
import asyncio
from collections import deque

from journal import emit, log

RATE = 16000
BLOCK = 1600  # 100 мс
LOUD = 500  # уровень (RMS int16), с которого блок считается речью
PRE = 3  # блоков предыстории перед речью: начало слова тише порога


class Mic:
    def __init__(self, pause_ms: int, muted=lambda: False) -> None:
        """muted() — сейчас не слушать (свой голос из колонок): блоки пропускаются, не считаются ни речью, ни тишиной."""
        self.hang, self.muted = pause_ms // 100, muted
        self.talking = False

    def stop(self) -> None:
        """Реплика оборвана (сбой отправки): следующий громкий блок начнёт новую."""
        self.talking = False

    async def blocks(self):
        """События ("start", b""), ("audio", pcm) — предыстория и сама речь, ("end", b""). Микрофон не открылся — ошибка в журнал, конец."""
        import numpy as np
        import sounddevice as sd
        loop, q = asyncio.get_running_loop(), asyncio.Queue()
        try:
            stream = sd.RawInputStream(samplerate=RATE, channels=1, dtype="int16", blocksize=BLOCK,
                                       callback=lambda data, *_: loop.call_soon_threadsafe(q.put_nowait, bytes(data)))
            stream.start()
        except Exception as ex:  # noqa: BLE001
            emit({"error": f"микрофон: {type(ex).__name__}: {ex}"})
            return
        emit({"state": "микрофон слушает"})
        quiet, pre = 0, deque(maxlen=PRE)
        while True:
            b = await q.get()
            if self.muted():
                pre.clear()
                continue
            loud = float(np.sqrt(np.mean(np.frombuffer(b, dtype=np.int16).astype(np.float32) ** 2))) > LOUD
            if loud and not self.talking:
                self.talking = True
                log("mic.speech.start")
                emit({"state": "слышу вас"})
                yield "start", b""
                for p in pre:
                    if self.talking:
                        yield "audio", p
                pre.clear()
            if not self.talking:
                pre.append(b)
                continue
            yield "audio", b
            if not self.talking:
                continue
            quiet = 0 if loud else quiet + 1
            if quiet > self.hang:
                self.talking = False
                log("mic.speech.end", pause_ms=self.hang * 100)
                yield "end", b""
