"""Read-only discovery of Claude Code sessions from a Claude home directory.

Everything here is derived from files Claude Code already writes:

    <home>/projects/<encoded-cwd>/<uuid>.jsonl   transcripts
    <home>/sessions/<pid>.json                   live-process registry

The only mutating function is delete_session().
"""

import json
import math
import os
import re
import sys

# \Z, not $: in Python `$` also matches immediately before a trailing newline,
# so "<uuid>\n" would pass this gate. is_uuid() is the first check in
# delete_session(), i.e. a security gate, so it has to match exactly.
_UUID_RE = re.compile(
    r"^[0-9a-fA-F]{8}-[0-9a-fA-F]{4}-[0-9a-fA-F]{4}"
    r"-[0-9a-fA-F]{4}-[0-9a-fA-F]{12}\Z"
)

# A name has to fit one line of the UI; a last prompt is a preview and can
# afford more. The two limits are independent — do not merge them.
_NAME_LIMIT = 80
_PROMPT_LIMIT = 160
# Width of the tool detail in an activity line ("Bash: <detail>"). Independent
# of the name width despite sharing a value today: they sit in different columns
# of the UI and are free to drift apart.
_DETAIL_LIMIT = 80

# Transcripts are written as compact JSON, so these substrings identify candidate
# lines without parsing. The filter must only ever over-select: every candidate is
# json.loads()-ed and its real "type" checked. A line that merely quotes
# '"type":"assistant"' inside a string is harmless; a missed line would not be.
_CANDIDATES = (
    '"type":"ai-title"',
    '"type":"last-prompt"',
    '"type":"assistant"',
    '"type":"user"',
)

# Claude Code stores its own bookkeeping as `user` entries: slash-command
# invocations, local command output, and pasted caveats. None of them is
# something you would recognise a session by.
_BOOKKEEPING = (
    "<command-name>",
    "<command-message>",
    "<local-command-",
    "<bash-input>",
    "<bash-stdout>",
)


def is_uuid(value):
    """Whether this is exactly a session id. Never raises.

    Total, like _truncate and slugify: `value or ""` absorbed None and "", but
    a truthy non-string reached .match() and raised TypeError. This is the
    first line of delete_session(), i.e. the security gate, and an id taken
    from a JSON request body is whatever type the body says — the caller
    catches DeleteError and nothing else, so a raise there is an unhandled
    traceback where a 400 was designed.
    """
    return isinstance(value, str) and bool(_UUID_RE.match(value))


def _iter_transcripts(claude_home):
    """Yield (project_dir, session_id, path) for every transcript found.

    Every project directory is scanned, not just the workspace one: the encoded
    directory name is an implementation detail of Claude Code, and agent-box
    normally has exactly one anyway.
    """
    root = os.path.join(claude_home, "projects")
    try:
        project_dirs = sorted(os.listdir(root))
    except OSError:
        return
    for project_dir in project_dirs:
        directory = os.path.join(root, project_dir)
        try:
            names = sorted(os.listdir(directory))
        except OSError:
            continue
        for name in names:
            if not name.endswith(".jsonl"):
                continue
            session_id = name[: -len(".jsonl")]
            if not is_uuid(session_id):
                continue
            yield project_dir, session_id, os.path.join(directory, name)


def _truncate(text, limit):
    """One bounded, single-line string, or None. Never raises.

    Total like the other derivation helpers: anything that is not a string —
    and any string that collapses to nothing — yields None, so call sites read
    another program's format without pre-guarding the type themselves.
    """
    if not isinstance(text, str):
        return None
    text = " ".join(text.split())
    if not text:
        return None
    return text if len(text) <= limit else text[:limit] + "…"


def _prompt_text(entry):
    """Text of a genuine user prompt, or None.

    Skips meta entries, tool results — which arrive as list content — and the
    CLI's own slash-command bookkeeping. Sidechain turns never reach here: the
    branch dispatch in parse_transcript filters them for user and assistant
    alike.
    """
    if entry.get("isMeta"):
        return None
    message = entry.get("message")
    content = message.get("content") if isinstance(message, dict) else None
    if not isinstance(content, str):
        return None
    text = content.strip()
    if not text or text.startswith(_BOOKKEEPING):
        return None
    return text


