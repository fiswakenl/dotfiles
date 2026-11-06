#!/usr/bin/env python3
"""Глобальный хук уведомлений: UserPromptSubmit — промпт мнемоники; Stop — сигнал и пересказ ответа пушем Bark; Notification — пуш «нужно разрешение».

Stop: короткий звук (не при маркере 🔊 — там свой голос, klali) → пересказ последнего ответа с контекстом
из транскрипта → пуш Bark, заголовок — имя папки проекта. Notification: только permission_prompt, idle — нет.
Сеть — в отвязанном фоновом процессе: Claude Code хук не ждёт.

Настройки — файлы в двух папках одного вида: проектной `<проект>/.claude/notify/` и глобальной ~/.claude/notify/.
Значение — содержимое файла (пробелы по краям не в счёт). Для каждой настройки отдельно: есть файл в проекте — он,
нет — глобальный, нет и его — дефолт. Читаются при каждом запуске: переключение срабатывает сразу.
  prompt.md — промпт пересказа;
  mnemo.md  — как модели писать мнемонику: есть файл — UserPromptSubmit подмешивает его к каждой реплике,
              модель начинает ответ невидимой строкой с мнемоникой, и пушем уходит она, без пересказа.
              Нет мнемоники в ответе (или файла) — пересказ по prompt.md. Порядок: свой mnemo.md проекта →
              свой prompt.md (проект на старой схеме, мнемоники нет) → глобальный mnemo.md; пустой файл — выключено;
  off       — есть файл (с любым содержимым) — хук молчит целиком;
  mnemo     — on (дефолт) | off: подмешивать мнемонику к реплике; mnemo.md при этом не трогается, вернуть — `notify.py mnemo`;
  style     — off (дефолт) | имя скилла (например call): UserPromptSubmit подмешивает к реплике правила стиля ответа из
              ~/.claude/skills/<имя>/SKILL.md (строки-пункты «- …» после шапки), вместо мнемоники; переключить — `notify.py style call|off`;
  ding      — on (дефолт) | off: короткий звук (свой — файл ~/.claude/notify/ding.aiff; иначе системный «Glass», другой — env KLALI_DING=Pop, /System/Library/Sounds);
  audio     — off (дефолт) | on: озвучка пересказа на маке (edge-tts → sox), если ввод был меньше 120 с назад;
              независима от дзиня — могут звучать оба;
  push      — on (дефолт) | off: пуши Bark на телефон; озвучка и звук на маке остаются;
  model     — groq (Qwen, дефолт) | gemini (Flash-Lite, плавающий псевдоним Google — свежая сама) | openai (gpt-6-luna); упала — пересказывает следующая.
Служебное — во временной папке системы: notify.pid (обрыв прошлой озвучки). Лога нет — ошибки в stderr.
Из терминала (проект — текущая папка): `notify.py show` — итоговые настройки;
`notify.py voice` / `notify.py voice all` — переключить audio проекта / глобально и оборвать текущую озвучку;
`notify.py mnemo|push [all]` — так же переключают мнемонику и пуши.
Пуши выключены, пока есть ~/.claude/push.off (звук остаётся).
Ключи: ~/.config/bark.env (BARK_KEY), ~/.config/bf.env (BF_TOKEN - текст пуша кладётся на сервер bf, айди в конце заголовка через « · »), ~/.config/gemini.env (GEMINI_API_KEY, GEMINI_PROXY), ~/.config/openai.env,
dnk/.env.spokenly (GROQ_*; Groq — через прокси dev из env ~/.claude/settings.json).
"""
import base64
import json
import os
import pathlib
import re
import shutil
import signal
import subprocess
import sys
import tempfile
import time
import urllib.request

