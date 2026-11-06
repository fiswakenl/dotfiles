#!/usr/bin/env python3
"""Тик «вернись в терминал»: launchd запускает раз в 30 с (com.iykisoba.claude-ping). Пока какая-то сессия ждёт тебя
(флаг в ~/.claude/ping/, его ставит notify.py на Stop и на запрос разрешения, снимает твоя следующая реплика, живёт TTL)
и на переднем плане не iTerm — пинг, всё настойчивее: 1–3 звук, 4–8 голос, с 9-го громче + уведомление macOS. Лимита нет.
Первый пинг — на первом же тике вне iTerm (до 30 с после ответа). Экран заблокирован — молчит,
вернулся в iTerm — счётчик с нуля. Выключатель — `notify.py ping [all]`; глобальный `off` гасит тик целиком."""
import base64
import io
import wave
import hashlib
import json
import os
import pathlib
import random
import plistlib
import subprocess
import time
import urllib.request

HOME = pathlib.Path.home()
DIR = HOME / ".claude/ping"
STATE = DIR / "state.json"
TTL = 2 * 3600  # флаг старше — сессия, видимо, умерла
AWAY_MIN = 1  # тиков вне iTerm до первого пинга: при 2 первый звонок приходил через 30–60 с и ты успевал вернуться раньше
ITERM = "com.googlecode.iterm2"
VOICE, SAY_FROM, LOUD_FROM = "Milena", 4, 9
DING = HOME / ".claude/notify/ding.aiff"  # тот же звук, что у хука на Stop
OFFSET = 0.05  # с между началами двух звуков пинга; короче самого звука (0,1 с) — они накладываются: ближе к одному «трынк»
SOUND = str(DING) if DING.exists() else "/System/Library/Sounds/Glass.aiff"


def run(*cmd: str) -> str:
    return subprocess.run(cmd, capture_output=True, text=True, timeout=10).stdout


def front_bundle() -> str:
    asn = run("lsappinfo", "front").strip()
    out = run("lsappinfo", "info", "-only", "bundleid", asn) if asn else ""
    return out.split("=", 1)[-1].strip().strip('"')


def locked() -> bool:
    try:
        return bool(plistlib.loads(run("ioreg", "-n", "Root", "-d1", "-a").encode()).get("IOConsoleUsers", [{}])[0].get("CGSSessionScreenIsLocked"))
    except Exception:
        return False


def main():
    off = HOME / ".claude/notify/ping"
    if off.is_file() and off.read_text().strip().lower() == "off":
        return
    now = time.time()
    flags = [f for f in DIR.glob("session-*") if now - f.stat().st_mtime < TTL]
    for f in DIR.glob("session-*"):
        if f not in flags:
            f.unlink(missing_ok=True)
    st = json.loads(STATE.read_text()) if STATE.exists() else {"away": 0, "count": 0}
    if not flags or front_bundle() == ITERM:
        st = {"away": 0, "count": 0}
    elif not locked():
        st["away"] += 1
        if st["away"] >= AWAY_MIN:
            st["count"] += 1
            st.setdefault("scenario", random.choice(list(SCENARIOS)))
            ping(st["count"], flags, st["scenario"])
    STATE.write_text(json.dumps(st))


def double(volume: str = "1"):
    """«Тык-тык»: звук хука и он же ещё раз со сдвигом OFFSET, одним файлом: два запуска afplay подряд давали разрыв больше секунды.
    Файл пересобирается, когда меняется звук хука."""
    out = DIR / f"double-{OFFSET}.aiff"
    if not out.exists() or out.stat().st_mtime < pathlib.Path(SOUND).stat().st_mtime:
        subprocess.run(["sox", "-m", SOUND, f"|sox {SOUND} -t aiff - pad {OFFSET} 0", str(out), "gain", "-n", "-1"], timeout=10)
    subprocess.run(["afplay", "-v", volume, str(out)])


