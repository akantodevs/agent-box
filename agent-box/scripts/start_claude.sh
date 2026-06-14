#!/bin/bash

cd /workspace || exit 1

echo "== Connection established. Starting Claude Code ==" > /var/log/container.log

# This script runs under `su - claude`, a clean login shell that strips the
# image's ENV vars — so anything Claude (or its MCP children) needs must be
# exported here, not just set in the Dockerfile.
export DISABLE_AUTOUPDATER=1                  # image owns the CLI version
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright # where the baked Chromium lives

# Terraform guard mode (No/Ask/Yes), consumed by the terraform-guard.js hook.
# Exported here so the hook subprocess Claude spawns inherits it; ep.sh passes
# it through the `su - claude` login. Defaults to empty -> the hook fails closed.
export ALLOW_TERRAFORM_MODIFY="${ALLOW_TERRAFORM_MODIFY:-}"

# Model is configurable via the CLAUDE_MODEL env var (set in docker-compose.yml,
# passed through the `su - claude` login by ep.sh). Defaults to "opus".
MODEL="${CLAUDE_MODEL:-opus}"

# Single resumable session: continue the existing conversation if one exists,
# otherwise start fresh. ttyd re-launches this script on every (re)connect, so
# this is what makes the session survive both container restarts and browser
# disconnects. The find check avoids hardcoding Claude's project-dir name
# encoding; this container only ever has the /workspace project, so any transcript
# means "resume".
if find "$HOME/.claude/projects" -name '*.jsonl' -print -quit 2>/dev/null | grep -q .; then
    exec claude --model "$MODEL" --continue --dangerously-skip-permissions
else
    exec claude --model "$MODEL" --dangerously-skip-permissions
fi
