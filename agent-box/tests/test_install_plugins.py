"""Unit tests for install_plugins.sh — the boot-time plugin bootstrap.

Every `claude` invocation is a full Node start of the CLI, close to a second
each, and the script runs on every container start before any session can be
opened. On a box whose plugins are already in place — every boot but the first
— the right number of CLI calls is therefore one: read the state, find nothing
to do. The tests pin that, alongside the behaviour the calls exist for.

The script is driven by its environment and by `claude` on PATH, so the tests
run the *real* script against a fake claude that keeps plugin and marketplace
state in a JSON file and logs every invocation. No network, no real ~/.claude.
"""

import json
import os
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
SCRIPTS_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts"))
INSTALL = os.path.join(SCRIPTS_DIR, "install_plugins.sh")

MARKET = "claude-plugins-official"
SUPERPOWERS = "superpowers@" + MARKET
PLAYWRIGHT = "playwright@" + MARKET

# A fake CLI with just enough of `claude plugin ...` for the script: state in
# $FAKE_STATE, one JSON line per invocation appended to $FAKE_LOG. Messages and
# exit codes mirror the real CLI where the script depends on them ("already
# enabled" / "already disabled" exit non-zero).
FAKE_CLAUDE = r'''#!/usr/bin/env python3
import json, os, sys

args = sys.argv[1:]
with open(os.environ["FAKE_LOG"], "a") as log:
    log.write(json.dumps(args) + "\n")
with open(os.environ["FAKE_STATE"]) as fh:
    state = json.load(fh)

def save():
    with open(os.environ["FAKE_STATE"], "w") as fh:
        json.dump(state, fh)

def find(spec):
    return next((p for p in state["plugins"] if p["id"] == spec), None)

if state.get("broken_json") and args[:2] == ["plugin", "list"] and "--json" in args:
    print("not json at all")
    sys.exit(0)

if args == ["plugin", "list", "--json"]:
    print(json.dumps([dict(p, scope="user") for p in state["plugins"]]))
elif args == ["plugin", "list"]:
    for p in state["plugins"]:
        print("  > %s\n    Status: %s" % (p["id"], "enabled" if p["enabled"] else "disabled"))
elif args == ["plugin", "marketplace", "list", "--json"]:
    print(json.dumps([{"name": m} for m in state["markets"]]))
elif args == ["plugin", "marketplace", "list"]:
    for m in state["markets"]:
        print("  > " + m)
elif args[:3] == ["plugin", "marketplace", "add"]:
    state["markets"].append(args[3].split("/")[-1])
    save()
elif args[:2] == ["plugin", "install"]:
    if args[2].split("@")[1] not in state["markets"]:
        print("marketplace not found", file=sys.stderr)
        sys.exit(1)
    state["plugins"].append({"id": args[2], "enabled": True})
    save()
elif args[:2] in (["plugin", "enable"], ["plugin", "disable"]):
    plugin = find(args[2])
    want = args[1] == "enable"
    if plugin["enabled"] == want:
        print("Plugin is already %sd" % args[1], file=sys.stderr)
        sys.exit(1)
    plugin["enabled"] = want
    save()
else:
    print("fake claude: unexpected " + repr(args), file=sys.stderr)
    sys.exit(2)
'''