HOME = pathlib.Path.home()
PUSH_OFF = HOME / ".claude/push.off"
IDLE_MAX = 120  # с; дольше без ввода — за маком никого, озвучка не нужна
TURNS = 4  # сколько последних пар «вопрос — ответ» отдать пересказу как контекст
PAST_CHARS = 1000  # прошлые ответы — только чтобы понять, о чём речь; вопросы и пересказываемый ответ целиком
GROQ_MODEL = "qwen/qwen3.8-27b"
OPENAI_MODEL = "gpt-6-luna"
TEXT_MODEL = "gemini-flash-lite-latest"
EDGE_TTS, VOICE = HOME / ".local/bin/edge-tts", "ru-RU-SvetlanaNeural"  # uv tool install edge-tts
API = "https://generativelanguage.googleapis.com/v1beta/models/"
TTS = "http://devml4dbs.duckdns.org:8767"  # озвучка для iPhone на dnk: POST /say, MP3 — /last.mp3
PLAY = shutil.which("play") or "/opt/homebrew/bin/play"  # у хука может не быть Homebrew в PATH
GLOBAL = HOME / ".claude/notify"  # глобальные настройки; проектные — <проект>/.claude/notify
DEFAULTS = {"ding": "on", "audio": "off", "push": "on", "mnemo": "on", "style": "off", "model": "groq"}
TMP = pathlib.Path(tempfile.gettempdir())  # служебное хука — вне репозиториев и ~/.claude
AUDIO_PID = TMP / "notify.pid"  # группа процессов текущей озвучки — динамик на маке один на все проекты


def setting_file(root: pathlib.Path, name: str) -> pathlib.Path | None:
    """Файл настройки: проектный бьёт глобальный; нет обоих — None."""
    return next((f for f in (root / ".claude/notify" / name, GLOBAL / name) if f.is_file()), None)


def setting(root: pathlib.Path, name: str) -> str:
    f = setting_file(root, name)
    return f.read_text().strip().lower() if f else DEFAULTS[name]


SWITCH = {"voice": ("audio", "озвучка на маке"), "mnemo": ("mnemo", "мнемоника"), "push": ("push", "пуши на телефон")}


def cli(args: list[str]):
    """show — итоговые настройки проекта (текущая папка); voice|mnemo|push [all] — переключить настройку проекта / глобально."""
    root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or os.getcwd())
    if args[0] == "show":
        for name in (*DEFAULTS, "off", "prompt.md"):
            f = setting_file(root, name)
            value = "" if name == "prompt.md" else setting(root, name) if name in DEFAULTS else bool(f)
            print(f"{name:9} {value!s:7} {f or 'дефолт'}")
    elif args[0] == "style":
        target = (GLOBAL if args[2:] == ["all"] else root / ".claude/notify") / "style"
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text((args[1] if args[1:] else "call") + "\n")
        print(f"стиль ответа {target.read_text().strip()}: {target}")
    elif args[0] in SWITCH:
        name, title = SWITCH[args[0]]
        on = setting(root, name) != "on"
        target = (GLOBAL if args[1:] == ["all"] else root / ".claude/notify") / name
        target.parent.mkdir(parents=True, exist_ok=True)
        target.write_text("on\n" if on else "off\n")
        print(f"{title} {'включена' if on else 'выключена'}: {target}")
        if name == "audio":
            try:
                os.killpg(int(AUDIO_PID.read_text()), signal.SIGTERM)
            except (OSError, ValueError):
                pass


if sys.argv[1:]:
    cli(sys.argv[1:])
    sys.exit(0)

try:
    hook = json.load(sys.stdin)
except Exception:
    sys.exit(0)
event = hook.get("hook_event_name", "")
root = pathlib.Path(os.environ.get("CLAUDE_PROJECT_DIR") or hook.get("cwd") or os.getcwd())
project = root.name  # заголовок пуша — из какого проекта
message = hook.get("last_assistant_message") or ""
# Мнемоника — строка для пуша, которую пишет сама модель: [//]: # "⟪…⟫" — markdown-определение ссылки,
# его не рисует ни терминал, ни remote control. Старый вид — просто ⟪…⟫ — тоже берём.
MNEMO = re.compile(r'\s*(?:\[//\]: # ")?⟪(.+?)⟫', re.S)
MNEMO_FORMAT = """Начни последнее сообщение хода (то, что после всех инструментов) строкой ровно в таком виде, дальше пустая строка и обычный ответ:
[//]: # "⟪мнемоника⟫"
Это markdown-комментарий: на экране его не видно, а мнемоника уходит человеку пушем. Внутри — без прямых кавычек, только «ёлочки».
Как писать мнемонику:
"""

