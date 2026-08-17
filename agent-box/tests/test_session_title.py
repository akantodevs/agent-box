"""Unit tests for session_title.py — the browser tab's name.

ttyd is started without `titleFixed`, so whatever this watcher writes to the
terminal as an OSC title becomes the tab's name. Two things therefore matter
beyond "the right text comes out":

  * the escape sequence is written to the *terminal*, i.e. into a stream Claude
    Code is also writing to, so it must be one bounded, single-line, control-
    character-free write and nothing else; and
  * the watcher outlives the `exec claude` in start_claude.sh as a child of the
    Claude process, so it has to end on its own when that process does —
    an orphan here would sit holding the pty for the life of the container.

The fixtures come from test_session_store: these are the same transcripts, read
by the same parser, so there is no second idea of what a session is called.
"""

import os
import shutil
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

from test_session_store import SESSION_A, SESSION_B, ai_title, user, write_transcript

import session_store
import session_title

SCRIPTS_DIR = os.path.abspath(
    os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)
SESSION_TITLE = os.path.join(SCRIPTS_DIR, "session_title.py")


class Recorder:
    """A stand-in for the terminal: records writes, or refuses them.

    `broken` models the one failure that actually happens — the tab closed and
    the pty went with it — which the watcher must treat as "stop", not as an
    error to keep retrying against a dead file descriptor.
    """

    def __init__(self, broken=False):
        self.writes = []
        self.broken = broken

    def write(self, text):
        if self.broken:
            raise OSError(5, "Input/output error")
        self.writes.append(text)

    def flush(self):
        if self.broken:
            raise OSError(5, "Input/output error")


def titles(recorder):
    """The title text of every OSC sequence the watcher wrote."""
    return [text[len("\033]0;"): -len("\007")] for text in recorder.writes]


class NoBoxNameTestCase(unittest.TestCase):
    """A temp Claude home, and no box name in the environment.

    AGENT_NAME is exported into every session in a real container — including
    the one this suite is being run from — and it would then appear on the end
    of every title asserted below. The tests that are about the box name set it
    themselves.
    """

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)
        environment = mock.patch.dict(os.environ)
        environment.start()
        self.addCleanup(environment.stop)
        os.environ.pop("AGENT_NAME", None)


class TitleTextTest(NoBoxNameTestCase):
    """What the tab is called, given a session."""

    def test_a_titled_session_is_named_by_its_title(self):
        write_transcript(self.home, SESSION_A, [ai_title("Multi-session admin page")])
        self.assertEqual(
            "Agent: Multi-session admin page",
            session_title.title_for(self.home, SESSION_A),
        )

    def test_an_untitled_session_falls_back_the_way_the_admin_page_does(self):
        # The tab and the row in the session list have to be recognisable as
        # the same session, so both derive the name from the same parser.
        write_transcript(self.home, SESSION_A, [user("check the logs")])
        self.assertEqual("Agent: check the logs", session_title.title_for(self.home, SESSION_A))

    def test_a_session_with_nothing_to_go_on_is_untitled_not_blank(self):
        write_transcript(self.home, SESSION_A, [])
        self.assertEqual("Agent: (untitled)", session_title.title_for(self.home, SESSION_A))

    def test_a_session_with_no_transcript_yet_is_a_new_session(self):
        # A tab opened with "+ New session" gets here: the id exists (start_claude.sh
        # generates it) but Claude Code has not written a line under it yet.
        self.assertEqual("Agent: new session", session_title.title_for(self.home, SESSION_A))

    def test_an_unusable_id_is_a_new_session_rather_than_a_lookup(self):
        for value in ("", None, "../etc", 123):
            with self.subTest(value=value):
                self.assertEqual(
                    "Agent: new session", session_title.title_for(self.home, value)
                )

    def test_the_title_of_another_session_is_not_borrowed(self):
        write_transcript(self.home, SESSION_B, [ai_title("Someone else's work")])
        self.assertEqual("Agent: new session", session_title.title_for(self.home, SESSION_A))


class AgentNameTest(NoBoxNameTestCase):
    """The box's own name, on the end of every session tab.

    One operator commonly has two boxes open, and their session tabs are
    otherwise indistinguishable. The session name stays first: it is the half a
    narrow tab actually shows.
    """

    def setUp(self):
        super().setUp()
        write_transcript(self.home, SESSION_A, [ai_title("Rebuild the image")])

    def title(self, agent_name):
        return session_title.title_for(self.home, SESSION_A, agent_name)

    def test_the_box_name_follows_the_session_name(self):
        self.assertEqual(
            "Agent: Rebuild the image · agent-box-dev", self.title("agent-box-dev")
        )

    def test_an_unnamed_box_adds_nothing(self):
        # agent_name.sh answers with the hostname rather than nothing, so this
        # is the case where even that failed — the tab keeps its session name.
        for value in ("", None, "   ", 17):
            with self.subTest(value=value):
                self.assertEqual("Agent: Rebuild the image", self.title(value))

    def test_the_box_name_is_sanitised_like_the_session_name(self):
        # It comes from docker-compose.yml or from `docker inspect`, and it is
        # going into the same control sequence. The escape and the newline are
        # dropped; what is left of them is text, exactly as for a session name.
        self.assertEqual(
            "Agent: Rebuild the image · a]0; b", self.title("a\033]0;\n b")
        )

    def test_a_long_box_name_cannot_crowd_out_the_session_name(self):
        title = self.title("y" * 500)
        self.assertTrue(title.startswith("Agent: Rebuild the image · "), title)
        self.assertLessEqual(len(title), 150)

    def test_the_name_comes_from_the_environment_when_it_is_not_passed(self):
        # How the container reaches it: ep.sh resolves it once and it travels
        # through the su chain as an environment variable.
        with mock.patch.dict(os.environ, {"AGENT_NAME": "agent-box-dev"}):
            self.assertEqual(
                "Agent: Rebuild the image · agent-box-dev",
                session_title.title_for(self.home, SESSION_A),
            )

    def test_a_new_session_is_named_after_the_box_too(self):
        self.assertEqual(
            "Agent: new session · agent-box-dev",
            session_title.title_for(self.home, "", "agent-box-dev"),
        )


