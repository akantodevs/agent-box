"""Claude Code's auto-memory, as the administration page sees it.

Memories are plain files under ~/.claude/projects/<project>/memory/: one fact
per *.md file with a small frontmatter header, plus MEMORY.md, the index whose
lines point at them. Only the index is loaded into a session's context, and
Claude Code cuts it off after INDEX_LINE_CAP lines or INDEX_BYTE_CAP bytes —
an index line past either cap is never seen, so the memory it points at is
effectively forgotten. This module reports where every memory stands against
that cutoff, and deletes a memory together with its index line.

Standard library only, like session_store.
"""

import datetime
import errno
import json
import os
import re
import shutil
import stat
import tempfile


INDEX_NAME = "MEMORY.md"

# Claude Code's own cutoff for MEMORY.md, read from the installed CLI. Whichever
# is reached first ends what a session sees of the index.
INDEX_LINE_CAP = 200
INDEX_BYTE_CAP = 25000

# A markdown link to a bare *.md filename: what an index line points with. A
# path or URL is not a memory in this directory, so it never matches.
_LINK = re.compile(r"\]\(([^()\s/\\]+\.md)\)")

# The types Claude Code's memory instructions define; anything else is grouped
# as "other" rather than inventing a group per typo.
TYPES = ("user", "feedback", "project", "reference")

# What a memory file may be called. It is also the whole of the validation a
# filename from a URL gets, so it admits no separator, no leading dot and no
# "..": the result can only ever name a file directly inside a memory dir.
_FILENAME = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*\.md")

# Frontmatter is a few hundred characters; this only bounds a pathological file.
_HEAD_CHARS = 65536

_NAME_LIMIT = 120
_TEXT_LIMIT = 300


class MemoryStoreError(Exception):
    """A refusal with the HTTP status it maps to; the message is the reason.

    400 and 404 messages are path-free by construction and go to the browser
    verbatim. A 500 carries the OSError and is for the log only — the same
    split sessions.py makes for session deletes. `public` is an optional
    path-free sentence a 500 may send the browser instead of the generic one,
    for when the generic one would be untrue (a delete that half-succeeded).
    """

    def __init__(self, status, message, public=None):
        super().__init__(message)
        self.status = status
        self.public = public


def _unquote(value):
    """A YAML scalar as Claude Code writes them: bare, "double" or 'single'."""
    value = value.strip()
    if len(value) >= 2 and value[0] == value[-1] == '"':
        # JSON's string escapes are the subset of YAML's that ever appear here.
        try:
            return json.loads(value)
        except ValueError:
            return value[1:-1]
    if len(value) >= 2 and value[0] == value[-1] == "'":
        return value[1:-1].replace("''", "'")
    return value


def parse_frontmatter(text):
    """The frontmatter's fields, nested keys flattened to "parent.key".

    Not a YAML parser: memory frontmatter only ever uses top-level scalars and
    one nested mapping (metadata:), and a dependency for that would be the only
    one in the image's Python. Anything unexpected is skipped rather than
    raised — one odd file must not take the whole listing down. No closing
    fence means the "---" was a horizontal rule, not frontmatter.
    """
    lines = text.splitlines()
    if not lines or lines[0].strip() != "---":
        return {}
    fields = {}
    parent = None
    for line in lines[1:]:
        if line.strip() == "---":
            return fields
        if not line.strip() or line.lstrip().startswith("#"):
            continue
        key, separator, value = line.strip().partition(":")
        if not separator:
            continue
        key = key.strip()
        if line[:1] in (" ", "\t"):
            if parent:
                fields["%s.%s" % (parent, key)] = _unquote(value)
        elif value.strip():
            fields[key] = _unquote(value)
            parent = None
        else:
            parent = key
    return {}


def parse_index(data):
    """MEMORY.md's index entries: the lines that link to a memory file.

    Takes bytes because the cap is in bytes. Each entry has its 1-based line
    number, its text, the filenames it links to, and whether it falls within
    both caps. bytes.splitlines() splits on \\n, \\r\\n and \\r only, which is
    what an editor would call a line.
    """
    entries = []
    offset = 0
    for number, raw in enumerate(data.splitlines(keepends=True), 1):
        body = raw.rstrip(b"\r\n")
        end = offset + len(body)
        offset += len(raw)
        text = body.decode("utf-8", "replace")
        targets = _LINK.findall(text)
        if not targets:
            continue
        entries.append({
            "line": number,
            "text": text,
            "targets": targets,
            "withinCap": number <= INDEX_LINE_CAP and end <= INDEX_BYTE_CAP,
        })
    return entries


