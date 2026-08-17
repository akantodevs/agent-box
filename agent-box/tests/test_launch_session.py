"""Unit tests for launch_session.sh — ttyd's entry point for every browser tab.

The script is the security boundary between a URL and a Claude process, so the
tests drive the *real* script with a real /bin/sh and assert on what it would
have executed. Two things are stubbed, both deliberately:

  * `su` — a stub earlier on PATH that records its argv and exits 0. The script
    execs su, so the stub replaces the process; the recorded argv is the only
    evidence of what would have launched. A real su would start a real Claude
    Code against a real transcript, which is exactly the corruption this script
    exists to prevent.
  * `start_claude.sh` — AGENT_BOX_SCRIPTS points at a temp directory holding a
    stub, so the command string the script builds names the stub and not the
    installed launcher. eval_command() below is then free to actually run that
    string, which is what pins the `su -c` quoting end to end.

session_store.py is *not* stubbed: it is symlinked into the temp scripts
directory, so the liveness gate is decided by the real module against a
tempfile CLAUDE_HOME. No signal other than 0 is ever sent to any pid (live_pid
stops its child by closing its stdin), and nothing here touches the real
~/.claude.
"""

import os
import shlex
import shutil
import signal
import subprocess
import sys
import tempfile
import threading
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from test_session_store import (
    FRESH,
    SESSION_A,
    SESSION_B,
    live_pid,
    write_registry,
    write_transcript,
)

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
LAUNCH_SESSION = os.path.join(SCRIPTS_DIR, "launch_session.sh")
SESSION_STORE = os.path.join(SCRIPTS_DIR, "session_store.py")

# The one path the script must never actually run.
REAL_START_CLAUDE = os.path.join(SCRIPTS_DIR, "start_claude.sh")

SU_STUB = """#!/bin/sh
# Records its argv NUL-separated and exits 0. NUL is the separator because the
# `su -c` command string may contain anything, newlines included.
printf '%s\\0' "$@" > "$SU_ARGV_FILE"
exit 0
"""

# Records what the `su -c` command string actually did: the arguments it passed
# to start_claude.sh, and the env values it set. Written by eval_command().
# NUL-separated records, because a value that contains a newline is exactly one
# of the things these tests are here to pin.
START_CLAUDE_STUB = """#!/bin/sh
{
    printf 'argc=%s\\0' "$#"
    for a in "$@"; do printf 'arg=%s\\0' "$a"; done
    printf 'cwd=%s\\0' "$PWD"
    printf 'CLAUDE_MODEL=%s\\0' "$CLAUDE_MODEL"
    printf 'ALLOW_TERRAFORM_MODIFY=%s\\0' "$ALLOW_TERRAFORM_MODIFY"
    printf 'REMOTE_CONTROL_NAME=%s\\0' "$REMOTE_CONTROL_NAME"
    printf 'AGENT_NAME=%s\\0' "$AGENT_NAME"
} > "$LAUNCH_RECORD"
"""


# A second stand-in for `su`, for the tests about closing a browser tab. It
# reproduces the two properties of the real chain that make a tab close hard to
# act on, both of them measured in a running container:
#
#   * util-linux su blocks almost every signal while it waits for its child
#     (SigBlk: fffffffe7ffb9ef9), SIGHUP among them — so ttyd's SIGHUP is never
#     acted on and sits pending on su forever;
#   * Claude Code puts itself in a new session (sid == pid, no controlling
#     terminal), so it is not in the process group ttyd signals either.
#
# The grandchild stands in for an MCP server: Claude's own children, which have
# no reason to outlive it.
SU_SESSION_STUB = '''#!/usr/bin/env python3
import os
import signal
import sys
import time


def record(path, pid):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(str(pid))


session = os.fork()
if session == 0:                                    # stands in for claude
    os.setsid()                                     # out of reach of the group
    signal.pthread_sigmask(signal.SIG_SETMASK, [])  # su's mask is inherited
    signal.signal(
        signal.SIGHUP,
        signal.SIG_IGN if os.environ.get("STUB_IGNORE_HUP") else signal.SIG_DFL,
    )
    mcp = os.fork()
    if mcp == 0:                                    # stands in for an MCP server
        signal.signal(signal.SIGHUP, signal.SIG_DFL)
        record(os.environ["STUB_MCP_PID_FILE"], os.getpid())
        time.sleep(600)
        os._exit(0)
    record(os.environ["STUB_SESSION_PID_FILE"], os.getpid())
    time.sleep(600)
    os._exit(0)

if os.environ.get("STUB_STDIN_FILE"):
    # Only reachable if the session was handed the terminal: a read of
    # /dev/null returns "" at once, and the test writes a line to prove it.
    record(os.environ["STUB_STDIN_FILE"], sys.stdin.readline().strip())

signal.pthread_sigmask(signal.SIG_BLOCK, {signal.SIGHUP, signal.SIGTERM})
while True:
    try:
        os.waitpid(session, 0)
        break
    except InterruptedError:
        continue
'''


