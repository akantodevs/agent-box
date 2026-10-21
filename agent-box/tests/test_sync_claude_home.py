"""Unit tests for sync_claude_home.sh — the boot-time mirror of image-owned
Claude Code content into the state volume.

The script is driven entirely by two environment variables, so the tests run the
*real* script against temp directories: AGENT_BOX_CLAUDE_HOME_SRC stands in for the
baked tree at /opt/agent-box/claude-home, and CLAUDE_STATE_DIR for ~/.claude. Nothing
is stubbed, and nothing outside the temp directories is touched.
"""

import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
SYNC = os.path.join(SCRIPTS_DIR, "sync_claude_home.sh")

MANIFEST = ".agent-box-synced"


class SyncTestCase(unittest.TestCase):
    def setUp(self):
        self.src = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.src, ignore_errors=True)
        self.dest = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dest, ignore_errors=True)

    # --- helpers -------------------------------------------------------

    def write(self, root, relpath, content):
        """Create a file under root, making parent directories as needed."""
        path = os.path.join(root, relpath)
        os.makedirs(os.path.dirname(path), exist_ok=True)
        with open(path, "w", encoding="utf-8") as fh:
            fh.write(content)
        return path

    def read(self, root, relpath):
        with open(os.path.join(root, relpath), encoding="utf-8") as fh:
            return fh.read()

    def exists(self, root, relpath):
        return os.path.exists(os.path.join(root, relpath))

    def run_sync(self, expect_rc=0, **env):
        proc = subprocess.run(
            ["sh", SYNC],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "AGENT_BOX_CLAUDE_HOME_SRC": self.src,
                "CLAUDE_STATE_DIR": self.dest,
                **env,
            },
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

    # --- tests ---------------------------------------------------------

    def test_fresh_destination_receives_manual_and_skills(self):
        self.write(self.src, "CLAUDE.md", "MANUAL v1\n")
        self.write(self.src, "skills/README.md", "scaffold\n")
        self.write(self.src, "skills/demo/SKILL.md", "demo skill\n")

        self.run_sync()

        self.assertEqual(self.read(self.dest, "CLAUDE.md"), "MANUAL v1\n")
        self.assertEqual(self.read(self.dest, "skills/README.md"), "scaffold\n")
        self.assertEqual(self.read(self.dest, "skills/demo/SKILL.md"), "demo skill\n")

    def test_image_wins_over_outdated_manual(self):
        self.write(self.dest, "CLAUDE.md", "MANUAL v1 (stale)\n")
        self.write(self.src, "CLAUDE.md", "MANUAL v2\n")

        self.run_sync()

        self.assertEqual(self.read(self.dest, "CLAUDE.md"), "MANUAL v2\n")

    def test_sync_is_idempotent(self):
        self.write(self.src, "CLAUDE.md", "MANUAL\n")
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")

        self.run_sync()
        first = self.read(self.dest, MANIFEST)
        self.run_sync()

        self.assertEqual(self.read(self.dest, MANIFEST), first)
        self.assertEqual(self.read(self.dest, "skills/demo/SKILL.md"), "demo\n")

    def test_user_created_skill_survives_sync(self):
        self.write(self.dest, "skills/my-own/SKILL.md", "hand-written\n")
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")

        self.run_sync()

        self.assertEqual(self.read(self.dest, "skills/my-own/SKILL.md"), "hand-written\n")
        self.assertEqual(self.read(self.dest, "skills/demo/SKILL.md"), "demo\n")

    def test_baked_skill_replaces_same_named_user_skill(self):
        self.write(self.dest, "skills/demo/SKILL.md", "user version\n")
        self.write(self.dest, "skills/demo/leftover.md", "stale file\n")
        self.write(self.src, "skills/demo/SKILL.md", "image version\n")

        self.run_sync()

        self.assertEqual(self.read(self.dest, "skills/demo/SKILL.md"), "image version\n")
        # Replaced wholesale, not merged — no file survives from the old directory.
        self.assertFalse(self.exists(self.dest, "skills/demo/leftover.md"))

    def test_skill_dropped_from_image_is_removed(self):
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")
        self.run_sync()
        self.assertTrue(self.exists(self.dest, "skills/demo/SKILL.md"))

        # Next image no longer ships it.
        shutil.rmtree(os.path.join(self.src, "skills/demo"))
        self.run_sync()

        self.assertFalse(self.exists(self.dest, "skills/demo"))

    def test_stale_removal_never_touches_user_skills(self):
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")
        self.run_sync()

        # User adds their own skill, then the image drops its skill entirely.
        self.write(self.dest, "skills/my-own/SKILL.md", "hand-written\n")
        shutil.rmtree(os.path.join(self.src, "skills/demo"))
        self.run_sync()

        self.assertFalse(self.exists(self.dest, "skills/demo"))
        self.assertEqual(self.read(self.dest, "skills/my-own/SKILL.md"), "hand-written\n")

    def test_manifest_lists_synced_paths(self):
        self.write(self.src, "CLAUDE.md", "MANUAL\n")
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")

        self.run_sync()

        entries = [line for line in self.read(self.dest, MANIFEST).splitlines() if line]
        self.assertCountEqual(entries, ["CLAUDE.md", "skills/demo"])

    def test_guarded_path_in_baked_tree_is_fatal(self):
        self.write(self.src, "CLAUDE.md", "MANUAL\n")
        self.write(self.src, "settings.json", '{"model": "opus"}\n')

        proc = self.run_sync(expect_rc=1)

        self.assertIn("settings.json", proc.stderr)

    def test_guarded_path_syncs_nothing(self):
        self.write(self.dest, "CLAUDE.md", "MANUAL v1 (stale)\n")
        self.write(self.src, "CLAUDE.md", "MANUAL v2\n")
        self.write(self.src, "projects", "not even a directory\n")

        self.run_sync(expect_rc=1)

        # The guard runs before anything is copied.
        self.assertEqual(self.read(self.dest, "CLAUDE.md"), "MANUAL v1 (stale)\n")
        self.assertFalse(self.exists(self.dest, MANIFEST))


