# Обзор того, что запущено. Туннели — boring (~/.boring.toml): boring list | open ИМЯ | close ИМЯ, -a — все.
mac-status() {
    echo "== расходится с dotfiles =="; chezmoi status | sed 's/^/  /'
    echo "== туннели =="; boring list 2>/dev/null | sed 's/^/  /'
    echo "== службы launchd =="
    launchctl list | awk 'NR>1 && $3 ~ /^com\.iykisoba\./ {printf "  %-40s %s\n", $3, ($1=="-" ? "не запущена, код " $2 : "работает, pid " $1)}' | sort
    echo "== слушают порты =="
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $9 ~ /^127\.0\.0\.1|^\*/ {print "  " $1 "\t" $9}' | sort -u
    echo "== tmux =="; tmux ls 2>/dev/null | sed 's/^/  /'
}
