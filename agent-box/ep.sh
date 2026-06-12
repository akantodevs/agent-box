#!/bin/sh
set -e

echo "***** Starting agent container *****"

CLAUDE_HOME=/home/claude
mkdir -p "$CLAUDE_HOME/.claude"

# Always refresh the operating manual from the baked-in copy so image rebuilds
# are reflected in the volume without having to recreate it.
cp /opt/agent-box/CLAUDE.md "$CLAUDE_HOME/.claude/CLAUDE.md"

chown -R claude:claude "$CLAUDE_HOME"

mkdir -p /repo
chown -R claude:claude /repo

# Grant the claude user access to the mounted Docker socket.
#
# The host socket is bind-mounted in as root:<gid> with group-rw (mode 660), so the
# unprivileged `claude` user can't reach it by default. We must NOT chmod/chown the
# socket itself: it shares the host's inode, so changing its perms would alter Docker
# access on the host. Instead, while we're still root here in the entrypoint, we add
# `claude` to a group that already owns the socket. This only touches the container's
# /etc/group, and the membership is picked up by the later `su - claude` logins.
DOCKER_SOCK=/var/run/docker.sock
if [ -S "$DOCKER_SOCK" ]; then
    SOCK_GID=$(stat -c '%g' "$DOCKER_SOCK")
    if [ "$SOCK_GID" = "0" ]; then
        # Socket is group-owned by root (common on Docker Desktop / WSL2). Add claude
        # to the root group so the socket's group-rw bit applies. This is no broader
        # than the docker access itself, which is already root-equivalent on the host.
        usermod -aG root claude \
            && echo "Granted claude access to Docker socket via root group (gid 0)." \
            || echo "WARN: failed to add claude to root group for Docker socket access"
    else
        # Reuse an existing group with the socket's GID, or create a 'docker' group
        # with it, then add claude to that group.
        SOCK_GROUP=$(getent group "$SOCK_GID" | cut -d: -f1)
        if [ -z "$SOCK_GROUP" ]; then
            groupadd -g "$SOCK_GID" docker && SOCK_GROUP=docker
        fi
        usermod -aG "$SOCK_GROUP" claude \
            && echo "Granted claude access to Docker socket via group '$SOCK_GROUP' (gid $SOCK_GID)." \
            || echo "WARN: failed to add claude to group '$SOCK_GROUP' for Docker socket access"
    fi
else
    echo "NOTE: $DOCKER_SOCK not present (or not a socket); skipping Docker access setup."
fi

# Skip onboarding. These two files have different lifetimes, so seed them
# independently:
#   - .claude.json lives on the container's ephemeral filesystem (only ~/.claude
#     is volume-backed), so it must be reseeded on every container recreate.
#   - settings.json lives in the claude-data volume and accumulates runtime
#     state (e.g. enabledPlugins, written by `claude plugin enable`). Seeding it
#     whenever .claude.json was missing clobbered that state on every container
#     recreate, leaving plugins installed but disabled. Only seed it when it
#     genuinely doesn't exist yet.
if [ ! -f "$CLAUDE_HOME/.claude.json" ]; then
cat > "$CLAUDE_HOME/.claude.json" <<'CLAUDEJSON'
{"hasCompletedOnboarding":true,"projects":{"/repo":{"hasTrustDialogAccepted":true}}}
CLAUDEJSON
fi

if [ ! -f "$CLAUDE_HOME/.claude/settings.json" ]; then
cat > "$CLAUDE_HOME/.claude/settings.json" <<'SETTINGS'
{"agentPushNotifEnabled":true,"skipDangerousModePermissionPrompt":true}
SETTINGS
fi

# Install Claude Code plugins listed in plugins.txt (idempotent; runs as claude)
echo "Installing plugins from /opt/agent-box/plugins.txt..."
su - claude -c "PLUGINS_FILE=/opt/agent-box/plugins.txt /opt/agent-box/scripts/install_plugins.sh" \
    || echo "WARN: plugin install reported problems (see output above)"

echo "Starting Claude Code via ttyd..."

TTYD_USER="${TTYD_USER:-admin}"
TTYD_PASSWORD="${TTYD_PASSWORD:-admin}"
TTYD_TITLE="${TTYD_TITLE:-Agent Box}"

# Model for Claude Code, overridable via the CLAUDE_MODEL env var; defaults to "opus".
# `su - claude` starts a login shell that strips inherited env vars, so we pass it
# explicitly into the command string below rather than relying on inheritance.
CLAUDE_MODEL="${CLAUDE_MODEL:-opus}"

# -m 1 limits ttyd to a single concurrent client so only one `claude --continue`
# ever runs against the persisted conversation (two would corrupt the transcript).
ttyd -p 7681 -m 1 -c "${TTYD_USER}:${TTYD_PASSWORD}" -t "titleFixed=Agent console" -W -T xterm-256color su - claude -c "cd /repo && CLAUDE_MODEL='${CLAUDE_MODEL}' /opt/agent-box/scripts/start_claude.sh" &

echo "Container ready. Access Claude Code at http://localhost:7681 with username '${TTYD_USER}' and password '${TTYD_PASSWORD}'." > /var/log/container.log
chown claude:claude /var/log/container.log

tail -f /var/log/container.log