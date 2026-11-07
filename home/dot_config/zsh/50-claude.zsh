# Claude под другим аккаунтом: токены — в ~/.config/claude-tokens.env (dotfiles, шифрованный), читаются при вызове.
_claude_token() { sed -n "s/^$1=//p" ~/.config/claude-tokens.env; }
claude-tok()  { CLAUDE_CODE_OAUTH_TOKEN="$(_claude_token CLAUDE_TOKEN_PERSONAL)" command claude "$@"; }
claude-team() { CLAUDE_CODE_OAUTH_TOKEN="$(_claude_token CLAUDE_TOKEN_TEAM)" command claude "$@"; }
