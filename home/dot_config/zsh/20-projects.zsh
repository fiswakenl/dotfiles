# Быстрый вход в проекты: cd в папку и Claude. Файл общий для мака и сервера — пути разные, ветвление по ОС здесь,
# а не шаблоном chezmoi: так файл остаётся ссылкой в репо и правится на месте.
# Claude Code с модом чистки промпта: ~/.claude/cc — ссылка chezmoi на dotfiles/claude/cc, на маке и на сервере.
# Функция, а не алиас: алиас не виден функциям ниже — zsh разбирает блок if целиком.
ccm() { CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 command claude --plugin-dir ~/.claude/cc "$@"; }
# То же + голос через Gemini Live (плагин gv: ~/.claude/gv — ссылка chezmoi на dotfiles/claude/gv).
ccg() { ccm --plugin-dir ~/.claude/gv "$@"; }

if [[ $OSTYPE == darwin* ]]; then
  dnk() { cd ~/Documents/store/dnk && ccm "$@"; }
  klali() { cd ~/Documents/store/klali && ccm "$@"; }
  wde() { cd ~/Documents/store/WDE && ccm "$@"; }
  sufler() { cd ~/Documents/store/sufler && ccm "$@"; }
  # то же на dev; ccm там алиас, поэтому zsh -i. Аргументы экранируются дважды: для ssh и для zsh -c
  sWDE() {
    local cmd="cd wc/urtblw/WDE && ccm ${(j: :)${(q)@}}"
    ssh -t dev "zsh -ic ${(q)cmd}"
  }
else
  # сервер: проекты — клоны в домашней папке
  sufler() { cd ~/sufler && ccm "$@"; }
  siphon() { cd ~/tools/siphon && ccm "$@"; }
fi