class SanitisingTest(NoBoxNameTestCase):
    """A title is whatever the model wrote, and it goes onto a terminal.

    An OSC sequence is terminated by BEL and by ESC — so a title containing
    either would end the sequence early and leave the rest of the text printed
    at, or interpreted by, the operator's terminal. The name is foreign text on
    its way into a control sequence: it is stripped, not escaped.
    """

    def named(self, title):
        write_transcript(self.home, SESSION_A, [ai_title(title)])
        return session_title.title_for(self.home, SESSION_A)

    def test_an_escape_cannot_be_smuggled_into_the_sequence(self):
        name = self.named("safe\033]0;pwned\007 tail")
        self.assertNotIn("\033", name)
        self.assertNotIn("\007", name)
        self.assertIn("pwned", name)  # stripped of controls, kept as text

    def test_control_characters_are_dropped(self):
        name = self.named("a\x00b\x07c\x1bd\x7fe\x9bf")
        self.assertEqual("Agent: abcdef", name)

    def test_the_title_is_one_line(self):
        self.assertEqual("Agent: one two", self.named("one\ntwo"))

    def test_bidi_overrides_are_dropped(self):
        # U+202E reverses everything after it, so one in a title would reverse
        # the rest of the tab's name. The admin page drops the same set.
        self.assertNotIn("‮", self.named("report‮gnp.exe"))

    def test_a_long_title_is_bounded(self):
        name = self.named("x" * 500)
        self.assertLessEqual(len(name), 100)


class OscTest(unittest.TestCase):
    """The exact bytes. This is a control sequence, not a string."""

    def test_the_sequence_is_osc_zero_terminated_by_bel(self):
        self.assertEqual("\033]0;Agent: x\007", session_title.osc("Agent: x"))

    def test_writing_is_a_single_flushed_write(self):
        # Claude Code is writing to the same terminal. One write is one thing
        # for the pty to interleave, rather than four.
        recorder = Recorder()
        self.assertTrue(session_title.write_title(recorder, "Agent: x"))
        self.assertEqual(["\033]0;Agent: x\007"], recorder.writes)

    def test_a_closed_terminal_reports_failure_rather_than_raising(self):
        self.assertFalse(session_title.write_title(Recorder(broken=True), "Agent: x"))


class WatchTest(NoBoxNameTestCase):
    """The loop: title now, retitle when the session is renamed, nothing else."""

    def watch(self, recorder, rounds, session_id=SESSION_A, between=None):
        """Run `rounds` iterations of the real loop, with no real sleeping.

        `between` is called in place of the sleep, which is where a test
        changes the transcript under the watcher — that is exactly when a
        rename happens in the container.
        """
        remaining = [rounds]

        def alive():
            remaining[0] -= 1
            return remaining[0] > 0

        def sleep(_seconds):
            if between:
                between()

        session_title.watch(
            recorder, self.home, session_id, interval=0, alive=alive, sleep=sleep
        )

    def test_the_title_is_written_immediately(self):
        write_transcript(self.home, SESSION_A, [ai_title("Rebuild the image")])
        recorder = Recorder()
        self.watch(recorder, rounds=1)
        self.assertEqual(["Agent: Rebuild the image"], titles(recorder))

    def test_an_unchanged_session_is_titled_once_and_left_alone(self):
        write_transcript(self.home, SESSION_A, [ai_title("Rebuild the image")])
        recorder = Recorder()
        self.watch(recorder, rounds=5)
        self.assertEqual(["Agent: Rebuild the image"], titles(recorder))

    def test_a_rename_reaches_the_tab(self):
        # The point of watching at all: a new session is "new session" until
        # Claude Code writes its first ai-title, which is several turns in.
        recorder = Recorder()
        renamed = [False]

        def rename():
            if renamed[0]:
                return
            renamed[0] = True
            write_transcript(self.home, SESSION_A, [ai_title("Tab titles")])

        self.watch(recorder, rounds=4, between=rename)
        self.assertEqual(["Agent: new session", "Agent: Tab titles"], titles(recorder))

    def test_writing_more_of_the_same_conversation_is_not_a_rename(self):
        # The transcript changes constantly; the *name* rarely does. Only a
        # changed name may reach the terminal.
        write_transcript(self.home, SESSION_A, [ai_title("Tab titles")])
        recorder = Recorder()

        def append():
            write_transcript(
                self.home, SESSION_A, [ai_title("Tab titles"), user("and now?")]
            )

        self.watch(recorder, rounds=4, between=append)
        self.assertEqual(["Agent: Tab titles"], titles(recorder))

    def test_the_loop_stops_when_the_terminal_is_gone(self):
        # A broken pipe is the tab closing. The alive() here would allow a
        # thousand rounds; the write failure is what must end the loop.
        recorder = Recorder(broken=True)
        session_title.watch(
            recorder,
            self.home,
            SESSION_A,
            interval=0,
            alive=lambda: True,
            sleep=lambda _s: None,
        )  # returns, rather than hanging or raising


