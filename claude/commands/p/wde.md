---
description: "Новая панель iTerm с claude в WDE. Звать на «открой новый WDE», «claude в WDE»."
argument-hint: "[new] [rc] [v]"
allowed-tools: Bash(~/.claude/commands/p/pane.sh *)
---

# /p:wde

Открывает панель снизу и продолжает последний диалог папки.
Если он уже открыт в другой панели — стартует чистой.

    /p:wde          продолжить
    /p:wde new      чистая сессия
    /p:wde rc       с remote control
    /p:wde v        сбоку

!`~/.claude/commands/p/pane.sh wde $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь одной строкой — его выводом. Больше ничего не запускай.
