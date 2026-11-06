#!/usr/bin/env python3
"""Хук UserPromptSubmit: переписать кривую диктовку до того, как её увидит сессия.

Ставится в ~/.claude/settings.json:

    "UserPromptSubmit": [{"hooks": [{"type": "command",
      "command": "python3 ~/.claude/hooks/fix-prompt.py", "timeout": 25}]}]

Срабатывает только по метке в конце фразы: «поправь», «фикс», «--» — переписать
начисто; «переведи», «перевод» — перевести на английский. Без метки текст уходит
как есть: молчаливая правка каждой реплики пугает сильнее, чем помогает.
Слэш-команды и короткие фразы не трогаем никогда.

Переписывает одноразовая чистая сессия: без инструментов, без settings.json,
CLAUDE.md, скиллов и MCP, один ход, cwd=/tmp — иначе на фразу уходит 26 тысяч
токенов вместо тысячи. Прокси из ~/.claude/settings.json передаём явно: с
пустыми setting_sources CLI сам его не читает.

Контекст: последние сообщения текущей сессии читаются из `transcript_path`
(JSON Lines) и уходят переписывающему в системном промпте — иначе «глянь там
по той папке» нечем раскрыть. Берём только текст и имена вызванных
инструментов, длинное режем, служебные строки (`<command-name>`,
`<local-command-caveat>`, системные напоминания) пропускаем.

Любая осечка — молчим и пропускаем исходный текст: хук не должен мешать работе.
"""
import json
import os
import pathlib
import re
import subprocess
import sys

SYSTEM = """Ты переписываешь надиктованную голосом реплику разработчика, адресованную ИИ-агенту в терминале.

Речь идёт через транскрибацию, поэтому фраза сбита: оговорки, повторы, «ну», «короче», «это самое», брошенные начала. Смысл в ней есть, и он один — твоя работа вытащить его и сказать то же самое внятно.

Правила: пиши от первого лица, его голосом, не переходи на канцелярит. Одна-две фразы. Не добавляй того, чего он не говорил: ни уточнений задачи, ни описи файлов, ни предположений о том, что он «наверное имел в виду».

Все названия сохраняй дословно, даже если они звучат как сленг или опечатка: «по тушке» — это папка Тушка, «обвес» — папка Обвес, «сиплень» — команда. Выбросить название хуже, чем оставить непонятное: без него агент пойдёт искать не там.

Ниже может идти контекст — последние сообщения того разговора, в который уйдёт реплика. Он нужен только чтобы раскрыть отсылки: «та папка», «этот файл», «то, что ты смотрел» заменяй на конкретное имя из контекста, если оно там однозначно есть. Сам контекст не пересказывай, на вопросы из него не отвечай, задач из него не добавляй.

Верни только переписанную реплику. Без кавычек, без пояснений, без вариантов."""

TRANSLATE = """Ты переводишь на английский реплику русскоязычного разработчика, надиктованную голосом.

Речь идёт через транскрибацию, поэтому фраза сбита: оговорки, повторы, «ну», «короче», брошенные начала. Переводи смысл, а не слова: по-английски должно звучать так, как сказал бы носитель в рабочем разговоре, живой разговорной лексикой, без канцелярита и без учебниковой кальки.

Имена файлов, папок и команд не переводи — оставляй как есть.

Технические термины переводи принятым английским термином, а не по звучанию: переобучение — overfitting (не retraining), недообучение — underfitting, обучение модели — training, признаки — features, выборка — set или sample по смыслу, дрейф — drift, развернуть — deploy. Ошибка в термине хуже корявой фразы: собеседник поймёт не то.

Верни только английский перевод. Без кавычек, без пояснений, без вариантов, без исходной фразы."""

# метка в конце фразы → что с ней сделать
MODES = {
    "поправь": "fix", "фикс": "fix", "--": "fix",
    "переведи": "translate", "перевод": "translate",
}
MIN_CHARS = 40

CONTEXT_MESSAGES = 12      # сколько последних сообщений тащим
MSG_CHARS = 400            # предел на одно сообщение
CONTEXT_CHARS = 5000       # предел на весь контекст
SKIP_PREFIXES = ("<command-name>", "<command-message>", "<local-command-caveat>",
                 "<system-reminder>", "<local-command-stdout>")


def proxy_env() -> dict:
    try:
        env = json.loads((pathlib.Path.home() / ".claude/settings.json").read_text()).get("env", {})
    except (OSError, ValueError):
        env = {}
    keys = ("HTTPS_PROXY", "HTTP_PROXY", "NO_PROXY")
    return {k: v for k in keys if (v := os.environ.get(k) or env.get(k))}


def clip(s: str, n: int) -> str:
    s = re.sub(r"\s+", " ", s).strip()
    return s if len(s) <= n else s[: n - 1] + "…"


def block_text(block: dict) -> str:
    """Текст блока сообщения; tool_use — одной строкой с именем и главным аргументом."""
    kind = block.get("type")
    if kind == "text":
        return block.get("text") or ""
    if kind == "tool_use":
        inp = block.get("input") or {}
        arg = inp.get("command") or inp.get("file_path") or inp.get("pattern") or inp.get("path") \
            or inp.get("description") or ""
        return f"[вызвал {block.get('name')}: {clip(str(arg), 120)}]" if arg else f"[вызвал {block.get('name')}]"
    return ""


