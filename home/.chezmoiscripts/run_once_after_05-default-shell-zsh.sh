#!/bin/sh
# Холодный старт: zsh — оболочка входа (как в установщике Oh My Zsh), иначе новый терминал откроется в bash
zsh="$(command -v zsh)" || { echo "  zsh не установлен — оболочку не меняю"; exit 0; }
[ "$(basename "${SHELL:-}")" = zsh ] && exit 0
grep -qx "$zsh" /etc/shells 2>/dev/null || echo "$zsh" | sudo tee -a /etc/shells >/dev/null
sudo -n chsh -s "$zsh" "$(id -un)" 2>/dev/null || chsh -s "$zsh" || echo "  не вышло сменить оболочку: chsh -s $zsh"
