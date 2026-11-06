#!/bin/sh
# Claude Code ставится своим установщиком (не brew) в ~/.local/bin
command -v claude >/dev/null 2>&1 || [ -x "$HOME/.local/bin/claude" ] || curl -fsSL https://claude.ai/install.sh | bash
