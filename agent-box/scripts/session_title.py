#!/usr/bin/env python3
"""Keep the browser tab named after the session it is showing.

One agent-box runs one Claude Code session per tab, and every tab is the same
ttyd page — so without this they are all called the same thing and the only way
to find a session is to open its tab and look. ttyd is therefore started
*without* `titleFixed`, which makes its client adopt any OSC title written to
the terminal; this watcher writes one, and rewrites it whenever the session's
name changes.

start_claude.sh backgrounds this immediately before `exec claude`, so:

  * it inherits the terminal as its stdout — the escape sequence has to reach
    the same pty Claude Code is drawing on, and nothing else may ever be
    written there (see write_title);
  * the exec keeps the pid, so this process's parent *is* the Claude process,
    which is what makes ending with the session possible at all (see
    _follow_parent). An orphan here would hold the pty for the life of the
    container.

The name itself comes from session_store, i.e. from the same parse the session
administration page lists — a tab and its row have to be recognisably the same
session, and two ideas of what a session is called would guarantee they are not.
"""

import os
import re
import sys
import time

import session_store

# What a tab is called. The prefix is a constant rather than an argument: it is
# the thing that makes a Claude tab identifiable among a browser window full of
# other tabs, so it is not a per-deployment decision.
PREFIX = "Agent: "

# A session with no transcript yet — a tab opened with "+ New session". Its id
# exists (start_claude.sh generates it up front) but Claude Code has not written
# a line under it, so there is nothing to name it by until the conversation
# starts. The watcher stays for that: the real name replaces this one in place.
NEW_SESSION = "new session"

# What the admin page calls a session it cannot name. Repeated rather than
# imported because it is a UI string in that module, not a contract.
UNTITLED = "(untitled)"

# What separates the session's name from the box's. A dot rather than a dash or
# a pipe: session names contain both, and neither would read as a separator.
SEPARATOR = " · "

# The box name's share of the title. It is a suffix, so it is bounded well below
# the session name's own limit — a box called something enormous must not push
# the session name out of the part of the tab you can actually see.
_BOX_LIMIT = 40

# Anything that would stop being *text* on the way to a terminal. A title is
# whatever the model wrote, and an OSC sequence ends at the first BEL or ESC —
# so a title containing either would close the sequence early and leave its tail
# to be interpreted by the terminal rather than shown in the tab. The bidi
# controls are dropped for the reason the admin page drops them: U+202E reverses
# everything after it, and a title cannot be allowed to reverse the tab it lands
# in. Stripped, not escaped: there is no escaping inside an OSC string.
_UNPRINTABLE = re.compile(
    "[\\x00-\\x1f\\x7f-\\x9f\\u061c\\u200e\\u200f\\u202a-\\u202e\\u2066-\\u2069]"
)

# How often the transcript is looked at. Nothing is re-read unless its (mtime,
# size) changed, so an idle session costs one stat per tick; a rename shows up
# within one of these. Overridable only so the tests need not wait for it.
DEFAULT_INTERVAL = 5.0


def _interval():
    """The poll interval from the environment, or the default. Never raises."""
    try:
        value = float(os.environ.get("AGENT_BOX_TITLE_INTERVAL", ""))
    except ValueError:
        return DEFAULT_INTERVAL
    # A negative or absurd value is a typo, not an instruction: a zero interval
    # would spin this process on the CPU for the life of the session.
    return value if 0 < value <= 3600 else DEFAULT_INTERVAL


def clean(text):
    """One safe, single-line title, or "". Never raises.

    Total like session_store's own derivation helpers: the caller is reading
    another program's format and should not have to pre-guard the type.
    """
    if not isinstance(text, str):
        return ""
    return " ".join(_UNPRINTABLE.sub("", text).split())


def _session_name(claude_home, session_id):
    """The session's own name, as the administration page would list it.

    An id with no transcript behind it — a brand-new session, or an id this
    box has never heard of — is "new session" rather than an error: the tab is
    already open and showing a Claude session, whatever the lookup says.
    """
    if not session_store.is_uuid(session_id):
        return NEW_SESSION
    path = session_store.transcript_for(claude_home, session_id)
    if path is None:
        return NEW_SESSION
    # A name that was nothing but control characters cleans down to "", which
    # is no more of a name than an absent one.
    return clean(session_store.parse_transcript(path)["name"]) or UNTITLED