def write_executable(path, text):
    with open(path, "w", encoding="utf-8") as handle:
        handle.write(text)
    os.chmod(path, 0o755)
    return path


class LaunchTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

        # Stub bin directory, first on PATH.
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.su_argv_file = os.path.join(self.bin, "su.argv")
        write_executable(os.path.join(self.bin, "su"), SU_STUB)

        # Stand-in AGENT_BOX_SCRIPTS: a stub start_claude.sh next to the real
        # session_store.py, so liveness is decided by the real module while the
        # launcher named in the command string is inert.
        self.scripts = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.scripts, ignore_errors=True)
        write_executable(
            os.path.join(self.scripts, "start_claude.sh"), START_CLAUDE_STUB
        )
        os.symlink(SESSION_STORE, os.path.join(self.scripts, "session_store.py"))
        self.launch_record = os.path.join(self.scripts, "launch.record")

    def script_env(self, **overrides):
        """The environment launch_session.sh runs under in these tests.

        Every environment variable the script reads is set explicitly — the test
        runner's own CLAUDE_MODEL or CLAUDE_HOME must not leak in and decide a
        result. The grace periods default to 0 so a refusal is immediate and a
        hangup does not linger; the tests that care about either set it
        themselves.
        """
        env = dict(os.environ)
        env.update(
            {
                "PATH": self.bin + os.pathsep + os.environ["PATH"],
                "SU_ARGV_FILE": self.su_argv_file,
                "CLAUDE_HOME": self.home,
                "AGENT_BOX_SCRIPTS": self.scripts,
                "AGENT_BOX_FAIL_DELAY": "0",
                "AGENT_BOX_LIVE_GRACE_MS": "0",
                "AGENT_BOX_HANGUP_GRACE_MS": "0",
                "CLAUDE_MODEL": "",
                "ALLOW_TERRAFORM_MODIFY": "",
                "REMOTE_CONTROL_NAME": "",
                "AGENT_NAME": "",
            }
        )
        for key, value in overrides.items():
            if value is None:
                env.pop(key, None)
            else:
                env[key] = value
        return env

    def run_script(self, *args, **overrides):
        """Run launch_session.sh to completion with the stubs in place."""
        return subprocess.run(
            [LAUNCH_SESSION, *args],
            env=self.script_env(**overrides),
            # Explicit, because the script dups its own stdin onto another
            # descriptor: whatever the test runner was started with must not
            # decide whether that succeeds.
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=120,
        )

    def su_argv(self):
        """The argv the stub su recorded, or None if su was never invoked."""
        if not os.path.exists(self.su_argv_file):
            return None
        with open(self.su_argv_file, "rb") as handle:
            raw = handle.read()
        parts = raw.split(b"\0")
        if parts and parts[-1] == b"":
            parts.pop()
        return [part.decode("utf-8", "replace") for part in parts]

    def assert_not_launched(self, result):
        """Nothing was started, and the failure was reported and readable."""
        self.assertIsNone(self.su_argv(), "su was invoked")
        self.assertEqual(1, result.returncode)
        stderr = result.stderr.decode("utf-8", "replace")
        self.assertIn("Refusing to resume", stderr)
        self.assertIn("admin page", stderr)
        return stderr

    def launched_command(self, result):
        """The `su -c` command string, asserting the su invocation is sane."""
        self.assertEqual(0, result.returncode, result.stderr)
        argv = self.su_argv()
        self.assertIsNotNone(argv, "su was not invoked")
        self.assertEqual(["-", "claude", "-c"], argv[:3])
        self.assertEqual(4, len(argv), argv)
        return argv[3]

    def eval_command(self, command):
        """Actually run the `su -c` string and return what start_claude.sh saw.

        This is what makes the quoting claims real rather than textual. It is
        only safe because the command names the stub launcher, which is checked
        before anything is executed.
        """
        self.assertIn(self.scripts, command)
        self.assertNotIn(REAL_START_CLAUDE, command)
        env = dict(os.environ)
        env["LAUNCH_RECORD"] = self.launch_record
        for key in ("CLAUDE_MODEL", "ALLOW_TERRAFORM_MODIFY", "REMOTE_CONTROL_NAME",
                    "AGENT_NAME"):
            env.pop(key, None)
        done = subprocess.run(
            ["/bin/sh", "-c", command],
            env=env,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )
        self.assertEqual(0, done.returncode, done.stderr)
        with open(self.launch_record, "rb") as handle:
            fields = handle.read().split(b"\0")
        record = {"arg": []}
        for field in fields[:-1]:  # the write ends with a separator
            key, _, value = field.decode("utf-8", "replace").partition("=")
            if key == "arg":
                record["arg"].append(value)
            else:
                record[key] = value
        return record


