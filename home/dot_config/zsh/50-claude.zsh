# Claude под другим аккаунтом: токены — в ~/.config/claude-tokens.env (dotfiles, шифрованный), читаются при вызове.
_claude_token() { sed -n "s/^$1=//p" ~/.config/claude-tokens.env; }
# На маке выключено: здесь claude ходит по подписке, а запуск с токеном менял аккаунт посреди сессии. Включить — убрать условие.
if [[ $OSTYPE != darwin* ]]; then
  claude-tok()  { CLAUDE_CODE_OAUTH_TOKEN="$(_claude_token CLAUDE_TOKEN_PERSONAL)" command claude "$@"; }
  claude-team() { CLAUDE_CODE_OAUTH_TOKEN="$(_claude_token CLAUDE_TOKEN_TEAM)" command claude "$@"; }
fi
