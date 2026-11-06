---
description: "Новая панель iTerm с claude в dnk. Звать на «открой новый dnk», «claude в dnk»."
argument-hint: "[new] [rc] [v]"
allowed-tools: Bash(~/.claude/commands/p/pane.sh *)
---

# /p:dnk

Открывает панель снизу и продолжает последний диалог папки.
Если он уже открыт в другой панели — стартует чистой.

    /p:dnk          продолжить
    /p:dnk new      чистая сессия
    /p:dnk rc       с remote control
    /p:dnk v        сбоку

!`~/.claude/commands/p/pane.sh dnk $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь одной строкой — его выводом. Больше ничего не запускай.
