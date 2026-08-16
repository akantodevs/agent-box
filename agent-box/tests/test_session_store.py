"""Unit tests for session_store.

Fixtures write compact JSON on purpose: session_store prefilters candidate lines
with substring matches against compact separators, exactly as Claude Code writes
them. Pretty-printed fixtures would not exercise the real path.
"""

import io
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import session_store


def write_transcript(home, session_id, entries, project="-workspace"):
    directory = os.path.join(home, "projects", project)
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, session_id + ".jsonl")
    with open(path, "w", encoding="utf-8") as handle:
        for entry in entries:
            handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
    return path


def write_registry(home, name, **fields):
    """Write one <home>/sessions/<name> registry file, as Claude Code does."""
    directory = os.path.join(home, "sessions")
    os.makedirs(directory, exist_ok=True)
    path = os.path.join(directory, name)
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(json.dumps(fields, separators=(",", ":")))
    return path


def dead_pid():
    """A pid that is certainly gone: spawned, waited for, and reaped.

    Guessing a number would be a lie — any number might belong to a running
    process, and a test that passes only because nothing happens to hold that
    pid today is worthless. Spawning and reaping is the only way to know.
    """
    process = subprocess.Popen([sys.executable, "-c", ""])
    process.wait()
    return process.pid


SESSION_A = "2026fc52-ed6a-4867-8f06-8f058939df11"
SESSION_B = "bccbe7c7-beff-436a-9c22-bd36757f016f"

# Registry timestamps are Unix milliseconds, and they are no longer free-floating
# numbers: a registry claiming to have been updated before the process it names
# started is now rejected as a pid-reuse collision (see PidReuseTest). Fixtures
# therefore have to carry plausible timestamps. FRESH and FRESHER are ordered so
# tests that separate two entries by freshness still can, and both are newer than
# the start of the process every fixture points at (the test runner itself).
FRESH = int(time.time() * 1000)
FRESHER = FRESH + 1000


class StoreTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def one_session(self, entries, session_id=SESSION_A, **kwargs):
        """Write a transcript and return its session record."""
        write_transcript(self.home, session_id, entries, **kwargs)
        return session_store.SessionStore(self.home).list_sessions()[0]


class UuidTest(unittest.TestCase):
    def test_is_uuid_accepts_uuids_and_rejects_empty_values(self):
        self.assertFalse(session_store.is_uuid(None))
        self.assertFalse(session_store.is_uuid(""))
        self.assertTrue(session_store.is_uuid(SESSION_A))
        self.assertTrue(session_store.is_uuid(SESSION_A.upper()))

    def test_a_trailing_newline_is_not_part_of_a_session_id(self):
        # `$` in a Python regex matches at the end of the string *or* just
        # before a trailing newline, so `<uuid>\n` used to pass this gate. It is
        # the first check in delete_session(), i.e. a security gate, so it has
        # to be exact: `\Z` matches only at the very end.
        self.assertFalse(session_store.is_uuid(SESSION_A + "\n"))
        self.assertFalse(session_store.is_uuid(SESSION_A + "\n\n"))
        self.assertFalse(session_store.is_uuid(SESSION_A + "\nx"))

    def test_non_strings_are_not_session_ids_rather_than_raising(self):
        # Total, like _truncate and slugify: `value or ""` absorbed None and "",
        # but a truthy non-string reached _UUID_RE.match() and raised TypeError.
        # It matters here more than anywhere: this is the first line of
        # delete_session(), and an id read out of a JSON request body — {"id":
        # 123} — is whatever type the body says. The caller catches DeleteError
        # and nothing else, so a raise is a 500 where a 400 was designed.
        for value in (123, 1.5, True, {"a": 1}, ["a"], SESSION_A.encode(), object()):
            with self.subTest(value=value):
                self.assertFalse(session_store.is_uuid(value))


class TruncateTest(unittest.TestCase):
    """_truncate is total: every input either truncates or returns None.

    Like UuidTest, this exercises a helper directly and needs no home directory.
    """

    def test_empty_and_whitespace_only_values_become_none(self):
        for value in (None, "", "  \n\t "):
            with self.subTest(value=value):
                self.assertIsNone(session_store._truncate(value, 80))

    def test_truthy_non_strings_become_none_rather_than_raising(self):
        # A truthy non-string used to survive the `text or ""` guard and reach
        # .split(), raising AttributeError — which is why every call site had to
        # pre-guard with isinstance. The helper absorbs it, as the other four do.
        for value in (123, 1.5, True, {"a": 1}, ["a"], object()):
            with self.subTest(value=value):
                self.assertIsNone(session_store._truncate(value, 80))


class DiscoveryTest(StoreTestCase):
    def test_finds_transcripts_in_every_project_dir(self):
        write_transcript(self.home, SESSION_A, [], project="-workspace")
        write_transcript(self.home, SESSION_B, [], project="-srv-other")
        found = {s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()}
        self.assertEqual({SESSION_A, SESSION_B}, set(found))
        self.assertEqual("-srv-other", found[SESSION_B]["projectDir"])

    def test_ignores_non_transcript_files(self):
        write_transcript(self.home, SESSION_A, [])
        directory = os.path.join(self.home, "projects", "-workspace")
        open(os.path.join(directory, "notes.txt"), "w").close()
        open(os.path.join(directory, "not-a-uuid.jsonl"), "w").close()
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual([SESSION_A], [s["id"] for s in sessions])

    def test_missing_projects_directory_is_not_an_error(self):
        self.assertEqual([], session_store.SessionStore(self.home).list_sessions())

    def test_non_directory_in_projects_root_is_skipped(self):
        write_transcript(self.home, SESSION_A, [])
        open(os.path.join(self.home, "projects", "stray.txt"), "w").close()
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual([SESSION_A], [s["id"] for s in sessions])

    def test_sorted_by_last_active_descending(self):
        path_a = write_transcript(self.home, SESSION_A, [])
        path_b = write_transcript(self.home, SESSION_B, [])
        os.utime(path_a, (1000, 1000))
        os.utime(path_b, (2000, 2000))
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual([SESSION_B, SESSION_A], [s["id"] for s in sessions])
        self.assertEqual(2000, sessions[0]["lastActive"])

    def test_reports_the_transcript_size_in_bytes(self):
        path = write_transcript(self.home, SESSION_A, [{"type": "mode", "mode": "normal"}])
        session = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertEqual(os.path.getsize(path), session["bytes"])


def ai_title(title):
    return {"type": "ai-title", "aiTitle": title}


def user(text, sidechain=False, meta=False):
    return {
        "type": "user",
        "isSidechain": sidechain,
        "isMeta": meta,
        "message": {"content": text},
    }


class NameTest(StoreTestCase):
    def test_prefers_the_last_ai_title(self):
        session = self.one_session(
            [user("do a thing"), ai_title("First title"), ai_title("Second title")]
        )
        self.assertEqual("Second title", session["name"])
        self.assertEqual("ai-title", session["nameSource"])

    def test_empty_ai_title_keeps_the_earlier_title(self):
        session = self.one_session([ai_title("Kept title"), ai_title("")])
        self.assertEqual("Kept title", session["name"])
        self.assertEqual("ai-title", session["nameSource"])

    def test_whitespace_only_ai_title_keeps_the_earlier_title(self):
        # A whitespace-only title is semantically empty: it must not overwrite
        # real information with a degenerate value.
        session = self.one_session([ai_title("Kept title"), ai_title("  \n\t ")])
        self.assertEqual("Kept title", session["name"])
        self.assertEqual("ai-title", session["nameSource"])

    def test_falls_back_to_the_first_real_prompt(self):
        session = self.one_session([user("add terraform"), user("and kubectl")])
        self.assertEqual("add terraform", session["name"])
        self.assertEqual("first-prompt", session["nameSource"])

    def test_skips_slash_commands_meta_and_tool_results(self):
        session = self.one_session(
            [
                user("<local-command-caveat>Caveat: ...</local-command-caveat>", meta=True),
                user("<command-name>/clear</command-name>"),
                user("<local-command-stdout>Set model</local-command-stdout>"),
                user([{"type": "tool_result", "content": "ok"}]),
                user("the real question"),
            ]
        )
        self.assertEqual("the real question", session["name"])

    def test_ignores_sidechain_prompts(self):
        session = self.one_session(
            [user("subagent instructions", sidechain=True), user("real prompt")]
        )
        self.assertEqual("real prompt", session["name"])

    def test_untitled_when_nothing_usable(self):
        session = self.one_session([{"type": "mode", "mode": "normal"}])
        self.assertEqual("(untitled)", session["name"])
        self.assertEqual("none", session["nameSource"])

    def test_whitespace_only_ai_title_falls_back_to_the_prompt(self):
        session = self.one_session([ai_title("   "), user("real prompt")])
        self.assertEqual("real prompt", session["name"])
        self.assertEqual("first-prompt", session["nameSource"])

    def test_whitespace_only_prompt_falls_back_to_untitled(self):
        session = self.one_session([user("  \n\t ")])
        self.assertEqual("(untitled)", session["name"])
        self.assertEqual("none", session["nameSource"])

    def test_multi_line_prompts_are_collapsed_to_one_line(self):
        session = self.one_session([user("line one\n\n  line two  ")])
        self.assertEqual("line one line two", session["name"])

    def test_long_names_are_truncated(self):
        name = self.one_session([user("x" * 200)])["name"]
        self.assertEqual(81, len(name))
        self.assertTrue(name.endswith("…"))

    def test_long_ai_titles_are_truncated(self):
        session = self.one_session([ai_title("y" * 200)])
        self.assertEqual(81, len(session["name"]))
        self.assertTrue(session["name"].endswith("…"))
        self.assertEqual("ai-title", session["nameSource"])

    def test_skips_meta_entries_that_look_like_ordinary_prose(self):
        # isMeta must be honoured on its own: a meta entry whose text carries no
        # bookkeeping prefix is invisible to _BOOKKEEPING and would otherwise
        # become the session name.
        session = self.one_session(
            [user("Caveat: the messages below were generated by the user", meta=True),
             user("the real question")]
        )
        self.assertEqual("the real question", session["name"])

    def test_only_the_expected_fields_are_derived_from_the_transcript(self):
        path = write_transcript(self.home, SESSION_A, [ai_title("A title")])
        self.assertEqual(
            {
                "name": "A title",
                "nameSource": "ai-title",
                "messages": 0,
                "lastPrompt": None,
                "contextTokens": None,
                "activity": None,
            },
            session_store.parse_transcript(path),
        )


def tool_use(name, params):
    return {"type": "tool_use", "name": name, "input": params}


def assistant(model="claude-opus-4-8", usage=None, tools=(), sidechain=False, text=None):
    content = [tool_use(name, params) for name, params in tools]
    # Real assistant messages almost always open with prose before any tool call.
    if text is not None:
        content.insert(0, {"type": "text", "text": text})
    return {
        "type": "assistant",
        "isSidechain": sidechain,
        "message": {"model": model, "content": content, "usage": usage or {}},
    }


