#!/bin/sh
set -e

# ttyd's entry point for every browser tab.
#
# ttyd runs with `-a`, so $1 is whatever the browser put in ?arg= — untrusted
# text from a URL. ttyd execvp()s this script, so the value arrives as one
# literal argv element and nothing is interpreted on the way in; it becomes
# dangerous only further along, which is where the checks below sit:
#
#   * it selects a transcript path;
#   * it is interpolated into the `su -c` command string, which *is* a shell;
#   * and it decides whether a second Claude process opens a transcript that
#     already has one, which corrupts the conversation irrecoverably.
#
# So it is checked against the session-id format, against an existing
# transcript, and against the live-session registry — in that order — before
# any of that happens. Only $1 is ever read; extra ?arg= values are inert.
#
# An empty $1 and no $1 at all both mean "start a new session": ttyd turns a
# bare `?arg=` into one empty argument, and that is a request for a new
# session, not a malformed id.
#
# This script also owns the other end of a session's life: it stays running as
# ttyd's child for as long as the tab is open, and ends the session when the
# tab closes (see hangup). Nothing else in the chain can do it — see that
# function for why the signal ttyd sends reaches nobody on its own.
#
# The env overrides exist so the test suite can drive this script; in the
# container the defaults are always used.

SCRIPTS="${AGENT_BOX_SCRIPTS:-/opt/agent-box/scripts}"
CLAUDE_HOME="${CLAUDE_HOME:-/home/claude/.claude}"
FAIL_DELAY="${AGENT_BOX_FAIL_DELAY:-5}"
# How long a departing session is given to shut down (see hangup), and how long
# an arriving tab waits for one to. A refresh is a close and an open at the same
# moment, so the second is waiting for the first: the wait has to outlast the
# teardown it is waiting for, or a refresh races it and is refused. Derived
# rather than written twice so the relationship survives either being retuned.
HANGUP_GRACE_MS="${AGENT_BOX_HANGUP_GRACE_MS:-3000}"
GRACE_MS="${AGENT_BOX_LIVE_GRACE_MS:-$((HANGUP_GRACE_MS * 2))}"

# One hex digit of a session id, spelled out because the gate below is a `case`
# glob and not a regular expression.
#
# `case` rather than the obvious `echo "$1" | grep -qE '^...$'`, for two
# reasons, both of which let a hostile value through:
#   * grep is line-oriented and -q succeeds if *any* line matches, so
#     "<uuid>\n<anything>" passes a $-anchored pattern — and a newline is a
#     command separator in the `su -c` string this value ends up inside;
#   * dash's echo expands \n, \t and friends, so it can manufacture that
#     second line out of a single-line value.
# `case` matches the whole word, newlines and all, and spawns nothing.
H='[0-9a-f]'
H4="$H$H$H$H"
H8="$H4$H4"
H12="$H8$H4"

# Lowercase only, deliberately, although session_store.is_uuid also accepts
# uppercase. Claude Code writes lowercase ids, and both lookups downstream are
# case-sensitive — `find -name` and the registry's sessionId comparison — so
# accepting a case that cannot occur would only create ways for those two to
# disagree about the same session.

fail() {
    # printf, not echo: the message carries the id, and dash's echo would
    # expand any backslash escape in it.
    printf '%s\n' "$1" >&2
    printf '%s\n' "Open the session admin page to pick a session." >&2
    # ttyd closes the tab as soon as this exits, so hold the message on screen.
    [ "$FAIL_DELAY" = "0" ] || sleep "$FAIL_DELAY"
    exit 1
}

quote() {
    # Single-quote a value for the `su -c` command string: ' becomes '\'' —
    # close the quote, an escaped quote, reopen. Without it a single quote in
    # any of these values ends the quoting and the remainder of the value runs
    # as commands, as root, before the su. They come from docker-compose.yml
    # rather than from a URL, so this is hardening rather than a hole a browser
    # can reach — but this script is the boundary, and the values are foreign
    # text going into a shell.
    printf "'%s'" "$(printf '%s' "$1" | sed "s/'/'\\\\''/g")"
}

child_pids() {
    # The direct children of every pid in $1. pgrep is procps, which the image
    # installs.
    for CHILD_PARENT in $1; do
        pgrep -P "$CHILD_PARENT" 2>/dev/null || true
    done
}

