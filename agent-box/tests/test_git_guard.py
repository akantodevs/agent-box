"""Unit tests for git-guard.js — the PreToolUse(Bash) hook behind ALLOW_GIT_WRITE.

The tests drive the real hook with node, exactly as Claude Code does: a JSON
payload on stdin, and a decision (or silence) on stdout. Silence with exit 0
means "allow"; a `permissionDecision: deny` means the Bash call is blocked.
"""

import json
import os
import subprocess
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
GUARD = os.path.abspath(os.path.join(TESTS_DIR, "..", "scripts", "git-guard.js"))

# Every one of these changes history, refs, the index or a remote — or, for
# checkout, throws away working-tree changes — and must be blocked unless the
# deployment opted in.
WRITES = [
    "git commit -m x",
    "git push",
    "git push origin HEAD",
    "git pull",
    "git merge main",
    "git rebase main",
    "git cherry-pick abc",
    "git revert abc",
    "git reset --hard",
    "git am x.patch",
    "git stash",
    "git checkout main",
    "git checkout -- f",
    "git switch -c b",
    "git worktree add x",
    "git update-ref refs/heads/x HEAD",
    "git tag v1",
    "git tag -d v1",
    "git branch new",
    "git branch -D old",
    "git branch -m a b",
    "git -C /x commit",
    "git -c user.name=x commit",
    "git --no-pager push",
    "FOO=1 git push",
    "git status && git push",
    "git status; git commit -am x",
    "cd /workspace && git push",
    "/usr/bin/git push",
    "gh pr create --fill",
    "gh pr merge 1",
    "gh issue comment 1 -b x",
    "gh repo create x",
    "gh release create v1",
    "gh api -X POST repos/x/y/issues",
    "gh api repos/x/y/issues -f title=x",
    "gh api --method PATCH repos/x/y",
    "gh api --method=DELETE repos/x/y",
    # Discarding work, rewriting history, or re-pointing where it goes.
    "git restore f",
    "git clean -fdx",
    "git filter-branch --all",
    "git symbolic-ref HEAD refs/heads/x",
    "git replace abc def",
    "git notes add -m x",
    "git reflog expire --expire=now --all",
    "git reflog delete HEAD@{1}",
    "git remote add fork https://x",
    "git remote set-url origin https://x",
    "git remote remove origin",
    "git config --global credential.helper store",
    "git config user.email x@y",
    "git stash push",
    "git stash pop",
    "git worktree remove x",
    "gh api graphql -f query='mutation { addStar(input:{}) { clientMutationId } }'",
    # Wrappers that run git all the same.
    "env git push",
    "env GIT_TRACE=1 git push",
    "command git push",
    "time git push",
    "nice git push",
    "nohup git push",
    "(git push)",
    "{ git push; }",
    "echo $(git push)",
]

# Print the token, which the manual forbids whatever the write mode.
TOKEN_PRINTS = [
    "gh auth token",
    "gh auth status --show-token",
    "gh auth status -t",
]

# Reads, and things that merely mention git. Allowed in every mode.
READS = [
    "git status",
    "git diff",
    "git log --oneline",
    "git show HEAD",
    "git fetch",
    "git clone https://github.com/x/y",
    "git branch",
    "git branch -a",
    "git branch --list 'f*'",
    "git branch -vv",
    "git tag",
    "git tag -l",
    "git tag --list 'v*'",
    "git remote -v",
    "git remote",
    "git remote show origin",
    "git remote get-url origin",
    "git config --get user.name",
    "git config --list",
    "git config -l",
    "git config --get-regexp user",
    "git stash list",
    "git stash show -p",
    "git worktree list",
    "git tag -n",
    "git tag --contains abc",
    "git tag --sort=-v:refname",
    "git notes list",
    "git notes show abc",
    "git reflog",
    "git reflog show",
    "git add -A",
    "git log --grep 'a b'",
    "gh api graphql -f query='query { viewer { login } }'",
    "gh api graphql -f query='{ viewer { login } }'",
    "gh run watch 1",
    "gh repo clone x/y",
    "gh release download v1",
    "gh browse",
    "git -C /x status",
    "GIT_PAGER=cat git log",
    "gh pr view 1",
    "gh pr list",
    "gh issue list",
    "gh issue view 4 --comments",
    "gh pr diff 1",
    "gh pr checks 1",
    "gh run list",
    "gh repo view",
    "gh auth status",
    "gh --version",
    "gh help",
    "gh api repos/x/y",
    "gh api -X GET repos/x/y",
    "gh search issues foo",
    "gh pr create --help",
    "echo git push",
    "grep -r 'git commit' .",
    "ls",
]


