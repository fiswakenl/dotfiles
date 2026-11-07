# /// script
# requires-python = ">=3.10"
# dependencies = ["claude-agent-sdk"]
# ///
"""Черновик (stdin или аргумент) → предложенная правка по признакам из features.md.

    echo "черновик" | uv run hl.py
    uv run hl.py "черновик"

Признаки - features.md, постановка - prompt.md, оба рядом. Вызов модели - тот же кирпичик lm.ask:
чистая сессия на подписке. Скрипт только предлагает: итог собирает скилл.
"""
import pathlib
import sys

HERE = pathlib.Path(__file__).resolve().parent
sys.path.insert(0, str(pathlib.Path.home() / ".claude/skills"))
from lm import ask  # noqa: E402

draft = " ".join(sys.argv[1:]) or sys.stdin.read()
if not draft.strip():
    sys.exit("пустой черновик: передайте текст аргументом или через stdin")
system = (HERE / "prompt.md").read_text().replace("{features}", (HERE / "features.md").read_text().strip())
print(ask(system, draft))
