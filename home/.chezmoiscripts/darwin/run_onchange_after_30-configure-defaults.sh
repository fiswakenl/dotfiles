#!/bin/bash
# Настройки macOS этого мака. Меняем только то, что отличается, и перезапускаем Dock/Finder, только если что-то поменяли.
restart=""
set_default() {  # домен ключ тип значение
    [ "$(defaults read "$1" "$2" 2>/dev/null)" = "$4" ] && return
    defaults write "$1" "$2" "-$3" "$4"; restart=1
}

# Тёмная тема
[ "$(defaults read -g AppleInterfaceStyle 2>/dev/null)" = Dark ] ||
    osascript -e 'tell application "System Events" to tell appearance preferences to set dark mode to true'

set_default com.apple.dock tilesize int 68                       # Dock: размер иконок
set_default com.apple.finder FXPreferredViewStyle string Nlsv    # Finder: списком
set_default com.apple.AppleMultitouchTrackpad Clicking bool 1    # трекпад: касание = клик
set_default com.apple.driver.AppleBluetoothMultitouch.trackpad Clicking bool 1

# Не спать с закрытой крышкой: Claude Code работает дальше
pmset -g | grep -qE 'SleepDisabled\s+1' || sudo pmset -a disablesleep 1

[ -n "$restart" ] && killall Dock Finder 2>/dev/null || true
