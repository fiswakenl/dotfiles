#!/bin/bash
# Панели текущего окна iTerm: список и закрытие.
#
# tabs.sh            список: номер, вкладка, папка, что запущено, rc; «← я» — панель, откуда вызвали
# tabs.sh rc НОМЕР   включить remote control в панели: в claude — /remote-control, в шелле — claude --remote-control
# tabs.sh restart    перезапустить свою сессию: новая панель в той же папке с -c и rc, а эта закрывается
# tabs.sh close      закрыть свою панель — с паузой, чтобы команда успела отчитаться
# tabs.sh close ЦЕЛЬ номер из списка | имя папки (кусок, без регистра)
#                    | others — панели моего проекта (та же папка или внутри неё), кроме меня
#                    | all — все панели окна, кроме меня
#
# Себя закрывает только без аргумента: others и all — про чужие панели.
# Закрытая сессия claude не пропадает — поднимается через -c в той же папке.
# Вызывается из /p:ls, /p:close, /p:rc и /p:restart.
#
# rc в списке — из ~/.claude/sessions/<pid>.json: Claude Code пишет туда состояние каждого
# процесса, и у сессии с remote control есть bridgeSessionId — как бы его ни включили.
set -u
me=${TERM_SESSION_ID#*:}

# Папка панели — рабочая папка процесса на переднем плане её tty (claude, а не его дети).
# Переменной path из iTerm верить нельзя: она обновляется только на приглашении шелла,
# а панель с `cd X && claude` до приглашения не доходит — и остаётся домашней.
fg_pid() { ps -t "${1#/dev/}" -o pid=,tpgid= | awk '$1 == $2 { print $1; exit }'; }
cwd_of() { lsof -a -p "$1" -d cwd -Fn 2>/dev/null | sed -n 's/^n//p'; }

rc_of() {
  local f=$HOME/.claude/sessions/$1.json
  [ -f "$f" ] && jq -r 'if .bridgeSessionId then "rc" else empty end' "$f" 2>/dev/null
}

panes() {
  osascript -e 'tell application "iTerm2"
    set out to ""
    set sep to character id 9
    set ti to 0
    repeat with t in tabs of current window
      set ti to ti + 1
      repeat with s in sessions of t
        set out to out & ti & sep & (id of s) & sep & (variable of s named "jobName") & sep & (tty of s) & sep & (variable of s named "path") & linefeed
      end repeat
    end repeat
    return out
  end tell' | while IFS=$'\t' read -r t id job tty dir; do
    [ -n "$id" ] || continue
    pid=$(fg_pid "$tty"); real=; rc=
    [ -n "$pid" ] && { real=$(cwd_of "$pid"); rc=$(rc_of "$pid"); }
    printf '%s\t%s\t%s\t%s\t%s\t%s\n' "$t" "$id" "$job" "${real:-$dir}" "${rc:--}" "${pid:--}"
  done
}

write_to() {
  local text=${2//\\/\\\\}; text=${text//\"/\\\"}
  osascript -e "tell application \"iTerm2\"
    repeat with t in tabs of current window
      repeat with s in sessions of t
        if id of s is \"$1\" then
          tell s to write text \"$text\"
          return
        end if
      end repeat
    end repeat
  end tell"
}

close_id() {
  osascript -e "tell application \"iTerm2\"
    repeat with t in tabs of current window
      repeat with s in sessions of t
        if id of s is \"$1\" then
          close s
          return
        end if
      end repeat
    end repeat
  end tell"
}

# claude переименовывает процесс в свою версию — показываем имя, а не «2.1.270»
list=$(panes | awk -F'\t' 'NF>=4 { if ($3 ~ /^[0-9]+\.[0-9]+\.[0-9]+$/) $3 = "claude"; n++; print n "\t" $0 }' OFS='\t') || exit 1
# n  вкладка  id  процесс  путь  rc  pid

# Вкладку показываем, только если их больше одной: панели одной вкладки — это её сплиты.
tabs=$(printf '%s\n' "$list" | cut -f2 | sort -u | wc -l | tr -d ' ')

show() {
  printf '%s\n' "$1" | while IFS=$'\t' read -r n t id job path rc pid; do
    mark=; [ "$id" = "$me" ] && mark="  ← я"
    tab=; [ "$tabs" -gt 1 ] && tab="вкладка $t  "
    r=; [ "$rc" = rc ] && r=rc
    printf '%s  %s%-10s %-7s %-3s%s\n' "$n" "$tab" "$(basename "$path")" "$job" "$r" "$mark"
  done
}

# Себя закрываем в фоне и с паузой: команда успеет отчитаться, новая панель — подняться.
close_me() {
  nohup bash -c "sleep 3; $(declare -f close_id); close_id $(printf %q "$me")" >/dev/null 2>&1 &
  echo "закрою эту панель через 3 секунды: $1"
}

my_row() {
  row=$(printf '%s\n' "$list" | awk -F'\t' -v me="$me" '$3 == me')
  [ -n "$row" ] || { echo "не нашёл свою панель в списке"; exit 1; }
  IFS=$'\t' read -r n t id job path rc pid <<< "$row"
}

if [ "${1:-}" = restart ]; then
  my_row
  "$(dirname "$0")/pane.sh" "$path" c rc || exit 1
  close_me "$n $(basename "$path")"
  exit
fi

if [ "${1:-}" = rc ]; then
  target=${2:-}
  row=$(printf '%s\n' "$list" | awk -F'\t' -v q="$target" '$1 == q')
  [ -n "$row" ] || { echo "какую панель: номер из списка"; show "$list"; exit 1; }
  IFS=$'\t' read -r n t id job path rc pid <<< "$row"
  [ "$rc" = rc ] && { echo "уже с remote control: $n $(basename "$path")"; exit 0; }
  if [ "$job" = claude ]; then
    write_to "$id" "/remote-control" &&
      echo "включил remote control: $n $(basename "$path")"
  else
    cmd=$(DRY=1 "$(dirname "$0")/pane.sh" "$path" rc) || exit 1
    write_to "$id" "${cmd#*: }" && echo "запустил claude с remote control: $n $(basename "$path")"
  fi
  exit
fi

help() {
  cat <<'H'

/p:rc N              remote control в панели N
/p:close [N]         без номера — эту; others — свои панели проекта, all — все, кроме меня
/p:sufler [new] [rc] новая панель: wde, klali, dnk, sufler, haraba; new — чистая сессия
/p:restart           перезапустить эту сессию: новая панель с тем же диалогом, эта закрывается
H
}

[ "${1:-}" = close ] || { show "$list"; help; exit 0; }

target=${2:-}
[ -n "$target" ] || { my_row; close_me "$n $(basename "$path")"; exit; }

case "$target" in
  others|остальные)
    # мой проект — папка моей панели; свои — та же папка или внутри неё
    mine=$(printf '%s\n' "$list" | awk -F'\t' -v me="$me" '$3 == me { print $5; exit }')
    [ -n "$mine" ] || { echo "не нашёл свою панель в списке"; exit 1; }
    hit=$(printf '%s\n' "$list" | awk -F'\t' -v me="$me" -v d="$mine" '$3 != me && ($5 == d || index($5, d "/") == 1)') ;;
  all|все) hit=$(printf '%s\n' "$list" | awk -F'\t' -v me="$me" '$3 != me') ;;
  *[!0-9]*) hit=$(printf '%s\n' "$list" | awk -F'\t' -v q="$target" '{ n=split($5, p, "/"); if (index(tolower(p[n]), tolower(q))) print }') ;;
  *)        hit=$(printf '%s\n' "$list" | awk -F'\t' -v q="$target" '$1 == q') ;;
esac

[ -n "$hit" ] || { echo "нет такой панели: $target"; show "$list"; exit 1; }
if [ "$target" != others ] && [ "$target" != остальные ] && [ "$target" != all ] && [ "$target" != все ] && [ "$(printf '%s\n' "$hit" | wc -l)" -gt 1 ]; then
  echo "под «${target}» подходит несколько — уточни номером:"; show "$hit"; exit 1
fi

closed=0
while IFS=$'\t' read -r n t id job path rc pid; do
  if [ "$id" = "$me" ]; then echo "себя не закрываю: $n $(basename "$path")"; continue; fi
  close_id "$id" && echo "закрыл: $n $(basename "$path") ($job)" && closed=1
done <<< "$hit"
[ $closed = 1 ]
