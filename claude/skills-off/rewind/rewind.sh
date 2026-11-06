#!/bin/bash
# Отмотать сессию Claude Code к своей реплике и отправить вместо неё новый текст.
# Скрипт только жмёт клавиши: что отматывать и чем заменить — решает скилл.
#
#   rewind.sh <zellij-сессия> <шагов вверх> <текст>
#
# шагов вверх — сколько раз нажать Up в окне отката: 1 = последняя моя реплика.
# PANE=terminal_0 — обязательно, если в сессии больше одной панели.
# DRY=1 — показать план и выйти.
#
# Панель должна простаивать: пока идёт генерация, первый Esc обрывает её,
# и окно отката не открывается.
set -u

[ $# -ge 3 ] || { echo "rewind.sh <сессия> <шагов вверх> <текст>"; exit 1; }
session=$1 up=$2 text=$3
case "$up" in ''|*[!0-9]*) echo "шагов вверх — число"; exit 1 ;; esac
[ "$up" -ge 1 ] || { echo "шагов вверх — минимум 1"; exit 1; }

screen=${TMPDIR:-/tmp}/rewind-$session.txt

[ -n "${DRY:-}" ] && { echo "$session: вверх $up, промпт: $text"; exit 0; }
zellij -s "$session" action list-panes >/dev/null 2>&1 ||
  { echo "нет сессии zellij: $session"; exit 1; }

# --- своя сессия: пережить собственный обрыв -------------------------------
# Первый Esc Esc убивает тот ход, из которого запущен скрипт. Поэтому при откате
# самого себя перезапускаемся отцеплённо и даём обрыву доиграться.
if [ "${ZELLIJ_SESSION_NAME:-}" = "$session" ] && [ -z "${REWIND_DETACHED:-}" ]; then
  REWIND_DETACHED=1 nohup bash "$0" "$@" >"$screen.log" 2>&1 &
  echo "своя сессия: отматываю отцеплённо, ход сейчас оборвётся; итог в $screen.log"
  exit 0
fi
[ -n "${REWIND_DETACHED:-}" ] && sleep 3

# --- какая панель ----------------------------------------------------------
# По фокусу не угадываем: он гуляет по другим панелям, и текст уехал бы не туда.
pane=${PANE:-}
if [ -z "$pane" ] && [ "${ZELLIJ_SESSION_NAME:-}" = "$session" ]; then
  pane=${ZELLIJ_PANE_ID:+terminal_$ZELLIJ_PANE_ID}
fi
if [ -z "$pane" ]; then
  panes=$(zellij -s "$session" action list-panes -a -j |
    jq -r '.[] | select(.is_plugin|not) | "terminal_\(.id)\t\(.title)"')
  if [ "$(printf '%s\n' "$panes" | grep -c .)" = 1 ]; then
    pane=${panes%%$'\t'*}
  else
    echo "в сессии $session несколько панелей — укажи PANE=terminal_N:"
    printf '%s\n' "$panes"
    exit 1
  fi
fi

key() { local action=$1; shift; zellij -s "$session" action "$action" -p "$pane" "$@" >/dev/null; }

snap() {
  zellij -s "$session" action dump-screen -p "$pane" --path "$screen" >/dev/null 2>&1 &&
    [ -s "$screen" ]
}

# Поле ввода в снимке обрамлено линиями «───», и искать его надо именно так:
# «последняя строка с ❯» ловит историю разговора, а не ввод.
input_line() {
  awk '{ l[NR] = $0 }
    END { for (i = NR; i > 1; i--)
            if (l[i] ~ /^❯/ && l[i-1] ~ /^─+[[:space:]]*$/) {
              s = l[i]; sub(/^❯[[:space:]]*/, "", s); print s; break } }' "$screen"
}

prompt_dirty() {
  snap || return 1
  [ -n "$(input_line | tr -d '[:space:]')" ]
}

# --- откат -----------------------------------------------------------------
# Ждём простоя: пока панель генерирует, Esc уйдёт на обрыв, а не в окно отката.
for _ in $(seq 20); do
  snap && grep -q "esc to interrupt" "$screen" || break
  sleep 0.5
done

key write 27 27; sleep 0.35                       # Esc Esc — окно отката
for _ in $(seq "$up"); do key send-keys "Up"; sleep 0.15; done
key write 13; sleep 0.3                          # выбрать реплику
key write 13; sleep 0.6                          # «1. Restore conversation»

# Откат возвращает старый текст в строку ввода не сразу, и до его приезда строка
# выглядит пустой. Напечатаешь раньше — получишь склейку, поэтому сначала ждём
# сам текст, потом сносим, и только два подряд пустых снимка считаем чистотой.
sleep 0.5
for _ in $(seq 10); do
  prompt_dirty && break
  sleep 0.2
done

# Ctrl-C сносит строку целиком, вместе с перенесённой частью; Ctrl-U так не умеет.
# Но два Ctrl-C подряд по пустой строке — это выход из Claude Code, поэтому бьём
# по одному и каждый раз смотрим на экран.
clean=0
for _ in 1 2 3 4; do
  if prompt_dirty; then
    key write 3; clean=0; sleep 0.35
  else
    clean=$((clean + 1))
    [ "$clean" -ge 2 ] && break
    sleep 0.35
  fi
done
[ "$clean" -ge 2 ] || { echo "строка ввода не чистится — смотри $screen"; exit 1; }

# --- новый промпт ----------------------------------------------------------
# Старый текст иногда доезжает уже после очистки, и наш текст приклеивается
# к нему. Поэтому печатаем, сверяем и при расхождении стираем и печатаем снова.
# Сверяем по началу: длинный текст в поле переносится, и первая строка снимка
# короче самого промпта, поэтому берём заведомо короткий кусок.
head=$(printf '%s' "$text" | head -c 15)
ok=0
for _ in 1 2 3; do
  key write-chars "$text"; sleep 1.2
  snap
  case "$(input_line)" in "$head"*) ok=1; break ;; esac
  key write 3; sleep 0.5
done
[ "$ok" = 1 ] || { echo "в строке ввода не мой текст — не отправляю, смотри $screen"; exit 1; }

key write 13

sleep 0.6
snap
echo "отправлено в $session ($pane), экран: $screen"
grep -v '^[[:space:]]*$' "$screen" | tail -5
