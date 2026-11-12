# /// script
# requires-python = ">=3.10"
# dependencies = ["aiohttp", "sounddevice", "numpy", "livekit-agents>=1.8.11,<2", "livekit-plugins-deepgram>=1.8.11,<2", "livekit-plugins-openai>=1.8.11,<2"]
# ///
"""Проба: слух GV на готовом цикле разговора LiveKit Agents (AgentSession) — без сервера, комнаты и аккаунта LiveKit.

    запись ──20 мс, в реальном времени──▶ AgentSession(stt, vad; llm и tts нет) ──▶ события с временем по звуку

Запись подаётся своим источником звука (session.input.audio) вместо комнаты. Цикл сам ведёт детектор речи, распознавание и
решение «реплика закончена»; конец реплики приходит в Agent.on_user_turn_completed — там у нас было бы «склеить или отдельным».
Чем решать конец реплики (--turn):
    mini   локальная модель по звуку из пакета (turn-detector-v1-mini), без сети
    ours   наш вопрос TURN классификатору OpenAI — как детектор цикла (по тексту)
    vad    только тишина после речи
    stt    конец речи по мнению самого распознавания
--stt openai шлёт в распознавание только речь: отсечка тишины стоит в узле stt_node, детекторы цикла слышат весь звук.
--trace ФАЙЛ — сообщения Deepgram пишутся в запись запуска тем же journal.trace, что в listen.py.
Платно, кроме --stt none: распознавание и, с --turn ours, классификатор.

    uv run --script tests/livekit_loop_probe.py [--stt deepgram|openai|none] [--turn mini|ours|vad|stt] [--min 0.7] [--max 2.5]"""
import argparse
import asyncio
import json
import logging
import os
import sys
import time
import wave
from collections import deque
from pathlib import Path

import aiohttp

HERE = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(HERE))
os.environ.setdefault("HTTPS_PROXY", json.load(open(Path.home() / ".claude/settings.json"))["env"]["HTTPS_PROXY"])
import journal  # noqa: E402
import listen  # noqa: E402
from livekit import rtc  # noqa: E402
from livekit.agents import Agent, AgentSession, StopResponse, inference  # noqa: E402
from livekit.agents.vad import VADEventType  # noqa: E402
from livekit.agents.voice import io  # noqa: E402
from livekit.plugins import openai  # noqa: E402

KEEP = Path.home() / ".claude/gv-keep"
TAIL_S = 5  # тишина после записи: последняя реплика успевает закрыться