class NewSessionTest(LaunchTestCase):
    """No id at all, and the empty id an empty ?arg= produces, both start fresh."""

    def assert_new_session(self, result):
        command = self.launched_command(result)
        self.assertTrue(
            command.rstrip().endswith("start_claude.sh"),
            "a session id was passed to start_claude.sh: %r" % command,
        )
        self.assertNotIn("Refusing", result.stderr.decode())
        return command

    def test_no_argument_starts_a_new_session(self):
        self.assert_new_session(self.run_script())

    def test_empty_argument_starts_a_new_session(self):
        # ttyd's `?arg=` with no value arrives as one empty argument, which is
        # distinct from no argument at all. Both mean "new session"; neither is
        # a validation failure.
        result = self.run_script("")
        self.assert_new_session(result)

    def test_new_session_passes_no_argument_to_start_claude(self):
        # Not merely "no uuid in the string": start_claude.sh must see zero
        # arguments, not one empty one.
        record = self.eval_command(self.assert_new_session(self.run_script("")))
        self.assertEqual("0", record["argc"])
        self.assertEqual([], record["arg"])
        self.assertEqual("/workspace", record["cwd"])

    def test_a_new_session_never_consults_the_session_store(self):
        # There is nothing to look up, so a broken store must not stop a new
        # session from starting.
        os.unlink(os.path.join(self.scripts, "session_store.py"))
        self.assert_new_session(self.run_script(""))

    def test_defaults_to_the_installed_scripts_directory(self):
        # The env overrides exist for these tests; the container uses the
        # defaults, so the default has to be the installed path.
        result = self.run_script(AGENT_BOX_SCRIPTS=None)
        command = self.launched_command(result)
        self.assertIn("/opt/agent-box/scripts/start_claude.sh", command)