def run_guard(payload, mode):
    env = dict(os.environ)
    env.pop("ALLOW_GIT_WRITE", None)
    if mode is not None:
        env["ALLOW_GIT_WRITE"] = mode
    raw = payload if isinstance(payload, str) else json.dumps(payload)
    return subprocess.run(
        ["node", GUARD],
        input=raw.encode("utf-8"),
        env=env,
        stdout=subprocess.PIPE,
        stderr=subprocess.PIPE,
        timeout=30,
    )


def bash(command):
    return {"tool_name": "Bash", "tool_input": {"command": command},
            "hook_event_name": "PreToolUse"}


class GuardTestCase(unittest.TestCase):
    def assert_allowed(self, payload, mode):
        result = run_guard(payload, mode)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertEqual(b"", result.stdout, "expected allow for %r (mode %r)" % (payload, mode))

    def assert_denied(self, payload, mode):
        result = run_guard(payload, mode)
        self.assertEqual(0, result.returncode, result.stderr)
        self.assertNotEqual(b"", result.stdout, "expected deny for %r (mode %r)" % (payload, mode))
        out = json.loads(result.stdout)["hookSpecificOutput"]
        self.assertEqual("PreToolUse", out["hookEventName"])
        self.assertEqual("deny", out["permissionDecision"])
        return out["permissionDecisionReason"]


class DisabledModeTest(GuardTestCase):
    """No, unset and anything unrecognised all block writes (fail closed)."""

    def test_writes_are_denied(self):
        for mode in ("No", "no", None, "", "maybe"):
            for command in WRITES:
                with self.subTest(mode=mode, command=command):
                    self.assert_denied(bash(command), mode)

    def test_reads_are_allowed(self):
        for command in READS:
            with self.subTest(command=command):
                self.assert_allowed(bash(command), "No")

    def test_the_token_is_never_printed(self):
        for mode in ("Yes", "No", None):
            for command in TOKEN_PRINTS:
                with self.subTest(mode=mode, command=command):
                    reason = self.assert_denied(bash(command), mode)
                    self.assertIn("token", reason)

    def test_the_reason_names_the_setting(self):
        reason = self.assert_denied(bash("git push"), None)
        self.assertIn("ALLOW_GIT_WRITE", reason)
        self.assertIn("working tree", reason)


class EnabledModeTest(GuardTestCase):
    def test_writes_are_allowed(self):
        for mode in ("Yes", "yes", " YES "):
            for command in WRITES:
                with self.subTest(mode=mode, command=command):
                    self.assert_allowed(bash(command), mode)

    def test_reads_are_allowed(self):
        for command in READS:
            with self.subTest(command=command):
                self.assert_allowed(bash(command), "Yes")


class FailOpenTest(GuardTestCase):
    """The guard never wedges tools it has nothing to say about."""

    def test_unparseable_payload(self):
        self.assert_allowed("not json", "No")

    def test_other_tools(self):
        self.assert_allowed({"tool_name": "Write", "tool_input": {"command": "git push"}}, "No")

    def test_empty_command(self):
        self.assert_allowed(bash(""), "No")

    def test_empty_payload(self):
        self.assert_allowed("", "No")


if __name__ == "__main__":
    unittest.main()
