---
description: Вытащить транскрипт видео в контекст; длинное режется, хвост дочитывается с диска
argument-hint: <url>
disable-model-invocation: true
allowed-tools: Bash(${CLAUDE_SKILL_DIR}/scripts/transcript.sh *), Read
---

!`${CLAUDE_SKILL_DIR}/scripts/transcript.sh "$ARGUMENTS"`