def title_for(claude_home, session_id, agent_name=None):
    """What the tab showing this session should be called.

    "Agent: <session> · <box>". The session comes first because it is the half
    a narrow tab actually shows; the box name is there for the operator with
    two agent-boxes open, whose session tabs are otherwise identical.

    agent_name defaults to the environment, which is how the container supplies
    it: ep.sh resolves it once at boot (see agent_name.sh) and it travels down
    through the su chain. An unnamed box simply adds nothing.
    """
    if agent_name is None:
        agent_name = os.environ.get("AGENT_NAME", "")
    title = PREFIX + _session_name(claude_home, session_id)
    box = clean(agent_name)[:_BOX_LIMIT]
    return "%s%s%s" % (title, SEPARATOR, box) if box else title


def osc(title):
    """The escape sequence that renames a terminal (and so, a browser tab)."""
    return "\033]0;%s\007" % title


def write_title(stream, title):
    """Put the title on the terminal. False when the terminal has gone.

    One write and one flush, deliberately: Claude Code is drawing on this same
    pty, and a sequence split across several writes is a sequence something else
    can be interleaved into. A failure is never raised onward — the tab closing
    while this is mid-write is the normal end of a session, not an error.
    """
    try:
        stream.write(osc(title))
        stream.flush()
    except (OSError, ValueError):
        # ValueError is what a closed stream raises, OSError what a closed pty
        # does. Both mean the same thing here: there is no tab to title.
        return False
    return True


def _transcript_key(claude_home, session_id):
    """A value that changes whenever this session's transcript does.

    (mtime, size) for the same reason SessionStore caches on it: neither alone
    is enough — a rewrite of equal length changes only the timestamp, and two
    writes inside one coarse filesystem tick change only the size. None means
    "no transcript", which is itself a state worth noticing the end of.
    """
    if not session_store.is_uuid(session_id):
        return None
    path = session_store.transcript_for(claude_home, session_id)
    if path is None:
        return None
    try:
        stat = os.stat(path)
    except OSError:
        return None
    return (path, stat.st_mtime_ns, stat.st_size)


def _follow_parent():
    """Ask the kernel to SIGHUP this process when its parent dies.

    The parent is the Claude process (start_claude.sh execs it, keeping the
    pid), and when a session simply ends nothing else signals this watcher:
    ttyd's SIGHUP goes to the launcher's process group, and launch_session.sh
    only reaps the tree when a *tab* closes. Best effort — the ppid check in
    watch() covers everything this cannot, one interval later.
    """
    try:
        import ctypes

        PR_SET_PDEATHSIG, SIGHUP = 1, 1
        ctypes.CDLL("libc.so.6", use_errno=True).prctl(PR_SET_PDEATHSIG, SIGHUP)
    except Exception:
        pass  # a kernel or libc without it costs a late exit, nothing more


def _parent_watch():
    """A predicate that answers whether the starting parent is still there.

    Reparenting is the signal: when the Claude process goes, this process is
    handed to init and getppid() changes. Comparing against the pid recorded
    now, rather than against 1, keeps this true under a subreaper too.

    Being init's child *already* is the same fact observed too late — the parent
    died in the moment between the fork and this call, which is also the window
    where PR_SET_PDEATHSIG silently does nothing (the death it would have
    signalled has already happened). There is no parent left to follow, so the
    watcher titles the terminal once and goes rather than watching forever.
    """
    original = os.getppid()
    if original <= 1:
        return lambda: False
    return lambda: os.getppid() == original


def watch(stream, claude_home, session_id, interval=DEFAULT_INTERVAL,
          alive=None, sleep=time.sleep):
    """Title the terminal, then keep it current until the session ends.

    Returns when the parent process is gone or the terminal stops accepting
    writes. Only a *changed* name is ever written: the transcript is appended to
    constantly, the name behind it changes a handful of times at most, and every
    write here lands on a terminal someone is reading.
    """
    if alive is None:
        alive = _parent_watch()
    written = None
    key = None
    title = None
    while True:
        current = _transcript_key(claude_home, session_id)
        if title is None or current != key:
            key = current
            title = title_for(claude_home, session_id)
        if title != written:
            if not write_title(stream, title):
                return
            written = title
        if not alive():
            return
        sleep(interval)


def main(argv):
    session_id = argv[1] if len(argv) > 1 else ""
    claude_home = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
    _follow_parent()
    if not session_store.is_uuid(session_id):
        # No session to follow: start_claude.sh reaches this only when it could
        # not generate an id. There is still a tab, and a generic name for it
        # beats whatever ttyd would otherwise leave in the title bar.
        write_title(sys.stdout, title_for(claude_home, session_id))
        return 0
    watch(sys.stdout, claude_home, session_id, interval=_interval())
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