descendants() {
    # Every process below the pids in $1, nearest first.
    #
    # The whole tree is collected before any of it is signalled, and that order
    # matters: the moment a process exits, its children are reparented to init,
    # and nothing is left to say they were ever part of this session.
    DESC_QUEUE="$1"
    DESC_ALL=""
    while :; do
        # Unquoted deliberately: pgrep prints one pid per line, and the split
        # collapses those to words. Quoted, a generation with no children would
        # leave a queue of newlines — which is not empty, so this would spin.
        set -- $(child_pids "$DESC_QUEUE")
        [ "$#" -gt 0 ] || break
        DESC_ALL="$DESC_ALL $*"
        DESC_QUEUE="$*"
    done
    printf '%s' "$DESC_ALL"
}

signal_pids() {
    # Send signal $1 to every pid in $2. A failure is ignored because the only
    # one that can realistically happen — no such process — is the outcome this
    # is trying to reach anyway.
    for SIGNAL_TARGET in $2; do
        kill -"$1" "$SIGNAL_TARGET" 2>/dev/null || true
    done
}

any_alive() {
    for ALIVE_CANDIDATE in $1; do
        # `if`, not `&&`: a plain AND-list whose last command fails would take
        # `set -e` with it.
        if kill -0 "$ALIVE_CANDIDATE" 2>/dev/null; then
            return 0
        fi
    done
    return 1
}

hangup() {
    # The browser tab has gone. ttyd signals the process group of its own child
    # — this script — and on its own that signal reaches nothing that matters:
    #
    #   * `su` blocks it. util-linux su masks nearly every signal while it waits
    #     for its child, so SIGHUP is neither acted on nor forwarded; it simply
    #     sits pending on su until the container stops.
    #   * Claude puts itself in a new session (sid == pid, no controlling
    #     terminal), so it is not in the signalled process group at all.
    #
    # What survives is a Claude process nothing can reach, still holding its
    # <pid>.json registry entry — which is exactly what the liveness gate above
    # reads. The session then reports "already open in another tab" forever and
    # its transcript can never be resumed. So the session is ended here, by pid.
    #
    # SESSION_PID is `su`; the tree below it is the session. su itself is left
    # out on purpose: it blocks the signal anyway, it exits of its own accord
    # once the session below it does, and being this shell's own child it would
    # linger as an unreaped zombie and read as alive for the whole wait below.
    # The fallback covers the microseconds between the fork and the assignment,
    # where all that exists is this shell's children.
    HANGUP_TREE=$(descendants "${SESSION_PID:-$$}")

    # Claude first and its children after, in the order the walk produced:
    # Claude shuts its own MCP servers down when it is asked to go, and asking
    # it first is what lets it. They are signalled too because if Claude goes
    # abruptly they are orphaned instead, holding a browser, a port or a socket
    # for the life of the container.
    signal_pids HUP "$HANGUP_TREE"

    HANGUP_WAITED=0
    while any_alive "$HANGUP_TREE"; do
        if [ "$HANGUP_WAITED" -ge "$HANGUP_GRACE_MS" ]; then
            # SIGHUP asks. Whatever is still here has had its grace period, and
            # leaving it running is the failure this whole function exists to
            # prevent — an unreachable process holding a transcript hostage.
            signal_pids KILL "$HANGUP_TREE"
            break
        fi
        sleep 0.1
        HANGUP_WAITED=$((HANGUP_WAITED + 100))
    done
    exit 0
}