def _token_count(value):
    """A usage value's numeric contribution: the value itself, or 0.

    Anything that is not a real number — string, dict, list, None — contributes
    nothing rather than raising. bool is excluded deliberately: isinstance(True,
    int) is True in Python, and a boolean token count is meaningless, so it must
    be ignored rather than counted as 1.

    Non-finite values are excluded for the reason _updated_at excludes them,
    carried one step further: json.load() accepts NaN and Infinity by default,
    NaN is truthy so `total or None` would hand one to the row, and json.dumps()
    then writes a bare NaN token that no JSON parser will read back.

    Only floats are asked. An int is finite by construction, and one too large
    for a float — json parses any run of digits as an int — would make
    math.isfinite() raise OverflowError, i.e. the guard would itself become the
    escaping exception it exists to prevent.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    if isinstance(value, float) and not math.isfinite(value):
        return 0  # NaN/Infinity survive json.load and are not valid JSON on the way out
    return value


def _context_tokens(usage):
    """Context in use, by the same formula the agent-box status line uses.

    Only the absolute figure can be derived here: the window size is never
    recorded in a transcript, and no denominator this module can see would be
    trustworthy. A zero total means "nothing to show", so it reports None — the
    UI then shows nothing rather than "0k".

    Both the container and its contents are guarded: this reads another
    program's private, version-drifting format and must never raise.
    """
    if not isinstance(usage, dict):
        return None
    total = (
        _token_count(usage.get("input_tokens"))
        + _token_count(usage.get("cache_creation_input_tokens"))
        + _token_count(usage.get("cache_read_input_tokens"))
    )
    return total or None


def _iter_entries(path):
    """Yield every parsable dict entry of a transcript. Never raises.

    A read error part-way through simply ends the stream: callers keep whatever
    was already yielded.
    """
    # errors="replace" is load-bearing: one invalid byte would otherwise raise
    # UnicodeDecodeError from the file iterator, outside the json.loads try.
    try:
        with open(path, "r", encoding="utf-8", errors="replace") as handle:
            for line in handle:
                if not any(token in line for token in _CANDIDATES):
                    continue
                try:
                    entry = json.loads(line)
                except (ValueError, RecursionError):
                    # RecursionError is not a ValueError, the same hole already
                    # closed on the registry side: json raises it on deeply
                    # nested input, and one such line here would empty the
                    # whole list, healthy sessions included.
                    continue  # truncated tail, or nesting deeper than json will go
                if isinstance(entry, dict):
                    yield entry
    except OSError:
        return  # unreadable transcript: caller falls through to defaults


def _tool_activity(message):
    """Readable label for the last tool call in an assistant message.

    This is the last *completed* call, so it lags a live terminal by seconds —
    good enough for an overview of what a session is doing.

    Claude Code's tool inputs are already written for humans, so no
    per-tool knowledge is needed: Bash carries a `description`, and the
    file-oriented tools carry a `file_path`. Anything else shows its bare name.

    Every value here comes from another program's private format, so each is
    checked for shape before use: an AttributeError raised in here would escape
    the read guard in list_sessions() and empty the whole session list.
    """
    content = message.get("content")
    if not isinstance(content, list):
        return None
    for block in reversed(content):
        if not isinstance(block, dict) or block.get("type") != "tool_use":
            continue
        params = block.get("input")
        params = params if isinstance(params, dict) else {}
        # Both halves go through _truncate: it absorbs whatever shape the field
        # turns out to have, and guarantees the bounded, single-line result the
        # rest of this module promises. A detail that is truthy but collapses to
        # nothing falls back to the bare name — "Bash: None" helps nobody.
        name = _truncate(block.get("name"), _DETAIL_LIMIT) or "tool"
        detail = _truncate(params.get("description") or params.get("file_path"),
                           _DETAIL_LIMIT)
        return "%s: %s" % (name, detail) if detail else name
    return None


def parse_transcript(path):
    """Derive display fields from one transcript. Never raises on bad content."""
    title = None
    first_prompt = None
    last_prompt = None
    messages = 0
    usage = None
    activity = None

    for entry in _iter_entries(path):
        kind = entry.get("type")
        if kind == "ai-title":
            new_title = entry.get("aiTitle")
            # Last *usable* title wins: a blank or whitespace-only title
            # is degenerate and must leave a good earlier one standing.
            if isinstance(new_title, str) and new_title.strip():
                title = new_title
        elif kind == "last-prompt":
            new_prompt = entry.get("lastPrompt")
            # Same rule as the title: the last *usable* value wins, and a
            # non-string one must not displace a good earlier one.
            if isinstance(new_prompt, str) and new_prompt.strip():
                last_prompt = new_prompt
        elif kind == "user":
            if entry.get("isSidechain"):
                continue
            messages += 1
            if first_prompt is None:
                first_prompt = _prompt_text(entry)
        elif kind == "assistant":
            if entry.get("isSidechain"):
                continue  # a subagent turn has its own separate context
            messages += 1
            # A non-dict message must not reach .get() — it would raise
            # AttributeError, escape the read guard, and empty the whole
            # session list. Same hole Task 4 closed on the user side.
            message = entry.get("message")
            if not isinstance(message, dict):
                message = {}
            # Synthetic entries are Claude Code's own bookkeeping and
            # carry all-zero usage; one is routinely the final line.
            if message.get("model") != "<synthetic>":
                # Same "last usable value wins" rule as the title and the last
                # prompt: an absent or empty usage leaves the earlier one up.
                new_usage = message.get("usage")
                if new_usage:
                    usage = new_usage
                # Only a real tool call replaces the activity: a prose-only
                # reply leaves the last known one standing rather than
                # blanking the line.
                found = _tool_activity(message)
                if found:
                    activity = found

    name = _truncate(title, _NAME_LIMIT)
    source = "ai-title"
    if name is None:
        name, source = _truncate(first_prompt, _NAME_LIMIT), "first-prompt"
    if name is None:
        name, source = "(untitled)", "none"

    return {
        "name": name,
        "nameSource": source,
        "messages": messages,
        "lastPrompt": _truncate(last_prompt, _PROMPT_LIMIT),
        "contextTokens": _context_tokens(usage),
        "activity": activity,
    }


def _pid_alive(pid):
    """Whether a process with this pid exists right now.

    Signal 0 is the only signal sent anywhere in this module: it performs the
    kernel's permission and existence checks and delivers nothing.

    bool is excluded for the same reason as in _token_count — isinstance(True,
    int) is True in Python, and os.kill(True, 0) would quietly ask about pid 1,
    which always exists in a container. Zero and negatives are excluded because
    they address process *groups*, not processes: os.kill(0, 0) would answer
    about the caller's own group and report every stale entry as live.
    """
    if isinstance(pid, bool) or not isinstance(pid, int) or pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, just owned by someone else
    except (OSError, OverflowError):
        # OverflowError is not an OSError: a pid too large for the kernel's
        # pid_t (2**70 from a malformed registry) raises it before any syscall
        # happens, and an unguarded one would empty the whole session list.
        return False
    return True


# A registry timestamp this far behind its process's start is still accepted.
# The collisions this guards against are 53-63 days out, so a generous window
# costs nothing — and only a generous window keeps clock skew, second-vs-
# millisecond rounding, and a clock stepped by NTP from declaring a running
# session dead, which is the expensive direction of this decision.
_START_TIME_TOLERANCE_MS = 60 * 1000


def _boot_time():
    """Unix time of the last boot, from /proc/stat's btime line."""
    with open("/proc/stat", encoding="utf-8") as handle:
        for line in handle:
            if line.startswith("btime "):
                return int(line.split()[1])
    raise ValueError("no btime in /proc/stat")