class Player(io.AudioInput):
    """Источник звука цикла вместо комнаты: запись блоками по BLOCK_S в реальном времени. at() — секунды по звуку."""

    def __init__(self, pcm: bytes) -> None:
        super().__init__(label="запись")
        self.pcm, self.n, self.t0 = pcm, 0, 0.0
        self.step = int(listen.RATE * listen.BLOCK_S) * 2

    def at(self) -> float:
        return round(time.monotonic() - self.t0, 2) if self.t0 else 0.0

    async def __anext__(self) -> rtc.AudioFrame:
        if self.n * self.step >= len(self.pcm):
            raise StopAsyncIteration
        if not self.t0:
            self.t0 = time.monotonic()
        await asyncio.sleep(max(0, self.t0 + self.n * listen.BLOCK_S - time.monotonic()))
        b = self.pcm[self.n * self.step:(self.n + 1) * self.step]
        self.n += 1
        return rtc.AudioFrame(b, listen.RATE, 1, len(b) // 2)


class OurTurn:
    """Наш вопрос «мысль закончена?» как детектор конца реплики цикла: вероятность complete; ниже порога цикл ждёт max_delay."""

    model, provider = listen.DECISIONS_MODEL, "openai"

    def __init__(self, say) -> None:
        self.say = say
        self.decisions = openai.DecisionModel(model=listen.DECISIONS_MODEL, api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"])

    async def unlikely_threshold(self, language) -> float:
        return 0.5

    async def supports_language(self, language) -> bool:
        return True

    async def predict_end_of_turn(self, chat_ctx, *, timeout=None) -> float:
        text = listen.plain(chat_ctx.items[-1].text_content or "")
        r = (await self.decisions.evaluate(input=f"user: {text}", decisions=listen.TURN, conn_options=listen.TURN_WAIT)).results["turn"]
        p = (r.probabilities or {}).get("complete", 1.0 if r.value == "complete" else 0.0)
        self.say("наш TURN", f"{r.value} p(complete)={p:.2f}")
        return p


class Ear(Agent):
    """Агент без модели и голоса: только слушает. Конец реплики — on_user_turn_completed."""

    def __init__(self, say, gate: bool) -> None:
        super().__init__(instructions="")
        self.say, self.gate, self.turns = say, gate, []

    async def on_user_turn_completed(self, turn_ctx, new_message) -> None:
        self.turns.append((self.say("КОНЕЦ РЕПЛИКИ", new_message.text_content or ""), new_message.text_content or ""))
        raise StopResponse  # отвечать некому: дальше решает наш код (склеить / отдельным → core.call)

    def stt_node(self, audio, model_settings):
        return Agent.default.stt_node(self, self.speech_only(audio) if self.gate else audio, model_settings)

    async def speech_only(self, audio):
        """Отсечка тишины перед распознаванием — то же, что Gate в listen.py: свой Silero слушает всё, в распознавание идёт
        запас GATE_BEFORE_S до речи и сама речь; GATE_AFTER_S тишины — поток молчит."""
        vad = inference.VAD(model="silero", min_silence_duration=listen.GATE_AFTER_S).stream()
        before: deque = deque(maxlen=int(listen.GATE_BEFORE_S / listen.BLOCK_S))
        gate = {"open": False, "sent": 0, "all": 0}

        async def watch() -> None:
            async for ev in vad:
                if ev.type == VADEventType.START_OF_SPEECH:
                    gate["open"] = True
                elif ev.type == VADEventType.END_OF_SPEECH:
                    gate["open"] = False
                    self.say("отсечка", f"в распознавание ушло {gate['sent'] * listen.BLOCK_S:.1f} с из {gate['all'] * listen.BLOCK_S:.1f} с")
        task = asyncio.create_task(watch())
        try:
            async for frame in audio:
                gate["all"] += 1
                vad.push_frame(frame)
                if not gate["open"]:
                    before.append(frame)
                    continue
                while before:
                    gate["sent"] += 1
                    yield before.popleft()
                gate["sent"] += 1
                yield frame
        finally:
            task.cancel()
            await vad.aclose()


class Verdicts(logging.Handler):
    """Ответ детектора конца реплики. Событием цикл его не отдаёт (eot_prediction уходит только в SessionHost комнаты или
    консоли), он есть лишь в отладочном журнале библиотеки — оттуда и берём."""

    say = None

    def emit(self, r: logging.LogRecord) -> None:
        if r.getMessage() == "eot prediction" and (p := getattr(r, "probability", None)) is not None:
            Verdicts.say("детектор конца", f"p={p:.2f} порог={r.unlikely_threshold:.2f} → ждать {r.endpointing_delay} с тишины (по {r.trigger})")
        elif r.levelno >= logging.WARNING:
            Verdicts.say("библиотека", r.getMessage()[:200])


async def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--wav", default="20261112-220530")
    ap.add_argument("--stt", default="deepgram", choices=["deepgram", "openai", "none"])
    ap.add_argument("--turn", default="mini", choices=["mini", "ours", "vad", "stt"])
    ap.add_argument("--min", type=float, default=listen.QUICK_MS / 1000, help="ждать после речи, когда детектор говорит «закончил»")
    ap.add_argument("--max", type=float, default=listen.PAUSE_MS / 1000, help="ждать, когда детектор говорит «договорит»")
    ap.add_argument("--seconds", type=int, default=60)
    ap.add_argument("--trace", help="файл записи запуска (journal.trace)")
    ap.add_argument("--debug", action="store_true")
    a = ap.parse_args()
    lk = logging.getLogger("livekit.agents")
    lk.setLevel(logging.DEBUG)
    lk.propagate = a.debug
    lk.addHandler(Verdicts())
    if a.trace:
        journal._state["trace"] = Path(a.trace)

    with wave.open(str(KEEP / f"{a.wav}.wav")) as w:
        pcm = w.readframes(a.seconds * listen.RATE) + bytes(TAIL_S * listen.RATE * 2)
    player = Player(pcm)

    def say(kind: str, text: str = "") -> float:
        at = player.at()
        print(f"{at:7.2f}  {kind:<16} {text}", flush=True)
        return at
    Verdicts.say = say

    # без trust_env, как в listen.py: Deepgram — напрямую, OpenAI — через прокси
    async with aiohttp.ClientSession(proxy=os.environ["HTTPS_PROXY"] if a.stt == "openai" else None) as http:
        if a.stt == "openai":
            stt = openai.STT(api_key=listen.env(".config/openai.env")["OPENAI_API_KEY"],
                             vad=inference.VAD(model="silero", min_silence_duration=listen.VAD_SILENCE_S), **listen.OPENAI_STT)
            stt._session = http
        elif a.stt == "deepgram":
            stt = listen.Deepgram(api_key=listen.env(".config/deepgram.env")["DEEPGRAM_API_KEY"], http_session=http, **listen.DEEPGRAM)
        else:
            stt = None
        turn = {"mini": lambda: inference.TurnDetector(version="v1-mini"), "ours": lambda: OurTurn(say),
                "vad": lambda: "vad", "stt": lambda: "stt"}[a.turn]()
        session = AgentSession(stt=stt, user_away_timeout=None,
                               turn_handling={"turn_detection": turn, "endpointing": {"min_delay": a.min, "max_delay": a.max}})
        ear = Ear(say, gate=a.stt == "openai")

        session.on("user_state_changed", lambda e: say({"speaking": "начало речи", "listening": "конец речи"}.get(e.new_state, e.new_state)))
        session.on("user_input_transcribed", lambda e: say("готовый текст" if e.is_final else "черновик", e.transcript))
        session.on("agent_state_changed", lambda e: say("агент", f"{e.old_state} → {e.new_state}"))
        session.on("agent_false_interruption", lambda e: say("перебивание", "ложное"))
        session.on("error", lambda e: say("ОШИБКА", f"{type(e.error).__name__}: {e.error}"))
        session.on("close", lambda e: say("закрыт", f"{e.reason}"))

        session.input.audio = player  # вместо комнаты; вывода звука нет вовсе
        print(f"stt={a.stt} turn={a.turn} min={a.min} max={a.max} запись={a.wav} {a.seconds} с")
        await session.start(ear)  # room не передаём
        while player.n * player.step < len(pcm):
            await asyncio.sleep(0.2)
        await asyncio.sleep(1)
        await session.aclose()
    print("\nконцов реплики:", len(ear.turns))
    for at, text in ear.turns:
        print(f"  {at:6.2f} с  {text[:110]}")


if __name__ == "__main__":
    asyncio.run(main())
