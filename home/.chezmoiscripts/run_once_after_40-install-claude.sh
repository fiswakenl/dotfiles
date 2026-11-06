#!/bin/sh
# Claude Code ставится своим установщиком (не brew) в ~/.local/bin; PATH — как в .zshrc, чтобы установщик не ругался
PATH="$HOME/.local/bin:$PATH"
command -v claude >/dev/null 2>&1 || curl -fsSL https://claude.ai/install.sh | bash