if setting_file(root, "off"):
    sys.exit(0)

if event == "UserPromptSubmit":  # есть mnemo.md — подмешиваем к реплике: Claude Code добавляет вывод хука в контекст
    style = setting(root, "style")
    if style != "off":  # правила стиля — из скилла: один источник, читаются при каждой реплике
        skill = HOME / ".claude/skills" / style / "SKILL.md"
        rules = [l for l in skill.read_text().split("---", 2)[-1].splitlines() if l.startswith("- ")] if skill.is_file() else []
        if rules:
            print("Отвечай в таком режиме, пока пользователь не скажет иначе:\n" + "\n".join(rules))
    if setting(root, "mnemo") == "off":
        sys.exit(0)
    own = root / ".claude/notify"  # свой prompt.md без своего mnemo.md — проект на старой схеме, глобальный mnemo.md не трогает
    f = own / "mnemo.md" if (own / "mnemo.md").is_file() else None if (own / "prompt.md").is_file() else setting_file(root, "mnemo.md")
    if f and f.read_text().strip():  # пустой mnemo.md — мнемоника в проекте выключена
        print(MNEMO_FORMAT + f.read_text())
    sys.exit(0)

if event == "Notification" and hook.get("notification_type") != "permission_prompt":
    sys.exit(0)

if event == "Stop" and "🔊" not in message and setting(root, "ding") != "off":
    snd = str(GLOBAL / "ding.aiff")  # свой звук: файл ~/.claude/notify/ding.aiff бьёт системный
    if not os.path.exists(snd):
        snd = f"/System/Library/Sounds/{os.environ.get('KLALI_DING', 'Glass')}.aiff"
    if not os.path.exists(snd):
        snd = "/System/Library/Sounds/Glass.aiff"
    subprocess.Popen(["afplay", snd], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                     stderr=subprocess.DEVNULL, start_new_session=True)

push = not PUSH_OFF.exists() and setting(root, "push") == "on"
audio = event == "Stop" and setting(root, "audio") == "on"
if not (push or audio) or (event == "Stop" and not message.strip() and not hook.get("transcript_path")):
    sys.exit(0)

# Дальше сеть: отвязываемся от Claude Code — родитель сразу выходит, ребёнок в своей сессии доделывает.
if os.fork():
    os._exit(0)
os.setsid()
devnull = os.open(os.devnull, os.O_RDWR)
for fd in (0, 1):
    os.dup2(devnull, fd)
t0 = time.monotonic()


def say(line: str):
    try:  # stderr хука; Claude Code к этому времени мог его уже закрыть
        print(f"{time.strftime('%H:%M:%S')} {event} {line}", file=sys.stderr, flush=True)
    except OSError:
        pass


def bf_id(text: str) -> str | None:
    """Текст на сервер bf (dnk) → айди: озвучка на iPhone забирает по нему полный текст, без обрезки iOS."""
    try:
        token = dict(l.strip().split("=", 1) for l in open(HOME / ".config/bf.env") if "=" in l)["BF_TOKEN"]
        return urllib.request.urlopen(urllib.request.Request(
            "https://bf.devml4dbs.duckdns.org/", data=text.encode(),
            headers={"Authorization": f"Bearer {token}"}), timeout=10).read().decode()
    except Exception as e:
        say(f"bf: {e}")
        return None


def bark(body: str):
    try:
        key = dict(l.strip().split("=", 1) for l in open(HOME / ".config/bark.env") if "=" in l)["BARK_KEY"]
        # Айди - в конце заголовка через « · »: заголовок iOS отдаёт автоматизации целиком,
        # там его отделяет «Разбить текст» по « · » (в адрес целый заголовок не годится - режется на пробеле).
        i = bf_id(body)
        # В пуш - начало: у APNs предел ~4 КБ, целиком текст лежит на сервере bf.
        short = body if not i or len(body) <= 1000 else body[:1000].rstrip() + "…"
        push = {"device_key": key, "title": f"{project} · {i}" if i else project, "body": short, "group": project}
        urllib.request.urlopen(urllib.request.Request(
            "https://api.day.app/push", headers={"Content-Type": "application/json"},
            data=json.dumps(push, ensure_ascii=False).encode()), timeout=10)
        say(f"пуш: {body}")
    except Exception as e:
        say(f"Bark: {e}")


