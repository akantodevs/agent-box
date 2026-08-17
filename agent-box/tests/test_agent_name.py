"""Unit tests for agent_name.sh — what this box is called.

The name is a display string with three sources, in this order: what the
operator set, what Docker calls the container, and the hostname. Only the first
is deliberate, so the other two are fallbacks rather than defaults — and both of
them can fail (no socket mounted, no daemon, a hostname that is a container id),
which is why the order matters more than any single answer.

`docker` and `hostname` are stubbed on PATH: the real ones would answer for the
container these tests are running *in*, which is the one thing they must not
depend on.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
AGENT_NAME_SH = os.path.join(SCRIPTS_DIR, "agent_name.sh")

CONTAINER_ID = "c830be2b252297183ce673bc1dbac81f2b1325b63e4afc43ed153eac53c98d3f"

# One real line from a container's /proc/self/mountinfo, which is where the id
# is read from: the hostname is only the id by default, and a deployment that
# sets `hostname:` in compose would otherwise hand `docker inspect` a name it
# cannot resolve.
MOUNTINFO = (
    "1234 1000 0:60 /containers/%s/resolv.conf /etc/resolv.conf rw,relatime "
    "- ext4 /dev/sdc rw\n" % CONTAINER_ID
)


class AgentNameTestCase(unittest.TestCase):
    def setUp(self):
        self.directory = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.directory, ignore_errors=True)
        self.bin = os.path.join(self.directory, "bin")
        os.makedirs(self.bin)
        self.docker_argv = os.path.join(self.directory, "docker.argv")
        self.stub("hostname", 'printf "%s\\n" "stub-host"\n')
        self.docker_answers("/agent-box-dev")

    def stub(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as handle:
            handle.write("#!/bin/sh\n" + body)
        os.chmod(path, 0o755)

    def docker_answers(self, output, status=0):
        """A `docker` that records its argv and answers with `output`."""
        self.stub(
            "docker",
            'printf "%s\\0" "$@" > "$DOCKER_ARGV_FILE"\n'
            'printf "%s\\n" "' + output + '"\n'
            "exit %d\n" % status,
        )

    def mountinfo(self, text=MOUNTINFO):
        path = os.path.join(self.directory, "mountinfo")
        with open(path, "w", encoding="utf-8") as handle:
            handle.write(text)
        return path

    def run_script(self, mountinfo=None, **overrides):
        env = {
            "PATH": self.bin + os.pathsep + os.environ["PATH"],
            "DOCKER_ARGV_FILE": self.docker_argv,
            "AGENT_BOX_MOUNTINFO": mountinfo if mountinfo is not None else self.mountinfo(),
        }
        for key, value in overrides.items():
            if value is not None:
                env[key] = value
        return subprocess.run(
            ["/bin/sh", AGENT_NAME_SH],
            env=env,
            stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=60,
        )

    def name(self, **kwargs):
        result = self.run_script(**kwargs)
        self.assertEqual(0, result.returncode, result.stderr)
        return result.stdout.decode("utf-8")

    def docker_argv_values(self):
        with open(self.docker_argv, "rb") as handle:
            parts = handle.read().split(b"\0")
        return [part.decode() for part in parts if part]


class SourceOrderTest(AgentNameTestCase):
    def test_an_explicit_name_wins(self):
        self.assertEqual("my-box\n", self.name(AGENT_NAME="my-box"))
        self.assertFalse(
            os.path.exists(self.docker_argv),
            "docker was asked even though the name was given",
        )

    def test_the_container_name_is_used_when_no_name_was_given(self):
        # Docker reports names with a leading slash; a tab title should not.
        self.assertEqual("agent-box-dev\n", self.name())

    def test_the_hostname_is_the_last_resort(self):
        # No socket mounted, no daemon, or a docker CLI that is not there:
        # every one of them ends here.
        self.docker_answers("", status=1)
        self.assertEqual("stub-host\n", self.name())

    def test_a_docker_that_answers_with_nothing_is_not_an_answer(self):
        self.docker_answers("", status=0)
        self.assertEqual("stub-host\n", self.name())

    def test_a_blank_name_is_treated_as_unset(self):
        # "AGENT_NAME=  " in a compose file is someone leaving it empty, not
        # naming a box after two spaces.
        self.assertEqual("agent-box-dev\n", self.name(AGENT_NAME="   "))


class ContainerLookupTest(AgentNameTestCase):
    def test_the_id_comes_from_mountinfo(self):
        self.name()
        self.assertIn(CONTAINER_ID, self.docker_argv_values())

    def test_without_mountinfo_the_hostname_is_asked_about(self):
        # The default case for Docker: the hostname *is* the short container id.
        self.name(mountinfo=os.path.join(self.directory, "nothing-here"))
        self.assertIn("stub-host", self.docker_argv_values())

    def test_mountinfo_without_a_container_id_falls_back_the_same_way(self):
        self.name(mountinfo=self.mountinfo("1 2 3:4 / / rw - ext4 /dev/sda rw\n"))
        self.assertIn("stub-host", self.docker_argv_values())


class OutputShapeTest(AgentNameTestCase):
    """The output is read by `$(...)` in ep.sh and printed into a title bar."""

    def test_exactly_one_line_is_printed(self):
        self.assertEqual(1, self.name().count("\n"))

    def test_nothing_is_printed_when_there_is_nothing_to_say(self):
        # Neither docker nor hostname answers: an empty line, and ep.sh carries
        # on with an unnamed box rather than failing to boot.
        self.docker_answers("", status=1)
        self.stub("hostname", "exit 1\n")
        self.assertEqual("\n", self.name())

    def test_the_script_says_nothing_on_stderr(self):
        # It runs inside a command substitution during boot; noise from a failed
        # docker lookup would land in the container log as if it were an error.
        self.docker_answers("", status=1)
        self.assertEqual(b"", self.run_script().stderr)


class SyntaxTest(unittest.TestCase):
    def test_the_script_parses_under_dash(self):
        done = subprocess.run(
            ["/bin/sh", "-n", AGENT_NAME_SH],
            stdout=subprocess.PIPE,
            stderr=subprocess.PIPE,
            timeout=30,
        )
        self.assertEqual(0, done.returncode, done.stderr)


if __name__ == "__main__":
    unittest.main()