class OwnerTest(SyncTestCase):
    """ep.sh runs the sync as root and no longer chowns the whole volume after
    it (issue #7: on a box months old that is 100k files, on every boot), so
    the sync hands over exactly what it wrote. A fake chown on PATH records the
    calls: the tests do not run as root, and need not."""

    def setUp(self):
        super().setUp()
        self.bin = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.bin, ignore_errors=True)
        self.log = os.path.join(self.bin, "chown.log")
        fake = os.path.join(self.bin, "chown")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write('#!/bin/sh\necho "$*" >> "%s"\n' % self.log)
        os.chmod(fake, 0o755)

    def chowned(self, owner=None):
        env = {"PATH": self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin")}
        if owner is not None:
            env["CLAUDE_STATE_OWNER"] = owner
        self.run_sync(**env)
        if not os.path.exists(self.log):
            return []
        with open(self.log, encoding="utf-8") as fh:
            return fh.read().splitlines()

    def test_without_an_owner_nothing_is_chowned(self):
        self.write(self.src, "CLAUDE.md", "MANUAL\n")
        self.assertEqual([], self.chowned())

    def test_each_synced_entry_and_the_manifest_are_handed_over(self):
        self.write(self.src, "CLAUDE.md", "MANUAL\n")
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")
        calls = self.chowned("claude:claude")
        d = self.dest
        self.assertIn("-R claude:claude %s/CLAUDE.md" % d, calls)
        self.assertIn("-R claude:claude %s/skills/demo" % d, calls)
        self.assertIn("claude:claude %s/skills" % d, calls)
        self.assertIn("claude:claude %s/%s" % (d, MANIFEST), calls)

    def test_user_skills_are_never_walked(self):
        # Only what the sync wrote: never the skills/ tree recursively, which
        # holds the user's own skills too.
        self.write(self.src, "skills/demo/SKILL.md", "demo\n")
        self.write(self.dest, "skills/my-own/SKILL.md", "hand-written\n")
        calls = self.chowned("claude:claude")
        self.assertFalse(any("my-own" in c for c in calls), calls)
        self.assertNotIn("-R claude:claude %s/skills" % self.dest, calls)


class EntrypointOwnershipTest(unittest.TestCase):
    """Issue #7: no recursive chown of the state volume on boot."""

    def setUp(self):
        with open(os.path.join(TESTS_DIR, "..", "ep.sh"), encoding="utf-8") as fh:
            self.lines = [
                line.strip() for line in fh if not line.lstrip().startswith("#")
            ]

    def test_home_and_state_volume_are_not_chowned_recursively(self):
        for line in self.lines:
            if "chown" in line and "-R" in line.split():
                self.assertNotIn("CLAUDE_HOME", line)
                self.assertNotIn("CLAUDE_STATE_DIR", line)

    def test_workspace_is_not_chowned_recursively(self):
        # Rewriting the inode of every file in the bind mount on every boot is
        # slow on a large tree and wakes every file watcher in sibling services;
        # only files that are actually not claude's get handed over.
        for line in self.lines:
            if "chown" in line and "-R" in line.split():
                self.assertNotIn("/workspace", line)

    def test_sync_is_told_whom_to_hand_its_files_to(self):
        sync = next(line for line in self.lines if "sync_claude_home.sh" in line)
        self.assertIn("CLAUDE_STATE_OWNER=claude:claude", sync)


if __name__ == "__main__":
    unittest.main()