class ResumeTest(LaunchTestCase):
    def test_valid_uuid_with_a_transcript_resumes(self):
        write_transcript(self.home, SESSION_A, [])
        command = self.launched_command(self.run_script(SESSION_A))
        self.assertTrue(
            command.rstrip().endswith("start_claude.sh " + SESSION_A), command
        )

    def test_the_session_id_reaches_start_claude_as_one_argument(self):
        write_transcript(self.home, SESSION_A, [])
        record = self.eval_command(self.launched_command(self.run_script(SESSION_A)))
        self.assertEqual("1", record["argc"])
        self.assertEqual([SESSION_A], record["arg"])

    def test_a_transcript_in_a_second_project_directory_is_found(self):
        # The find is -mindepth 2 -maxdepth 2 over all project directories, not
        # just the workspace one.
        write_transcript(self.home, SESSION_A, [], project="-workspace")
        write_transcript(self.home, SESSION_B, [], project="-workspace-thing")
        command = self.launched_command(self.run_script(SESSION_B))
        self.assertTrue(command.rstrip().endswith(SESSION_B), command)

    def test_extra_arguments_are_ignored(self):
        # ttyd -a passes every ?arg= value through. Only $1 is ever read, so
        # extras are inert; this pins that rather than leaving it incidental.
        write_transcript(self.home, SESSION_A, [])
        command = self.launched_command(self.run_script(SESSION_A, "; rm -rf /", ""))
        self.assertTrue(command.rstrip().endswith("start_claude.sh " + SESSION_A))
        self.assertNotIn("rm -rf", command)


class EnvironmentTest(LaunchTestCase):
    """The values `su -` strips and this script therefore carries over."""

    def test_environment_is_passed_through(self):
        write_transcript(self.home, SESSION_A, [])
        result = self.run_script(
            SESSION_A,
            CLAUDE_MODEL="sonnet",
            ALLOW_TERRAFORM_MODIFY="Ask",
            REMOTE_CONTROL_NAME="agent-box-one",
            AGENT_NAME="agent-box-dev",
        )
        command = self.launched_command(result)
        self.assertIn("CLAUDE_MODEL=", command)
        self.assertIn("ALLOW_TERRAFORM_MODIFY=", command)
        self.assertIn("REMOTE_CONTROL_NAME=", command)
        self.assertIn("AGENT_NAME=", command)
        record = self.eval_command(command)
        self.assertEqual("sonnet", record["CLAUDE_MODEL"])
        self.assertEqual("Ask", record["ALLOW_TERRAFORM_MODIFY"])
        self.assertEqual("agent-box-one", record["REMOTE_CONTROL_NAME"])
        self.assertEqual("agent-box-dev", record["AGENT_NAME"])

    def test_unset_environment_arrives_empty(self):
        result = self.run_script(
            CLAUDE_MODEL=None,
            ALLOW_TERRAFORM_MODIFY=None,
            REMOTE_CONTROL_NAME=None,
            AGENT_NAME=None,
        )
        record = self.eval_command(self.launched_command(result))
        self.assertEqual("", record["CLAUDE_MODEL"])
        self.assertEqual("", record["ALLOW_TERRAFORM_MODIFY"])
        self.assertEqual("", record["REMOTE_CONTROL_NAME"])
        self.assertEqual("", record["AGENT_NAME"])

    def test_a_quote_in_an_environment_value_cannot_break_out(self):
        # These come from docker-compose.yml rather than from a URL, but they
        # are interpolated into a shell command string all the same: a single
        # quote closes the quoting and everything after it becomes commands.
        marker = os.path.join(self.scripts, "breakout")
        hostile = "one'; touch '%s'; echo '" % marker
        result = self.run_script(REMOTE_CONTROL_NAME=hostile)
        command = self.launched_command(result)
        # Well-formed shell, with the value intact as a single word.
        self.assertIn("REMOTE_CONTROL_NAME=" + hostile, shlex.split(command))
        record = self.eval_command(command)
        self.assertEqual(hostile, record["REMOTE_CONTROL_NAME"])
        self.assertFalse(os.path.exists(marker), "the command string broke out")

    def test_a_newline_in_an_environment_value_cannot_break_out(self):
        marker = os.path.join(self.scripts, "breakout-nl")
        hostile = "one\ntouch '%s'\n" % marker
        result = self.run_script(REMOTE_CONTROL_NAME=hostile)
        record = self.eval_command(self.launched_command(result))
        self.assertFalse(os.path.exists(marker), "the command string broke out")
        # A trailing newline is lost to the command substitution inside the
        # quoting helper — harmless, and the only thing that is not verbatim.
        self.assertEqual(hostile.rstrip("\n"), record["REMOTE_CONTROL_NAME"])


