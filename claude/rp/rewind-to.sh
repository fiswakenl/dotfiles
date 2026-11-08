#!/bin/zsh
# Откат разговора в tmux-сессии со стендом rp до N-го набранного сообщения (считая сверху): rewind-to.sh СЕССИЯ N [--yes]
# Открывает меню Rewind командой плагина, жмёт «вверх», пока экран не перестанет меняться (это верх списка), идёт вниз на N-1 и,
# с --yes, подтверждает Restore conversation. Без --yes только показывает, что выбрано, и закрывает меню.
S=${1:?сессия tmux}; N=${2:-1}; YES=$3
pane() { tmux capture-pane -t "$S" -p | grep -v '^\s*$' | tail -12; }
echo '{"run":"rewind"}' > ~/.claude/rp.cmd; sleep 3
prev=""; ups=0
while true; do
  tmux send-keys -t "$S" Up; sleep 0.4; ups=$((ups+1))
  cur=$(pane)
  [ "$cur" = "$prev" ] && break          # экран не изменился: выше некуда
  prev=$cur; [ $ups -gt 60 ] && break
done
echo "вверх нажато: $ups"
for ((i=1;i<N;i++)); do tmux send-keys -t "$S" Down; sleep 0.3; done
echo "выбрано (N=$N):"; pane | sed -n '/the point before/,$p' | grep -A1 '❯' | head -2
if [ "$YES" = "--yes" ]; then tmux send-keys -t "$S" Enter; sleep 1.5; tmux send-keys -t "$S" Enter; sleep 2; echo "restore выполнен"
else tmux send-keys -t "$S" Escape; echo "(сухой прогон, меню закрыто)"; fi
