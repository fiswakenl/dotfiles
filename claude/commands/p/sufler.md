---
description: "Новая панель iTerm с claude в sufler. Звать на «открой новый sufler», «claude в sufler»."
argument-hint: "[new] [rc] [v]"
allowed-tools: Bash(~/.claude/commands/p/pane.sh *)
---

# /p:sufler

Открывает панель снизу и продолжает последний диалог папки.
Если он уже открыт в другой панели — стартует чистой.

    /p:sufler          продолжить
    /p:sufler new      чистая сессия
    /p:sufler rc       с remote control
    /p:sufler v        сбоку

!`~/.claude/commands/p/pane.sh sufler $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь одной строкой — его выводом. Больше ничего не запускай.