def assistant_blocks(content, model="claude-opus-4-8"):
    """An assistant entry whose message content is supplied verbatim.

    assistant() can only build well-formed tool_use blocks in tool order; the
    out-of-order and malformed shapes below need the raw value.
    """
    return {
        "type": "assistant",
        "isSidechain": False,
        "message": {"model": model, "content": content, "usage": {}},
    }


class MessageCountTest(StoreTestCase):
    def test_counts_user_and_assistant_only(self):
        session = self.one_session(
            [
                {"type": "mode", "mode": "normal"},
                user("one"),
                assistant(),
                {"type": "file-history-snapshot", "messageId": "x"},
                ai_title("Some title"),
                user("two"),
                assistant(),
            ]
        )
        self.assertEqual(4, session["messages"])

    def test_excludes_sidechain_turns(self):
        session = self.one_session(
            [user("one"), assistant(), user("sub", sidechain=True), assistant(sidechain=True)]
        )
        self.assertEqual(2, session["messages"])

    def test_counts_meta_entries_it_refuses_to_name_the_session_after(self):
        # Deliberate asymmetry: `messages` is a volume metric and counts every
        # real turn, while naming is a heuristic that skips Claude Code's own
        # bookkeeping. A refactor must not quietly align the two.
        session = self.one_session(
            [user("Caveat: injected by the CLI", meta=True), user("the real question")]
        )
        self.assertEqual(2, session["messages"])
        self.assertEqual("the real question", session["name"])


class LastPromptTest(StoreTestCase):
    def test_last_prompt_is_the_most_recent(self):
        session = self.one_session(
            [
                {"type": "last-prompt", "lastPrompt": "older"},
                {"type": "last-prompt", "lastPrompt": "newer"},
            ]
        )
        self.assertEqual("newer", session["lastPrompt"])

    def test_last_prompt_is_none_when_absent(self):
        self.assertIsNone(self.one_session([user("hi")])["lastPrompt"])

    def test_whitespace_only_last_prompt_keeps_the_earlier_one(self):
        session = self.one_session(
            [
                {"type": "last-prompt", "lastPrompt": "Good prompt"},
                {"type": "last-prompt", "lastPrompt": "  \n\t "},
            ]
        )
        self.assertEqual("Good prompt", session["lastPrompt"])

    def test_long_last_prompts_are_truncated(self):
        # A last prompt is a preview, so it gets a longer budget than a name.
        session = self.one_session(
            [{"type": "last-prompt", "lastPrompt": "z" * 300}]
        )
        self.assertEqual(161, len(session["lastPrompt"]))
        self.assertTrue(session["lastPrompt"].endswith("…"))


USAGE = {
    "input_tokens": 4,
    "cache_creation_input_tokens": 10,
    "cache_read_input_tokens": 63869,
}
SYNTHETIC_USAGE = {
    "input_tokens": 0,
    "cache_creation_input_tokens": 0,
    "cache_read_input_tokens": 0,
}


class ContextTest(StoreTestCase):
    def test_sums_the_last_real_usage(self):
        # Shaped like a real turn: prose, then a tool call, then the usage.
        session = self.one_session(
            [user("hi"), assistant(text="On it.", tools=[("Read", {"file": "x"})], usage=USAGE)]
        )
        self.assertEqual(63883, session["contextTokens"])

    def test_ignores_a_trailing_synthetic_entry(self):
        # Claude Code's own bookkeeping turn: all-zero usage, and routinely the
        # final line of a live transcript.
        session = self.one_session(
            [
                assistant(usage=USAGE),
                assistant(model="<synthetic>", usage=SYNTHETIC_USAGE),
            ]
        )
        self.assertEqual(63883, session["contextTokens"])

    def test_ignores_sidechain_usage(self):
        # A subagent turn has its own separate context window.
        session = self.one_session(
            [
                assistant(usage=USAGE),
                assistant(usage={"input_tokens": 999}, sidechain=True),
            ]
        )
        self.assertEqual(63883, session["contextTokens"])

    def test_context_is_reported_as_absolute_tokens_only(self):
        # A percentage needs a context window size, which a transcript never
        # records — the status line only knows it because Claude Code hands it
        # over on stdin. Assuming one silently mis-reports a long session, so
        # the admin page shows absolute tokens and nothing else.
        session = self.one_session([assistant(usage=USAGE)])
        self.assertEqual(63883, session["contextTokens"])
        self.assertNotIn("contextPercent", session)

    def test_none_when_there_is_no_usable_usage(self):
        session = self.one_session([user("hi"), assistant()])
        self.assertIsNone(session["contextTokens"])

    def test_an_all_zero_usage_reports_nothing_rather_than_zero(self):
        # A real, non-synthetic turn can still carry an all-zero usage. The dict
        # is truthy, so it reaches _context_tokens; a zero total is "nothing to
        # show", not "0k". Without this the `return total or None` rule is
        # unpinned — the <synthetic> guard hides the only other all-zero path.
        session = self.one_session(
            [assistant(usage={
                "input_tokens": 0,
                "cache_creation_input_tokens": 0,
                "cache_read_input_tokens": 0,
            })]
        )
        self.assertIsNone(session["contextTokens"])

    def test_float_token_counts_are_counted(self):
        session = self.one_session(
            [assistant(usage={"input_tokens": 1.5, "cache_read_input_tokens": 10})]
        )
        self.assertEqual(11.5, session["contextTokens"])

    def test_negative_token_counts_are_counted(self):
        # Nonsense, but numeric: the guard filters on type, not on plausibility.
        session = self.one_session(
            [assistant(usage={"input_tokens": -5, "cache_read_input_tokens": 100})]
        )
        self.assertEqual(95, session["contextTokens"])


class ActivityTest(StoreTestCase):
    """What a session is doing: the last tool call of the last real turn.

    Deliberately the last *completed* call, so it lags a live terminal by
    seconds. That is good enough for an overview and is by design.
    """

    def test_uses_the_bash_description(self):
        session = self.one_session(
            [assistant(tools=[("Bash", {"command": "ls -la", "description": "List files"})])]
        )
        self.assertEqual("Bash: List files", session["activity"])

    def test_falls_back_to_the_file_path(self):
        session = self.one_session(
            [assistant(tools=[("Read", {"file_path": "/workspace/ep.sh"})])]
        )
        self.assertEqual("Read: /workspace/ep.sh", session["activity"])

    def test_bare_tool_name_when_there_is_no_detail(self):
        session = self.one_session(
            [assistant(tools=[("AskUserQuestion", {"questions": []})])]
        )
        self.assertEqual("AskUserQuestion", session["activity"])

    def test_reports_the_most_recent_tool_call(self):
        session = self.one_session(
            [
                assistant(tools=[("Bash", {"description": "older"})]),
                assistant(tools=[("Bash", {"description": "newer"})]),
            ]
        )
        self.assertEqual("Bash: newer", session["activity"])

    def test_none_when_no_tools_were_used(self):
        self.assertIsNone(self.one_session([user("hi"), assistant()])["activity"])

    def test_skips_a_text_block_before_the_tool_call(self):
        # The shape real assistant messages almost always have: prose first,
        # then the tool call.
        session = self.one_session(
            [assistant(text="Let me look.", tools=[("Read", {"file_path": "/etc/hosts"})])]
        )
        self.assertEqual("Read: /etc/hosts", session["activity"])

    def test_skips_a_text_block_after_the_tool_call(self):
        # Blocks are scanned in reverse, so a trailing text block is the one
        # that would be picked up first if the type check were dropped.
        session = self.one_session(
            [
                assistant_blocks(
                    [
                        {"type": "text", "text": "Let me look."},
                        tool_use("Read", {"file_path": "/etc/hosts"}),
                        {"type": "text", "text": "That file is short."},
                    ]
                )
            ]
        )
        self.assertEqual("Read: /etc/hosts", session["activity"])

    def test_none_when_the_message_is_only_text(self):
        session = self.one_session([assistant(text="Here is what I found.")])
        self.assertIsNone(session["activity"])

    def test_the_last_tool_call_within_one_message_wins(self):
        session = self.one_session(
            [
                assistant(
                    tools=[
                        ("Read", {"file_path": "/etc/hosts"}),
                        ("Bash", {"description": "Check cache dirs"}),
                    ]
                )
            ]
        )
        self.assertEqual("Bash: Check cache dirs", session["activity"])

    def test_ignores_sidechain_tool_calls(self):
        # A subagent's tools are its own work, not the session's.
        session = self.one_session(
            [
                assistant(tools=[("Bash", {"description": "the real work"})]),
                assistant(tools=[("Grep", {"description": "subagent work"})], sidechain=True),
            ]
        )
        self.assertEqual("Bash: the real work", session["activity"])

    def test_ignores_a_trailing_synthetic_entry(self):
        # Same bookkeeping turn that is excluded from the context total.
        session = self.one_session(
            [
                assistant(tools=[("Bash", {"description": "the real work"})]),
                assistant(
                    model="<synthetic>", tools=[("Bash", {"description": "bookkeeping"})]
                ),
            ]
        )
        self.assertEqual("Bash: the real work", session["activity"])

    def test_a_toolless_turn_does_not_clear_an_earlier_activity(self):
        # Only a tool call replaces the activity; a prose-only reply leaves the
        # last known one standing rather than blanking the line.
        session = self.one_session(
            [
                assistant(tools=[("Bash", {"description": "the real work"})]),
                assistant(text="All done."),
            ]
        )
        self.assertEqual("Bash: the real work", session["activity"])

    def test_long_details_are_truncated(self):
        session = self.one_session([assistant(tools=[("Bash", {"description": "d" * 200})])])
        self.assertEqual(len("Bash: ") + 81, len(session["activity"]))
        self.assertTrue(session["activity"].endswith("…"))

    def test_multi_line_details_are_collapsed_to_one_line(self):
        session = self.one_session(
            [assistant(tools=[("Bash", {"description": "line one\n\n  line two  "})])]
        )
        self.assertEqual("Bash: line one line two", session["activity"])

    def test_whitespace_only_detail_falls_back_to_the_bare_tool_name(self):
        # Truthy before truncation, None after: reporting "Bash: None" would be
        # worse than reporting nothing at all.
        session = self.one_session(
            [assistant_blocks([tool_use("Bash", {"description": "  \n\t "})])]
        )
        self.assertEqual("Bash", session["activity"])

    def test_multi_line_tool_names_are_collapsed_to_one_line(self):
        # activity is a single-line field. Every other string this module emits
        # is guaranteed single-line because it passes through _truncate; the
        # tool name is read from another program's format and must too.
        session = self.one_session(
            [assistant_blocks([tool_use("Read\nmore", {"file_path": "/x"})])]
        )
        self.assertEqual("Read more: /x", session["activity"])

    def test_long_tool_names_are_truncated(self):
        # MCP tool names run long in this environment — 51 characters for
        # mcp__plugin_playwright_playwright__browser_navigate — so an uncapped
        # name would blow the activity column well past the detail budget.
        session = self.one_session([assistant_blocks([tool_use("n" * 200, {})])])
        self.assertEqual(81, len(session["activity"]))
        self.assertTrue(session["activity"].endswith("…"))


