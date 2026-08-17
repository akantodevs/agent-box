"""Unit tests for start_claude.sh — the last step before a Claude process exists.

The script's whole body is `exec claude`, so the tests drive the *real* script
with a real bash and assert on the argv the exec would have used. One thing is
stubbed, and it is not optional:

  * `claude` — a stub first on PATH that records its argv NUL-separated and
    exits 0. Launching the real binary here would open a second Claude Code on
    a live transcript, which corrupts the conversation irrecoverably; the whole
    session-selection machinery exists to prevent exactly that. Every run also
    gets stdin from /dev/null and a timeout, so a stub that somehow failed to
    shadow the real CLI would die rather than sit at a prompt, and
    ClaudeStubTest below pins the shadowing itself.

session_store.py is *not* stubbed: AGENT_BOX_SCRIPTS points at the real scripts
directory, so the Remote Control suffix is decided by the real slug lookup
against a tempfile CLAUDE_HOME. AGENT_BOX_LOG likewise redirects the connection
banner away from /var/log/container.log — the container's real log is being
tailed by ep.sh while these tests run, and a test suite has no business
appending to it.
"""

import os
import re
import shutil
import subprocess
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_session_store import (
    SESSION_A,
    SESSION_B,
    ai_title,
    write_transcript,
)

import session_store

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
START_CLAUDE = os.path.join(SCRIPTS_DIR, "start_claude.sh")

# The binary that must never actually run.
REAL_CLAUDE = "/usr/bin/claude"

CLAUDE_STUB = """#!/bin/sh
# Records its argv NUL-separated and exits 0. NUL is the separator because a
# Remote Control name comes from docker-compose.yml and may contain anything,
# spaces and newlines included — which is the point of asserting on argv.
printf '%s\\0' "$@" > "$CLAUDE_ARGV_FILE"
exit 0
"""

# The real session whose title lands exactly on slugify's 32-character limit.
TITLE = "Add Terraform to container image"
TITLE_SLUG = "add-terraform-to-container-image"


class StartTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.argv_file = os.path.join(self.bin, "claude.argv")
        stub = os.path.join(self.bin, "claude")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(CLAUDE_STUB)
        os.chmod(stub, 0o755)

        self.log = os.path.join(self.bin, "container.log")

    def env(self, **overrides):
        """A clean environment for the script.

        Every variable the script reads is set explicitly: the test runner is
        itself a Claude Code session, so CLAUDE_MODEL, REMOTE_CONTROL_NAME,
        AGENT_NAME and CLAUDE_HOME are all plausibly set in the inherited
        environment and would otherwise decide the results.
        """
        env = dict(os.environ)
        env.update(
            {
                "PATH": self.bin + os.pathsep + os.environ["PATH"],
                "CLAUDE_ARGV_FILE": self.argv_file,
                "CLAUDE_HOME": self.home,
                "AGENT_BOX_SCRIPTS": SCRIPTS_DIR,
                "AGENT_BOX_LOG": self.log,
            }
        )
        env.pop("CLAUDE_MODEL", None)
        env.pop("REMOTE_CONTROL_NAME", None)
        env.pop("ALLOW_TERRAFORM_MODIFY", None)
        env.pop("AGENT_NAME", None)
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_script(self, *args, **overrides):
        return subprocess.run(
            ["/bin/bash", START_CLAUDE, *args],
            env=self.env(**overrides),
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def argv(self, result):
        """The argv the stub claude recorded."""
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertTrue(
            os.path.exists(self.argv_file),
            "claude was never invoked: %r" % (result.stderr,),
        )
        with open(self.argv_file, "rb") as handle:
            parts = handle.read().split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [part.decode("utf-8", "replace") for part in parts]

    def launch(self, *args, **overrides):
        return self.argv(self.run_script(*args, **overrides))

    def value_after(self, argv, flag):
        """The argument following a flag, asserting the flag appears once."""
        self.assertEqual(1, argv.count(flag), "%s in %r" % (flag, argv))
        index = argv.index(flag)
        self.assertLess(index + 1, len(argv), "%s has no value in %r" % (flag, argv))
        return argv[index + 1]


class ClaudeStubTest(StartTestCase):
    """The safety net every other test in this file rests on."""

    def test_claude_resolves_to_the_stub_not_the_real_cli(self):
        found = subprocess.run(
            ["/bin/bash", "-c", "command -v claude"],
            env=self.env(),
            stdout=subprocess.PIPE,
            timeout=30,
        )
        resolved = found.stdout.decode().strip()
        self.assertEqual(os.path.join(self.bin, "claude"), resolved)
        self.assertNotEqual(REAL_CLAUDE, resolved)

    def test_the_script_never_names_the_real_cli(self):
        with open(START_CLAUDE, encoding="utf-8") as handle:
            self.assertNotIn(REAL_CLAUDE, handle.read())


class SyntaxTest(unittest.TestCase):
    def test_the_script_parses(self):
        done = subprocess.run(
            ["/bin/bash", "-n", START_CLAUDE],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(0, done.returncode, done.stderr)

    def test_the_shebang_is_bash(self):
        """The script uses arrays, which are a syntax error under dash."""
        with open(START_CLAUDE, encoding="utf-8") as handle:
            self.assertEqual("#!/bin/bash", handle.readline().strip())


class SessionSelectionTest(StartTestCase):
    def test_no_argument_starts_a_new_session_with_a_generated_id(self):
        argv = self.launch()
        self.assertNotIn("--resume", argv)
        self.assertNotIn("--continue", argv)
        generated = self.value_after(argv, "--session-id")
        self.assertTrue(session_store.is_uuid(generated), generated)

    def test_an_id_argument_resumes_that_session(self):
        argv = self.launch(SESSION_A)
        self.assertNotIn("--session-id", argv)
        self.assertNotIn("--continue", argv)
        self.assertEqual(SESSION_A, self.value_after(argv, "--resume"))

    def test_an_empty_argument_is_a_new_session(self):
        """launch_session.sh passes no argument, but ttyd's `?arg=` can be
        empty and an empty first argument must not become `--resume ''` —
        which is claude's interactive session picker, not a session."""
        argv = self.launch("")
        self.assertNotIn("--resume", argv)
        self.assertTrue(session_store.is_uuid(self.value_after(argv, "--session-id")))

    def test_two_new_sessions_get_different_ids(self):
        first = self.value_after(self.launch(), "--session-id")
        os.remove(self.argv_file)
        second = self.value_after(self.launch(), "--session-id")
        self.assertNotEqual(first, second)

    def test_extra_arguments_are_ignored(self):
        argv = self.launch(SESSION_A, SESSION_B)
        self.assertEqual(SESSION_A, self.value_after(argv, "--resume"))
        self.assertNotIn(SESSION_B, argv)


class ModelTest(StartTestCase):
    def test_model_comes_from_claude_model(self):
        argv = self.launch(CLAUDE_MODEL="sonnet")
        self.assertEqual("sonnet", self.value_after(argv, "--model"))

    def test_model_defaults_to_opus_when_unset(self):
        argv = self.launch(CLAUDE_MODEL=None)
        self.assertEqual("opus", self.value_after(argv, "--model"))

    def test_model_defaults_to_opus_when_empty(self):
        """launch_session.sh always passes the variable, empty when unset, so
        the empty case is the live one — `${CLAUDE_MODEL-opus}` would leave
        claude with `--model ''`."""
        argv = self.launch(CLAUDE_MODEL="")
        self.assertEqual("opus", self.value_after(argv, "--model"))


class PermissionsTest(StartTestCase):
    def test_skip_permissions_on_a_new_session(self):
        self.assertIn("--dangerously-skip-permissions", self.launch())

    def test_skip_permissions_on_a_resumed_session(self):
        self.assertIn("--dangerously-skip-permissions", self.launch(SESSION_A))

    def test_skip_permissions_with_remote_control(self):
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME="box")
        self.assertIn("--dangerously-skip-permissions", argv)


class RemoteControlTest(StartTestCase):
    def test_no_flag_when_the_name_is_unset(self):
        """Today's default, and the one regression that would silently expose
        every deployment to Remote Control."""
        self.assertNotIn("--remote-control", self.launch(REMOTE_CONTROL_NAME=None))

    def test_no_flag_when_the_name_is_empty(self):
        self.assertNotIn("--remote-control", self.launch(REMOTE_CONTROL_NAME=""))

    def test_no_flag_when_unset_even_with_a_titled_session(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME=None)
        self.assertNotIn("--remote-control", argv)

    def test_suffix_is_the_slugified_ai_title(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME="agent-box-dev")
        self.assertEqual(
            "agent-box-dev-" + TITLE_SLUG, self.value_after(argv, "--remote-control")
        )

    def test_suffix_falls_back_to_the_id_head_without_an_ai_title(self):
        """The live path, not a theoretical one: a session keeps its
        first-prompt name until Claude titles it."""
        write_transcript(self.home, SESSION_A, [])
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME="agent-box-dev")
        self.assertEqual(
            "agent-box-dev-2026fc52", self.value_after(argv, "--remote-control")
        )

    def test_suffix_falls_back_when_no_transcript_exists_at_all(self):
        argv = self.launch(SESSION_B, REMOTE_CONTROL_NAME="agent-box-dev")
        self.assertEqual(
            "agent-box-dev-" + SESSION_B[:8], self.value_after(argv, "--remote-control")
        )

    def test_new_session_suffix_is_the_head_of_the_generated_id(self):
        """No transcript exists yet, so the slug lookup must come back empty
        and the id it falls back to must be the id claude is told to use."""
        argv = self.launch(REMOTE_CONTROL_NAME="agent-box-dev")
        generated = self.value_after(argv, "--session-id")
        name = self.value_after(argv, "--remote-control")
        self.assertEqual("agent-box-dev-" + generated[:8], name)
        self.assertEqual(8, len(name.rsplit("-", 1)[1]))

    def test_the_name_is_one_argv_element(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME="agent box dev")
        self.assertEqual(
            "agent box dev-" + TITLE_SLUG, self.value_after(argv, "--remote-control")
        )

    def test_a_name_with_shell_metacharacters_neither_splits_nor_injects(self):
        """The name comes from docker-compose.yml and is interpolated into a
        shell-built argument; a quote or a $( ) in it must stay text."""
        marker = os.path.join(self.bin, "pwned")
        name = "Anders' box; touch %s $(touch %s) `touch %s`" % (
            marker,
            marker,
            marker,
        )
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        argv = self.launch(SESSION_A, REMOTE_CONTROL_NAME=name)
        self.assertEqual(
            name + "-" + TITLE_SLUG, self.value_after(argv, "--remote-control")
        )
        self.assertFalse(os.path.exists(marker), "the name executed")

    def test_a_session_id_is_never_reinterpreted_by_the_shell(self):
        """launch_session.sh vouches for the id, but this script is where it
        becomes an argument, so it is asserted here too."""
        marker = os.path.join(self.bin, "pwned2")
        argv = self.launch("$(touch %s)" % marker, REMOTE_CONTROL_NAME="box")
        self.assertEqual("$(touch %s)" % marker, self.value_after(argv, "--resume"))
        self.assertFalse(os.path.exists(marker), "the id executed")


