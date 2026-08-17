#!/bin/sh
set -e

echo "***** Starting agent container *****"

# In *this* script CLAUDE_HOME is the claude user's home directory. Everything
# downstream (launch_session.sh, sessions.py) means the state directory by that
# name, so the two must not be confused — hence CLAUDE_STATE_DIR below, and the
# unset: were CLAUDE_HOME ever set in the container's environment it would still
# carry its export flag through this assignment and reach those children with a
# value one level too high.
unset CLAUDE_HOME
CLAUDE_HOME=/home/claude
CLAUDE_STATE_DIR="$CLAUDE_HOME/.claude"
mkdir -p "$CLAUDE_STATE_DIR"

# Always refresh the content the image owns (operating manual, baked skills) from
# the baked tree so image rebuilds are reflected in the volume without having to
# recreate it. Runs before the chown below, so synced files land owned by claude.
CLAUDE_STATE_DIR="$CLAUDE_STATE_DIR" /opt/agent-box/scripts/sync_claude_home.sh
chown -R claude:claude "$CLAUDE_HOME"

mkdir -p /workspace
chown -R claude:claude /workspace

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
{"hasCompletedOnboarding":true,"projects":{"/workspace":{"hasTrustDialogAccepted":true}}}
CLAUDEJSON
fi

if [ ! -f "$CLAUDE_HOME/.claude/settings.json" ]; then
cat > "$CLAUDE_HOME/.claude/settings.json" <<'SETTINGS'
{"agentPushNotifEnabled":true,"skipDangerousModePermissionPrompt":true}
SETTINGS
fi

# Default status line: point settings.json at the baked-in script, but only if
# no statusLine is configured yet — an existing key means the user customized
# it (or a previous boot already set it), so leave it alone. settings.json
# lives in the claude-data volume, so this also retrofits volumes created
# before the status line existed.
node -e '
const fs = require("fs");
const f = process.argv[1];
const s = JSON.parse(fs.readFileSync(f, "utf8"));
if (!s.statusLine) {
  // refreshInterval keeps the rate-limit reset countdown current while idle;
  // without it the status line only re-renders on session events.
  s.statusLine = { type: "command", command: "node /opt/agent-box/scripts/statusline.js", refreshInterval: 60 };
  fs.writeFileSync(f, JSON.stringify(s, null, 2) + "\n");
}
' "$CLAUDE_HOME/.claude/settings.json" \
    && chown claude:claude "$CLAUDE_HOME/.claude/settings.json" \
    || echo "WARN: failed to configure default status line"

# Terraform safety hook: gate infrastructure- or state-mutating terraform
# commands (apply/destroy/import/state rm|mv/taint/...) per the ALLOW_TERRAFORM_MODIFY
# guardrail in the operating manual, even under --dangerously-skip-permissions.
# The one script is registered for BOTH PreToolUse (decide) and PostToolUse
# (remember an approved directory). Registered idempotently per event — each is
# added only if a terraform-guard entry isn't already there — so existing
# claude-data volumes (which already have the PreToolUse entry) gain PostToolUse
# on the next boot without duplicating either.
node -e '
const fs = require("fs");
const f = process.argv[1];
const s = JSON.parse(fs.readFileSync(f, "utf8"));
const entry = () => ({
  matcher: "Bash",
  hooks: [{ type: "command", command: "node /opt/agent-box/scripts/terraform-guard.js" }],
});
s.hooks = s.hooks || {};
let changed = false;
for (const ev of ["PreToolUse", "PostToolUse"]) {
  s.hooks[ev] = s.hooks[ev] || [];
  if (!JSON.stringify(s.hooks[ev]).includes("terraform-guard")) {
    s.hooks[ev].push(entry());
    changed = true;
  }
}
if (changed) fs.writeFileSync(f, JSON.stringify(s, null, 2) + "\n");
' "$CLAUDE_HOME/.claude/settings.json" \
    && chown claude:claude "$CLAUDE_HOME/.claude/settings.json" \
    || echo "WARN: failed to register terraform safety hook"

# Install Claude Code plugins listed in plugins.txt (idempotent; runs as claude).
# Both values are passed explicitly because `su -` is a login shell and strips
# the inherited environment; they travel as command-prefix assignments carried
# through the login by su's -w whitelist rather than pasted into the -c string,
# so an apostrophe in an operator-set DISABLE_PLAYWRIGHT cannot end the quoting
# and run the rest as root. (Same reasoning as the sessions.py launch below.)
echo "Installing plugins from /opt/agent-box/plugins.txt..."
PLUGINS_FILE=/opt/agent-box/plugins.txt \
DISABLE_PLAYWRIGHT="${DISABLE_PLAYWRIGHT:-}" \
su -w PLUGINS_FILE,DISABLE_PLAYWRIGHT \
    - claude -c 'exec /opt/agent-box/scripts/install_plugins.sh' \
    || echo "WARN: plugin install reported problems (see output above)"

echo "Starting Claude Code via ttyd..."

TTYD_USER="${TTYD_USER:-admin}"
TTYD_PASSWORD="${TTYD_PASSWORD:-admin}"

# Host ports these two servers were published on. The container cannot discover
# them itself: TTYD_PUBLIC_PORT is what the admin page builds its terminal links
# from, and ADMIN_PUBLIC_PORT is only used for the readiness message below.
TTYD_PUBLIC_PORT="${TTYD_PUBLIC_PORT:-8085}"
ADMIN_PUBLIC_PORT="${ADMIN_PUBLIC_PORT:-8086}"