class _ExplodingFile:
    """File-like object that yields some lines and then fails mid-read.

    Models a transcript on a failing or network-backed mount: open() succeeds,
    the OSError only surfaces while iterating.
    """

    def __init__(self, lines):
        self._lines = lines

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def __iter__(self):
        yield from self._lines
        raise OSError(5, "Input/output error")


class UnreadableTranscriptTest(StoreTestCase):
    """The bytes will not read or will not parse: fall through to defaults.

    session_store reads another program's private format, so every class below
    pins the same overall rule from a different angle: it must never raise.
    """

    def test_unreadable_transcript_falls_back_to_defaults(self):
        # A directory named <uuid>.jsonl makes open() raise IsADirectoryError.
        # chmod would be useless here: tests may run as root.
        os.makedirs(os.path.join(self.home, "projects", "-workspace", SESSION_A + ".jsonl"))
        session = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertEqual(SESSION_A, session["id"])
        self.assertEqual("(untitled)", session["name"])
        self.assertEqual("none", session["nameSource"])

    def test_read_error_part_way_through_keeps_what_was_parsed(self):
        path = write_transcript(self.home, SESSION_A, [ai_title("Partial title")])
        with open(path, "r", encoding="utf-8") as handle:
            lines = handle.readlines()
        with mock.patch("builtins.open", return_value=_ExplodingFile(lines)):
            record = session_store.parse_transcript(path)
        self.assertEqual(
            {
                "name": "Partial title",
                "nameSource": "ai-title",
                "messages": 0,
                "lastPrompt": None,
                "contextTokens": None,
                "activity": None,
            },
            record,
        )

    def test_non_dict_json_line_is_skipped(self):
        # A bare JSON array still contains the candidate substring, so it reaches
        # json.loads() and must be rejected on shape, not crash.
        session = self.one_session([[user("array wrapped")], user("real prompt")])
        self.assertEqual("real prompt", session["name"])

    def test_malformed_json_line_is_skipped(self):
        path = write_transcript(self.home, SESSION_A, [user("real prompt")])
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type":"ai-title","aiTitle":"Truncated tai\n')
        session = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertEqual("real prompt", session["name"])
        self.assertEqual("first-prompt", session["nameSource"])

    def test_invalid_utf8_byte_does_not_sink_the_parse(self):
        directory = os.path.join(self.home, "projects", "-workspace")
        os.makedirs(directory, exist_ok=True)
        with open(os.path.join(directory, SESSION_A + ".jsonl"), "wb") as handle:
            handle.write(b'{"type":"ai-title","aiTitle":"caf\xe9 corrupt"}\n')
        session = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertEqual("caf� corrupt", session["name"])  # errors="replace"
        self.assertEqual("ai-title", session["nameSource"])


class MalformedEntryFieldTest(StoreTestCase):
    """An entry parses, but a field it carries has the wrong type."""

    def test_non_string_ai_title_is_ignored(self):
        session = self.one_session([ai_title(123)])
        self.assertEqual("(untitled)", session["name"])
        self.assertEqual("none", session["nameSource"])

    def test_non_string_ai_title_does_not_replace_a_good_one(self):
        session = self.one_session([ai_title("Good title"), ai_title(["nope"])])
        self.assertEqual("Good title", session["name"])
        self.assertEqual("ai-title", session["nameSource"])

    def test_non_string_last_prompt_is_ignored(self):
        session = self.one_session([{"type": "last-prompt", "lastPrompt": 123}])
        self.assertIsNone(session["lastPrompt"])

    def test_non_string_last_prompt_does_not_replace_a_good_one(self):
        session = self.one_session(
            [
                {"type": "last-prompt", "lastPrompt": "Good prompt"},
                {"type": "last-prompt", "lastPrompt": {"nope": True}},
            ]
        )
        self.assertEqual("Good prompt", session["lastPrompt"])

    def test_non_dict_message_is_ignored(self):
        session = self.one_session(
            [{"type": "user", "message": "hello"}, user("real prompt")]
        )
        self.assertEqual("real prompt", session["name"])

    def test_non_dict_assistant_message_is_ignored(self):
        session = self.one_session(
            [{"type": "assistant", "message": "hello"}, assistant(usage=USAGE)]
        )
        self.assertEqual(2, session["messages"])
        self.assertEqual(63883, session["contextTokens"])


class MalformedUsageTest(StoreTestCase):
    """Neither the usage container nor its values can be trusted to be numeric."""

    def test_non_dict_usage_is_ignored(self):
        session = self.one_session([assistant(usage="lots")])
        self.assertIsNone(session["contextTokens"])

    def test_string_usage_value_is_ignored(self):
        # Being robust to the container's type but not its contents is the
        # inconsistent half of the guard: "4" + 63869 raises TypeError.
        session = self.one_session(
            [assistant(usage={"input_tokens": "4", "cache_read_input_tokens": 63869})]
        )
        self.assertEqual(63869, session["contextTokens"])

    def test_other_non_numeric_usage_values_are_ignored(self):
        # One branch, one test: dict, list and null all take the identical
        # `not isinstance(value, (int, float))` path. Only bool below exercises
        # a distinct clause, and the string case above earns its own name as
        # the historical regression.
        for value in ({"total": 4}, [4], None):
            with self.subTest(value=value):
                session = self.one_session(
                    [assistant(usage={
                        "input_tokens": value,
                        "cache_read_input_tokens": 63869,
                    })]
                )
                self.assertEqual(63869, session["contextTokens"])

    def test_boolean_usage_value_is_ignored(self):
        # isinstance(True, int) is True in Python, so a bool would otherwise be
        # counted as one token. A boolean token count is meaningless.
        session = self.one_session(
            [assistant(usage={"input_tokens": True, "cache_read_input_tokens": 63869})]
        )
        self.assertEqual(63869, session["contextTokens"])

    def test_a_non_finite_usage_value_is_ignored(self):
        # json.load accepts NaN, Infinity and 1e999 by default and all three are
        # floats, so they pass the isinstance check. The same guard _updated_at
        # already carries, for the same reason: a non-finite number is not a
        # usable one. NaN is truthy too, so `total or None` would pass it on.
        for value in (float("nan"), float("inf"), float("-inf")):
            with self.subTest(value=value):
                session = self.one_session(
                    [assistant(usage={
                        "input_tokens": value,
                        "cache_read_input_tokens": 63869,
                    })]
                )
                self.assertEqual(63869, session["contextTokens"])

    def test_an_integer_too_large_for_a_float_is_still_counted(self):
        # json parses a 400-digit literal as an int, and math.isfinite() on one
        # raises OverflowError trying to convert it — a raise of exactly the
        # kind the non-finite guard exists to prevent. Only a float can be NaN
        # or Infinity, so only a float is asked. A huge int is finite by
        # construction, is counted like any other number, and JSON has no
        # trouble carrying it.
        huge = 10 ** 400
        session = self.one_session(
            [assistant(usage={"input_tokens": huge, "cache_read_input_tokens": 10})]
        )
        self.assertEqual(huge + 10, session["contextTokens"])
        json.dumps(session, allow_nan=False)

    def test_a_row_carrying_a_non_finite_usage_still_serialises(self):
        # The failure this really guards against is one layer up: json.dumps
        # emits bare NaN/Infinity tokens, which are not valid JSON, and the
        # browser's JSON.parse throws a SyntaxError on the *whole* response —
        # one bad usage value empties the admin page exactly as an escaping
        # exception would. allow_nan=False is what the HTTP layer effectively
        # does, so serialising the row under it is the assertion.
        session = self.one_session([assistant(usage={"input_tokens": float("nan")})])
        self.assertIsNone(session["contextTokens"])
        json.dumps(session, allow_nan=False)


class MalformedToolBlockTest(StoreTestCase):
    """The assistant content the activity line is derived from is misshapen."""

    def test_non_list_assistant_content_is_ignored(self):
        # A number, not a string, on purpose: a string is iterable, so without
        # the isinstance(content, list) guard reversed() would walk its
        # characters, each would fail the dict check, and the result would be
        # None anyway — the guard could be deleted and the test would not
        # notice. Only a value reversed() refuses justifies the guard.
        session = self.one_session([assistant_blocks(5)])
        self.assertIsNone(session["activity"])
        self.assertEqual(1, session["messages"])

    def test_string_assistant_content_is_ignored(self):
        session = self.one_session([assistant_blocks("just a string")])
        self.assertIsNone(session["activity"])
        self.assertEqual(1, session["messages"])

    def test_non_dict_content_block_is_skipped(self):
        # Last block first: a bare string reached before the tool_use would
        # raise AttributeError off .get() without the shape guard.
        session = self.one_session(
            [assistant_blocks([tool_use("Bash", {"description": "real work"}), "junk"])]
        )
        self.assertEqual("Bash: real work", session["activity"])

    def test_non_dict_tool_input_is_ignored(self):
        session = self.one_session([assistant_blocks([tool_use("Bash", "ls -la")])])
        self.assertEqual("Bash", session["activity"])

    def test_non_string_tool_description_is_ignored(self):
        # A truthy non-string wins the `or` and reaches _truncate, which absorbs
        # it. Before _truncate was total, .split() raised AttributeError here —
        # and that escapes the read guard and empties the whole session list.
        session = self.one_session([assistant_blocks([tool_use("Bash", {"description": 123})])])
        self.assertEqual("Bash", session["activity"])

    def test_non_string_file_path_is_ignored(self):
        session = self.one_session(
            [assistant_blocks([tool_use("Read", {"file_path": {"path": "/etc/hosts"}})])]
        )
        self.assertEqual("Read", session["activity"])

    def test_non_string_tool_name_falls_back_to_a_placeholder(self):
        session = self.one_session(
            [assistant_blocks([tool_use(["Bash"], {"description": "real work"})])]
        )
        self.assertEqual("tool: real work", session["activity"])

    def test_missing_tool_name_falls_back_to_a_placeholder(self):
        session = self.one_session(
            [assistant_blocks([{"type": "tool_use", "input": {"description": "real work"}}])]
        )
        self.assertEqual("tool: real work", session["activity"])