# голос пинга: нейросеть OpenAI, манера растёт вместе с уровнем; фраза озвучивается один раз и лежит файлом
TTS_MODEL, TTS_VOICE = "gpt-4o-mini-tts-2025-12-15", "shimmer"  # запасной голос, если Gemini не ответил
# 3.8-flash(-lite)-tts читает инструкцию вслух и добавляет в конец щелчок; 2.5-flash-preview-tts выполняет её молча и без щелчка
GEMINI_MODEL, GEMINI_VOICE, GEMINI_TAIL_CUT = "gemini-2.5-flash-preview-tts", "Leda", 0
# Сценарий — персонаж: выбирается на первом пинге ожидания и держится, пока ты не вернёшься, так что эскалация — одна история.
# На каждом уровне (с какого пинга) несколько фраз на выбор; подкол добрый, нарастает напор, а не злость.
SCENARIOS = {
    "друг": ("Говори по-русски тепло и игриво, как близкий друг, который шутливо подкалывает и зовёт тебя обратно: с улыбкой, без злости.", [
        (4, ["Эй, ты куда делся? {p} уже скучает.", "Я всё вижу, между прочим. Давай обратно, {p} ждёт.",
             "Ну и где мой любимый человек? {p} без тебя заскучал."]),
        (7, ["Так, я не обижаюсь. Но {p} уже вздыхает, а я за компанию.", "Ты там что, интереснее нашёл? Ай, ну спасибо. Вернись, {p} ждёт.",
             "Я посижу тут, помолчу, посмотрю на тебя с укором. Ну иди уже сюда."]),
        (LOUD_FROM, ["Так, всё! Бросай там всё и возвращайся, {p} ждёт, я жду, мы оба ждём!", "Ну сколько можно! Я же сейчас начну кричать. Ласково, но громко. Назад, к {p}!",
                     "Последний шанс по-хорошему! Иначе буду звонить тебе голосом, и так до самого вечера!"])]),
    "ревнивая": ("Говори по-русски мило, с шутливой ревностью и обиженной ноткой, как девушка, которая делает вид, что дуется, а на самом деле скучает.", [
        (4, ["Это что, у тебя другая вкладка? {p} всё слышит.", "Смотри-смотри, а я тут жду. Совсем одна, с {p}.",
             "Мне показалось, или ты ушёл? Нет, мне показалось, да?"]),
        (7, ["Ага, значит, браузер тебе милее. Ну ладно, ладно, я не ревную. Почти.", "Я ничего не говорю. Но {p} всё видит и всё запомнит.",
             "Знаешь, я бы обиделась. Но мне лень, поэтому просто вернись."]),
        (LOUD_FROM, ["Всё, я обиделась! Прямо сейчас вернись и извинись перед {p}!", "Ты вообще меня слышишь? Назад, быстро, а то я совсем расстроюсь!",
                     "Я так больше не могу! Бросай всё и иди сюда, ну пожалуйста!"])]),
    "диктор": ("Говори по-русски торжественным голосом диктора новостей, серьёзно, но с явной улыбкой и иронией.", [
        (4, ["Срочные новости: пользователь исчез, а {p} остался без него.", "В студии экстренный выпуск. Очевидцы видели, как вы ушли от {p}.",
             "Сообщаем: терминал вас ждёт. Подробности, как только вернётесь."]),
        (7, ["Ситуация обостряется. Свидетели говорят, что вы всё ещё не вернулись.", "Эксперты в замешательстве: как можно так долго не заглядывать к {p}?",
             "Мы продолжаем следить за пустым терминалом. Он держится, но уже грустит."]),
        (LOUD_FROM, ["Прерываем эфир! Срочно требуется ваше возвращение к {p}!", "Это не учения! Бросайте всё и бегом в терминал!",
                     "Экстренное включение! Весь эфир отдан одной просьбе: вернитесь, пожалуйста!"])]),
    "навигатор": ("Говори по-русски ровным мягким голосом автомобильного навигатора, вежливо и невозмутимо, с лёгким флиртом и иронией.", [
        (4, ["Вы свернули не туда, дорогой. Пожалуйста, развернитесь и возвращайтесь к {p}.", "Перестраиваю маршрут. Через сто метров разворот, и мы снова вместе.",
             "Ваша цель, {p}, осталась позади. Но я не обижаюсь."]),
        (7, ["Вы удаляетесь от цели. Я это терплю, но с трудом.", "Маршрут перестроен уже в который раз. Милый, вернись, пожалуйста.",
             "Повторяю с улыбкой: {p} находится в другой стороне."]),
        (LOUD_FROM, ["Внимание! Вы окончательно заблудились! Разворот прямо сейчас, к {p}!", "Маршрут не построить, пока вы не вернётесь! Я серьёзно!",
                     "Разворот! Разворот! Разворот! Ну пожалуйста!"])]),
    "тайный поклонник": ("Говори по-русски тихо, загадочно и игриво, как будто шепчешь на ухо что-то личное: с интригой, лёгким флиртом и улыбкой.", [
        (4, ["Это я, твой терминал. Я тут скучаю. Заглянешь?", "Тс-с. Никому не говори, но {p} по тебе соскучился.",
             "Есть одна тайна: без тебя тут совсем не то. Возвращайся."]),
        (7, ["Я бы сказала, что скучаю. Но я уже сказала. Дважды.", "Ты же знаешь, что я не умею ждать красиво? Вот и я не умею. Возвращайся к {p}.",
             "Тут стало тихо. Слишком тихо. Мне это не нравится, иди сюда."]),
        (LOUD_FROM, ["Всё, больше не шепчу! Вернись сейчас же, {p} ждёт, и я тоже!", "Я не умею ждать молча! Иди сюда, ну пожалуйста, быстро!",
                     "Последний раз прошу по-хорошему. Дальше буду просить по-плохому, и тебе понравится ещё меньше!"])]),
}
TTS_STYLE_TAIL = " Голос женский: мягкий, заискивающий, с лёгким флиртом и игривыми придыханиями, но без пошлости. Фраза короткая."


