#!/bin/bash
# Новая панель с claude — панелью iTerm.
#
# pane.sh [цель] [флаги...]
#   цель:  wde | klali | dnk | dev [папка] | ПАПКА | ничего — текущая папка
#   new    не продолжать последний диалог; по умолчанию -c, если в папке была история
#          и последний диалог не открыт в другом живом claude
#   c      продолжить последний диалог, даже если он уже открыт (так зовёт /p:restart)
#   rc     --remote-control (на dev сервер remote-control и так всегда)
#   v      панель сбоку, а не снизу
#   прочее уходит в claude как есть
#
# На dev папка считается от ~, короткое имя ищется ещё и в ~/code:
# `dev klali` → ~/code/klali. Там поднимается tmux rc-ИМЯ с `claude remote-control`,
# повторный вызов подцепляется к живому.
# Вызывается из /p:wde, /p:klali, /p:dnk — файлы рядом.
# DRY=1 — только напечатать команду, панель не открывать.
set -u
self="$(cd "$(dirname "$0")" && pwd -P)/$(basename "$0")"

# Внутри панели: ssh на dev и tmux там. Вынесено сюда, чтобы не городить
# кавычки внутри AppleScript.
if [ "${1:-}" = --dev ]; then
  remote='
    cd ~ || exit 1
    d=${1:-.}
    [ -d "$d" ] || [ ! -d "code/$d" ] || d=code/$d
    [ -d "$d" ] || { echo "на dev нет папки: $1"; exit 1; }
    d=$(cd "$d" && pwd -P)
    c=
    enc=$(printf %s "$d" | sed "s/[^A-Za-z0-9]/-/g")
    [ "$2" != 0 ] && ls ~/.claude/projects/"$enc"/*.jsonl >/dev/null 2>&1 && c=-c
    exec tmux new -A -s "rc-$(basename "$d")" -c "$d" "$HOME/.local/bin/claude remote-control $c"
  '
  exec ssh -t dev "bash -c $(printf %q "$remote") _ $(printf %q "$2") $3"
fi

project() {
  case "$(printf %s "$1" | tr '[:upper:]' '[:lower:]')" in
    wde)   echo ~/Documents/store/WDE ;;
    klali) echo ~/Documents/store/klali ;;
    dnk)   echo ~/Documents/store/dnk ;;
    sufler) echo ~/Documents/store/sufler ;;
    haraba) echo ~/Documents/store/haraba ;;
    *) return 1 ;;
  esac
}

is_flag() {
  case "$1" in new|n|-n|новый|rc|-rc|v|-v|сбоку|c|-c) return 0 ;; *) return 1 ;; esac
}

# История диалогов лежит в ~/.claude/projects под физическим путём,
# где всё кроме букв и цифр заменено на дефис; файл назван id диалога.
# -c берёт самый свежий — его id и печатаем. Нет истории — -c упадёт.
last_dialog() {
  local enc f
  enc=$(cd "$1" && pwd -P | sed 's/[^A-Za-z0-9]/-/g') || return 1
  f=$(ls -t "$HOME/.claude/projects/$enc/"*.jsonl 2>/dev/null | head -1)
  [ -n "$f" ] && basename "$f" .jsonl
}

# Диалог уже открыт в живом claude: каждый процесс пишет о себе
# ~/.claude/sessions/<pid>.json с sessionId. Печатаем pid занявшего.
dialog_busy() {
  local f pid
  for f in "$HOME"/.claude/sessions/*.json; do
    pid=$(basename "$f" .json)
    kill -0 "$pid" 2>/dev/null || continue
    [ "$(jq -r .sessionId "$f" 2>/dev/null)" = "$1" ] && { echo "$pid"; return 0; }
  done
  return 1
}

dev=0 dir=$PWD remote_dir="" cont=1 side=horizontally
extra=()

if [ $# -gt 0 ] && ! is_flag "$1"; then
  if [ "$1" = dev ]; then
    dev=1; shift
    if [ $# -gt 0 ] && ! is_flag "$1"; then remote_dir=$1; shift; fi
  elif p=$(project "$1"); then
    dir=$p; shift
  elif [ -d "${1/#\~/$HOME}" ]; then
    dir=${1/#\~/$HOME}; shift
  else
    echo "не знаю, где это: $1 — wde, klali, dnk, sufler, haraba, dev или папка" >&2; exit 1
  fi
fi

for a in "$@"; do
  case "$a" in
    new|n|-n|новый) cont=0 ;;
    c|-c)           cont=2 ;;
    rc|-rc)         extra+=(--remote-control) ;;
    v|-v|сбоку)     side=vertically ;;
    *)              extra+=("$a") ;;
  esac
done

if [ $dev = 1 ]; then
  [ ${#extra[@]} -eq 0 ] || [ "${extra[*]}" = --remote-control ] ||
    echo "на dev флаги claude не передаются: ${extra[*]}" >&2
  cmd="$(printf %q "$self") --dev $(printf %q "$remote_dir") $cont"
  what="dev:${remote_dir:-~}"
else
  # Панель iTerm — интерактивный zsh, алиас ccm из ~/.zshrc в ней есть: запуск claude описан в одном месте.
  cmd="cd $(printf %q "$dir") && ccm"
  note=
  if [ $cont != 0 ] && last=$(last_dialog "$dir"); then
    # По умолчанию не цепляемся к диалогу, который уже открыт в другой панели:
    # два процесса на одной сессии не видят друг друга и мешают историю.
    # Явный c (его передаёт /p:restart) продолжает в любом случае.
    if [ $cont = 1 ] && busy=$(dialog_busy "$last"); then
      note=" — последний диалог уже открыт (pid $busy), эта чистая"
    else
      cmd+=" -c"
    fi
  fi
  for e in ${extra[@]+"${extra[@]}"}; do cmd+=" $(printf %q "$e")"; done
  what=$cmd$note
fi

where=снизу; [ $side = vertically ] && where=сбоку
[ -n "${DRY:-}" ] && { echo "$where: $cmd${note:-}"; exit 0; }

esc=${cmd//\\/\\\\}; esc=${esc//\"/\\\"}
osascript -e "tell application \"iTerm2\" to tell current session of current window
  tell (split $side with default profile) to write text \"$esc\"
end tell" >/dev/null || exit 1
echo "открыл $where: $what"