class LivenessTest(StoreTestCase):
    """Which sessions are actually running, per <home>/sessions/<pid>.json.

    Two facts about that registry, both established from real files in a
    running container, drive every case here:

    * A registry file outlives the process it describes. ttyd SIGHUPs its child
      on disconnect without cleaning up, so the file alone proves nothing —
      the pid has to actually exist.
    * Several files can name the same session across restarts, so the one with
      the highest updatedAt wins.

    A false positive is the expensive direction: the admin page uses `live` to
    decide whether a session can be resumed, so a stale file read as live
    offers a session that can never be reopened, and a live one read as dead
    invites a second Claude onto a transcript that is already open.

    Names below are chosen so that filename sort order and updatedAt order
    disagree — the loop walks sorted(names), so anything that lets position
    decide the winner instead of the timestamp has to fail here.
    """

    # "205.json" < "2118.json" as strings ('0' < '1' at the third character),
    # which is also how the two real files in this container are named.
    FIRST = "205.json"
    LAST = "2118.json"

    def session(self, session_id=SESSION_A):
        """The record for one session that does have a transcript."""
        write_transcript(self.home, session_id, [ai_title("A title")])
        found = {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }
        return found[session_id]

    def test_a_live_pid_marks_the_session_live(self):
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        session = self.session()
        self.assertTrue(session["live"])
        self.assertEqual("busy", session["status"])
        self.assertEqual(os.getpid(), session["pid"])
        self.assertIsNone(session["waitingFor"])

    def test_a_dead_pid_is_not_live(self):
        write_registry(
            self.home, self.FIRST, pid=dead_pid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        session = self.session()
        self.assertFalse(session["live"])
        self.assertIsNone(session["status"])
        self.assertIsNone(session["pid"])
        self.assertIsNone(session["waitingFor"])

    def test_a_session_with_no_registry_file_is_not_live(self):
        session = self.session()
        self.assertFalse(session["live"])
        self.assertIsNone(session["status"])
        self.assertIsNone(session["pid"])

    def test_the_freshest_registry_file_wins_when_it_sorts_first(self):
        # Both entries are live, so only updatedAt can separate them. Here the
        # freshest is also the first name walked: an implementation that kept
        # the last file seen would report "idle".
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESHER,
        )
        write_registry(
            self.home, self.LAST, pid=os.getpid(), sessionId=SESSION_A,
            status="idle", updatedAt=FRESH,
        )
        self.assertEqual("busy", self.session()["status"])

    def test_the_freshest_registry_file_wins_when_it_sorts_last(self):
        # The mirror image: an implementation that kept the first file seen
        # would report "idle" here.
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="idle", updatedAt=FRESH,
        )
        write_registry(
            self.home, self.LAST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESHER,
        )
        self.assertEqual("busy", self.session()["status"])

    def test_a_stale_dead_entry_does_not_hide_a_live_one(self):
        # The real-data shape: an abandoned file from an earlier run sits
        # alongside the current one, naming the same session.
        gone = dead_pid()
        write_registry(
            self.home, self.FIRST, pid=gone, sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        write_registry(
            self.home, self.LAST, pid=os.getpid(), sessionId=SESSION_A,
            status="idle", updatedAt=FRESHER,
        )
        session = self.session()
        self.assertTrue(session["live"])
        self.assertEqual(os.getpid(), session["pid"])
        self.assertEqual("idle", session["status"])

    def test_a_fresher_dead_entry_does_not_hide_a_live_one(self):
        # And the reverse: the newest file on disk is the dead one. Liveness is
        # decided before freshness, so the surviving process still wins — an
        # implementation that picked the freshest file and only then tested its
        # pid would call this session dead.
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="idle", updatedAt=FRESH,
        )
        write_registry(
            self.home, self.LAST, pid=dead_pid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESHER,
        )
        session = self.session()
        self.assertTrue(session["live"])
        self.assertEqual(os.getpid(), session["pid"])
        self.assertEqual("idle", session["status"])

    def test_waiting_for_is_carried_through(self):
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="waiting", waitingFor="input needed", updatedAt=FRESH,
        )
        session = self.session()
        self.assertEqual("waiting", session["status"])
        self.assertEqual("input needed", session["waitingFor"])

    def test_liveness_is_per_session(self):
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        write_transcript(self.home, SESSION_A, [ai_title("Live one")])
        write_transcript(self.home, SESSION_B, [ai_title("Dead one")])
        found = {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }
        self.assertTrue(found[SESSION_A]["live"])
        self.assertFalse(found[SESSION_B]["live"])

    def test_missing_sessions_directory_is_not_an_error(self):
        self.assertEqual({}, session_store.live_sessions(self.home))
        self.assertFalse(self.session()["live"])

    def test_corrupt_registry_file_is_skipped(self):
        path = write_registry(
            self.home, self.LAST, pid=os.getpid(), sessionId=SESSION_A, updatedAt=FRESH
        )
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"pid":205,"sessionId":"trunc')
        self.assertEqual({}, session_store.live_sessions(self.home))

    def test_registry_file_that_is_not_an_object_is_skipped(self):
        path = write_registry(self.home, self.LAST, pid=os.getpid())
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('[{"pid":205,"sessionId":"%s"}]' % SESSION_A)
        self.assertEqual({}, session_store.live_sessions(self.home))

    def test_registry_entry_without_a_session_id_is_ignored(self):
        write_registry(self.home, self.LAST, pid=os.getpid(), status="busy", updatedAt=FRESH)
        self.assertEqual({}, session_store.live_sessions(self.home))

    def test_a_registry_naming_a_session_with_no_transcript_does_not_appear(self):
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_B,
            status="busy", updatedAt=FRESH,
        )
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual([], sessions)

    def test_non_json_files_in_the_registry_are_ignored(self):
        # The real directory holds <pid>.<hash>.key files next to the JSON.
        # This one is given registry-shaped, perfectly parsable content on
        # purpose: unparsable bytes would be dropped by the json guard anyway,
        # so only a readable non-.json file can show that the name filter is
        # doing the work. What a file is named decides whether it is a
        # registry, not whether its bytes happen to parse.
        write_registry(self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
                       status="busy", updatedAt=FRESH)
        write_registry(self.home, "205.abc123.key", pid=os.getpid(),
                       sessionId=SESSION_B, status="busy", updatedAt=FRESHER)
        self.assertEqual([SESSION_A], list(session_store.live_sessions(self.home)))

    def test_a_non_integer_pid_is_not_live(self):
        # True is the trap: isinstance(True, int) is True in Python, so an
        # unguarded bool would test pid 1 — which always exists in a container.
        for pid in ("205", None, 205.0, True, {"pid": 205}):
            with self.subTest(pid=pid):
                write_registry(
                    self.home, self.FIRST, pid=pid, sessionId=SESSION_A,
                    status="busy", updatedAt=FRESH,
                )
                self.assertEqual({}, session_store.live_sessions(self.home))

    def test_a_non_positive_pid_is_not_live(self):
        # 0 and negatives address process groups rather than a process, so
        # os.kill would answer about the caller's own group and report a dead
        # session as live.
        for pid in (0, -1, -os.getpid()):
            with self.subTest(pid=pid):
                write_registry(
                    self.home, self.FIRST, pid=pid, sessionId=SESSION_A,
                    status="busy", updatedAt=FRESH,
                )
                self.assertEqual({}, session_store.live_sessions(self.home))

    def test_a_pid_owned_by_another_user_counts_as_live(self):
        # os.kill raises PermissionError for a process this user may not
        # signal. That is proof the process exists, not proof it is gone.
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        with mock.patch("os.kill", side_effect=PermissionError(1, "not permitted")):
            self.assertTrue(self.session()["live"])

    def test_a_non_numeric_updated_at_does_not_raise(self):
        # updatedAt is foreign JSON and is used in a >= comparison, so a string
        # there raises TypeError — which escapes list_sessions and empties the
        # whole list. Both entries are live so the comparison is really reached.
        write_registry(
            self.home, self.FIRST, pid=os.getpid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        write_registry(
            self.home, self.LAST, pid=os.getpid(), sessionId=SESSION_A,
            status="idle", updatedAt="soon",
        )
        # The unusable timestamp sorts below any real one, so the good entry
        # stands rather than being displaced by a value nothing can order.
        self.assertEqual("busy", self.session()["status"])


class BlastRadiusTest(StoreTestCase):
    """One sick transcript must not take the healthy ones down with it.

    A different property at a different level from the classes above: these go
    through list_sessions() with two transcripts and check that the healthy one
    still comes back. parse_transcript() is called without a try, so anything
    that escapes it empties the entire list, not just one row.

    Transcript-side only. The same property on the registry side belongs to
    RegistryRobustnessTest, so that each of the two has one unambiguous home.
    """

    def test_a_non_numeric_usage_value_does_not_sink_the_whole_session_list(self):
        # The blast radius that matters: a TypeError from one transcript's usage
        # escapes the read guard and empties the list for every healthy session.
        write_transcript(self.home, SESSION_A, [assistant(usage={"input_tokens": "4"})])
        write_transcript(self.home, SESSION_B, [assistant(usage=USAGE)])
        sessions = {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertEqual(63883, sessions[SESSION_B]["contextTokens"])
        self.assertIsNone(sessions[SESSION_A]["contextTokens"])

    def test_a_malformed_tool_block_does_not_sink_the_whole_session_list(self):
        # The blast radius that matters: an exception raised while deriving one
        # session's activity escapes the read guard and empties the list for
        # every healthy session too.
        write_transcript(
            self.home,
            SESSION_A,
            [assistant_blocks([tool_use("Bash", {"description": 123}), "junk"])],
        )
        write_transcript(
            self.home, SESSION_B, [assistant(tools=[("Bash", {"description": "healthy"})])]
        )
        sessions = {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertEqual("Bash: healthy", sessions[SESSION_B]["activity"])
        self.assertEqual("Bash", sessions[SESSION_A]["activity"])

    def test_one_bad_entry_does_not_sink_the_whole_session_list(self):
        write_transcript(self.home, SESSION_A, [ai_title(123)])
        write_transcript(self.home, SESSION_B, [ai_title("Healthy session")])
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual({SESSION_A, SESSION_B}, {s["id"] for s in sessions})

    def test_deeply_nested_transcript_json_does_not_sink_the_whole_list(self):
        # The transcript-side half of the registry test of the same name:
        # json.loads raises RecursionError on deep nesting, and RecursionError
        # is not a ValueError, so the guard around it would miss one. The
        # prefix is what carries the line past the candidate prefilter — an
        # unparsed line is skipped before json.loads ever sees it.
        path = write_transcript(self.home, SESSION_A, [])
        with open(path, "w", encoding="utf-8") as handle:
            handle.write('{"type":"assistant","message":{"content":' + "[" * 100_000)
        write_transcript(self.home, SESSION_B, [ai_title("Healthy session")])
        sessions = {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertEqual("Healthy session", sessions[SESSION_B]["name"])
        self.assertEqual("(untitled)", sessions[SESSION_A]["name"])

    def test_a_dangling_transcript_symlink_does_not_sink_the_whole_list(self):
        # The stat in list_sessions() is outside parse_transcript's own guards,
        # so its OSError has the same total blast radius. Two ways here: a
        # transcript symlinked at a target that is gone (os.stat follows the
        # link and raises FileNotFoundError), and the window between the
        # directory scan and the stat when a session is deleted mid-poll.
        write_transcript(self.home, SESSION_B, [ai_title("Healthy session")])
        directory = os.path.join(self.home, "projects", "-workspace")
        os.symlink(
            os.path.join(directory, "gone.jsonl"),
            os.path.join(directory, SESSION_A + ".jsonl"),
        )
        sessions = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual({SESSION_B}, {s["id"] for s in sessions})
        self.assertEqual("Healthy session", sessions[0]["name"])


def live_pid(test):
    """A pid that is certainly alive, and certainly started just now.

    The mirror image of dead_pid(). The child blocks reading stdin, so it stays
    alive until the test closes the pipe — and closing the pipe is how it is
    stopped, because no signal other than 0 is ever sent to any pid in this
    module. Its start time is *now*, which is what the pid-reuse cross-check
    compares registry timestamps against.
    """
    process = subprocess.Popen(
        [sys.executable, "-c", "import sys; sys.stdin.read()"], stdin=subprocess.PIPE
    )

    def stop():
        process.stdin.close()  # EOF: the child returns from read() and exits
        process.wait()

    test.addCleanup(stop)
    return process.pid


def fake_proc(files):
    """Patch open() so reads under /proc come from `files`; everything else is real.

    A value may be a string (the file's contents) or an exception to raise.
    Anything under /proc that is not listed raises ENOENT. Only /proc is
    diverted: live_sessions() reads the registry through the same open().
    """
    real_open = open

    def opener(path, *args, **kwargs):
        name = path if isinstance(path, str) else ""
        if name.startswith("/proc"):
            value = files.get(name, OSError(2, "no such file or directory"))
            if isinstance(value, Exception):
                raise value
            return io.StringIO(value)
        return real_open(path, *args, **kwargs)

    return mock.patch("builtins.open", side_effect=opener)


def proc_stat_line(pid, comm, ticks):
    """One /proc/<pid>/stat line: pid, comm, state, 18 fillers, then starttime.

    starttime is field 22. comm is field 2 and is unquoted and unescaped, so it
    is only findable by its closing parenthesis — the whole point of this
    fixture is that it contains both spaces and a parenthesis.
    """
    fillers = " ".join(str(i) for i in range(18))
    return "%d (%s) S %s %d\n" % (pid, comm, fillers, ticks)


class PidReuseTest(StoreTestCase):
    """A live pid is not enough: it has to be the *same* process.

    <home>/sessions lives in a persistent volume while pids restart from low
    numbers on every container boot, so old registry files routinely name pids
    that some unrelated process has since been given. Observed in this
    container: 226.json, 232.json and 233.json each named a pid that existed —
    and the sessions they named were reported live and therefore unresumable,
    which is the expensive direction of this error.

    A cmdline check would not catch it (the colliding processes are themselves
    started by the entrypoint), but time does: a process cannot have been
    updated before it started. The real gaps are 53-63 days, so the check is
    given a minute of tolerance and falls back to "alive" on any doubt at all —
    a false *dead* would invite a second Claude onto an open transcript.
    """

    def registry(self, pid, **fields):
        write_registry(
            self.home, "205.json", pid=pid, sessionId=SESSION_A, status="busy", **fields
        )

    def test_a_registry_older_than_the_process_is_not_live(self):
        # The real-data shape: the entry predates its pid by two months.
        self.registry(live_pid(self), updatedAt=FRESH - 60 * 86400 * 1000)
        self.assertEqual({}, session_store.live_sessions(self.home))

    def test_a_stale_entry_leaves_no_liveness_on_the_row(self):
        write_transcript(self.home, SESSION_A, [ai_title("A title")])
        self.registry(live_pid(self), updatedAt=FRESH - 60 * 86400 * 1000)
        session = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertFalse(session["live"])
        self.assertIsNone(session["pid"])
        self.assertIsNone(session["status"])

    def test_a_registry_updated_after_the_process_started_is_live(self):
        pid = live_pid(self)
        self.registry(pid, updatedAt=int(time.time() * 1000))
        self.assertEqual(pid, session_store.live_sessions(self.home)[SESSION_A]["pid"])

    def test_a_registry_a_moment_older_than_the_process_is_still_live(self):
        # Clock skew and second-vs-millisecond rounding must never make a
        # running session unresumable, so the check tolerates a whole minute.
        # The real collisions are 53-63 days out, so that costs nothing.
        pid = live_pid(self)
        self.registry(pid, updatedAt=int(time.time() * 1000) - 30_000)
        self.assertEqual(pid, session_store.live_sessions(self.home)[SESSION_A]["pid"])

    def test_an_unknown_updated_at_is_not_treated_as_ancient(self):
        # _updated_at maps a missing or unusable timestamp to 0. That is
        # "unknown", not "1970": it must not be evidence of pid reuse.
        for fields in ({}, {"updatedAt": 0}, {"updatedAt": "soon"}):
            with self.subTest(fields=fields):
                self.registry(live_pid(self), **fields)
                self.assertIn(SESSION_A, session_store.live_sessions(self.home))

    def test_an_unreadable_proc_falls_back_to_live(self):
        self.registry(live_pid(self), updatedAt=FRESH - 60 * 86400 * 1000)
        with fake_proc({}):  # every /proc path raises ENOENT
            self.assertIn(SESSION_A, session_store.live_sessions(self.home))

    def test_an_unparsable_proc_stat_falls_back_to_live(self):
        pid = live_pid(self)
        self.registry(pid, updatedAt=FRESH - 60 * 86400 * 1000)
        broken = {
            "/proc/stat": "btime nonsense\n",
            "/proc/%d/stat" % pid: proc_stat_line(pid, "claude", 500),
        }
        with fake_proc(broken):
            self.assertIn(SESSION_A, session_store.live_sessions(self.home))
        missing_btime = {
            "/proc/stat": "cpu 1 2 3\nprocesses 40\n",
            "/proc/%d/stat" % pid: proc_stat_line(pid, "claude", 500),
        }
        with fake_proc(missing_btime):
            self.assertIn(SESSION_A, session_store.live_sessions(self.home))
        truncated = {
            "/proc/stat": "btime 1000000\n",
            "/proc/%d/stat" % pid: "%d (claude) S 1 2 3\n" % pid,
        }
        with fake_proc(truncated):
            self.assertIn(SESSION_A, session_store.live_sessions(self.home))

    def test_a_comm_with_spaces_and_parentheses_is_parsed_correctly(self):
        # Real data: the pids that collided belonged to threads named
        # "Bun Pool 7" and "HTTP Client". comm is field 2 of a space-separated
        # line and is neither quoted nor escaped, so splitting the whole line
        # reads a filler field as starttime and computes a start time barely
        # after boot — which would let this two-month-old entry pass.
        pid = live_pid(self)
        hz = os.sysconf("SC_CLK_TCK")
        started = 1_000_000 + 1_000_000  # btime + ticks/HZ, in Unix seconds
        self.registry(pid, updatedAt=(started - 500_000) * 1000)
        for comm in ("Bun Pool 7", "HTTP Client", "a (parenthesised) name"):
            with self.subTest(comm=comm):
                with fake_proc({
                    "/proc/stat": "cpu 1 2 3\nbtime 1000000\nprocesses 40\n",
                    "/proc/%d/stat" % pid: proc_stat_line(pid, comm, 1_000_000 * hz),
                }):
                    self.assertEqual({}, session_store.live_sessions(self.home))

    def test_liveness_is_still_read_once_for_the_whole_list(self):
        # The cross-check adds two file reads per candidate registry entry, so
        # it must stay inside the single liveness scan rather than becoming a
        # per-session lookup.
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Two")])
        with mock.patch.object(
            session_store, "live_sessions", wraps=session_store.live_sessions
        ) as scan:
            session_store.SessionStore(self.home).list_sessions()
        self.assertEqual(1, scan.call_count)


class RegistryRobustnessTest(StoreTestCase):
    """live_sessions() reads foreign JSON once for the entire list.

    Anything it raises escapes list_sessions() and empties every row, healthy
    or not — the widest blast radius in the module, which is why each of these
    checks the healthy session still comes back rather than only that no
    exception was raised.
    """

    def healthy(self):
        write_registry(
            self.home, "9.json", pid=os.getpid(), sessionId=SESSION_B,
            status="busy", updatedAt=FRESH,
        )
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Two")])

    def rows(self):
        return {
            s["id"]: s for s in session_store.SessionStore(self.home).list_sessions()
        }

    def test_an_unhashable_session_id_is_ignored(self):
        # A list or dict sessionId passes the truthiness test and then raises
        # TypeError: unhashable type when used as a dict key.
        for session_id in (["x"], {"a": 1}, 205, 1.5, True):
            with self.subTest(session_id=session_id):
                write_registry(
                    self.home, "1.json", pid=os.getpid(), sessionId=session_id,
                    status="busy", updatedAt=FRESH,
                )
                self.assertEqual({}, session_store.live_sessions(self.home))

    def test_an_unhashable_session_id_does_not_sink_the_whole_list(self):
        self.healthy()
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=["x"],
            status="busy", updatedAt=FRESH,
        )
        rows = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(rows))
        self.assertTrue(rows[SESSION_B]["live"])

    def test_a_pid_too_large_for_the_kernel_does_not_sink_the_whole_list(self):
        # os.kill(2**70, 0) raises OverflowError, which is not an OSError.
        self.healthy()
        write_registry(
            self.home, "1.json", pid=2 ** 70, sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        rows = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(rows))
        self.assertFalse(rows[SESSION_A]["live"])
        self.assertTrue(rows[SESSION_B]["live"])

    def test_deeply_nested_registry_json_does_not_sink_the_whole_list(self):
        # json.load raises RecursionError on deep nesting, and RecursionError
        # is not a ValueError, so the parse guard alone would miss it.
        self.healthy()
        path = write_registry(self.home, "1.json", pid=os.getpid())
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("[" * 100_000)
        rows = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(rows))
        self.assertTrue(rows[SESSION_B]["live"])

    def test_an_infinite_updated_at_cannot_beat_a_real_one(self):
        # json.load accepts Infinity by default, and it beats every real
        # timestamp, so a malformed file would always win the freshness race.
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status="real", updatedAt=FRESH,
        )
        write_registry(
            self.home, "2.json", pid=os.getpid(), sessionId=SESSION_A,
            status="displaced", updatedAt=float("inf"),
        )
        self.assertEqual("real", session_store.live_sessions(self.home)[SESSION_A]["status"])

    def test_a_nan_updated_at_cannot_displace_a_real_one(self):
        # NaN is worse than Infinity: every comparison against it is False, so
        # it loses to whatever follows it and displaces whatever precedes it —
        # the winner becomes a function of filename order.
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status="real", updatedAt=FRESH,
        )
        write_registry(
            self.home, "2.json", pid=os.getpid(), sessionId=SESSION_A,
            status="displaced", updatedAt=float("nan"),
        )
        self.assertEqual("real", session_store.live_sessions(self.home)[SESSION_A]["status"])

    def test_a_non_string_status_or_waiting_for_is_dropped(self):
        # Both are passed to the UI. Every other outward-facing string in the
        # module goes through _truncate, which returns None for a non-string.
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status={"state": "busy"}, waitingFor=["input"], updatedAt=FRESH,
        )
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        row = session_store.SessionStore(self.home).list_sessions()[0]
        self.assertTrue(row["live"])
        self.assertIsNone(row["status"])
        self.assertIsNone(row["waitingFor"])

    def test_an_overlong_status_is_bounded_like_every_other_ui_string(self):
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status="busy " * 100, updatedAt=FRESH,
        )
        status = session_store.live_sessions(self.home)[SESSION_A]["status"]
        self.assertTrue(status.endswith("…"))
        self.assertLessEqual(len(status), 81)

    def test_a_corrupt_registry_file_does_not_sink_the_whole_session_list(self):
        # Liveness is read once for the whole list, so a single unreadable
        # registry file has the widest blast radius of anything in this module.
        path = write_registry(self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("}{ not json")
        write_registry(
            self.home, "2.json", pid=os.getpid(), sessionId=SESSION_B,
            status="busy", updatedAt=FRESH,
        )
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Two")])
        sessions = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertTrue(sessions[SESSION_B]["live"])
        self.assertFalse(sessions[SESSION_A]["live"])

    def test_a_non_numeric_updated_at_does_not_sink_the_whole_session_list(self):
        # A string updatedAt raises TypeError from the freshness comparison,
        # which empties the list for every healthy session too.
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A, updatedAt=FRESH
        )
        write_registry(
            self.home, "2.json", pid=os.getpid(), sessionId=SESSION_A, updatedAt="soon"
        )
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Healthy session")])
        sessions = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertTrue(sessions[SESSION_A]["live"])

    def test_an_enormous_updated_at_does_not_sink_the_whole_session_list(self):
        # json parses a long digit run as an int, and math.isfinite() on an int
        # too large for a float raises OverflowError — so the guard against
        # NaN/Infinity became the failure it was written to prevent. The same
        # shape was caught in _token_count; this is its sibling.
        write_registry(
            self.home,
            "1.json",
            pid=os.getpid(),
            sessionId=SESSION_A,
            updatedAt=int("1" + "0" * 400),
        )
        write_transcript(self.home, SESSION_A, [ai_title("One")])
        write_transcript(self.home, SESSION_B, [ai_title("Healthy session")])
        sessions = self.rows()
        self.assertEqual({SESSION_A, SESSION_B}, set(sessions))
        self.assertEqual("Healthy session", sessions[SESSION_B]["name"])