class ParentWatchTest(unittest.TestCase):
    """How the watcher knows the session it belongs to has ended.

    PR_SET_PDEATHSIG does this immediately in the container; this predicate is
    the fallback that covers what a kernel call cannot — including the race
    where the parent dies before the watcher has run a single line.
    """

    def test_reparenting_reads_as_the_parent_being_gone(self):
        with mock.patch("os.getppid", return_value=4242):
            alive = session_title._parent_watch()
            self.assertTrue(alive())
        with mock.patch("os.getppid", return_value=1):
            self.assertFalse(alive())

    def test_a_watcher_that_starts_orphaned_has_nothing_to_follow(self):
        # The parent died between the fork and the first instruction, which is
        # also the window PR_SET_PDEATHSIG cannot cover: there is no later death
        # to be signalled, and no pid to compare against either.
        with mock.patch("os.getppid", return_value=1):
            self.assertFalse(session_title._parent_watch()())


class ProcessTest(unittest.TestCase):
    """The script as start_claude.sh runs it: backgrounded, then orphaned."""

    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def env(self, **overrides):
        env = dict(os.environ)
        env["CLAUDE_HOME"] = self.home
        env.update(overrides)
        return env

    def test_it_titles_the_terminal_and_exits_when_there_is_nothing_to_watch(self):
        # No id at all: start_claude.sh gets here only when uuid generation
        # failed, and a tab with no title is worse than a generic one.
        done = subprocess.run(
            [sys.executable, SESSION_TITLE],
            env=self.env(),
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        self.assertEqual(b"\033]0;Agent: new session\007", done.stdout)

    def test_it_writes_the_title_of_the_session_it_is_given(self):
        """A watcher with a session to follow does not return — it is killed
        with the tab. So the title is read off the pipe rather than waited for,
        under a watchdog that kills it if it never writes at all."""
        write_transcript(self.home, SESSION_A, [ai_title("Publish the image")])
        watcher = subprocess.Popen(
            [sys.executable, SESSION_TITLE, SESSION_A],
            env=self.env(AGENT_BOX_TITLE_INTERVAL="0.05"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        # Registered before the kill/wait pair, so they run after it: the pipes
        # are closed only once the process holding their other end is reaped.
        self.addCleanup(watcher.stdout.close)
        self.addCleanup(watcher.stderr.close)
        self.addCleanup(watcher.wait)
        self.addCleanup(watcher.kill)
        watchdog = threading.Timer(30, watcher.kill)
        watchdog.start()
        self.addCleanup(watchdog.cancel)

        expected = b"\033]0;Agent: Publish the image\007"
        self.assertEqual(expected, watcher.stdout.read(len(expected)))

    def test_it_ends_with_the_process_it_was_started_from(self):
        """No orphan holding the terminal.

        start_claude.sh backgrounds this and then execs claude, so the watcher's
        parent *is* the Claude process. When a session ends normally nothing
        signals the watcher — it has to notice for itself.
        """
        write_transcript(self.home, SESSION_A, [ai_title("Publish the image")])
        parent = subprocess.Popen(
            [
                "/bin/bash",
                "-c",
                # The watcher's own output goes to /dev/null: it would otherwise
                # share this pipe with the pid and hold it open past the exit.
                #
                # The sleep is the container: claude is running when the watcher
                # is forked, and only exits later. Without it the parent would be
                # gone before the watcher had started at all, which is a
                # different case (see ParentWatchTest) and not this one.
                '"$1" "$2" "$3" >/dev/null 2>&1 & echo "$!"; sleep 1; exit 0',
                "bash",
                sys.executable,
                SESSION_TITLE,
                SESSION_A,
            ],
            env=self.env(AGENT_BOX_TITLE_INTERVAL="0.05"),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
        )
        out, _ = parent.communicate(timeout=60)
        watcher = int(out.split()[0])

        deadline = time.time() + 30
        while time.time() < deadline:
            try:
                os.kill(watcher, 0)
            except ProcessLookupError:
                return  # gone, as it must be
            time.sleep(0.1)
        os.kill(watcher, 9)
        self.fail("the title watcher outlived the process that started it")


if __name__ == "__main__":
    unittest.main()