def _process_start_time(pid):
    """Unix time at which this pid started, from /proc/<pid>/stat."""
    with open("/proc/%d/stat" % pid, encoding="utf-8") as handle:
        line = handle.read()
    # Field 2 (comm) is the raw executable name in parentheses, unquoted and
    # unescaped, and it really does contain spaces and parentheses in this
    # container ("Bun Pool 7", "HTTP Client"). Splitting the whole line would
    # therefore read some other field as starttime, so the fields are split
    # after the *last* ')': state (field 3) then leads, putting starttime
    # (field 22) at index 19.
    fields = line[line.rindex(")") + 1:].split()
    return _boot_time() + int(fields[19]) / os.sysconf("SC_CLK_TCK")


def _registry_matches_process(pid, updated_at_ms):
    """Whether this pid can really be the process the registry describes.

    A live pid is not enough. <home>/sessions sits in a persistent volume while
    pids restart from low numbers on every container boot, so an abandoned
    registry file naming pid 232 collides with whatever holds pid 232 in this
    boot — and since the entrypoint starts claude early, the colliding process is
    itself claude, so comparing command lines would not separate them either.
    Time does: a process cannot have been updated before it started.

    Every uncertainty resolves to True, i.e. to the pre-existing "the pid
    exists, so it is live" behaviour. A false live is annoying — the admin page
    offers a session that cannot be reopened. A false dead is destructive — it
    invites a second Claude onto a transcript that is already open. So only
    unambiguous evidence, a timestamp older than the process by more than
    _START_TIME_TOLERANCE_MS, is allowed to reject an entry.
    """
    # 0 is what _updated_at reports for a missing or unusable timestamp: that
    # is "unknown", not "1970", and unknown is no evidence of anything. Non-
    # finite values are unknown for the same reason (NaN compares False against
    # everything, which would read as stale).
    usable = (
        isinstance(updated_at_ms, (int, float))
        and not isinstance(updated_at_ms, bool)
        and math.isfinite(updated_at_ms)
        and updated_at_ms > 0
    )
    if not usable:
        return True
    try:
        started_ms = _process_start_time(pid) * 1000
    except Exception:
        # Deliberately total: an unreadable /proc, a pid that vanished between
        # the liveness test and this read, a field that does not parse, a
        # kernel that words /proc differently — none of them is evidence that
        # the process is stale, and this check must never be the thing that
        # invents a false dead.
        return True
    return updated_at_ms >= started_ms - _START_TIME_TOLERANCE_MS


