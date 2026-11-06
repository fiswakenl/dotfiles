# ---- цвет окна по папке (iTerm) -------------------------------------------
# Схемы и карта папок — в ~/.config/iterm-themes. Меняется правкой map.conf,
# перезапуск не нужен: покраска идёт escape-кодом на живой сессии.
# Руками: theme "Rose Pine", список схем — theme без аргумента.
ITERM_THEMES=~/.config/iterm-themes

theme() { python3 "$ITERM_THEMES/apply.py" "$@" }

# Пути разворачиваем через :A — половина проектов лежит симлинками на iCloud,
# и без этого PWD никогда не совпадёт с тем, что написано в карте.
_theme_for_pwd() {
  [[ "$TERM_PROGRAM" == "iTerm.app" ]] || return
  [[ -f "$ITERM_THEMES/map.conf" ]] || return
  local line dir scheme here="${PWD:A}"
  while IFS= read -r line; do
    [[ "$line" == \#* || -z "${line//[[:space:]]/}" ]] && continue
    [[ "$line" != *=* ]] && continue
    dir="${${line%%=*}//[[:space:]]/}"
    scheme="${line#*=}"
    scheme="${scheme#"${scheme%%[![:space:]]*}"}"
    scheme="${scheme%"${scheme##*[![:space:]]}"}"
    dir="${dir/#\~/$HOME}"
    if [[ "$dir" == default ]]; then
      python3 "$ITERM_THEMES/apply.py" "$scheme" 2>/dev/null
      return
    fi
    dir="${dir:A}"
    if [[ "$here" == "$dir" || "$here" == "$dir"/* ]]; then
      python3 "$ITERM_THEMES/apply.py" "$scheme" 2>/dev/null
      return
    fi
  done < "$ITERM_THEMES/map.conf"
}

autoload -Uz add-zsh-hook
add-zsh-hook chpwd _theme_for_pwd
_theme_for_pwd
