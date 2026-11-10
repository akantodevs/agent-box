"""Unit tests for setup_playwright_mcp.sh — the boot-time Playwright MCP wiring.

The playwright plugin's own server is `npx @playwright/mcp@latest`, which moves
with every upstream release and then looks for a Chromium revision the image
never installed. The image ships a pinned @playwright/mcp together with the
browser it was built against, so the script registers *that* binary as a
user-scope MCP server in ~/.claude.json and switches the plugin off in
~/.claude/settings.json, on every boot (~/.claude.json does not survive a
container recreate). The tests run the real script against temp files.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPT = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts", "setup_playwright_mcp.sh"))

PLUGIN = "playwright@claude-plugins-official"
SERVER = {
    "type": "stdio",
    "command": "/usr/bin/playwright-mcp",
    "args": [],
    "env": {},
}


class SetupPlaywrightMcpTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.claude_json = os.path.join(self.dir, ".claude.json")
        self.settings = os.path.join(self.dir, "settings.json")

    def write(self, path, data):
        with open(path, "w", encoding="utf-8") as fh:
            json.dump(data, fh)

    def read(self, path):
        with open(path, encoding="utf-8") as fh:
            return json.load(fh)

    def run_script(self, **env):
        full = dict(os.environ)
        full.update(CLAUDE_JSON=self.claude_json, CLAUDE_SETTINGS=self.settings)
        full.pop("DISABLE_PLAYWRIGHT", None)
        full.update(env)
        result = subprocess.run(
            ["sh", SCRIPT], env=full, capture_output=True, text=True, timeout=30
        )
        self.assertEqual(0, result.returncode, result.stdout + result.stderr)
        return result

    # --- registration --------------------------------------------------------

    def test_registers_the_image_binary_and_keeps_other_state(self):
        self.write(self.claude_json, {
            "hasCompletedOnboarding": True,
            "mcpServers": {"other": {"type": "stdio", "command": "x", "args": [], "env": {}}},
        })
        self.write(self.settings, {})
        self.run_script()
        data = self.read(self.claude_json)
        self.assertEqual(SERVER, data["mcpServers"]["playwright"])
        self.assertIn("other", data["mcpServers"])
        self.assertTrue(data["hasCompletedOnboarding"])

    def test_replaces_a_stale_playwright_entry(self):
        self.write(self.claude_json, {"mcpServers": {"playwright": {
            "type": "stdio", "command": "npx", "args": ["@playwright/mcp@latest"], "env": {}}}})
        self.write(self.settings, {})
        self.run_script()
        self.assertEqual(SERVER, self.read(self.claude_json)["mcpServers"]["playwright"])

    def test_is_idempotent(self):
        self.write(self.claude_json, {})
        self.write(self.settings, {"enabledPlugins": {PLUGIN: True}})
        self.run_script()
        first = (self.read(self.claude_json), self.read(self.settings))
        self.run_script()
        self.assertEqual(first, (self.read(self.claude_json), self.read(self.settings)))

    # --- the plugin's own server ---------------------------------------------

    def test_disables_an_enabled_plugin_and_keeps_others(self):
        self.write(self.claude_json, {})
        self.write(self.settings, {"model": "opus", "enabledPlugins": {
            PLUGIN: True, "superpowers@claude-plugins-official": True}})
        self.run_script()
        settings = self.read(self.settings)
        self.assertIs(False, settings["enabledPlugins"][PLUGIN])
        self.assertIs(True, settings["enabledPlugins"]["superpowers@claude-plugins-official"])
        self.assertEqual("opus", settings["model"])

    def test_leaves_settings_untouched_when_plugin_is_absent(self):
        self.write(self.claude_json, {})
        self.write(self.settings, {"enabledPlugins": {"superpowers@claude-plugins-official": True}})
        before = os.stat(self.settings).st_mtime_ns
        self.run_script()
        self.assertEqual(before, os.stat(self.settings).st_mtime_ns)
        self.assertNotIn(PLUGIN, self.read(self.settings)["enabledPlugins"])

    def test_missing_settings_file_is_not_created(self):
        self.write(self.claude_json, {})
        self.run_script()
        self.assertFalse(os.path.exists(self.settings))
        self.assertIn("playwright", self.read(self.claude_json)["mcpServers"])

    # --- DISABLE_PLAYWRIGHT --------------------------------------------------

    def test_disable_removes_the_server_and_keeps_the_plugin_off(self):
        self.write(self.claude_json, {"mcpServers": {"playwright": SERVER, "other": {}}})
        self.write(self.settings, {"enabledPlugins": {PLUGIN: True}})
        out = self.run_script(DISABLE_PLAYWRIGHT="Yes").stdout
        data = self.read(self.claude_json)
        self.assertNotIn("playwright", data["mcpServers"])
        self.assertIn("other", data["mcpServers"])
        self.assertIs(False, self.read(self.settings)["enabledPlugins"][PLUGIN])
        self.assertIn("DISABLE_PLAYWRIGHT", out)

    def test_other_disable_values_still_register(self):
        self.write(self.claude_json, {})
        self.write(self.settings, {})
        self.run_script(DISABLE_PLAYWRIGHT="no")
        self.assertEqual(SERVER, self.read(self.claude_json)["mcpServers"]["playwright"])

    # --- failure handling ----------------------------------------------------

    def test_unparseable_claude_json_is_left_alone(self):
        with open(self.claude_json, "w", encoding="utf-8") as fh:
            fh.write("{not json")
        self.write(self.settings, {})
        result = self.run_script()
        with open(self.claude_json, encoding="utf-8") as fh:
            self.assertEqual("{not json", fh.read())
        self.assertIn("WARN", result.stdout + result.stderr)


if __name__ == "__main__":
    unittest.main()
