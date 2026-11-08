# Быстрый вход в проекты: cd в папку и Claude. Файл общий для мака и сервера — пути разные, ветвление по ОС здесь,
# а не шаблоном chezmoi: так файл остаётся ссылкой в репо и правится на месте.
if [[ $OSTYPE == darwin* ]]; then
  # Claude Code с модом чистки промпта (dnk/tools/cc) — без патча, штатный бинарник
  # функция, а не алиас: алиас внутри if не виден функциям того же блока — zsh разбирает блок целиком
  ccm() { CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 command claude --plugin-dir ~/Documents/store/dnk/tools/cc "$@"; }
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
  sufler() { cd ~/sufler && claude "$@"; }
  siphon() { cd ~/tools/siphon && claude "$@"; }
fi
