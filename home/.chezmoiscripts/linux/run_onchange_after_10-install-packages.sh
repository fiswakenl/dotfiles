#!/bin/sh
# Сервер: то, что на маке ставит Homebrew, — в ~/.local/bin без sudo. Docker и системное — не здесь (ставит root).
# run_onchange: перезапускается при правке этого файла; ставит только недостающее.
set -e
BIN="$HOME/.local/bin"; mkdir -p "$BIN"; PATH="$BIN:$PATH"

# uv — Python-проекты и uv run
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh

# gh — вход git в GitHub (токен в ~/.config/gh/hosts.yml из dotfiles); на dnk уже стоит из apt
if ! command -v gh >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  v=$(curl -fsSL https://api.github.com/repos/cli/cli/releases/latest | python3 -c 'import json,sys; print(json.load(sys.stdin)["tag_name"].lstrip("v"))')
  curl -fsSL "https://github.com/cli/cli/releases/download/v$v/gh_${v}_linux_amd64.tar.gz" | tar -xz -C "$tmp"
  install -m 755 "$tmp/gh_${v}_linux_amd64/bin/gh" "$BIN/"
  rm -rf "$tmp"
fi

# yazi — файловый менеджер, конфиг общий с маком (~/.config/yazi)
if ! command -v yazi >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/yazi.zip" https://github.com/sxyazi/yazi/releases/latest/download/yazi-x86_64-unknown-linux-gnu.zip
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/yazi.zip" "$tmp"
  install -m 755 "$tmp"/yazi-x86_64-unknown-linux-gnu/yazi "$tmp"/yazi-x86_64-unknown-linux-gnu/ya "$BIN"/
  rm -rf "$tmp"
fi

# zoxide — z ПАПКА, прыжок в частую папку (плагин в ~/.zshrc); ставится в ~/.local/bin
command -v zoxide >/dev/null 2>&1 || curl -sSfL https://raw.githubusercontent.com/ajeetdsouza/zoxide/main/install.sh | sh
