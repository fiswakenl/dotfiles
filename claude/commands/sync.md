---
description: "Синк заметок: Notes → локально, раскладка дней, облако подгоняется под локальное — лишнее в iCloud режется. Звать на «синкни заметки», «/sync»"
allowed-tools: Bash(python3 ~/.claude/skills/notes/sync.py *)
---

!`python3 ~/.claude/skills/notes/sync.py sync 2>&1 && python3 ~/.claude/skills/notes/sync.py ls`

Одной строкой: что пришло, что удалено в облаке. Карту не пересказывать.
