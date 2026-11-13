#!/bin/sh
# Чем GV решает «продолжение?»: openai | gemini. Выбор — в ~/.claude/gv.decide, его читает listen.py при запуске,
# поэтому идущий сервис гасим: плагин сам поднимет его заново через несколько секунд.
model=$1
echo "$model" > "$HOME/.claude/gv.decide"
if curl -s -m 2 -o /dev/null -X POST "http://127.0.0.1:${GV_PORT:-8790}/quit"; then
  echo "Решает $model. Сервис перезапускается, через несколько секунд слушает."
else
  echo "Решает $model — со следующего включения /gv (сейчас голос выключен)."
fi