def _bounded(value, limit):
    """One line of at most `limit` characters, whitespace collapsed."""
    return " ".join(str(value).split())[:limit]


def _memory_dirs(claude_home):
    """(project, directory) for every project that has a memory dir."""
    root = os.path.join(claude_home, "projects")
    try:
        projects = sorted(os.listdir(root))
    except OSError:
        return
    for project in projects:
        directory = os.path.join(root, project, "memory")
        if (_encodes(project) and os.path.isdir(directory)
                and not os.path.islink(directory)):
            yield project, directory


def _read_head(path):
    """The first _HEAD_CHARS of a memory file, BOM dropped; "" if unreadable."""
    try:
        with open(path, encoding="utf-8-sig", errors="replace") as handle:
            return handle.read(_HEAD_CHARS)
    except OSError:
        return ""


def _read_index(directory):
    """MEMORY.md as bytes, or b"" when there is none."""
    try:
        # O_NOFOLLOW: a symlinked index counts as absent, like a symlinked memory.
        descriptor = os.open(os.path.join(directory, INDEX_NAME),
                             os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, "rb") as handle:
            return handle.read()
    except OSError:
        return b""


def _modified(fields, mtime):
    """The frontmatter's modified time as epoch seconds, else the mtime."""
    value = fields.get("metadata.modified") or fields.get("modified") or ""
    try:
        moment = datetime.datetime.fromisoformat(value.replace("Z", "+00:00"))
    except ValueError:
        return int(mtime)
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return int(moment.timestamp())


def _memory_files(directory):
    """{filename: lstat} for the regular memory files in one directory.

    lstat, not stat: a symlink would let a delete or a read reach outside the
    memory dir, and Claude Code never writes one.
    """
    files = {}
    try:
        names = os.listdir(directory)
    except OSError:
        return files
    for name in names:
        if name == INDEX_NAME or not _FILENAME.fullmatch(name):
            continue
        try:
            info = os.lstat(os.path.join(directory, name))
        except OSError:
            continue
        if stat.S_ISREG(info.st_mode):
            files[name] = info
    return files


def list_memories(claude_home):
    """Every memory in the home, its status, the dangling index lines, totals."""
    memories = []
    dangling = []
    index_bytes = 0
    index_lines = 0
    for project, directory in _memory_dirs(claude_home):
        data = _read_index(directory)
        index_bytes = max(index_bytes, len(data))
        index_lines = max(index_lines, len(data.splitlines()))
        files = _memory_files(directory)

        first_entry = {}
        for entry in parse_index(data):
            for target in entry["targets"]:
                if target in files:
                    first_entry.setdefault(target, entry)
                elif not os.path.lexists(os.path.join(directory, target)):
                    dangling.append({
                        "project": project,
                        "file": target,
                        "line": entry["line"],
                        "text": _bounded(entry["text"], _TEXT_LIMIT),
                    })

        for name, info in files.items():
            fields = parse_frontmatter(_read_head(os.path.join(directory, name)))
            entry = first_entry.get(name)
            if entry is None:
                status = "orphaned"
            elif entry["withinCap"]:
                status = "loaded"
            else:
                status = "cutoff"
            memory_type = fields.get("metadata.type") or fields.get("type")
            memories.append({
                "id": "%s/%s" % (project, name),
                "project": project,
                "file": name,
                "name": _bounded(fields.get("name") or name[:-len(".md")], _NAME_LIMIT),
                "description": _bounded(fields.get("description", ""), _TEXT_LIMIT),
                "type": memory_type if memory_type in TYPES else "other",
                "bytes": info.st_size,
                "modified": _modified(fields, info.st_mtime),
                "status": status,
                "indexLine": entry["line"] if entry else None,
            })

    memories.sort(key=lambda memory: (memory["name"].casefold(), memory["id"]))
    totals = {
        "count": len(memories),
        "bytes": sum(memory["bytes"] for memory in memories),
        "indexBytes": index_bytes,
        "indexLines": index_lines,
        "indexByteCap": INDEX_BYTE_CAP,
        "indexLineCap": INDEX_LINE_CAP,
        "dangling": len(dangling),
    }
    for status in ("loaded", "cutoff", "orphaned"):
        totals[status] = sum(1 for memory in memories if memory["status"] == status)
    return {"memories": memories, "dangling": dangling, "totals": totals}


# The most characters read_memory() hands back. A memory is a few hundred bytes;
# the bound only keeps one runaway file from becoming a runaway response.
_READ_CHARS = 1048576


def _encodes(text):
    """Whether `text` survives the filesystem encoding (no lone surrogates)."""
    try:
        text.encode("utf-8")
    except UnicodeEncodeError:
        return False
    return True


