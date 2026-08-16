#!/bin/bash
#
# Launches the Claude Code process for one browser tab.
#
# The call chain is: ttyd -a -> launch_session.sh [id] -> su - claude ->
# this script [id] -> exec claude. Arrays below are why the shebang is bash
# and not sh: under dash `ARGS=(...)` is a syntax error, not an empty array.

cd /workspace || exit 1

# Overridable only so the test suite can drive this script; in the container the
# defaults are always used.
SCRIPTS="${AGENT_BOX_SCRIPTS:-/opt/agent-box/scripts}"
CONTAINER_LOG="${AGENT_BOX_LOG:-/var/log/container.log}"

# This script runs under `su - claude`, a clean login shell that strips the
# image's ENV vars — so anything Claude (or its MCP children) needs must be
# exported here, not just set in the Dockerfile.
export DISABLE_AUTOUPDATER=1                  # image owns the CLI version
export PLAYWRIGHT_BROWSERS_PATH=/ms-playwright # where the baked Chromium lives

# Terraform guard mode (No/Ask/Yes), consumed by the terraform-guard.js hook.
# Exported here so the hook subprocess Claude spawns inherits it; launch_session.sh
# passes it through the `su - claude` login. Defaults to empty -> the hook fails
# closed. `:-` and not `-`: the launcher always sets the variable, empty when the
# deployment leaves it unset, so "set but empty" is the live case for all three
# of these.
export ALLOW_TERRAFORM_MODIFY="${ALLOW_TERRAFORM_MODIFY:-}"

# Model is configurable via the CLAUDE_MODEL env var (set in docker-compose.yml,
# passed through the `su - claude` login by launch_session.sh). Defaults to "opus".
MODEL="${CLAUDE_MODEL:-opus}"

# The session to run is chosen by the admin page and validated by
# launch_session.sh before it gets here: $1 is either empty or an id that has a
# transcript and no live process. No argument means a fresh session; its id is
# generated up front so the session is identifiable — in the admin page and in
# the Remote Control name — from the moment it starts, rather than only once
# Claude has written its first transcript line.
#
# An empty $1 must take the new-session branch and not become `--resume ''`:
# that is claude's interactive session picker, which is not something a tab can
# answer.
SESSION_ID="${1:-}"
if [ -n "$SESSION_ID" ]; then
    SESSION_ARGS=(--resume "$SESSION_ID")
elif SESSION_ID=$(python3 -c 'import uuid; print(uuid.uuid4())' 2>/dev/null) \
    && [ -n "$SESSION_ID" ]; then
    SESSION_ARGS=(--session-id "$SESSION_ID")
else
    # Nothing about a missing id is worth failing a launch over: claude picks
    # its own, and the only loss is that the session appears in the admin page
    # once it writes its first transcript line instead of immediately.
    SESSION_ID=""
    SESSION_ARGS=()
fi

# Appended, never truncated: ep.sh tails this one file and every browser tab runs
# this script, so a truncating write would wipe the readiness banner and whatever
# the other sessions logged. The id is what tells the lines apart once several
# tabs are open. Errors are discarded — the log is a convenience, the terminal is
# the product — and `2>/dev/null` comes *first* because redirections are applied
# left to right: after the append it would arrive too late to swallow the shell's
# own "No such file or directory" for the append itself, which would then be the
# first thing on the operator's fresh terminal.
printf '== Connection established. Starting Claude Code (session %s) ==\n' \
    "${SESSION_ID:-unknown}" 2>/dev/null >> "$CONTAINER_LOG"

# Remote Control: when REMOTE_CONTROL_NAME is set (in docker-compose.yml, passed
# through the `su - claude` login by launch_session.sh), launch with
# `--remote-control <name>` so the session is remote-controllable and shows up
# under that name. Empty/unset leaves Remote Control off (the default).
#
# Names must be unique across concurrent sessions, so the configured name carries
# a suffix: the session's slugified ai-title when it has one (what you would
# recognise it by in the Remote Control list), otherwise the head of its id. The
# name is fixed at launch and does not follow later retitles.
#
# The lookup can only fail to answer — a new session has no transcript yet, and
# an untitled one has no ai-title — so both an empty stdout and a non-zero exit
# mean the same thing here, and the fallback covers both. The slug is
# [a-z0-9-] by construction, so it cannot smuggle anything into the name.
REMOTE_CONTROL_ARGS=()
if [ -n "${REMOTE_CONTROL_NAME:-}" ]; then
    RC_NAME="$REMOTE_CONTROL_NAME"
    if [ -n "$SESSION_ID" ]; then
        SUFFIX=$(python3 "$SCRIPTS/session_store.py" --slug "$SESSION_ID" 2>/dev/null) \
            || SUFFIX=""
        # Bash's own substring rather than `echo … | cut`: two fewer processes,
        # and no echo to expand a backslash on the way through.
        [ -n "$SUFFIX" ] || SUFFIX="${SESSION_ID:0:8}"
        RC_NAME="$REMOTE_CONTROL_NAME-$SUFFIX"
    fi
    REMOTE_CONTROL_ARGS=(--remote-control "$RC_NAME")
fi

exec claude --model "$MODEL" "${SESSION_ARGS[@]}" \
    --dangerously-skip-permissions "${REMOTE_CONTROL_ARGS[@]}"
