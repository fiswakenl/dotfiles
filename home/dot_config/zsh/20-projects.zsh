# Claude Code с модом чистки промпта (dnk/tools/cc) — без патча, штатный бинарник
alias ccm="CLAUDE_CODE_ENABLE_FUNCTION_HOOKS=1 claude --plugin-dir $HOME/Documents/store/dnk/tools/cc"
# ccm раскрывается в функциях при их определении, поэтому алиас стоит выше
dnk() { cd ~/Documents/store/dnk && ccm "$@"; }
klali() { cd ~/Documents/store/klali && ccm "$@"; }
wde() { cd ~/Documents/store/WDE && ccm "$@"; }
sufler() { cd ~/Documents/store/sufler && ccm "$@"; }
# то же на dev; ccm там алиас, поэтому zsh -i. Аргументы экранируются дважды: для ssh и для zsh -c
sWDE() {
  local cmd="cd wc/urtblw/WDE && ccm ${(j: :)${(q)@}}"
  ssh -t dev "zsh -ic ${(q)cmd}"
}