class InstallPluginsTestCase(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.bin = os.path.join(self.dir, "bin")
        os.mkdir(self.bin)
        fake = os.path.join(self.bin, "claude")
        with open(fake, "w", encoding="utf-8") as fh:
            fh.write(FAKE_CLAUDE)
        os.chmod(fake, 0o755)
        self.state_file = os.path.join(self.dir, "state.json")
        self.log_file = os.path.join(self.dir, "calls.log")
        self.plugins_file = os.path.join(self.dir, "plugins.txt")
        self.write_plugins(SUPERPOWERS, PLAYWRIGHT)
        self.set_state(plugins=[], markets=[])

    # --- helpers -------------------------------------------------------

    def write_plugins(self, *lines):
        with open(self.plugins_file, "w", encoding="utf-8") as fh:
            fh.write("# a comment\n\n" + "\n".join(lines) + "\n")

    def set_state(self, plugins, markets, **extra):
        with open(self.state_file, "w", encoding="utf-8") as fh:
            json.dump(dict(plugins=plugins, markets=markets, **extra), fh)

    def state(self):
        with open(self.state_file, encoding="utf-8") as fh:
            return json.load(fh)

    def calls(self):
        if not os.path.exists(self.log_file):
            return []
        with open(self.log_file, encoding="utf-8") as fh:
            return [json.loads(line) for line in fh]

    def mutating_calls(self):
        reads = (["plugin", "list"], ["plugin", "marketplace", "list"])
        return [c for c in self.calls() if not any(c[: len(r)] == r for r in reads)]

    def run_install(self, **env):
        proc = subprocess.run(
            ["sh", INSTALL],
            env=dict(
                PATH=self.bin + os.pathsep + os.environ.get("PATH", "/usr/bin:/bin"),
                PLUGINS_FILE=self.plugins_file,
                FAKE_STATE=self.state_file,
                FAKE_LOG=self.log_file,
                **env,
            ),
            capture_output=True,
            text=True,
            stdin=subprocess.DEVNULL,
            timeout=60,
        )
        self.assertEqual(0, proc.returncode, proc.stdout + proc.stderr)
        return proc

    def installed(self, *specs, enabled=True):
        return [{"id": s, "enabled": enabled} for s in specs]


class SteadyStateTest(InstallPluginsTestCase):
    def test_a_box_with_everything_in_place_calls_the_cli_once(self):
        self.set_state(self.installed(SUPERPOWERS, PLAYWRIGHT), [MARKET])
        self.run_install()
        self.assertEqual([["plugin", "list", "--json"]], self.calls())

    def test_steady_state_still_reports_each_plugin(self):
        self.set_state(self.installed(SUPERPOWERS, PLAYWRIGHT), [MARKET])
        out = self.run_install().stdout
        self.assertIn(SUPERPOWERS + " already installed", out)
        self.assertIn(PLAYWRIGHT + " enabled", out)


class EnableTest(InstallPluginsTestCase):
    def test_an_installed_but_disabled_plugin_is_enabled(self):
        self.set_state(
            self.installed(SUPERPOWERS) + self.installed(PLAYWRIGHT, enabled=False),
            [MARKET],
        )
        self.run_install()
        self.assertEqual([["plugin", "enable", PLAYWRIGHT]], self.mutating_calls())
        self.assertTrue(all(p["enabled"] for p in self.state()["plugins"]))


class InstallTest(InstallPluginsTestCase):
    def test_a_missing_plugin_is_installed_from_a_known_marketplace(self):
        self.set_state(self.installed(SUPERPOWERS), [MARKET])
        self.run_install()
        self.assertEqual([["plugin", "install", PLAYWRIGHT]], self.mutating_calls())

    def test_a_fresh_volume_adds_the_marketplace_once_then_installs(self):
        self.run_install()
        self.assertEqual(
            [
                ["plugin", "marketplace", "add", "anthropics/" + MARKET],
                ["plugin", "install", SUPERPOWERS],
                ["plugin", "install", PLAYWRIGHT],
            ],
            self.mutating_calls(),
        )
        self.assertEqual(
            {SUPERPOWERS: True, PLAYWRIGHT: True},
            {p["id"]: p["enabled"] for p in self.state()["plugins"]},
        )

    def test_an_unknown_marketplace_without_a_source_is_skipped(self):
        self.write_plugins("thing@nowhere")
        out = self.run_install().stdout
        self.assertIn("marketplace 'nowhere' unknown", out)
        self.assertEqual([], self.mutating_calls())

    def test_an_explicit_source_is_used_for_the_marketplace(self):
        self.write_plugins("thing@elsewhere  someone/elsewhere")
        self.run_install()
        self.assertEqual(
            [
                ["plugin", "marketplace", "add", "someone/elsewhere"],
                ["plugin", "install", "thing@elsewhere"],
            ],
            self.mutating_calls(),
        )

    def test_unreadable_state_falls_back_to_installing(self):
        # A CLI whose --json output cannot be parsed must not be read as
        # "nothing to do": the install path is idempotent, so it is taken.
        self.set_state(self.installed(SUPERPOWERS), [MARKET], broken_json=True)
        self.write_plugins(SUPERPOWERS)
        self.run_install()
        self.assertIn(["plugin", "install", SUPERPOWERS], self.mutating_calls())


class DisablePlaywrightTest(InstallPluginsTestCase):
    def test_an_enabled_playwright_is_disabled(self):
        self.set_state(self.installed(SUPERPOWERS, PLAYWRIGHT), [MARKET])
        self.run_install(DISABLE_PLAYWRIGHT="true")
        self.assertEqual([["plugin", "disable", PLAYWRIGHT]], self.mutating_calls())

    def test_an_already_disabled_playwright_costs_no_call(self):
        self.set_state(
            self.installed(SUPERPOWERS) + self.installed(PLAYWRIGHT, enabled=False),
            [MARKET],
        )
        out = self.run_install(DISABLE_PLAYWRIGHT="yes").stdout
        self.assertEqual([], self.mutating_calls())
        self.assertIn(PLAYWRIGHT + " disabled", out)

    def test_a_missing_playwright_is_not_installed(self):
        self.set_state(self.installed(SUPERPOWERS), [MARKET])
        out = self.run_install(DISABLE_PLAYWRIGHT="1").stdout
        self.assertEqual([], self.mutating_calls())
        self.assertIn("skipping " + PLAYWRIGHT, out)


if __name__ == "__main__":
    unittest.main()
