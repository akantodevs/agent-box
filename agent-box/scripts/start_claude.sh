#!/bin/bash

cd /repo || exit 1

echo "== Connection established. Starting Claude Code ==" > /var/log/container.log

# Single resumable session: continue the existing conversation if one exists,
# otherwise start fresh. ttyd re-launches this script on every (re)connect, so
# this is what makes the session survive both container restarts and browser
# disconnects. The find check avoids hardcoding Claude's project-dir name
# encoding; this container only ever has the /repo project, so any transcript
# means "resume".
if find "$HOME/.claude/projects" -name '*.jsonl' -print -quit 2>/dev/null | grep -q .; then
    exec claude --continue --dangerously-skip-permissions
else
    exec claude --dangerously-skip-permissions
fi
