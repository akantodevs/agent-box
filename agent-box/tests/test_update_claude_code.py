"""Unit tests for update_claude_code.sh — the optional boot-time CLI upgrade.

The image owns the Claude Code version: the Dockerfile installs an exact
version resolved at build time, and the in-session auto-updater stays off
(the npm global dir is root-owned and sessions run as `claude`). A box that
is not rebuilt therefore stays where it is, which is the point — but it also
means a long-running box drifts behind the published CLI.

STARTUP_AUTO_UPDATE is the opt-in that closes that gap at the one moment it
is safe to: container startup, as root, before any session exists. Updating
mid-session would swap the CLI under a running conversation, and every
session in the box shares this one install.

The script is driven entirely by its environment and by `npm` on PATH, so the
tests run the *real* script with a fake npm and a fake claude in a temp bin
directory. Nothing is stubbed inside the script, no network is touched, and
the global npm install of the machine running the tests is never reached.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.abspath(os.path.join(TESTS_DIR, ".."))
SCRIPTS_DIR = os.path.join(IMAGE_DIR, "scripts")
UPDATE = os.path.join(SCRIPTS_DIR, "update_claude_code.sh")

PACKAGE = "@anthropic-ai/claude-code"


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


class UpdateScriptTestCase(unittest.TestCase):
    """Behaviour of scripts/update_claude_code.sh itself."""

    def setUp(self):
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.npm_log = os.path.join(self.bin, "npm.log")
        self.fake_npm(rc=0)
        self.fake_claude("2.1.278 (Claude Code)")

    # --- helpers -------------------------------------------------------

    def _install(self, name, body):
        path = os.path.join(self.bin, name)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(body)
        os.chmod(path, 0o755)
        return path

    def fake_npm(self, rc=0):
        """An npm that records its argv and exits with the given status."""
        self._install(
            "npm",
            "#!/bin/sh\n"
            f"printf '%s\\n' \"$*\" >> '{self.npm_log}'\n"
            f"exit {rc}\n",
        )

    def fake_claude(self, *versions):
        """A claude whose --version walks the given list across calls.

        One version repeats forever; several let a test see the reported
        version change across the update. The last entry sticks, so a script
        that asks one extra time still gets a sensible answer.
        """
        versions_file = os.path.join(self.bin, "claude.versions")
        with open(versions_file, "w", encoding="utf-8") as fh:
            fh.write("\n".join(versions) + "\n")
        count_file = os.path.join(self.bin, "claude.calls")
        self._install(
            "claude",
            "#!/bin/sh\n"
            f"count='{count_file}'\n"
            f"versions='{versions_file}'\n"
            'n=$(cat "$count" 2>/dev/null || echo 0)\n'
            "n=$((n + 1))\n"
            'echo "$n" > "$count"\n'
            'total=$(wc -l < "$versions")\n'
            '[ "$n" -gt "$total" ] && n="$total"\n'
            'sed -n "${n}p" "$versions"\n',
        )

    def npm_calls(self):
        if not os.path.exists(self.npm_log):
            return []
        return [line for line in read(self.npm_log).splitlines() if line.strip()]

    def run_update(self, value=None, expect_rc=0):
        env = {
            "PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
        }
        if value is not None:
            env["STARTUP_AUTO_UPDATE"] = value
        proc = subprocess.run(
            ["sh", UPDATE],
            env=env,
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=30,
        )
        self.assertEqual(
            proc.returncode,
            expect_rc,
            f"rc={proc.returncode}\nstdout={proc.stdout}\nstderr={proc.stderr}",
        )
        return proc

    # --- disabled ------------------------------------------------------

    def test_unset_flag_does_not_touch_npm(self):
        """The default is off: an unset flag must never reach the network."""
        self.run_update(value=None)
        self.assertEqual(self.npm_calls(), [])

    def test_explicit_no_does_not_touch_npm(self):
        self.run_update(value="No")
        self.assertEqual(self.npm_calls(), [])

    def test_unrecognized_value_does_not_touch_npm(self):
        """Fail-closed, like the git and terraform guards: only Yes enables."""
        self.run_update(value="maybe")
        self.assertEqual(self.npm_calls(), [])

    def test_disabled_run_reports_the_baked_version(self):
        proc = self.run_update(value=None)
        self.assertIn("2.1.278", proc.stdout)

    # --- enabled -------------------------------------------------------

    def test_yes_installs_the_latest_published_version(self):
        self.run_update(value="Yes")
        self.assertEqual(self.npm_calls(), ["install -g %s@latest" % PACKAGE])

    def test_yes_is_case_insensitive(self):
        for value in ("yes", "YES", "yEs", " Yes "):
            with self.subTest(value=value):
                self.setUp()
                self.run_update(value=value)
                self.assertEqual(len(self.npm_calls()), 1, value)

    def test_reports_the_version_it_moved_to(self):
        self.fake_claude("2.1.278 (Claude Code)", "2.1.280 (Claude Code)")
        proc = self.run_update(value="Yes")
        self.assertIn("2.1.278", proc.stdout)
        self.assertIn("2.1.280", proc.stdout)

    # --- failure must never cost the boot ------------------------------

    def test_npm_failure_is_a_warning_not_a_boot_failure(self):
        """No network, a bad registry, a yanked version: the box still starts
        on the version baked into the image."""
        self.fake_npm(rc=1)
        proc = self.run_update(value="Yes", expect_rc=0)
        self.assertIn("WARN", proc.stdout + proc.stderr)

    def test_missing_npm_is_a_warning_not_a_boot_failure(self):
        os.remove(os.path.join(self.bin, "npm"))
        proc = self.run_update(value="Yes", expect_rc=0)
        self.assertIn("WARN", proc.stdout + proc.stderr)


class WiringTestCase(unittest.TestCase):
    """The agreement between the script, the entrypoint, the image and the
    compose file. Nothing at runtime can detect a disagreement here: a script
    the entrypoint never calls is simply dead code, and a build arg the
    workflow never passes silently reinstalls whatever the build cache holds.
    """

    def setUp(self):
        self.entrypoint = read(IMAGE_DIR, "ep.sh")
        self.dockerfile = read(IMAGE_DIR, "Dockerfile")
        self.compose = read(IMAGE_DIR, "..", "docker-compose.yml")
        self.workflow = read(
            IMAGE_DIR, "..", ".github", "workflows", "publish-agent-box.yml"
        )

    def test_script_is_executable(self):
        self.assertTrue(os.access(UPDATE, os.X_OK), "%s is not executable" % UPDATE)

    def test_entrypoint_runs_the_updater(self):
        self.assertIn("update_claude_code.sh", self.entrypoint)

    def test_entrypoint_updates_before_launching_sessions(self):
        """An update must land before ttyd can hand anyone a session: the CLI
        is a single shared install, and swapping it under a live conversation
        is exactly what the disabled in-session auto-updater avoids."""
        self.assertLess(
            self.entrypoint.index("update_claude_code.sh"),
            self.entrypoint.index("ttyd -p"),
        )

    def test_entrypoint_passes_the_flag_through(self):
        self.assertIn("STARTUP_AUTO_UPDATE", self.entrypoint)

    def test_dockerfile_takes_the_version_as_a_build_arg(self):
        self.assertRegex(self.dockerfile, r"ARG CLAUDE_CODE_VERSION")
        self.assertRegex(
            self.dockerfile,
            r"npm install -g %s@\$\{CLAUDE_CODE_VERSION\}" % re_escape(PACKAGE),
        )

    def test_workflow_resolves_the_latest_version_and_passes_it(self):
        """Unpinned `npm install -g <pkg>` would be a build-cache hit forever:
        the layer's command text never changes, so CI would keep republishing
        whatever CLI version was current when the layer was first cached.
        Resolving the version into the command is what busts that cache."""
        self.assertIn("npm view %s version" % PACKAGE, self.workflow)
        self.assertIn("build-args", self.workflow)
        self.assertIn("CLAUDE_CODE_VERSION=", self.workflow)

    def test_compose_documents_the_flag(self):
        self.assertIn("STARTUP_AUTO_UPDATE", self.compose)


def re_escape(text):
    import re

    return re.escape(text)


if __name__ == "__main__":
    unittest.main()
