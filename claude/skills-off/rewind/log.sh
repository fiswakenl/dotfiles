#!/bin/bash
# Последние сообщения чужой сессии: кто, когда, что — плюс правки файлов.
# По этому списку выбирается точка отката: сколько раз нажать Up в окне отката.
#
# log.sh [папка проекта | zellij-сессия] [сколько сообщений]
#   без аргументов — сессия dnk, 10 сообщений.
#   Папка проекта — путь в ~/.claude/projects (можно куском имени).
set -u
q=${1:-dnk} n=${2:-10}

d=$(ls -dt "$HOME/.claude/projects/"*"$q"* 2>/dev/null | head -1)
[ -n "$d" ] || { echo "нет папки проекта под «${q}»"; exit 1; }
f=$(ls -t "$d"/*.jsonl 2>/dev/null | head -1)
[ -n "$f" ] || { echo "в $d нет логов"; exit 1; }

echo "лог: ${d##*/}/$(basename "$f")"
echo

# «вверх N» — сколько раз жать Up, чтобы попасть на эту реплику: считаем с конца.
jq -r '
  select(.type=="user" or .type=="assistant")
  | if .type=="user" and (.message.content|type=="string")
      and (.message.content|startswith("<")|not)
    then "Я\t\(.timestamp[11:16])\t\(.message.content|gsub("\n";" ")|.[0:90])"
    elif .type=="assistant" then
      (.message.content[]? | select(.type=="tool_use")
       | if (.name=="Edit" or .name=="Write" or .name=="NotebookEdit")
         then "ПРАВКА\t\t\(.name) \(.input.file_path // "")"
         else empty end)
    else empty end' "$f" |
tail -"$n" |
awk -F'\t' '{ lines[NR]=$0; if ($1=="Я") mine[NR]=1 } END {
  up=0
  for (i=NR; i>=1; i--) { if (mine[i]) { up++; pos[i]=up } }
  for (i=1; i<=NR; i++) {
    split(lines[i], c, "\t")
    if (mine[i]) printf "вверх %d\t%s\t%s\n", pos[i], c[2], c[3]
    else printf "        \t\t%s\n", c[3]
  }
}'
