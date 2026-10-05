"""Unit tests for terraform-guard.js — the Pre/PostToolUse(Bash) hook behind ALLOW_TERRAFORM_MODIFY.

Driven like git-guard's tests: a JSON payload on stdin, a decision (or silence =
allow) on stdout. Besides `terraform` itself, the guard must recognise the baked
`tf` wrapper (agent-box/bin/tf), which runs terraform with an environment's
secrets, and the `secrets <env> exec -- terraform …` form it expands to.
"""

import json
import os
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts", "terraform-guard.js"))

MUTATING = [
    "terraform apply",
    "terraform -chdir=x destroy",
    "terraform state rm a.b",
    "terraform workspace delete w",
    "/usr/bin/terraform apply",
    "tf apply",
    "tf apply -auto-approve",
    "tf -chdir=x import a b",
    "tf state mv a b",
    "tf taint a.b",
    "cd /workspace/infrastructure/stage/app && tf apply",
    "tf plan && tf apply",
    "/usr/local/bin/tf destroy",
    "secrets stage exec -- terraform apply",
    "secrets production exec -- terraform -chdir=/x apply",
]

READ_ONLY = [
    "terraform plan",
    "terraform state list",
    "tf plan",
    "tf init -upgrade",
    "tf validate",
    "tf state list",
    "tf output -json",
    "secrets stage exec -- terraform plan",
    "ls tf",
    "cat tfapply",
]


def run(command, mode, cwd="/workspace", event="PreToolUse", home=None):
    env = {k: v for k, v in os.environ.items() if k != "ALLOW_TERRAFORM_MODIFY"}
    if mode is not None:
        env["ALLOW_TERRAFORM_MODIFY"] = mode
    if home:
        env["HOME"] = home
    payload = {"tool_name": "Bash", "hook_event_name": event, "cwd": cwd,
               "tool_input": {"command": command}}
    out = subprocess.run(["node", GUARD], input=json.dumps(payload), env=env,
                         capture_output=True, text=True, check=True).stdout
    return json.loads(out)["hookSpecificOutput"] if out.strip() else None


class GuardTest(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        os.mkdir(os.path.join(self.home, ".claude"))

    def test_mutating_commands_are_denied_under_no(self):
        for command in MUTATING:
            with self.subTest(command=command):
                decision = run(command, "No", home=self.home)
                self.assertIsNotNone(decision, command)
                self.assertEqual(decision["permissionDecision"], "deny")

    def test_mutating_commands_fail_closed_when_unset(self):
        for command in MUTATING:
            with self.subTest(command=command):
                self.assertEqual(run(command, None, home=self.home)["permissionDecision"], "deny")

    def test_read_only_commands_pass(self):
        for command in READ_ONLY:
            with self.subTest(command=command):
                self.assertIsNone(run(command, "No", home=self.home))

    def test_yes_allows(self):
        self.assertIsNone(run("tf apply", "Yes", home=self.home))

    def test_tf_ask_is_remembered_per_directory(self):
        stage = "/workspace/infrastructure/stage/app"
        prod = "/workspace/infrastructure/production/app"
        self.assertEqual(run("tf apply", "Ask", cwd=stage, home=self.home)["permissionDecision"], "ask")
        run("tf apply", "Ask", cwd=stage, event="PostToolUse", home=self.home)
        self.assertIsNone(run("tf apply", "Ask", cwd=stage, home=self.home))
        self.assertEqual(run("tf apply", "Ask", cwd=prod, home=self.home)["permissionDecision"], "ask")

    def test_tf_chdir_keys_the_approval(self):
        decision = run("tf -chdir=app apply", "Ask", cwd="/workspace/infrastructure/stage", home=self.home)
        self.assertIn("/workspace/infrastructure/stage/app", decision["permissionDecisionReason"])


if __name__ == "__main__":
    unittest.main()
