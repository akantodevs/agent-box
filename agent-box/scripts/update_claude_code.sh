#!/bin/sh
# Optional boot-time upgrade of the Claude Code CLI, gated by STARTUP_AUTO_UPDATE.
#
# The image owns the CLI version: the Dockerfile installs an exact version
# resolved at build time, and DISABLE_AUTOUPDATER keeps the in-session updater
# off (the npm global dir is root-owned while sessions run as `claude`, so it
# could not write there anyway). Rebuilding is therefore how the version moves
# — deliberate, reproducible, identical on every box from a given tag.
#
# That leaves one gap: a box nobody rebuilds drifts behind the published CLI.
# This script closes it on request, at the only moment an upgrade is safe.
# Container startup runs as root, before ttyd exists, so no conversation is
# live: all sessions in the box share this single install, and replacing it
# under a running one is exactly what the disabled in-session updater avoids.
#
# STARTUP_AUTO_UPDATE is read the way the git and terraform guards read theirs:
# case-insensitively, with only "Yes" enabling and everything else — including
# unset and unrecognized — leaving the baked version alone.
#
# Never fails the boot. A box that cannot reach the registry is still a working
# box running the version its image shipped, which is a far better outcome than
# a container that refuses to start.

set -e

PACKAGE="@anthropic-ai/claude-code"
TARGET="latest"

# claude --version prints "<version> (Claude Code)"; take the whole line and let
# it be missing without tripping `set -e`.
cli_version() {
    claude --version 2>/dev/null || echo "unknown"
}

enabled="$(printf '%s' "${STARTUP_AUTO_UPDATE:-}" | tr '[:upper:]' '[:lower:]' | tr -d '[:space:]')"

if [ "$enabled" != "yes" ]; then
    echo "[claude-update] STARTUP_AUTO_UPDATE is ${STARTUP_AUTO_UPDATE:-unset} — keeping the image's CLI: $(cli_version)"
    exit 0
fi

before="$(cli_version)"
echo "[claude-update] STARTUP_AUTO_UPDATE=${STARTUP_AUTO_UPDATE} — updating Claude Code from $before to $TARGET..."

# Runs as root, which is what the root-owned npm global prefix needs. The
# package is a small wrapper that fetches a large native binary on install, so
# this adds both time and bandwidth to every container start — the reason the
# flag exists rather than this being the default.
if npm install -g "${PACKAGE}@${TARGET}"; then
    after="$(cli_version)"
    if [ "$after" = "$before" ]; then
        echo "[claude-update] already current: $after"
    else
        echo "[claude-update] updated: $before -> $after"
    fi
else
    echo "WARN: [claude-update] update failed; continuing on the image's CLI: $before" >&2
fi

exit 0