class SlugLookupTest(StartTestCase):
    """The contract start_claude.sh depends on, asserted directly."""

    def slug(self, session_id):
        done = subprocess.run(
            [sys.executable, os.path.join(SCRIPTS_DIR, "session_store.py"),
             "--slug", session_id],
            env=self.env(),
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        return done.returncode, done.stdout.decode()

    def test_an_unknown_session_is_empty_and_successful(self):
        """A brand-new session has no transcript when the lookup runs."""
        self.assertEqual((0, "\n"), self.slug(SESSION_A))

    def test_a_titled_session_yields_its_slug(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        self.assertEqual((0, TITLE_SLUG + "\n"), self.slug(SESSION_A))


class ArgvShapeTest(StartTestCase):
    def test_the_whole_argv_for_a_resumed_session(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        argv = self.launch(SESSION_A, CLAUDE_MODEL="opus", REMOTE_CONTROL_NAME="box")
        self.assertEqual(
            [
                "--model", "opus",
                "--resume", SESSION_A,
                "--dangerously-skip-permissions",
                "--remote-control", "box-" + TITLE_SLUG,
            ],
            argv,
        )

    def test_the_whole_argv_for_a_new_session_without_remote_control(self):
        argv = self.launch()
        self.assertEqual(["--model", "opus", "--session-id"], argv[:3])
        self.assertEqual(["--dangerously-skip-permissions"], argv[4:])


class ContainerLogTest(StartTestCase):
    """ep.sh tails one log file and every tab runs this script, so the banner
    has to accumulate rather than truncate what the other sessions wrote."""

    def test_the_banner_is_appended_not_truncated(self):
        with open(self.log, "w", encoding="utf-8") as handle:
            handle.write("Container ready.\n")
        self.launch(SESSION_A)
        os.remove(self.argv_file)
        self.launch(SESSION_B)
        with open(self.log, encoding="utf-8") as handle:
            lines = handle.read().splitlines()
        self.assertEqual("Container ready.", lines[0])
        self.assertEqual(3, len(lines), lines)
        self.assertIn(SESSION_A, lines[1])
        self.assertIn(SESSION_B, lines[2])

    def test_an_unwritable_log_does_not_stop_the_launch(self):
        """The log is a convenience; the terminal is the product."""
        result = self.run_script(SESSION_A, AGENT_BOX_LOG="/proc/nonexistent/log")
        self.assertEqual(SESSION_A, self.value_after(self.argv(result), "--resume"))

    def test_an_unwritable_log_says_nothing_on_the_terminal(self):
        """A failed append must not greet the operator with a shell error —
        `2>/dev/null` after the redirection is too late to swallow it."""
        result = self.run_script(SESSION_A, AGENT_BOX_LOG="/proc/nonexistent/log")
        self.assertEqual(b"", result.stderr)


class EnvironmentTest(StartTestCase):
    """The exports Claude and its hook children need survive the rewrite."""

    def _exported(self, name, **overrides):
        probe = os.path.join(self.bin, "claude")
        with open(probe, "w", encoding="utf-8") as handle:
            handle.write('#!/bin/sh\nprintf \'%%s\' "${%s-<unset>}" > "$CLAUDE_ARGV_FILE"\n' % name)
        os.chmod(probe, 0o755)
        self.run_script(SESSION_A, **overrides)
        with open(self.argv_file, encoding="utf-8") as handle:
            return handle.read()

    def test_terraform_guard_mode_is_exported(self):
        self.assertEqual("Ask", self._exported("ALLOW_TERRAFORM_MODIFY",
                                               ALLOW_TERRAFORM_MODIFY="Ask"))

    def test_terraform_guard_mode_is_exported_empty_when_unset(self):
        """Unset would let the hook read some other value; empty fails closed."""
        self.assertEqual("", self._exported("ALLOW_TERRAFORM_MODIFY",
                                            ALLOW_TERRAFORM_MODIFY=None))

    def test_autoupdater_is_disabled(self):
        self.assertEqual("1", self._exported("DISABLE_AUTOUPDATER"))

    def test_playwright_browsers_path_is_exported(self):
        self.assertEqual("/ms-playwright", self._exported("PLAYWRIGHT_BROWSERS_PATH"))

    def test_the_box_name_is_exported(self):
        """The tab-title watcher reads it from the environment, and it arrives
        here as a command-prefix assignment from launch_session.sh."""
        self.assertEqual("agent-box-dev",
                         self._exported("AGENT_NAME", AGENT_NAME="agent-box-dev"))

    def test_the_box_name_is_exported_empty_when_unset(self):
        self.assertEqual("", self._exported("AGENT_NAME", AGENT_NAME=None))

    def test_claude_does_not_title_the_terminal_itself(self):
        """Two writers, one title bar: Claude Code renames the terminal after
        whatever it is doing, which would overwrite the session name the tab is
        there to show. The tab is named by session_title.py, so this is off."""
        self.assertEqual("1", self._exported("CLAUDE_CODE_DISABLE_TERMINAL_TITLE"))


class TabTitleTest(StartTestCase):
    """The terminal is named after the session before Claude takes it over.

    The watcher is backgrounded and writes to this script's stdout, which is the
    pty in the container and a pipe here — so the escape sequence it writes is
    visible to these tests exactly as ttyd's client sees it.
    """

    # Like CLAUDE_STUB, but alive for a moment. start_claude.sh backgrounds the
    # watcher and immediately execs claude; a stub that exits in microseconds
    # would routinely be gone before the watcher's first write, which is a race
    # only the test has (a real session lasts minutes).
    SLOW_CLAUDE_STUB = """#!/bin/sh
printf '%s\\0' "$@" > "$CLAUDE_ARGV_FILE"
sleep 1
exit 0
"""

    def setUp(self):
        super().setUp()
        stub = os.path.join(self.bin, "claude")
        with open(stub, "w", encoding="utf-8") as handle:
            handle.write(self.SLOW_CLAUDE_STUB)
        os.chmod(stub, 0o755)

    def title(self, result):
        """The last OSC title written to the terminal, or None."""
        found = re.findall(r"\033\]0;(.*?)\007", result.stdout.decode("utf-8", "replace"))
        return found[-1] if found else None

    def test_a_resumed_session_names_the_tab_after_itself(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        result = self.run_script(SESSION_A)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Agent: " + TITLE, self.title(result))

    def test_a_new_session_says_so_until_it_has_a_name(self):
        result = self.run_script()
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual("Agent: new session", self.title(result))

    def test_the_box_name_is_on_the_end_of_the_tab_name(self):
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        result = self.run_script(SESSION_A, AGENT_NAME="agent-box-dev")
        self.assertEqual("Agent: %s · agent-box-dev" % TITLE, self.title(result))

    def test_the_terminal_carries_nothing_but_the_title_sequence(self):
        """Whatever else the watcher has to say, it does not say it here.

        This stdout is the terminal Claude Code is about to draw on: a stray
        line from a background process would land in the middle of its UI.
        """
        write_transcript(self.home, SESSION_A, [ai_title(TITLE)])
        result = self.run_script(SESSION_A)
        self.assertEqual("\033]0;Agent: %s\007" % TITLE, result.stdout.decode())


if __name__ == "__main__":
    unittest.main()
