---
description: "Новая панель iTerm с claude в klali. Звать на «открой новый klali», «claude в klali»."
argument-hint: "[new] [rc] [v]"
allowed-tools: Bash(~/.claude/commands/p/pane.sh *)
---

# /p:klali

Открывает панель снизу и продолжает последний диалог папки.
Если он уже открыт в другой панели — стартует чистой.

    /p:klali          продолжить
    /p:klali new      чистая сессия
    /p:klali rc       с remote control
    /p:klali v        сбоку

!`~/.claude/commands/p/pane.sh klali $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь одной строкой — его выводом. Больше ничего не запускай.
