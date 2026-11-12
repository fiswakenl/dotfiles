#!/bin/sh
# Чем GV распознаёт речь: deepgram | openai. Выбор — в ~/.claude/gv.ears, его читает listen.py при запуске,
# поэтому идущий сервис гасим: плагин сам поднимет его заново через несколько секунд.
ears=$1
echo "$ears" > "$HOME/.claude/gv.ears"
if curl -s -m 2 -o /dev/null -X POST "http://127.0.0.1:${GV_PORT:-8790}/quit"; then
  echo "Распознаёт $ears. Сервис перезапускается, через несколько секунд слушает."
else
  echo "Распознаёт $ears — со следующего включения /gv (сейчас голос выключен)."
fi