# The values a Claude session needs. Exported, not interpolated:
# launch_session.sh now sits between ttyd and `su` and reads them from its own
# environment, so ttyd's child inherits them and this script assembles no shell
# string at all. That also removes a real flaw in the old ttyd line, which
# pasted these into a single-quoted `su -c` string — one apostrophe in a value
# (a Remote Control name like "Anders' box" is enough) ended the quoting and ran
# the remainder as root, before the su. Nothing interpolated, nothing to escape.
#
# `export` is load-bearing even though Compose exports whatever it sets: when a
# variable is *unset* the default below creates it fresh, and a plain assignment
# is not inherited by ttyd's children.

# Model for Claude Code, overridable via the CLAUDE_MODEL env var; defaults to "opus".
export CLAUDE_MODEL="${CLAUDE_MODEL:-opus}"

# Terraform guard mode (No/Ask/Yes) for the terraform-guard.js hook. Empty/unset
# makes the hook fail closed (block mutating commands).
export ALLOW_TERRAFORM_MODIFY="${ALLOW_TERRAFORM_MODIFY:-}"

# Remote Control base name. When set, start_claude.sh launches Claude Code with
# `--remote-control <name>-<per-session suffix>` so concurrent sessions stay
# distinguishable; empty/unset leaves Remote Control off.
export REMOTE_CONTROL_NAME="${REMOTE_CONTROL_NAME:-}"

# What this box is called, in browser tabs: the session administration page is
# titled "Sessions: <name>" and every session tab ends in it, which is how an
# operator with two agent-boxes open tells them apart.
#
# Resolved once, here, rather than per session or per page load: the lookup
# talks to Docker, and neither the admin page (running as `claude`) nor a
# session should have to repeat it — or cope with it failing. agent_name.sh
# prefers the operator's AGENT_NAME, falls back to the container's name, then
# to the hostname; see that script for why each one is where it is.
# `|| true` because this whole script runs under `set -e`: a name lookup is
# never a reason not to boot a container.
export AGENT_NAME="$(/opt/agent-box/scripts/agent_name.sh || true)"
echo "This box is called '${AGENT_NAME}' (set AGENT_NAME to change it)."

# -a lets the browser pass a session id as ?arg=<uuid>; launch_session.sh
# validates it before anything reaches a shell. The old -m 1 cap is gone: it
# existed to stop two clients sharing one conversation, which is now enforced
# per session in launch_session.sh instead of by allowing only one client total.
# (-m 0 is ttyd's documented "no limit"; note -o is --once and -O is
# --check-origin, so neither is a typo for the other.)
#
# There is deliberately no `-t titleFixed=...` here any more. A fixed title is
# exactly that — every tab in the browser called the same thing, which with one
# session per tab is the same as having no names at all. Without it ttyd's
# client adopts whatever OSC title the terminal is sent, and session_title.py
# (started per session by start_claude.sh) sends "Agent: <session name>".
#
# agent-session is a symlink to launch_session.sh, made in the Dockerfile. ttyd
# hands its client the window title "<command> (<hostname>)" and the client
# appends it to the tab's own name, so the command is user-visible text: the
# short name is there to keep that tail readable.
ttyd -p 8081 -a -m 0 -c "${TTYD_USER}:${TTYD_PASSWORD}" \
    -W -T xterm-256color \
    agent-session &

# Session administration page. Runs as `claude` because it only ever touches
# ~/.claude, where root-owned files would break Claude Code. Supervised in a
# loop so a crash here never costs you the terminal.
#
# `su -` is a login shell and clears the environment, which is why the old code
# pasted values into the command string. It is not done that way here: the
# `-c` string is a fixed literal with nothing interpolated into it (an
# apostrophe in an operator-chosen TTYD_PASSWORD would otherwise break out of
# the quoting and run as root, exactly as described above), and the values
# travel as ordinary command-prefix assignments carried through the login by
# su's -w whitelist. They reach python3 as environment entries that no shell
# ever re-parses. HOME/SHELL/USER/LOGNAME/PATH are always reset by the login
# regardless of -w, which is what we want.
#
# CLAUDE_STATE_DIR, not CLAUDE_HOME: see the note at the top of this script —
# sessions.py means the state directory by that name, this script means the home.
(
    while true; do
        CLAUDE_HOME="$CLAUDE_STATE_DIR" \
        TTYD_USER="$TTYD_USER" \
        TTYD_PASSWORD="$TTYD_PASSWORD" \
        TTYD_PUBLIC_PORT="$TTYD_PUBLIC_PORT" \
        AGENT_NAME="$AGENT_NAME" \
        SESSIONS_PORT=8082 \
        su -w CLAUDE_HOME,TTYD_USER,TTYD_PASSWORD,TTYD_PUBLIC_PORT,AGENT_NAME,SESSIONS_PORT \
            - claude -c 'exec python3 /opt/agent-box/scripts/sessions.py' \
            || echo "WARN: sessions.py exited; restarting in 5s"
        sleep 5
    done
) &

# Truncating `>`, deliberately: this is the first write of the boot and starts
# the file fresh. start_claude.sh appends to it afterwards, once per tab.
echo "Container ready. Sessions: http://localhost:${ADMIN_PUBLIC_PORT}  Terminal: http://localhost:${TTYD_PUBLIC_PORT} (user '${TTYD_USER}')." > /var/log/container.log
chown claude:claude /var/log/container.log

tail -f /var/log/container.log