class MalformedIdTest(LaunchTestCase):
    """Anything that is not exactly a lowercase session id is refused."""

    # Every case runs with a real transcript for SESSION_A present, so a refusal
    # can only come from the format gate — not from the lookup that follows it.
    CASES = {
        "shell metacharacters": "; rm -rf /",
        "command substitution": "$(id)",
        "backticks": "`id`",
        "an absolute path": "/etc/passwd",
        "traversal": "../../etc/passwd",
        "a transcript path": "projects/-workspace/%s.jsonl" % SESSION_A,
        "a filename": SESSION_A + ".jsonl",
        "a trailing newline": SESSION_A + "\n",
        "a leading space": " " + SESSION_A,
        "a trailing space": SESSION_A + " ",
        "a second line": SESSION_A + "\nrm -rf /",
        "a null-ish suffix": SESSION_A + "0",
        "one character short": SESSION_A[:-1],
        "uppercase": SESSION_A.upper(),
        "not hex": "gggggggg-ed6a-4867-8f06-8f058939df11",
        "a glob": "????????-????-????-????-????????????",
        "the string new": "new",
        "a dot": ".",
        "a tilde": "~",
    }

    def test_malformed_ids_are_refused(self):
        for label, value in self.CASES.items():
            with self.subTest(label):
                self.setUp()  # a fresh su recorder per case
                write_transcript(self.home, SESSION_A, [])
                stderr = self.assert_not_launched(self.run_script(value))
                self.assertIn("is not a session id", stderr)

    def test_a_multiline_id_is_refused_by_the_format_gate(self):
        # Called out on its own because it is the one an `echo | grep -q '...$'`
        # gate lets through: grep is line-oriented and -q succeeds on any
        # matching line, so a value whose first line is a uuid passes — and the
        # second line would then be a separate command in the `su -c` string.
        write_transcript(self.home, SESSION_A, [])
        stderr = self.assert_not_launched(
            self.run_script(SESSION_A + "\ntouch /tmp/agent-box-launch-test")
        )
        self.assertIn("is not a session id", stderr)
        self.assertFalse(os.path.exists("/tmp/agent-box-launch-test"))

    def test_a_backslash_escape_is_not_expanded_into_the_message(self):
        # dash's echo expands \n; printf '%s' does not. If the gate used echo,
        # this value would become two lines on the way into it.
        write_transcript(self.home, SESSION_A, [])
        self.assert_not_launched(self.run_script(SESSION_A + "\\nrm -rf /"))

    def test_the_reported_id_carries_no_terminal_escapes(self):
        # The refusal is printed to a terminal, so the untrusted id is reduced
        # to characters a session id could contain before it is echoed back.
        # The text of the id may survive — it is the control characters that
        # would drive the terminal, and none of them may.
        write_transcript(self.home, SESSION_A, [])
        result = self.run_script("\x1b]0;title\x07\x1b[2J\r\x07")
        stderr = self.assert_not_launched(result)
        control = [c for c in stderr if ord(c) < 32 and c != "\n"]
        self.assertEqual([], control, repr(stderr))
        self.assertNotIn("\x1b", stderr)

    def test_a_very_long_id_is_refused_and_not_echoed_whole(self):
        result = self.run_script("f" * 5000)
        stderr = self.assert_not_launched(result)
        self.assertLess(len(stderr), 500)


class UnknownSessionTest(LaunchTestCase):
    def test_a_well_formed_id_with_no_transcript_is_refused(self):
        write_transcript(self.home, SESSION_A, [])
        stderr = self.assert_not_launched(self.run_script(SESSION_B))
        self.assertIn("no transcript", stderr)
        self.assertIn(SESSION_B, stderr)

    def test_a_missing_projects_directory_is_not_an_error(self):
        # Nothing has ever run in this home: projects/ does not exist. find must
        # not print to the terminal, and the script must refuse cleanly.
        self.assertFalse(os.path.exists(os.path.join(self.home, "projects")))
        stderr = self.assert_not_launched(self.run_script(SESSION_A))
        self.assertIn("no transcript", stderr)
        self.assertNotIn("No such file", stderr)
        self.assertNotIn("find:", stderr)

    def test_a_transcript_one_level_too_deep_is_not_found(self):
        # -mindepth 2 -maxdepth 2: projects/<project>/<id>.jsonl and nothing else.
        nested = os.path.join(self.home, "projects", "-workspace", "sub")
        os.makedirs(nested)
        open(os.path.join(nested, SESSION_A + ".jsonl"), "w").close()
        self.assert_not_launched(self.run_script(SESSION_A))