launch() {
    # $1 here is this function's argument — the validated session id, or
    # nothing at all for a new session — not the script's.
    #
    # It is the one thing interpolated unquoted, on purpose: the gate above
    # proves it is 36 characters of [0-9a-f-], and quoting it would turn "no
    # session" into an empty first argument to start_claude.sh, which is a
    # different thing to hand it than no argument.
    #
    # `su -` is a login shell and strips the environment, so anything Claude
    # needs has to travel in this string.
    #
    # Started in the background and waited for, rather than exec'd: an exec'd su
    # leaves nobody in this process to notice the tab closing, and su itself
    # will not notice for us. This shell stays as ttyd's child for exactly as
    # long as the session runs, and hangup() does the rest.
    #
    # fd 3 is the terminal, saved before the fork. A shell with job control off
    # gives an asynchronous command /dev/null for its standard input, and an
    # explicit `<&0` does not undo that — under dash fd 0 has already been
    # replaced by the time redirections are applied. Reading from fd 3 is what
    # leaves a keyboard attached to the session; the child closes it again,
    # having no further use for it.
    exec 3<&0
    # INT is trapped alongside the two shutdown signals, and it ends the session
    # like they do. The terminal can only generate one before Claude puts it in
    # raw mode, i.e. during startup, and ending a session a second after it
    # began — with its transcript intact and resumable — beats leaking one.
    trap hangup HUP TERM INT
    su - claude -c "cd /workspace \
&& CLAUDE_MODEL=$(quote "${CLAUDE_MODEL:-}") \
ALLOW_TERRAFORM_MODIFY=$(quote "${ALLOW_TERRAFORM_MODIFY:-}") \
REMOTE_CONTROL_NAME=$(quote "${REMOTE_CONTROL_NAME:-}") \
AGENT_NAME=$(quote "${AGENT_NAME:-}") \
$SCRIPTS/start_claude.sh $1" <&3 3<&- &
    SESSION_PID=$!

    # Every trap above exits, and a trapped signal interrupts wait, so this
    # returns only when the session itself ends. Its status becomes this
    # script's, which the exec used to give for free: ttyd puts a non-zero exit
    # on the terminal, and that is worth keeping.
    SESSION_STATUS=0
    wait "$SESSION_PID" || SESSION_STATUS=$?
    exit "$SESSION_STATUS"
}

is_live() {
    # session_store.py --is-live answers with its exit status: 0 live, 1 not
    # live, 2 bad usage. Only a clean 1 is allowed to mean "go ahead". Anything
    # else — a missing interpreter, a module that failed to import, a usage the
    # CLI stopped recognising — means the question was not answered, and an
    # unanswered question must not read as "nothing is running": refusing a
    # resume costs a page reload, while a second Claude on an open transcript
    # destroys the conversation. So this fails closed.
    IS_LIVE_STATUS=0
    CLAUDE_HOME="$CLAUDE_HOME" python3 "$SCRIPTS/session_store.py" --is-live "$1" \
        || IS_LIVE_STATUS=$?
    case "$IS_LIVE_STATUS" in
        0) return 0 ;;
        1) return 1 ;;
        *) fail "Refusing to resume: could not tell whether $SAFE_ID is running." ;;
    esac
}

SESSION_ID="$1"
[ -n "$SESSION_ID" ] || launch

# The id is echoed back to a terminal on failure, so it is first reduced to
# characters a session id could contain and bounded in length: an ESC in a URL
# is otherwise a terminal escape sequence printed straight at the operator.
SAFE_ID=$(printf '%s' "$SESSION_ID" | tr -c 'A-Za-z0-9._-' '?' | cut -c1-64)

case "$SESSION_ID" in
    $H8-$H4-$H4-$H4-$H12) ;;
    *) fail "Refusing to resume: '$SAFE_ID' is not a session id." ;;
esac

# Past the gate the id is safe to use as a filename pattern: no glob character
# survives it. `|| true` keeps set -e out of it — find exits non-zero when
# projects/ does not exist yet, which is simply "no transcript".
TRANSCRIPT=$(find "$CLAUDE_HOME/projects" -mindepth 2 -maxdepth 2 \
    -name "$SESSION_ID.jsonl" -print -quit 2>/dev/null || true)
[ -n "$TRANSCRIPT" ] || fail "Refusing to resume: no transcript for $SAFE_ID."

# Two Claude processes on one transcript corrupt it, so a second client is
# refused. A browser refresh lands here too — ttyd SIGHUPs the old child and
# starts the new one immediately, so the old pid is routinely still alive when
# this runs — hence the grace period: poll rather than refuse on the first
# reading, and only give up once the whole window has passed. The first check
# happens before any sleep, so the common case (a dead session) costs nothing.
WAITED=0
while is_live "$SESSION_ID"; do
    [ "$WAITED" -lt "$GRACE_MS" ] \
        || fail "Refusing to resume: session $SAFE_ID is already open in another tab."
    sleep 0.25
    WAITED=$((WAITED + 250))
done

launch "$SESSION_ID"