if event == "Notification":
    text = " ".join((hook.get("message") or "").split())
    text = re.sub(r"^Claude needs your permission to use ", "", text)  # суть — имя инструмента
    bark("Нужно твоё разрешение: " + (text[:120] or "Claude ждёт"))
    sys.exit(0)

if event != "Stop":
    sys.exit(0)


def clip(s: str) -> str:
    return s if len(s) <= PAST_CHARS else s[:PAST_CHARS - 1] + "…"


def dialog() -> tuple[str, str]:
    """(контекст — прошлые пары и последний вопрос, последний ответ) из транскрипта."""
    turns: list[list] = []  # [вопрос, [куски ответа]]
    try:
        for line in open(hook["transcript_path"]):
            e = json.loads(line)
            content = e.get("message", {}).get("content")
            if e.get("type") == "user" and isinstance(content, str) and not content.lstrip().startswith("<"):
                turns.append([content.strip(), []])  # реплика человека, не служебная вставка и не результат инструмента
            elif e.get("type") == "assistant" and isinstance(content, list) and turns:
                turns[-1][1] += [b["text"] for b in content if b.get("type") == "text"]
    except Exception as e:
        say(f"транскрипт: {e}")
    if not turns:
        return "", message
    *past, (question, parts) = turns[-TURNS:]
    answer = message or "\n\n".join(parts)
    context = "\n\n".join(f"Пользователь: {q}\nАссистент: {clip(chr(10).join(a))}" for q, a in past)
    return f"{context}\n\nПользователь: {question}".strip(), answer


mnemo = MNEMO.match(message)
context, answer = ("", message) if mnemo else dialog()
if not answer.strip():
    sys.exit(0)
system = setting_file(root, "prompt.md").read_text()
request = f"Контекст разговора:\n<<<\n{context}\n>>>\n\nОтвет для пересказа:\n<<<\n{answer[:8000]}\n>>>"

gem_env: dict = {}


def gemini(model: str, body: dict, stream: bool = False) -> subprocess.Popen:
    """Gemini через curl: ходит только через SOCKS из ~/.config/gemini.env."""
    if not gem_env:
        gem_env.update(l.strip().split("=", 1) for l in open(HOME / ".config/gemini.env") if "=" in l)
    url = API + model + (":streamGenerateContent?alt=sse" if stream else ":generateContent")
    return subprocess.Popen(
        ["curl", "-sSN", "--max-time", "120", "--retry", "3", "--retry-all-errors", "--retry-delay", "1",
         "-x", gem_env["GEMINI_PROXY"].replace("socks5:", "socks5h:"), "-H", "x-goog-api-key: " + gem_env["GEMINI_API_KEY"],
         "-H", "Content-Type: application/json", "-d", json.dumps(body), url],
        stdout=subprocess.PIPE, text=True)


def via_gemini() -> str:
    """Gemini Flash-Lite: ~1 с, своя квота; thinkingBudget lite не принимает."""
    out = json.loads(gemini(TEXT_MODEL, {
        "systemInstruction": {"parts": [{"text": system}]},
        "contents": [{"parts": [{"text": request}]}],
    }).communicate()[0])
    return "".join(p.get("text", "") for p in out["candidates"][0]["content"]["parts"]).strip()


