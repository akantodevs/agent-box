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

    def run_sync(self, expect_rc=0):
        proc = subprocess.run(
            ["sh", SYNC],
            env={
                "PATH": os.environ.get("PATH", "/usr/bin:/bin"),
                "AGENT_BOX_CLAUDE_HOME_SRC": self.src,
                "CLAUDE_STATE_DIR": self.dest,
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


if __name__ == "__main__":
    unittest.main()