def _updated_at(value):
    """A registry timestamp as an orderable number, or 0.

    updatedAt decides which of several registry files wins, so it reaches a >=
    comparison. It comes from another program's format, and a string or dict
    there raises TypeError — which escapes list_sessions() and empties the
    entire session list. An unusable timestamp sorts below every real one.

    json.load() accepts NaN and Infinity by default and both are floats, so
    both reach that comparison. Infinity beats every real timestamp, and NaN is
    worse: every comparison against it is False, so it displaces whatever
    precedes it and loses to whatever follows — the winner would become a
    function of filename order. Only a finite number is orderable.
    """
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return 0
    try:
        if not math.isfinite(value):
            return 0
    except OverflowError:
        # An int too large to convert to a float. It is orderable against other
        # ints, but _registry_matches_process() compares it to a float process
        # start time, so it would only move the crash downstream. Unusable.
        # Note _token_count keeps such an int: a token total is only ever added
        # to other ints and serialised, and JSON carries arbitrary precision.
        return 0
    return value


def live_sessions(claude_home):
    """Map session id -> {pid, status, waitingFor, updatedAt} for running sessions.

    Registry files outlive their processes: ttyd SIGHUPs its child on disconnect
    without cleaning up, so a file alone proves nothing — and the pid it names
    may since have been handed to an unrelated process, which
    _registry_matches_process separates out. Several files can also name the
    same session across restarts, so the freshest one wins.

    Liveness is decided before freshness, deliberately: the newest file on disk
    may well be the abandoned one. Both errors are expensive — a session
    reported live but unreachable can never be reopened, and one reported dead
    while running invites a second Claude onto an open transcript.

    updatedAt is carried in the result because it is what resolves that race;
    it is of no interest to a caller, but leaving it out would mean recomputing
    it or keeping a second dict alongside this one.
    """
    result = {}
    directory = os.path.join(claude_home, "sessions")
    try:
        names = sorted(os.listdir(directory))
    except OSError:
        return result
    for name in names:
        # The real directory holds <pid>.<hash>.key files alongside the JSON.
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(directory, name), encoding="utf-8") as handle:
                data = json.load(handle)
        except (OSError, ValueError, RecursionError):
            # RecursionError is not a ValueError: json raises it on deeply
            # nested input, and one such file here would empty the whole list.
            continue
        if not isinstance(data, dict):
            continue
        # The id is used as a dict key one line down, so a list or dict here
        # raises TypeError: unhashable type — the same one-file-empties-the-
        # whole-list hole the updatedAt guard closes on the next line.
        session_id = data.get("sessionId")
        if not isinstance(session_id, str) or not session_id:
            continue
        pid = data.get("pid")
        updated_at = _updated_at(data.get("updatedAt"))
        if not _pid_alive(pid) or not _registry_matches_process(pid, updated_at):
            continue
        existing = result.get(session_id)
        # Ties keep the entry already seen, so the first filename in sort order
        # wins. Ties are reachable — equal timestamps, both missing, or both
        # unusable (the guard maps either to 0, so it creates them) — and no
        # information distinguishes the entries when they happen, so filename
        # order is as good a rule as any. What matters is that it is fixed:
        # a stable winner keeps the admin page from flickering between two
        # equally-good registry files on consecutive reads.
        if existing and existing["updatedAt"] >= updated_at:
            continue
        result[session_id] = {
            "pid": pid,
            # status and waitingFor are foreign JSON on their way to the UI, so
            # they go through _truncate like every other outward-facing string
            # in this module: bounded, single-line, and None when the registry
            # hands over a dict or a list instead of a string. They share the
            # name's limit because they share its budget — one line of the UI.
            "status": _truncate(data.get("status"), _NAME_LIMIT),
            "waitingFor": _truncate(data.get("waitingFor"), _NAME_LIMIT),
            "updatedAt": updated_at,
        }
    return result


