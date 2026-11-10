# Обзор того, что запущено. Службы и туннели — ~/dotfiles/home/.chezmoidata/services.yaml.
mac-status() {
    echo "== расходится с dotfiles =="; chezmoi status | sed 's/^/  /'
    echo "== службы launchd =="
    local names=" $(ls ~/.local/state/dotfiles/services 2>/dev/null | tr '\n' ' ') "  # службы из services.yaml
    launchctl list | awk -v n="$names" 'NR>1 && (index(n, " " $3 " ") || $3 ~ /^com\.iykisoba\./) {printf "  %-24s %s\n", $3, ($1=="-" ? "ждёт, код " $2 : "работает, pid " $1)}' | sort
    echo "== слушают порты =="
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $9 ~ /^127\.0\.0\.1|^\*/ {print "  " $1 "\t" $9}' | sort -u
    echo "== tmux =="; tmux ls 2>/dev/null | sed 's/^/  /'
}
