#!/bin/sh
#
# Print the name this box is known by, on one line.
#
# It is a display string, not an identifier: it titles the browser tabs of the
# session administration page and of every session terminal, which is how an
# operator with two boxes open tells them apart. ep.sh runs this once at boot
# and exports the answer as AGENT_NAME, so nothing downstream has to resolve it
# again — or has to cope with the lookup failing.
#
# Three sources, in this order, because only the first is a decision:
#
#   1. AGENT_NAME, set on the service in docker-compose.yml. Deliberate, and
#      the only one that can say something a machine could not work out.
#   2. The container's name, asked of Docker over the mounted socket. Free and
#      usually right — "agent-box-dev" is what the operator typed in compose —
#      but it needs a socket, a daemon, and a CLI, any of which a deployment is
#      entitled not to have.
#   3. The hostname. Always answers, and in a default Docker setup answers with
#      the short container id: a poor name, but better than an unnamed tab.
#
# Nothing here fails: an unnamed box is a cosmetic loss, and this runs inside a
# command substitution in the entrypoint, where an error would be a boot problem
# in exchange for a title. Errors are discarded for the same reason — a failed
# docker lookup is an expected outcome, not something to report in the log.
#
# The env override exists so the test suite can drive this script; in the
# container the default is always used.

MOUNTINFO="${AGENT_BOX_MOUNTINFO:-/proc/self/mountinfo}"

blank() {
    # True when $1 holds nothing but whitespace. `AGENT_NAME: "  "` in a compose
    # file is someone leaving it unset, not naming a box after two spaces.
    case "$1" in
        *[![:space:]]*) return 1 ;;
        *) return 0 ;;
    esac
}

container_id() {
    # Docker leaves the full container id in the mount source of the files it
    # bind-mounts into every container (/etc/resolv.conf and friends), which is
    # the one place it survives a `hostname:` set in compose. The hostname is
    # the fallback because it *is* the short id unless someone changed it.
    ID=$(sed -n 's|.*/containers/\([0-9a-f]\{64\}\)/.*|\1|p' "$MOUNTINFO" 2>/dev/null \
        | head -n 1)
    [ -n "$ID" ] || ID=$(hostname 2>/dev/null)
    printf '%s' "$ID"
}

container_name() {
    # `timeout` because this runs during boot: the socket is usually there and
    # answers at once, but a wedged daemon must not hold the container's
    # terminal hostage for a display string. Docker reports names with a
    # leading slash, which no title bar wants.
    ID=$(container_id)
    [ -n "$ID" ] || return 0
    NAME=$(timeout 5 docker inspect --format '{{.Name}}' "$ID" 2>/dev/null) || NAME=""
    printf '%s' "${NAME#/}"
}

NAME="${AGENT_NAME:-}"
if blank "$NAME"; then
    NAME=$(container_name)
fi
if blank "$NAME"; then
    NAME=$(hostname 2>/dev/null) || NAME=""
fi

printf '%s\n' "$NAME"