class DeleteError(Exception):
    """A delete that could not be performed, carrying an HTTP status.

    The status is the whole point: the HTTP layer above has no way to tell a
    malformed id from a running session from a missing one otherwise, and the
    three answers it owes the browser are different.
    """

    def __init__(self, status, message):
        super().__init__(message)
        self.status = status


def delete_session(claude_home, session_id):
    """Remove a session's transcript. Refuses live sessions and bad ids.

    Returns the path that was unlinked, so a caller holding a SessionStore can
    evict it from the parse cache — this function holds no store of its own.

    The order of the refusals is part of the contract. A malformed id is
    rejected before anything is joined onto a path or read from disk, and a
    running session is refused before the transcript tree is walked at all:
    deleting the transcript of a session someone is talking to would destroy an
    unrecoverable conversation out from under a live process.
    """
    if not is_uuid(session_id):
        raise DeleteError(400, "not a session id")
    if session_id in live_sessions(claude_home):
        raise DeleteError(409, "session is running")

    root = os.path.realpath(os.path.join(claude_home, "projects"))
    for _project_dir, found_id, path in _iter_transcripts(claude_home):
        if found_id != session_id:
            continue
        # The uuid check already rules out traversal in the id itself; this
        # additionally refuses a transcript that is a symlink out of the tree.
        real = os.path.realpath(path)
        # The separator is load-bearing: without it a sibling directory that
        # merely shares the prefix — <home>/projects-old/ — would read as inside
        # the tree, and a symlinked transcript could delete anything in it.
        if not real.startswith(root + os.sep):
            raise DeleteError(400, "transcript resolves outside the projects directory")
        try:
            os.unlink(real)
        except FileNotFoundError:
            # The transcript is already gone, which is exactly what the 404 on
            # the last line of this function means — so say the same thing, and
            # the operation reads as idempotent from the caller's side. Two
            # ways here: a dangling in-tree symlink (unlink removes the target
            # and leaves the link, so the id keeps resolving), and the window
            # between the liveness check above and this unlink. Neither is
            # worth a lock; both are simply a session that is no longer there.
            raise DeleteError(404, "no such session")
        except OSError as error:
            # Everything else — a directory named <uuid>.jsonl, a read-only
            # project directory, a failing disk. The caller catches DeleteError
            # and nothing else, so a bare OSError would escape the HTTP layer as
            # an unhandled traceback. The reason is included because it is
            # operator-facing and this module never sees secrets.
            raise DeleteError(500, "could not remove the transcript: %s" % error)
        return real
    raise DeleteError(404, "no such session")