def via_groq() -> str:
    """Groq, Qwen без рассуждений: ~0,15–0,5 с, но дневной лимит. Ключ — dnk/.env.spokenly, только через прокси dev."""
    groq = dict(l.split("=", 1) for l in (HOME / "Documents/store/dnk/.env.spokenly").read_text().splitlines()
                if "=" in l and not l.lstrip().startswith("#"))
    https = os.environ.get("HTTPS_PROXY") or json.loads((HOME / ".claude/settings.json").read_text())["env"]["HTTPS_PROXY"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": https.strip()}))
    reply = opener.open(urllib.request.Request(groq["GROQ_BASE_URL"].strip() + "/chat/completions", data=json.dumps({
        "model": GROQ_MODEL, "reasoning_effort": "none", "max_tokens": 300,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": request}]}).encode(),
        headers={"Authorization": "Bearer " + groq["GROQ_API_KEY"].strip(), "Content-Type": "application/json",
                 "User-Agent": "notify"}), timeout=15)
    return json.load(reply)["choices"][0]["message"]["content"].strip()


def via_openai() -> str:
    """OpenAI без рассуждений: ~2,5 с. Из России закрыт — только через прокси dev."""
    key = dict(l.split("=", 1) for l in (HOME / ".config/openai.env").read_text().splitlines() if "=" in l)
    https = os.environ.get("HTTPS_PROXY") or json.loads((HOME / ".claude/settings.json").read_text())["env"]["HTTPS_PROXY"]
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({"https": https.strip()}))
    reply = opener.open(urllib.request.Request("https://api.openai.com/v1/chat/completions", data=json.dumps({
        "model": OPENAI_MODEL, "reasoning_effort": "none", "max_completion_tokens": 300,
        "messages": [{"role": "system", "content": system}, {"role": "user", "content": request}]}).encode(),
        headers={"Authorization": "Bearer " + key["OPENAI_API_KEY"].strip().strip('"'),
                 "Content-Type": "application/json"}), timeout=15)
    return json.load(reply)["choices"][0]["message"]["content"].strip()


models = {"groq": via_groq, "gemini": via_gemini, "openai": via_openai}
first = setting(root, "model")
order = [first, *(m for m in models if m != first)] if first in models else list(models)
speech = " ".join(mnemo.group(1).split()) if mnemo else ""
if speech:
    say("пересказ — мнемоника модели")
for name in [] if speech else order:
    try:
        speech = models[name]()
        if speech:
            say(f"пересказ {name} {time.monotonic() - t0:.1f} с ({len(speech)} знаков)")
            break
    except Exception as e:
        say(f"{name}: {e}")
speech = speech or " ".join(answer.split())[:200]  # обе модели упали — начало ответа как есть

if push:
    bark(speech)

# Озвучка файлом для iPhone выключена 05.10: в iOS 27 автоматизация с «Получить содержимое URL» нестабильна, на iPhone читает Siri.
# Вернуть — раскомментировать: текст уходит на dnk (tools/tts/tts.py), там edge-tts делает MP3 для «Команд».
# try:
#     urllib.request.build_opener(urllib.request.ProxyHandler({})).open(
#         urllib.request.Request(TTS + "/say", data=speech.encode()), timeout=3)
# except Exception as e:
#     say(f"tts на dnk: {e}")


def idle_seconds() -> float:
    """Сколько секунд не было ввода с клавиатуры и мыши; не узнали — считаем, что за маком никого."""
    try:
        out = subprocess.run(["ioreg", "-c", "IOHIDSystem"], capture_output=True, text=True, timeout=5).stdout
        return int(re.search(r'"HIDIdleTime" = (\d+)', out).group(1)) / 1e9
    except Exception:
        return float("inf")


if not audio:
    sys.exit(0)
idle = idle_seconds()
if idle >= IDLE_MAX:
    say(f"озвучки нет: простой {idle:.0f} с")
    sys.exit(0)
# Обрываем прошлую озвучку (любого проекта): она живёт в своей группе процессов вместе с edge-tts и sox.
try:
    os.killpg(int(AUDIO_PID.read_text()), signal.SIGTERM)
except (OSError, ValueError):
    pass
AUDIO_PID.write_text(str(os.getpgid(0)))

edge = subprocess.Popen([str(EDGE_TTS), "-v", VOICE, "-t", speech], stdout=subprocess.PIPE, stderr=subprocess.PIPE)
player = subprocess.run([PLAY, "-q", "-t", "mp3", "-"], stdin=edge.stdout)  # играем по мере прихода MP3
edge.stdout.close()
if edge.wait():
    say(f"edge-tts: {' '.join(edge.stderr.read().decode().split())[:300]}")
say(f"озвучка: {time.monotonic() - t0:.1f} с")
