---
description: "Новая панель iTerm с claude в haraba. Звать на «открой новый haraba», «claude в haraba»."
argument-hint: "[new] [rc] [v]"
allowed-tools: Bash(~/.claude/commands/p/pane.sh *)
---

# /p:haraba

Открывает панель снизу и продолжает последний диалог папки.
Если он уже открыт в другой панели — стартует чистой.

    /p:haraba          продолжить
    /p:haraba new      чистая сессия
    /p:haraba rc       с remote control
    /p:haraba v        сбоку

!`~/.claude/commands/p/pane.sh haraba $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь одной строкой — его выводом. Больше ничего не запускай.