class SessionStore:
    """Lists sessions from a Claude home directory."""

    def __init__(self, claude_home):
        self.claude_home = claude_home
        # Per instance, not module-level: two stores are two independent
        # readers, and a shared cache would outlive whatever set it up.
        self._cache = {}

    def _parsed(self, path, stat):
        """Parsed transcript fields, re-read only when the file has changed.

        The admin page polls every few seconds; without this, every poll would
        re-read every transcript, including multi-megabyte ones. Only the
        session actually being written is ever re-read.

        Size joins mtime in the key because neither alone is enough: a rewrite
        of equal length changes only the timestamp, and two writes inside one
        coarse filesystem tick change only the size. The stat is the one the
        caller already took, so the key costs no extra syscall.
        """
        key = (stat.st_mtime_ns, stat.st_size)
        cached = self._cache.get(path)
        if cached is not None and cached[0] == key:
            return cached[1]
        parsed = parse_transcript(path)
        self._cache[path] = (key, parsed)
        return parsed

    def forget(self, path):
        """Drop a cached parse, e.g. after its transcript has been deleted.

        Hygiene, not correctness: a deleted session's entry is simply never
        read again, and the (mtime_ns, size) key already catches a recreate at
        the same path. It exists so that a caller which deletes a transcript —
        delete_session() hands back the path it unlinked — has a way to say so
        without reaching into a private attribute of this class.
        """
        self._cache.pop(path, None)

    def list_sessions(self):
        # Read once for the whole list: liveness is a single directory scan,
        # not a per-session lookup.
        live = live_sessions(self.claude_home)
        sessions = []
        for project_dir, session_id, path in _iter_transcripts(self.claude_home):
            try:
                stat = os.stat(path)
            except OSError:
                continue
            # The copy is load-bearing now rather than merely defensive: the
            # parsed dict is the cached one, so a caller mutating this record
            # would otherwise corrupt what every later poll hands back.
            record = dict(self._parsed(path, stat))
            record.update(
                {
                    "id": session_id,
                    "projectDir": project_dir,
                    "lastActive": int(stat.st_mtime),
                    "bytes": stat.st_size,
                    "live": session_id in live,
                    "status": live.get(session_id, {}).get("status"),
                    "waitingFor": live.get(session_id, {}).get("waitingFor"),
                    "pid": live.get(session_id, {}).get("pid"),
                }
            )
            sessions.append(record)
        sessions.sort(key=lambda s: s["lastActive"], reverse=True)
        return sessions


def slugify(text, limit=32):
    """Lowercase, alphanumerics and dashes only, collapsed and trimmed.

    Total, like _truncate: `text or ""` already absorbs None and "", but a
    truthy non-string would reach .lower() and raise AttributeError, and every
    derivation helper here answers with a sentinel instead of raising — the
    caller reads another program's format and should not have to pre-guard.

    Non-ASCII is deliberately not transliterated: an accented or CJK character
    is a separator exactly like punctuation. The result is interpolated into a
    shell-built name, so restricting it to [a-z0-9-] is the point.

    The second strip is what keeps truncation honest: cutting at the limit can
    land on a separator, and a name ending in a dangling dash is not a name.
    """
    if not isinstance(text, str):
        return ""
    slug = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return slug[:limit].strip("-")


def transcript_for(claude_home, session_id):
    """Path of this session's transcript, or None when it has none yet.

    Public because session_title.py needs the file itself rather than a parse
    of it: it stats the path to know whether re-reading could change anything.
    A session legitimately has no transcript for the first moments of its life
    — Claude Code writes the file when the conversation starts, not when the
    process does.
    """
    for _project_dir, found_id, path in _iter_transcripts(claude_home):
        if found_id == session_id:
            return path
    return None


def slug_for(claude_home, session_id):
    """Slug of a session's ai-title, or '' when it has none yet.

    Used for the Remote Control name, which must be unique across concurrent
    sessions. Callers supply their own fallback when this returns ''.

    Only an ai-title qualifies. A first-prompt name is stable — it is the first
    thing ever said and never changes — but it is routinely noise like "ping",
    and nameSource is exactly the discriminator for that. '' lets the caller
    fall back to the uuid, which identifies the session better than "ping" does.
    """
    path = transcript_for(claude_home, session_id)
    if path is None:
        return ""
    parsed = parse_transcript(path)
    return slugify(parsed["name"]) if parsed["nameSource"] == "ai-title" else ""


def _cli(argv):
    """Answer the two questions the shell scripts need.

    The channels are the interface, and they differ per flag. launch_session.sh
    branches on the *exit code* of --is-live — it is the guard that stops two
    Claude processes opening one transcript, which corrupts it — so 0 means live
    and non-zero means not. start_claude.sh reads *stdout* from --slug, so
    nothing but the slug may ever be printed there; the usage line goes to
    stderr for the same reason.
    """
    home = os.environ.get("CLAUDE_HOME") or os.path.expanduser("~/.claude")
    if len(argv) == 3 and argv[1] == "--is-live":
        return 0 if argv[2] in live_sessions(home) else 1
    if len(argv) == 3 and argv[1] == "--slug":
        print(slug_for(home, argv[2]))
        return 0
    print("usage: session_store.py [--is-live|--slug] <session-id>", file=sys.stderr)
    return 2


if __name__ == "__main__":
    sys.exit(_cli(sys.argv))
