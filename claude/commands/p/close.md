---
description: "Закрыть панель iTerm. Звать на «закрой панель klali», «закрой вторую», «закрой остальные», «закрой эту»."
argument-hint: "[номер|папка|others|all]"
allowed-tools: Bash(~/.claude/commands/p/tabs.sh *)
---

# /p:close

    /p:close           эту панель
    /p:close 2         по номеру из /p:ls
    /p:close klali     по имени папки
    /p:close others    остальные панели этого проекта
    /p:close all       все, кроме этой

Диалог не пропадает: следующая панель в той же папке его продолжит.

!`~/.claude/commands/p/tabs.sh close $ARGUMENTS 2>&1`

Скрипт уже отработал. Ответь коротко — его выводом. Сам больше ничего не закрывай.
