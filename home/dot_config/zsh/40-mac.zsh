# Туннель к серверу (служба launchd com.iykisoba.tunnel) и обзор того, что запущено.
_proxy=com.iykisoba.tunnel
proxy-up()     { launchctl print "gui/$UID/$_proxy" >/dev/null 2>&1 || launchctl bootstrap "gui/$UID" ~/Library/LaunchAgents/$_proxy.plist; launchctl kickstart "gui/$UID/$_proxy"; }
proxy-down()   { launchctl bootout "gui/$UID/$_proxy" 2>/dev/null; echo "туннель остановлен до proxy-up или следующего входа"; }
proxy-status() { launchctl print "gui/$UID/$_proxy" 2>/dev/null | awk '/state =|pid =|last exit code =/'; lsof -nP -iTCP:1081 -sTCP:LISTEN; }

mac-status() {
    echo "== расходится с dotfiles =="; chezmoi status | sed 's/^/  /'
    echo "== службы launchd =="
    launchctl list | awk 'NR>1 && $3 ~ /^com\.iykisoba\./ {printf "  %-40s %s\n", $3, ($1=="-" ? "не запущена, код " $2 : "работает, pid " $1)}' | sort
    echo "== слушают порты =="
    lsof -nP -iTCP -sTCP:LISTEN 2>/dev/null | awk 'NR>1 && $9 ~ /^127\.0\.0\.1|^\*/ {print "  " $1 "\t" $9}' | sort -u
    echo "== tmux =="; tmux ls 2>/dev/null | sed 's/^/  /'
}