class LiveSessionTest(LaunchTestCase):
    """A second Claude on one transcript corrupts it, so a live id is refused."""

    def make_live(self, session_id=SESSION_A):
        write_transcript(self.home, session_id, [])
        pid = live_pid(self)
        # A fresh updatedAt: the store rejects a timestamp older than the
        # process it names as pid reuse, and would then call this dead.
        self.registry = write_registry(
            self.home,
            "%d.json" % pid,
            sessionId=session_id,
            pid=pid,
            updatedAt=FRESH,
            status="working",
        )
        return pid

    def test_a_live_session_is_refused(self):
        self.make_live()
        stderr = self.assert_not_launched(self.run_script(SESSION_A))
        self.assertIn("already open", stderr)

    def test_a_live_session_is_refused_only_after_the_grace_period(self):
        self.make_live()
        started = time.monotonic()
        result = self.run_script(SESSION_A, AGENT_BOX_LIVE_GRACE_MS="750")
        elapsed = time.monotonic() - started
        self.assert_not_launched(result)
        self.assertGreaterEqual(elapsed, 0.7)

    def test_a_session_that_dies_during_the_grace_period_is_resumed(self):
        # An F5 lands here: ttyd starts the new child before the old one has
        # finished dying, so the pid is still live when this check first runs.
        self.make_live()
        registry = self.registry

        def clear():
            time.sleep(0.5)
            os.unlink(registry)

        worker = threading.Thread(target=clear)
        worker.start()
        self.addCleanup(worker.join)
        started = time.monotonic()
        result = self.run_script(SESSION_A, AGENT_BOX_LIVE_GRACE_MS="3000")
        elapsed = time.monotonic() - started
        command = self.launched_command(result)
        self.assertTrue(command.rstrip().endswith("start_claude.sh " + SESSION_A))
        self.assertGreaterEqual(elapsed, 0.5, "it did not wait")

    def test_a_stale_registry_entry_does_not_block_a_resume(self):
        # The pid is gone; the file it left behind is not evidence of anything.
        write_transcript(self.home, SESSION_A, [])
        gone = subprocess.Popen([sys.executable, "-c", ""])
        gone.wait()
        write_registry(
            self.home,
            "%d.json" % gone.pid,
            sessionId=SESSION_A,
            pid=gone.pid,
            updatedAt=FRESH,
        )
        self.launched_command(self.run_script(SESSION_A))

    def test_another_sessions_liveness_does_not_block_this_one(self):
        self.make_live(SESSION_B)
        write_transcript(self.home, SESSION_A, [])
        self.launched_command(self.run_script(SESSION_A))

    def test_an_unanswerable_liveness_check_refuses(self):
        # Fail closed: a missing or broken session_store is not evidence that
        # nothing is running, and guessing wrong here destroys a conversation.
        write_transcript(self.home, SESSION_A, [])
        os.unlink(os.path.join(self.scripts, "session_store.py"))
        self.assert_not_launched(self.run_script(SESSION_A))


class FailureBehaviourTest(LaunchTestCase):
    def test_a_refusal_holds_the_message_on_screen(self):
        # ttyd closes the tab as soon as this exits, so the delay is the only
        # thing that lets a human read the reason.
        started = time.monotonic()
        result = self.run_script("nonsense", AGENT_BOX_FAIL_DELAY="1")
        elapsed = time.monotonic() - started
        self.assert_not_launched(result)
        self.assertGreaterEqual(elapsed, 1.0)

    def test_nothing_is_written_to_stdout_on_failure(self):
        result = self.run_script("nonsense")
        self.assert_not_launched(result)
        self.assertEqual(b"", result.stdout)