def message_text(entry: dict) -> str:
    content = (entry.get("message") or {}).get("content")
    if isinstance(content, str):
        text = content
    elif isinstance(content, list):
        text = " ".join(t for b in content if isinstance(b, dict) and (t := block_text(b)))
    else:
        return ""
    text = re.sub(r"<system-reminder>.*?</system-reminder>", "", text, flags=re.S).strip()
    if not text or text.startswith(SKIP_PREFIXES):
        return ""
    return text


def context(transcript_path: str) -> str:
    """Последние реплики сессии в виде «user: …» / «assistant: …»."""
    if not transcript_path:
        return ""
    try:
        lines = pathlib.Path(transcript_path).read_text(encoding="utf-8").splitlines()
    except OSError:
        return ""
    turns = []
    # идём с конца: нужны только последние N, файл может быть большим
    for line in reversed(lines):
        try:
            entry = json.loads(line)
        except ValueError:
            continue
        role = entry.get("type")
        if role not in ("user", "assistant") or entry.get("isMeta") or entry.get("isSidechain"):
            continue
        text = message_text(entry)
        if not text:
            continue
        turns.append(f"{role}: {clip(text, MSG_CHARS)}")
        if len(turns) >= CONTEXT_MESSAGES:
            break
    turns.reverse()
    out = "\n".join(turns)
    if len(out) > CONTEXT_CHARS:
        out = out[-CONTEXT_CHARS:]
    return out


def rewrite(text: str, ctx: str, system: str = SYSTEM) -> str:
    """Через CLI, а не через SDK: тот же чистый вызов, но вдвое быстрее.

    Пустые --settings и --allowed-tools нужны, чтобы не приехали CLAUDE.md,
    скиллы и инструменты: иначе он начинает отвечать по существу вместо того,
    чтобы переписать фразу.
    """
    if ctx:
        system += f"\n\n<контекст разговора>\n{ctx}\n</контекст разговора>"
    out = subprocess.run(
        # sonnet, не haiku: haiku теряет названия папок и иногда отвечает по
        # существу вместо переписывания, а по времени он тут даже медленнее.
        ["claude", "-p", "--model", "sonnet", "--strict-mcp-config",
         "--settings", "{}", "--allowed-tools", "", "--system-prompt", system],
        input=text, capture_output=True, text=True, timeout=20,
        # Метка для самого себя: вложенный claude тоже дёрнет этот хук, и без
        # неё на каждую правку плодился бы лишний процесс.
        cwd="/tmp", env={**os.environ, **proxy_env(), "FIX_PROMPT_NESTED": "1"},
    )
    return out.stdout.strip() if out.returncode == 0 else ""


def main() -> None:
    if os.environ.get("FIX_PROMPT_NESTED"):
        return
    try:
        data = json.load(sys.stdin)
    except (ValueError, OSError):
        return
    # в живом хуке поле называется `prompt`; `user_input` — из старых доков
    text = (data.get("prompt") or data.get("user_input") or "").strip()

    if not text or text.startswith("/") or len(text) < MIN_CHARS:
        return
    tail = text.lower().rstrip(" .!?")
    mode = next((m for word, m in MODES.items() if tail.endswith(word)), "")
    if not mode:
        return

    # метку саму в переписывание не тащим
    body = re.sub(r"[\s.,!?]*(" + "|".join(MODES) + r")[\s.!?]*$", "", text, flags=re.I).strip()
    if not body:
        return

    try:
        new = rewrite(body, context(data.get("transcript_path") or ""),
                      TRANSLATE if mode == "translate" else SYSTEM)
    except Exception:
        return
    if not new or new == body:
        return

    if mode == "translate":
        print(json.dumps({
            "systemMessage": f"перевёл: {new}",
            "hookSpecificOutput": {
                "hookEventName": "UserPromptSubmit",
                "additionalContext": (
                    "Пользователь просил перевести свою фразу на английский. Перевод "
                    f"уже готов — покажи его первой строкой, ничего не меняя:\n{new}"),
            },
        }, ensure_ascii=False))
        return

    # Подменить сам промпт в 2.1.273 нельзя: поля updatedPrompt в этой версии
    # нет, а suppressOriginalPrompt работает только вместе с decision "block",
    # который гасит реплику целиком и оставляет модель без задачи. Поэтому
    # чистую версию отдаём припиской — оригинал остаётся в истории.
    # systemMessage — поле верхнего уровня, внутри hookSpecificOutput его не видно.
    print(json.dumps({
        "systemMessage": f"переписал: {new}",
        "hookSpecificOutput": {
            "hookEventName": "UserPromptSubmit",
            "additionalContext": (
                "Реплика надиктована голосом и потому сбита. Вот её чистая версия, "
                f"работай по ней:\n{new}"),
        },
    }, ensure_ascii=False))


if __name__ == "__main__":
    main()