def api_key() -> str:
    for line in (HOME / ".config/openai.env").read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k.removeprefix("export ").strip() == "OPENAI_API_KEY":
            return v.strip().strip("\"'")
    raise KeyError("OPENAI_API_KEY")


def proxy_opener() -> urllib.request.OpenerDirector:
    """У launchd своего прокси нет, а OpenAI и Gemini напрямую отвечают 403: берём тот же HTTP-прокси, что у Claude Code."""
    proxy = os.environ.get("HTTPS_PROXY") or json.loads((HOME / ".claude/settings.json").read_text())["env"]["HTTPS_PROXY"]
    return urllib.request.build_opener(urllib.request.ProxyHandler({"https": proxy}))


def gemini_key() -> str:
    for line in (HOME / ".config/gemini.env").read_text().splitlines():
        k, _, v = line.strip().partition("=")
        if k.strip() == "GEMINI_API_KEY":
            return v.strip().strip("\"'")
    raise KeyError("GEMINI_API_KEY")


def tts_gemini(text: str, style: str) -> bytes:
    """Gemini TTS: манеру задаёт сам текст запроса; ответ — сырой PCM 24 кГц, заворачиваем в wav."""
    body = {"contents": [{"parts": [{"text": f"{style}{TTS_STYLE_TAIL} Скажи: {text}"}]}], "generationConfig": {"responseModalities": ["AUDIO"],
            "speechConfig": {"voiceConfig": {"prebuiltVoiceConfig": {"voiceName": GEMINI_VOICE}}}}}
    req = urllib.request.Request(f"https://generativelanguage.googleapis.com/v1beta/models/{GEMINI_MODEL}:generateContent",
                                 headers={"x-goog-api-key": gemini_key(), "Content-Type": "application/json"}, data=json.dumps(body).encode())
    pcm = base64.b64decode(json.loads(proxy_opener().open(req, timeout=60).read())["candidates"][0]["content"]["parts"][0]["inlineData"]["data"])
    if GEMINI_TAIL_CUT:  # у моделей 3.8 в конце файла громкий щелчок (~150 мс на пределе громкости после тишины)
        pcm = pcm[:-2 * int(24000 * GEMINI_TAIL_CUT)]
    buf = io.BytesIO()
    with wave.open(buf, "wb") as w:
        w.setnchannels(1), w.setsampwidth(2), w.setframerate(24000), w.writeframes(pcm)
    return buf.getvalue()


def tts_openai(text: str, style: str) -> bytes:
    req = urllib.request.Request("https://api.openai.com/v1/audio/speech", headers={"Authorization": f"Bearer {api_key()}", "Content-Type": "application/json"},
                                 data=json.dumps({"model": TTS_MODEL, "voice": TTS_VOICE, "input": text, "instructions": style + TTS_STYLE_TAIL, "response_format": "mp3"}).encode())
    return proxy_opener().open(req, timeout=20).read()


def voice(n: int, project: str, scenario: str):
    """Фраза уровня n в манере сценария: Gemini, не вышло — OpenAI, не вышло — системный голос. Готовая фраза лежит файлом."""
    style, levels = SCENARIOS[scenario]
    phrases = [lv for lv in levels if lv[0] <= n][-1][1]
    text = random.choice(phrases).format(p=project)
    key = hashlib.md5((text + style + GEMINI_MODEL + GEMINI_VOICE).encode()).hexdigest()[:12]
    f = DIR / "voice" / (key + ".wav")
    f.parent.mkdir(exist_ok=True)
    if not f.exists():
        for name, make, ext in (("Gemini", tts_gemini, ".wav"), ("OpenAI", tts_openai, ".mp3")):
            try:
                f = f.with_suffix(ext)
                f.write_bytes(make(text, style))
                break
            except Exception as e:
                f.unlink(missing_ok=True)
                with (DIR / "ping.log").open("a") as log:
                    log.write(f"{time.strftime('%H:%M:%S')} {name} не вышел ({e})\n")
        else:
            subprocess.run(["say", "-v", VOICE, text])
            return
    subprocess.run(["afplay", str(f)])


def ping(n: int, flags: list[pathlib.Path], scenario: str):
    """Звук и голос играем и дожидаемся: launchd убивает всё, что осталось от задачи после её выхода."""
    with (DIR / "ping.log").open("a") as f:  # след каждого пинга: проверить, что тик вообще звонил
        f.write(f"{time.strftime('%H:%M:%S')} пинг {n} ({scenario})\n")
    project = max(flags, key=lambda f: f.stat().st_mtime).read_text().strip() or "Claude"
    if n < SAY_FROM:
        double()
        return
    if n >= LOUD_FROM:
        double("2")
        subprocess.run(["osascript", "-e", f'display notification "Вернись в терминал: {project} ждёт" with title "Claude"'])
    voice(n, project, scenario)


if __name__ == "__main__":
    DIR.mkdir(parents=True, exist_ok=True)
    main()