class FreshnessTieTest(StoreTestCase):
    """Equal timestamps: the first filename in sort order wins, and stays winning.

    Ties are reachable three ways — genuinely equal values, both timestamps
    missing, and both unusable (the guard maps either to 0, so it *creates*
    ties) — and until now nothing pinned which entry survived one.
    """

    def statuses(self, first, second, **fields):
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status=first, **fields,
        )
        write_registry(
            self.home, "2.json", pid=os.getpid(), sessionId=SESSION_A,
            status=second, **fields,
        )
        return session_store.live_sessions(self.home)[SESSION_A]["status"]

    def test_equal_timestamps_are_resolved_by_filename_order(self):
        self.assertEqual("first", self.statuses("first", "second", updatedAt=FRESH))

    def test_two_missing_timestamps_tie_and_are_resolved_the_same_way(self):
        self.assertEqual("first", self.statuses("first", "second"))

    def test_two_unusable_timestamps_tie_and_are_resolved_the_same_way(self):
        # The guard maps both to 0, so entries that were never comparable in
        # the file end up exactly equal here.
        self.assertEqual(
            "first", self.statuses("first", "second", updatedAt="soon")
        )


class CacheTest(StoreTestCase):
    """Transcript parses are cached per store, keyed on (mtime_ns, size).

    The admin page polls every few seconds, so without a cache every poll
    re-reads every transcript — including the multi-megabyte ones. The tests
    below pin both halves of that bargain: an unchanged file is parsed once,
    and *any* change the key can see puts a fresh parse back on the table.

    Timestamps are set with explicit os.utime(ns=...) values throughout.
    Waiting for the wall clock to tick would make these tests a function of
    the filesystem's timestamp granularity, which is exactly the flakiness
    this cache's key is most exposed to.

    One gap is deliberate: that mtime differences finer than a second are seen
    is not separately pinned, because it is not portably observable — /workspace
    is a 9p mount with one-second granularity while ext4 keeps nanoseconds — and
    the size half of the key is what covers a coarse-granularity mount anyway.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            session_store, "parse_transcript", wraps=session_store.parse_transcript
        )
        self.parse = patcher.start()
        self.addCleanup(patcher.stop)

    def write(self, entries, session_id=SESSION_A, mtime_ns=None):
        """Write a transcript, optionally pinning its mtime exactly."""
        path = write_transcript(self.home, session_id, entries)
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def append(self, path, entries, mtime_ns=None):
        with open(path, "a", encoding="utf-8") as handle:
            for entry in entries:
                handle.write(json.dumps(entry, separators=(",", ":")) + "\n")
        if mtime_ns is not None:
            os.utime(path, ns=(mtime_ns, mtime_ns))
        return path

    def test_an_unchanged_transcript_is_parsed_once_across_polls(self):
        self.write([ai_title("stable"), user("hello")])
        store = session_store.SessionStore(self.home)
        names = [store.list_sessions()[0]["name"] for _ in range(3)]
        self.assertEqual(["stable"] * 3, names)
        self.assertEqual(1, self.parse.call_count)

    def test_a_changed_transcript_is_reparsed(self):
        path = self.write([ai_title("before"), user("hello")])
        store = session_store.SessionStore(self.home)
        self.assertEqual("before", store.list_sessions()[0]["name"])
        self.append(path, [ai_title("after")], mtime_ns=2_000_000_000_000_000_000)
        self.assertEqual("after", store.list_sessions()[0]["name"])
        self.assertEqual(2, self.parse.call_count)

    def test_a_caller_mutating_a_record_does_not_corrupt_the_cache(self):
        # The records handed out are copies; a caller that scribbles on one —
        # a formatter, a serialiser, a test — must not have that scribble come
        # back on the next poll.
        self.write([ai_title("original"), user("hello")])
        store = session_store.SessionStore(self.home)
        record = store.list_sessions()[0]
        record["name"] = "mutated"
        record["messages"] = 999
        again = store.list_sessions()[0]
        self.assertEqual("original", again["name"])
        self.assertEqual(1, again["messages"])
        self.assertEqual(1, self.parse.call_count)

    def test_same_size_but_a_newer_mtime_is_reparsed(self):
        # A rewrite of equal length — the case size alone cannot see. Both
        # titles are three characters, so the two files are byte-for-byte the
        # same length.
        self.write([ai_title("aaa")], mtime_ns=1_000_000_000_000_000_000)
        store = session_store.SessionStore(self.home)
        first = store.list_sessions()[0]
        self.assertEqual("aaa", first["name"])
        path = self.write([ai_title("bbb")], mtime_ns=1_500_000_000_000_000_000)
        self.assertEqual(first["bytes"], os.stat(path).st_size)
        self.assertEqual("bbb", store.list_sessions()[0]["name"])
        self.assertEqual(2, self.parse.call_count)

    def test_same_mtime_but_a_different_size_is_reparsed(self):
        # st_mtime_ns has nanosecond resolution, but the filesystem underneath
        # may not: two writes inside one coarse tick share a timestamp. Size is
        # the second axis of the key precisely for that.
        path = self.write([ai_title("before")])
        pinned = os.stat(path).st_mtime_ns
        store = session_store.SessionStore(self.home)
        self.assertEqual(0, store.list_sessions()[0]["messages"])
        self.append(path, [user("one"), user("two")], mtime_ns=pinned)
        self.assertEqual(pinned, os.stat(path).st_mtime_ns)
        session = store.list_sessions()[0]
        self.assertEqual(2, session["messages"])
        self.assertEqual(2, self.parse.call_count)

    def test_a_deleted_and_recreated_transcript_is_not_served_stale(self):
        # Same path, different file. The cache is keyed on the path, so a
        # recreated transcript would be a plausible way to hand back the dead
        # one's contents.
        path = self.write([ai_title("first life")], mtime_ns=1_000_000_000_000_000_000)
        store = session_store.SessionStore(self.home)
        self.assertEqual("first life", store.list_sessions()[0]["name"])
        os.remove(path)
        self.assertEqual([], store.list_sessions())
        self.write(
            [ai_title("second life"), user("hello")],
            mtime_ns=1_500_000_000_000_000_000,
        )
        self.assertEqual("second life", store.list_sessions()[0]["name"])

    def test_the_cache_is_per_instance_and_not_shared_between_stores(self):
        # Two stores over one home are two independent readers. A module-level
        # cache would also leak between tests and between an admin page and a
        # CLI in one process.
        self.write([ai_title("shared home")])
        first = session_store.SessionStore(self.home)
        second = session_store.SessionStore(self.home)
        self.assertEqual("shared home", first.list_sessions()[0]["name"])
        self.assertEqual("shared home", second.list_sessions()[0]["name"])
        self.assertEqual(2, self.parse.call_count)

    def test_a_growing_transcript_is_reparsed_on_every_poll_that_grows_it(self):
        # The live-session case: the transcript the operator's own session is
        # writing. Its counts have to keep climbing, not freeze at whatever was
        # cached on the first poll.
        path = self.write([user("hello")], mtime_ns=1_000_000_000_000_000_000)
        store = session_store.SessionStore(self.home)
        counts = [store.list_sessions()[0]["messages"]]
        for step in range(1, 4):
            self.append(
                path,
                [assistant(usage={"input_tokens": 100 * step})],
                mtime_ns=1_000_000_000_000_000_000 + step * 1_000_000_000,
            )
            session = store.list_sessions()[0]
            counts.append(session["messages"])
            self.assertEqual(100 * step, session["contextTokens"])
        self.assertEqual([1, 2, 3, 4], counts)
        self.assertEqual(4, self.parse.call_count)

    def test_liveness_is_still_read_fresh_behind_a_warm_cache(self):
        # The cache covers transcript parsing only. Liveness reflects live
        # process state and must be re-read on every poll, or the stale-state
        # bug this module just fixed comes straight back.
        self.write([ai_title("live one")])
        write_registry(
            self.home, "1.json", pid=os.getpid(), sessionId=SESSION_A,
            status="working", updatedAt=FRESH,
        )
        store = session_store.SessionStore(self.home)
        self.assertTrue(store.list_sessions()[0]["live"])
        os.remove(os.path.join(self.home, "sessions", "1.json"))
        session = store.list_sessions()[0]
        self.assertFalse(session["live"])
        self.assertIsNone(session["status"])
        self.assertEqual(1, self.parse.call_count)


class ForgetTest(StoreTestCase):
    """forget() evicts one parse, so a caller that deletes a transcript can say so.

    Hygiene rather than correctness: a deleted session's entry is simply never
    read again, and the (mtime_ns, size) key already catches a recreate at the
    same path. It exists so the HTTP layer does not have to reach into _cache,
    which stays private.
    """

    def setUp(self):
        super().setUp()
        patcher = mock.patch.object(
            session_store, "parse_transcript", wraps=session_store.parse_transcript
        )
        self.parse = patcher.start()
        self.addCleanup(patcher.stop)

    def test_forget_drops_the_cached_parse_so_the_next_list_reparses(self):
        path = write_transcript(self.home, SESSION_A, [ai_title("cached")])
        store = session_store.SessionStore(self.home)
        self.assertEqual("cached", store.list_sessions()[0]["name"])
        self.assertEqual("cached", store.list_sessions()[0]["name"])
        self.assertEqual(1, self.parse.call_count)
        store.forget(path)
        self.assertEqual("cached", store.list_sessions()[0]["name"])
        self.assertEqual(2, self.parse.call_count)

    def test_forget_is_a_no_op_for_an_unknown_path(self):
        write_transcript(self.home, SESSION_A, [ai_title("cached")])
        store = session_store.SessionStore(self.home)
        store.list_sessions()
        store.forget(os.path.join(self.home, "projects", "-workspace", "nope.jsonl"))
        store.forget("/nowhere/at/all.jsonl")
        store.list_sessions()
        self.assertEqual(1, self.parse.call_count)

    def test_forgetting_a_deleted_transcript_leaves_the_list_correct(self):
        # The Task 11 flow: delete, then evict what was deleted.
        write_transcript(self.home, SESSION_A, [ai_title("doomed")])
        write_transcript(self.home, SESSION_B, [ai_title("kept")])
        store = session_store.SessionStore(self.home)
        self.assertEqual(2, len(store.list_sessions()))
        store.forget(session_store.delete_session(self.home, SESSION_A))
        self.assertEqual([SESSION_B], [s["id"] for s in store.list_sessions()])


class DeleteTest(StoreTestCase):
    """delete_session() is the only mutating function in the module.

    It refuses more than it accepts, and the order of its refusals is part of
    the contract: a malformed id is rejected before anything reaches the
    filesystem, and a running session is refused before the transcript tree is
    walked at all. Every test here works inside its own temporary home — a
    transcript is an unrecoverable conversation.
    """

    def transcript(self, session_id=SESSION_A, project="-workspace"):
        return write_transcript(
            self.home, session_id, [ai_title("A session")], project=project
        )

    def test_removes_the_transcript_and_returns_the_path_it_removed(self):
        # The return value is the interface: the caller holds the store, not
        # this function, so it is what lets the parse cache be evicted.
        path = self.transcript()
        removed = session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(os.path.realpath(path), removed)
        self.assertFalse(os.path.exists(path))

    def test_a_non_uuid_id_is_rejected_without_touching_the_filesystem(self):
        # Traversal-shaped input must never reach a path join, so the check
        # comes first — before liveness and before the transcript walk.
        path = self.transcript()
        for session_id in (
            "../../etc/passwd",
            "..",
            "/etc/passwd",
            SESSION_A + "/../../etc/passwd",
            SESSION_A + ".jsonl",
            # `$` matched before a trailing newline, so this one used to slip
            # the gate and reach live_sessions() and the filesystem walk.
            SESSION_A + "\n",
            "",
            None,
            "not-a-uuid",
        ):
            with self.subTest(session_id=session_id):
                with mock.patch.object(session_store, "_iter_transcripts") as walk:
                    with mock.patch.object(session_store, "live_sessions") as live:
                        with self.assertRaises(session_store.DeleteError) as caught:
                            session_store.delete_session(self.home, session_id)
                self.assertEqual(400, caught.exception.status)
                self.assertEqual(0, walk.call_count)
                self.assertEqual(0, live.call_count)
        self.assertTrue(os.path.exists(path))

    def test_an_unknown_but_well_formed_uuid_is_rejected_with_404(self):
        self.transcript()
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_B)
        self.assertEqual(404, caught.exception.status)

    def test_a_live_session_is_refused_and_left_on_disk(self):
        path = self.transcript()
        write_registry(
            self.home, "1.json", pid=live_pid(self), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(409, caught.exception.status)
        self.assertTrue(os.path.exists(path))

    def test_liveness_is_decided_before_the_transcript_tree_is_walked(self):
        # A live session with no transcript is a 409, not a 404: the running
        # process is the reason to refuse, and it is established first.
        write_registry(
            self.home, "1.json", pid=live_pid(self), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(409, caught.exception.status)

    def test_a_transcript_symlinked_out_of_the_tree_is_refused(self):
        # The uuid check rules out traversal in the id itself; a transcript
        # that is a symlink to somewhere else does not go through the id at
        # all, so the resolved path is checked against the projects root too.
        outside = os.path.join(self.home, "elsewhere")
        os.makedirs(outside)
        target = os.path.join(outside, "precious.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("do not delete me\n")
        directory = os.path.join(self.home, "projects", "-workspace")
        os.makedirs(directory)
        link = os.path.join(directory, SESSION_A + ".jsonl")
        os.symlink(target, link)
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(400, caught.exception.status)
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.islink(link))

    def test_a_transcript_symlinked_into_a_prefix_sharing_sibling_is_refused(self):
        # The separator in `root + os.sep` is the whole guard here: without it,
        # <home>/projects-old/ shares the projects root's prefix as a plain
        # string and every transcript in it becomes deletable through a symlink.
        outside = os.path.join(self.home, "projects-old")
        os.makedirs(outside)
        target = os.path.join(outside, "precious.jsonl")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("do not delete me\n")
        directory = os.path.join(self.home, "projects", "-workspace")
        os.makedirs(directory)
        link = os.path.join(directory, SESSION_A + ".jsonl")
        os.symlink(target, link)
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(400, caught.exception.status)
        self.assertTrue(os.path.exists(target))
        self.assertTrue(os.path.islink(link))

    def test_deleting_one_session_leaves_its_siblings_untouched(self):
        doomed = self.transcript(SESSION_A)
        kept = self.transcript(SESSION_B)
        other_project = write_transcript(
            self.home, SESSION_B, [ai_title("Elsewhere")], project="-workspace-two"
        )
        session_store.delete_session(self.home, SESSION_A)
        self.assertFalse(os.path.exists(doomed))
        self.assertTrue(os.path.exists(kept))
        self.assertTrue(os.path.exists(other_project))

    def test_a_registered_session_whose_pid_was_reused_can_be_deleted(self):
        # The case the pid-reuse fix unlocked: the registry names a pid that
        # exists, but the entry predates that process by two months, so the
        # session is not running and must not be undeletable.
        path = self.transcript()
        write_registry(
            self.home, "205.json", pid=live_pid(self), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH - 60 * 86400 * 1000,
        )
        self.assertEqual({}, session_store.live_sessions(self.home))
        session_store.delete_session(self.home, SESSION_A)
        self.assertFalse(os.path.exists(path))

    def test_a_deleted_session_is_gone_from_the_list(self):
        self.transcript(SESSION_A)
        self.transcript(SESSION_B)
        session_store.delete_session(self.home, SESSION_A)
        listed = session_store.SessionStore(self.home).list_sessions()
        self.assertEqual([SESSION_B], [s["id"] for s in listed])

    def test_a_home_with_no_projects_directory_is_a_404(self):
        # _iter_transcripts yields nothing rather than raising, so this must
        # come out as an ordinary "no such session".
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(404, caught.exception.status)

    def test_the_error_carries_a_status_and_a_readable_message(self):
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, "nonsense")
        error = caught.exception
        self.assertEqual(400, error.status)
        self.assertTrue(str(error).strip())
        self.assertIsInstance(str(error), str)


class DeleteUnlinkFailureTest(StoreTestCase):
    """A failing unlink still owes the caller a DeleteError with a status.

    delete_session()'s contract is that everything it refuses arrives as a
    DeleteError carrying an HTTP status; the HTTP layer catches nothing else, so
    a bare OSError from os.unlink() escapes as an unhandled traceback and a 500
    with no body. Four triggers are reachable in practice, and each is pinned
    below: a dangling in-tree symlink, the race between the liveness check and
    the unlink, a directory named <uuid>.jsonl, and a read-only project
    directory.
    """

    def transcript(self, session_id=SESSION_A):
        return write_transcript(self.home, session_id, [ai_title("A session")])

    def project_dir(self):
        directory = os.path.join(self.home, "projects", "-workspace")
        os.makedirs(directory, exist_ok=True)
        return directory

    def test_a_dangling_in_tree_symlink_is_a_404_not_a_raw_oserror(self):
        # In-tree symlinked transcripts are allowed, and unlinking resolves the
        # link: the target goes and the link stays. _iter_transcripts() only
        # lists names, so it still resolves the id — and a second delete used to
        # raise FileNotFoundError straight out of the module, which left the
        # session permanently undeletable through the HTTP layer.
        directory = self.project_dir()
        target = os.path.join(directory, "target.jsonl")  # in-tree, not an id
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("{}\n")
        link = os.path.join(directory, SESSION_A + ".jsonl")
        os.symlink(target, link)

        self.assertEqual(os.path.realpath(target),
                         session_store.delete_session(self.home, SESSION_A))
        self.assertFalse(os.path.exists(target))
        self.assertTrue(os.path.islink(link))  # the link outlives its target

        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(404, caught.exception.status)

    def test_a_transcript_removed_inside_the_toctou_window_is_a_404(self):
        # The window between the liveness check and the unlink is real and does
        # not lose benignly: whoever else got there first leaves a
        # FileNotFoundError behind. Nothing is locked — the whole fix is to turn
        # the race into the same clean 404 an already-gone session gets.
        path = self.transcript()
        doomed = os.path.realpath(path)
        real_realpath = os.path.realpath

        def racing_realpath(value, *args, **kwargs):
            resolved = real_realpath(value, *args, **kwargs)
            if resolved == doomed and os.path.exists(resolved):
                os.unlink(resolved)  # another deleter wins the race
            return resolved

        with mock.patch.object(os.path, "realpath", racing_realpath):
            with self.assertRaises(session_store.DeleteError) as caught:
                session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(404, caught.exception.status)
        self.assertFalse(os.path.exists(path))

    def test_a_directory_named_like_a_transcript_is_a_500(self):
        # listdir() cannot tell a directory from a file, so <uuid>.jsonl/ is
        # walked like any other transcript and unlink() raises IsADirectoryError.
        directory = os.path.join(self.project_dir(), SESSION_A + ".jsonl")
        os.makedirs(directory)
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(500, caught.exception.status)
        self.assertTrue(str(caught.exception).strip())
        self.assertTrue(os.path.isdir(directory))

    def test_a_read_only_project_directory_is_a_500(self):
        path = self.transcript()
        directory = self.project_dir()
        os.chmod(directory, 0o500)
        # Restored before the tree is removed: cleanups run last-registered
        # first, and setUp() registered the rmtree.
        self.addCleanup(os.chmod, directory, 0o700)
        with self.assertRaises(session_store.DeleteError) as caught:
            session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(500, caught.exception.status)
        self.assertTrue(os.path.exists(path))

    def test_the_500_names_the_underlying_reason(self):
        # Operator-facing: this module never sees secrets, and "could not
        # remove it" without the errno is unactionable in a log.
        self.transcript()
        boom = OSError(28, "No space left on device")
        with mock.patch.object(session_store.os, "unlink", side_effect=boom):
            with self.assertRaises(session_store.DeleteError) as caught:
                session_store.delete_session(self.home, SESSION_A)
        self.assertEqual(500, caught.exception.status)
        self.assertIn("No space left on device", str(caught.exception))


class SlugTest(StoreTestCase):
    """slugify() and slug_for(): the Remote Control name for a session.

    The slug has to survive being interpolated into a shell-built name, so it
    is restricted to lowercase alphanumerics and dashes — everything else is a
    separator, including every non-ASCII letter.
    """

    def test_lowercases_and_joins_words_with_dashes(self):
        self.assertEqual(
            "add-terraform-to-container-image",
            session_store.slugify("Add Terraform to container image"),
        )

    def test_collapses_runs_of_separators_and_trims_the_edges(self):
        self.assertEqual("a-b", session_store.slugify("  ***A***   b!!  "))

    def test_truncates_to_the_limit(self):
        # The default limit is what a Remote Control name has room for.
        slug = session_store.slugify("word " * 40)
        self.assertEqual(32, len(slug))
        self.assertEqual("word-" * 6 + "wo", slug)

    def test_truncation_never_leaves_a_trailing_dash(self):
        # The cut lands exactly on the dash between "abc" and "def": a slug
        # ending in a separator would read as a name with a dangling hyphen.
        self.assertEqual("abc", session_store.slugify("abc def ghi", limit=4))
        self.assertEqual("abc", session_store.slugify("abc def ghi", limit=3))
        self.assertEqual("abc-d", session_store.slugify("abc def ghi", limit=5))

    def test_empty_values_become_an_empty_slug(self):
        for value in (None, "", "   ", "\n\t"):
            with self.subTest(value=value):
                self.assertEqual("", session_store.slugify(value))

    def test_non_strings_become_an_empty_slug_rather_than_raising(self):
        # Total, like _truncate: `text or ""` absorbs None and "", but a truthy
        # non-string would reach .lower() and raise AttributeError. Every
        # derivation helper in this module answers with a sentinel instead.
        for value in (123, 1.5, True, {"a": 1}, ["a"], object()):
            with self.subTest(value=value):
                self.assertEqual("", session_store.slugify(value))

    def test_non_ascii_letters_are_separators_not_letters(self):
        # Deliberate and pinned: there is no transliteration here, so accented
        # and CJK characters are dropped exactly like punctuation is. The ASCII
        # around them still survives, which is what makes the slug readable.
        self.assertEqual("caf-m-nster", session_store.slugify("Café Münster"))
        self.assertEqual("report", session_store.slugify("日本語 report"))

    def test_a_title_of_only_separators_slugifies_to_nothing(self):
        for value in ("***", "…", "!!! ???", "日本語"):
            with self.subTest(value=value):
                self.assertEqual("", session_store.slugify(value))

    def test_slug_for_uses_the_ai_title(self):
        write_transcript(self.home, SESSION_A, [ai_title("Add Terraform to image")])
        self.assertEqual(
            "add-terraform-to-image",
            session_store.slug_for(self.home, SESSION_A),
        )

    def test_slug_for_ignores_a_first_prompt_name(self):
        # A first-prompt name is stable but frequently noise ("ping"), and a
        # Remote Control called "ping" is worse than one called by its uuid —
        # so the caller's uuid fallback wins here.
        write_transcript(self.home, SESSION_A, [user("ping")])
        self.assertEqual("", session_store.slug_for(self.home, SESSION_A))

    def test_slug_for_an_unknown_session_is_empty(self):
        write_transcript(self.home, SESSION_A, [ai_title("A title")])
        self.assertEqual("", session_store.slug_for(self.home, SESSION_B))

    def test_slug_for_a_transcript_with_no_name_at_all_is_empty(self):
        # nameSource "none": parse_transcript hands back "(untitled)", which
        # must not become the slug "untitled".
        write_transcript(self.home, SESSION_A, [{"type": "mode", "mode": "normal"}])
        self.assertEqual("", session_store.slug_for(self.home, SESSION_A))


STORE_PATH = os.path.abspath(session_store.__file__)


class CliTest(StoreTestCase):
    """The shell-facing CLI, exercised as a real subprocess.

    Two shell scripts consume this module, and each uses a different channel:
    launch_session.sh branches on the *exit code* of --is-live to stop a second
    Claude opening a transcript that is already open, and start_claude.sh reads
    *stdout* from --slug to build a Remote Control name. Calling _cli() in
    process would test neither, so every case here runs the file the way `sh`
    does.
    """

    def run_cli(self, *args, home=None):
        env = dict(os.environ)
        env["CLAUDE_HOME"] = self.home if home is None else home
        return subprocess.run(
            [sys.executable, STORE_PATH, *args],
            capture_output=True, text=True, env=env,
        )

    def live_registry(self, session_id=SESSION_A):
        write_registry(
            self.home, "205.json", pid=os.getpid(), sessionId=session_id,
            status="busy", updatedAt=FRESH,
        )

    def test_is_live_exits_zero_for_a_live_session(self):
        self.live_registry()
        self.assertEqual(0, self.run_cli("--is-live", SESSION_A).returncode)

    def test_is_live_exits_non_zero_for_a_dead_session(self):
        write_registry(
            self.home, "205.json", pid=dead_pid(), sessionId=SESSION_A,
            status="busy", updatedAt=FRESH,
        )
        self.assertEqual(1, self.run_cli("--is-live", SESSION_A).returncode)

    def test_is_live_exits_non_zero_for_an_unknown_session(self):
        self.live_registry()
        self.assertEqual(1, self.run_cli("--is-live", SESSION_B).returncode)

    def test_is_live_exits_non_zero_for_a_non_uuid_argument(self):
        # The shell passes through whatever it was given; a junk argument must
        # answer "not live" rather than crash with a traceback.
        self.live_registry()
        for value in ("", "not-a-uuid", "../../etc/passwd", "-"):
            with self.subTest(value=value):
                result = self.run_cli("--is-live", value)
                self.assertEqual(1, result.returncode)
                self.assertEqual("", result.stderr)

    def test_slug_prints_the_slug_and_exits_zero(self):
        write_transcript(self.home, SESSION_A, [ai_title("Add Terraform to image")])
        result = self.run_cli("--slug", SESSION_A)
        self.assertEqual(0, result.returncode)
        self.assertEqual("add-terraform-to-image\n", result.stdout)

    def test_slug_prints_just_a_newline_when_there_is_no_ai_title(self):
        # The shell falls back to the first 8 characters of the uuid on empty
        # output, so "no slug" has to be an empty line and a zero exit.
        write_transcript(self.home, SESSION_A, [user("ping")])
        result = self.run_cli("--slug", SESSION_A)
        self.assertEqual(0, result.returncode)
        self.assertEqual("\n", result.stdout)

    def test_slug_prints_nothing_but_the_slug_on_stdout(self):
        # start_claude.sh interpolates this straight into a name, so a stray
        # print — a warning, a parse diagnostic — would corrupt it. A busy
        # transcript with a malformed tail is the shape that would provoke one.
        path = write_transcript(
            self.home,
            SESSION_A,
            [
                user("do a thing"),
                assistant(usage={"input_tokens": 10}, tools=[("Bash", {"description": "ls"})]),
                ai_title("Slugs and the shell CLI"),
                {"type": "mode", "mode": "normal"},
            ],
        )
        with open(path, "a", encoding="utf-8") as handle:
            handle.write('{"type":"assistant","message":{"con')
        result = self.run_cli("--slug", SESSION_A)
        self.assertEqual(0, result.returncode)
        self.assertEqual("slugs-and-the-shell-cli\n", result.stdout)
        self.assertEqual(1, result.stdout.count("\n"))

    def test_bad_usage_exits_two_with_the_message_on_stderr(self):
        # stdout belongs to --slug; a usage line printed there would be read as
        # a Remote Control name by a script that forgot to check the exit code.
        for args in ((), ("--is-live",), ("--slug",), (SESSION_A,),
                     ("--is-live", SESSION_A, SESSION_B),
                     ("--bogus", SESSION_A), ("--help",)):
            with self.subTest(args=args):
                result = self.run_cli(*args)
                self.assertEqual(2, result.returncode)
                self.assertEqual("", result.stdout)
                self.assertIn("usage", result.stderr)

    def test_claude_home_selects_the_home_that_is_read(self):
        write_transcript(self.home, SESSION_A, [ai_title("The temp home")])
        other = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, other, ignore_errors=True)
        write_transcript(other, SESSION_A, [ai_title("The other home")])
        self.assertEqual(
            "the-temp-home\n", self.run_cli("--slug", SESSION_A).stdout
        )
        self.assertEqual(
            "the-other-home\n",
            self.run_cli("--slug", SESSION_A, home=other).stdout,
        )

    def test_falls_back_to_home_claude_when_claude_home_is_unset(self):
        # HOME is redirected at a temp directory so this exercises the ~/.claude
        # fallback without reading — let alone touching — the real one.
        fake_home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, fake_home, ignore_errors=True)
        write_transcript(
            os.path.join(fake_home, ".claude"), SESSION_A, [ai_title("Default home")]
        )
        env = dict(os.environ)
        env.pop("CLAUDE_HOME", None)
        env["HOME"] = fake_home
        result = subprocess.run(
            [sys.executable, STORE_PATH, "--slug", SESSION_A],
            capture_output=True, text=True, env=env,
        )
        self.assertEqual(0, result.returncode)
        self.assertEqual("default-home\n", result.stdout)


if __name__ == "__main__":
    unittest.main()