def resolve(claude_home, project, filename):
    """(directory, path) of one memory file, or MemoryStoreError(400/404).

    The project is a single path component that must already exist; the
    filename must look like a memory and must not be the index. The path is
    then lstat'ed, and a symlinked memory dir is refused: only a regular file
    counts, so a symlink planted in the memory dir cannot be read or deleted
    through it.
    """
    if (not isinstance(project, str) or project in ("", ".", "..")
            or any(c in project for c in "/\\\x00")
            or not _encodes(project)
            or not isinstance(filename, str)
            or not _FILENAME.fullmatch(filename) or filename == INDEX_NAME):
        raise MemoryStoreError(400, "not a memory id")
    directory = os.path.join(claude_home, "projects", project, "memory")
    path = os.path.join(directory, filename)
    if os.path.islink(directory):
        raise MemoryStoreError(404, "no such memory")
    try:
        info = os.lstat(path)
    except OSError:
        raise MemoryStoreError(404, "no such memory")
    if not stat.S_ISREG(info.st_mode):
        raise MemoryStoreError(404, "no such memory")
    return directory, path


def read_memory(claude_home, project, filename):
    """One memory file's full text, frontmatter included."""
    _, path = resolve(claude_home, project, filename)
    try:
        # O_NOFOLLOW closes the window between resolve()'s lstat and this open
        # in which the file could be swapped for a symlink.
        descriptor = os.open(path, os.O_RDONLY | os.O_NOFOLLOW)
        with os.fdopen(descriptor, encoding="utf-8", errors="replace") as handle:
            return handle.read(_READ_CHARS)
    except FileNotFoundError:
        raise MemoryStoreError(404, "no such memory")
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise MemoryStoreError(404, "no such memory")
        raise MemoryStoreError(500, "could not read %s: %s" % (path, error))


def _drop_index_lines(directory, filename):
    """Rewrite MEMORY.md without the lines that link to `filename`.

    Bytes in, bytes out: every kept line goes back exactly as it was — line
    endings, encoding errors, a missing final newline. The new index is written
    beside the old one and renamed over it, so a session starting at that
    moment reads either the old index or the new one, never half of one. No
    index, or no matching line, means nothing is written at all.

    A line that links to `filename` and to other files goes whole; the others
    may then show as orphaned (unless another line links them). A line a
    session appends between the read and the rename is lost (accepted: a
    one-entry window). A symlinked index is refused with OSError rather than
    replaced.
    """
    index = os.path.join(directory, INDEX_NAME)
    try:
        info = os.lstat(index)
    except FileNotFoundError:
        return
    if not stat.S_ISREG(info.st_mode):
        raise OSError("%s is not a regular file" % index)
    try:
        descriptor = os.open(index, os.O_RDONLY | os.O_NOFOLLOW)
    except FileNotFoundError:
        return
    except OSError as error:
        if error.errno == errno.ELOOP:
            raise OSError("%s is a symlink" % index)
        raise
    with os.fdopen(descriptor, "rb") as handle:
        data = handle.read()
    lines = data.splitlines(keepends=True)
    kept = [line for line in lines
            if filename not in _LINK.findall(line.decode("utf-8", "replace"))]
    if len(kept) == len(lines):
        return
    descriptor, temporary = tempfile.mkstemp(dir=directory, prefix=".MEMORY.md.")
    try:
        with os.fdopen(descriptor, "wb") as handle:
            handle.write(b"".join(kept))
            handle.flush()
            os.fsync(handle.fileno())
        shutil.copymode(index, temporary)
        os.replace(temporary, index)
    except BaseException:
        try:
            os.unlink(temporary)
        except OSError:
            pass
        raise


def delete_memory(claude_home, project, filename):
    """Delete one memory file and every MEMORY.md line that links to it.

    The file goes first. Should the index rewrite then fail, what is left is a
    dangling line the page reports and a retry cannot fix (the file is gone),
    which is the honest state; the reverse order would leave an orphan that
    looks deleted from the index but is still on disk. A line linking to it and
    to others goes too; the others may then show as orphaned (unless another
    line links them).
    """
    directory, path = resolve(claude_home, project, filename)
    try:
        os.unlink(path)
    except FileNotFoundError:
        raise MemoryStoreError(404, "no such memory")
    except OSError as error:
        raise MemoryStoreError(500, "could not delete %s: %s" % (path, error))
    try:
        _drop_index_lines(directory, filename)
    except OSError as error:
        raise MemoryStoreError(
            500, "deleted %s but could not update %s: %s"
            % (path, os.path.join(directory, INDEX_NAME), error),
            public="deleted the memory, but could not update MEMORY.md")
