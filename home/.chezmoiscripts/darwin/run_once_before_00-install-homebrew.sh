#!/bin/bash
# Чистый мак: Homebrew (заодно ставит git и инструменты разработчика)
[ -x /opt/homebrew/bin/brew ] || /bin/bash -c "$(curl -fsSL https://raw.githubusercontent.com/Homebrew/install/HEAD/install.sh)"