class HangupTest(LaunchTestCase):
    """Closing the browser tab must end the session, not orphan it.

    ttyd signals the process group of its own child and nothing else, and
    neither the `su` in the middle nor the Claude process at the end of the
    chain can be reached that way (see SU_SESSION_STUB). A session that outlives
    its tab keeps its <pid>.json registry entry alive, so the liveness gate goes
    on refusing to resume it — for as long as the container runs.
    """

    def setUp(self):
        super().setUp()
        write_executable(os.path.join(self.bin, "su"), SU_SESSION_STUB)
        self.session_pid_file = os.path.join(self.bin, "session.pid")
        self.mcp_pid_file = os.path.join(self.bin, "mcp.pid")
        self.stdin_file = os.path.join(self.bin, "stdin.seen")

    def start(self, *args, **overrides):
        """Start the script and leave it running, as ttyd does.

        start_new_session puts it in a process group of its own, so the test can
        signal that group the way ttyd signals its child's — and so a stray
        signal cannot reach the test runner instead.
        """
        env = self.script_env(**overrides)
        env.update(
            {
                "STUB_SESSION_PID_FILE": self.session_pid_file,
                "STUB_MCP_PID_FILE": self.mcp_pid_file,
            }
        )
        proc = subprocess.Popen(
            [LAUNCH_SESSION, *args],
            env=env,
            stdin=subprocess.PIPE,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            start_new_session=True,
        )
        self.addCleanup(self.stop, proc)
        return proc

    def stop(self, proc):
        """Leave nothing of a failed test behind."""
        for path in (self.session_pid_file, self.mcp_pid_file):
            pid = self.read_pid(path)
            if pid:
                try:
                    os.kill(pid, signal.SIGKILL)
                except OSError:
                    pass
        if proc.poll() is None:
            try:
                os.killpg(proc.pid, signal.SIGKILL)
            except OSError:
                pass
        proc.wait(timeout=30)
        for stream in (proc.stdin, proc.stdout, proc.stderr):
            if stream:
                stream.close()

    @staticmethod
    def read_pid(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return int(handle.read().strip())
        except (OSError, ValueError):
            return None

    def await_pid(self, path, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            pid = self.read_pid(path)
            if pid:
                return pid
            time.sleep(0.02)
        self.fail("the session stub never recorded a pid in %s" % path)

    def assert_alive(self, pid):
        os.kill(pid, 0)  # raises if it is gone, which is the assertion

    def assert_gone(self, pid, timeout=30):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            try:
                os.kill(pid, 0)
            except ProcessLookupError:
                return
            time.sleep(0.02)
        self.fail("pid %d outlived the browser tab" % pid)

    def hangup(self, proc):
        """Exactly what ttyd does when a client disconnects."""
        os.killpg(proc.pid, signal.SIGHUP)

    def test_a_closed_tab_terminates_the_session(self):
        proc = self.start()
        session = self.await_pid(self.session_pid_file)
        self.assert_alive(session)
        self.hangup(proc)
        self.assert_gone(session)

    def test_a_closed_tab_takes_the_sessions_own_children_with_it(self):
        # Claude's MCP servers are its children, and an orphaned one goes on
        # holding a browser, a port or a socket for the life of the container.
        proc = self.start()
        session = self.await_pid(self.session_pid_file)
        mcp = self.await_pid(self.mcp_pid_file)
        self.hangup(proc)
        self.assert_gone(session)
        self.assert_gone(mcp)

    def test_the_script_exits_once_it_has_ended_the_session(self):
        # ttyd holds the pty open until its child exits, so the script must not
        # sit there after doing the killing.
        proc = self.start()
        self.await_pid(self.session_pid_file)
        self.hangup(proc)
        self.assertEqual(0, proc.wait(timeout=30))

    def test_a_closed_tab_is_asked_before_it_is_killed(self):
        # A grace period this test never waits out: if the session only ever
        # went away because SIGKILL eventually followed, this would sit here for
        # thirty seconds and fail. Claude flushes a transcript on the way out,
        # so the polite path has to be the one that normally runs.
        proc = self.start(AGENT_BOX_HANGUP_GRACE_MS="30000")
        session = self.await_pid(self.session_pid_file)
        self.hangup(proc)
        self.assert_gone(session, timeout=15)

    def test_a_session_that_ignores_the_hangup_is_killed(self):
        # SIGHUP asks; it does not compel. Whatever the reason — a wedged
        # process, a handler that never returns — the session has to be gone by
        # the end of the grace period, or the transcript stays unreachable.
        proc = self.start(STUB_IGNORE_HUP="1", AGENT_BOX_HANGUP_GRACE_MS="500")
        session = self.await_pid(self.session_pid_file)
        self.hangup(proc)
        self.assert_gone(session)

    def test_the_session_is_given_the_terminal_and_not_dev_null(self):
        # The regression this pins: dash hands an asynchronous command
        # /dev/null for stdin when job control is off, and an explicit `<&0`
        # does not undo it — fd 0 has already been replaced by the time
        # redirections are applied. A session that reads /dev/null gets EOF
        # instead of a keyboard, which is a terminal nobody can type into.
        proc = self.start(STUB_STDIN_FILE=self.stdin_file)
        proc.stdin.write(b"typed-by-the-user\n")
        proc.stdin.flush()
        deadline = time.monotonic() + 30
        while time.monotonic() < deadline:
            if self.read_text(self.stdin_file):
                break
            time.sleep(0.02)
        self.assertEqual("typed-by-the-user", self.read_text(self.stdin_file))

    @staticmethod
    def read_text(path):
        try:
            with open(path, encoding="utf-8") as handle:
                return handle.read().strip()
        except OSError:
            return ""

    def test_a_hangup_during_the_liveness_wait_starts_nothing(self):
        # The tab can close while the script is still waiting for a previous
        # session to let go of the transcript. Nothing has been launched yet, so
        # there is nothing to kill — but the script must not go on to launch one
        # after its terminal has gone away.
        self.make_live_session()
        proc = self.start(SESSION_A, AGENT_BOX_LIVE_GRACE_MS="30000")
        time.sleep(0.5)
        self.hangup(proc)
        proc.wait(timeout=30)
        self.assertIsNone(self.read_pid(self.session_pid_file))

    def make_live_session(self):
        write_transcript(self.home, SESSION_A, [])
        pid = live_pid(self)
        write_registry(
            self.home,
            "%d.json" % pid,
            sessionId=SESSION_A,
            pid=pid,
            updatedAt=FRESH,
            status="working",
        )


class ExitStatusTest(LaunchTestCase):
    def test_the_sessions_exit_status_is_the_scripts_exit_status(self):
        # The script used to exec su, so su's status was its own by
        # construction. It waits for it now instead, and that has to stay true:
        # ttyd reports a non-zero exit in the terminal.
        write_executable(os.path.join(self.bin, "su"), "#!/bin/sh\nexit 7\n")
        self.assertEqual(7, self.run_script().returncode)


class ScriptHygieneTest(unittest.TestCase):
    def test_the_script_is_executable(self):
        self.assertTrue(os.access(LAUNCH_SESSION, os.X_OK))

    def test_it_parses_as_posix_sh(self):
        done = subprocess.run(
            ["/bin/sh", "-n", LAUNCH_SESSION], stderr=subprocess.PIPE, timeout=30
        )
        self.assertEqual(0, done.returncode, done.stderr)

    def test_it_parses_as_dash(self):
        dash = shutil.which("dash")
        if not dash:
            self.skipTest("dash not installed")
        done = subprocess.run(
            [dash, "-n", LAUNCH_SESSION], stderr=subprocess.PIPE, timeout=30
        )
        self.assertEqual(0, done.returncode, done.stderr)

    def test_it_has_a_posix_shebang(self):
        with open(LAUNCH_SESSION, encoding="utf-8") as handle:
            self.assertEqual("#!/bin/sh", handle.readline().strip())


if __name__ == "__main__":
    unittest.main()
