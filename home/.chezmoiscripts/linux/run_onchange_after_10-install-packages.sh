#!/bin/sh
# Сервер: то, что на маке ставит Homebrew, — в ~/.local/bin без sudo. Docker и системное — не здесь (ставит root).
# run_onchange: перезапускается при правке этого файла; ставит только недостающее.
set -e
BIN="$HOME/.local/bin"; mkdir -p "$BIN"; PATH="$BIN:$PATH"

# uv — Python-проекты и uv run
command -v uv >/dev/null 2>&1 || curl -LsSf https://astral.sh/uv/install.sh | env UV_NO_MODIFY_PATH=1 sh

# yazi — файловый менеджер, конфиг общий с маком (~/.config/yazi)
if ! command -v yazi >/dev/null 2>&1; then
  tmp=$(mktemp -d)
  curl -fsSL -o "$tmp/yazi.zip" https://github.com/sxyazi/yazi/releases/latest/download/yazi-x86_64-unknown-linux-gnu.zip
  python3 -c 'import sys, zipfile; zipfile.ZipFile(sys.argv[1]).extractall(sys.argv[2])' "$tmp/yazi.zip" "$tmp"
  install -m 755 "$tmp"/yazi-x86_64-unknown-linux-gnu/yazi "$tmp"/yazi-x86_64-unknown-linux-gnu/ya "$BIN"/
  rm -rf "$tmp"
fi
