"""Tests for the baked infrastructure helpers in agent-box/bin: `secrets` and `tf`.

Both live in /usr/local/bin in the image and work on /workspace/infrastructure/<env>,
where each environment has a `.sops-infra.yaml` and a `secrets.enc.env`. The
tests point AGENT_BOX_INFRA_DIR at a temp tree and put fake `sops` / `terraform`
first on PATH, so they run anywhere: the fake sops records its argv and, for
exec-env, runs the command string through /bin/sh as the real one does.
"""

import os
import shutil
import stat
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.abspath(os.path.join(TESTS_DIR, ".."))
BIN_DIR = os.path.join(IMAGE_DIR, "bin")

FAKE_SOPS = """#!/bin/sh
printf '%s\\n' "$@" > "$FAKE_LOG"
if [ "$3" = exec-env ]; then SECRET_FROM_SOPS=1; export SECRET_FROM_SOPS; exec /bin/sh -c "$5"; fi
"""
FAKE_TERRAFORM = """#!/bin/sh
{ echo "cwd=$(pwd -P)"; echo "secret=${SECRET_FROM_SOPS:-}"; printf 'arg=%s\\n' "$@"; } > "$FAKE_TF_LOG"
"""


class InfraBinTest(unittest.TestCase):
    def setUp(self):
        self.tmp = os.path.realpath(tempfile.mkdtemp())
        self.addCleanup(shutil.rmtree, self.tmp, ignore_errors=True)
        self.infra = os.path.join(self.tmp, "infrastructure")
        for env in ("stage", "production"):
            os.makedirs(os.path.join(self.infra, env, "app"))
            open(os.path.join(self.infra, env, ".sops-infra.yaml"), "w").close()
        os.makedirs(os.path.join(self.infra, "modules", "net"))
        fakes = os.path.join(self.tmp, "fakes")
        os.mkdir(fakes)
        self.script(fakes, "sops", FAKE_SOPS)
        self.script(fakes, "terraform", FAKE_TERRAFORM)
        self.sops_log = os.path.join(self.tmp, "sops.log")
        self.tf_log = os.path.join(self.tmp, "tf.log")
        self.env = dict(os.environ, AGENT_BOX_INFRA_DIR=self.infra, FAKE_LOG=self.sops_log,
                        FAKE_TF_LOG=self.tf_log, PATH=f"{fakes}:{BIN_DIR}:{os.environ['PATH']}")
        self.env.pop("CLAUDECODE", None)

    def script(self, directory, name, body):
        path = os.path.join(directory, name)
        with open(path, "w") as fh:
            fh.write(body)
        os.chmod(path, os.stat(path).st_mode | stat.S_IEXEC)

    def run_bin(self, name, *args, cwd=None):
        return subprocess.run([os.path.join(BIN_DIR, name), *args], cwd=cwd or self.tmp,
                              env=self.env, capture_output=True, text=True)

    def log(self, path):
        with open(path) as fh:
            return fh.read().splitlines()

    # --- secrets -------------------------------------------------------

    def test_secrets_rejects_unknown_environment(self):
        for env in ("qa", "modules", "..", "stage/app"):
            with self.subTest(env=env):
                result = self.run_bin("secrets", env, "updatekeys")
                self.assertEqual(result.returncode, 2, result.stderr)
                self.assertFalse(os.path.exists(self.sops_log))

    def test_secrets_updatekeys_uses_the_environment_config(self):
        result = self.run_bin("secrets", "stage", "updatekeys")
        self.assertEqual(result.returncode, 0, result.stderr)
        stage = os.path.join(self.infra, "stage")
        self.assertEqual(self.log(self.sops_log), [
            "--config", f"{stage}/.sops-infra.yaml", "updatekeys", "--yes", f"{stage}/secrets.enc.env"])

    def test_secrets_edit(self):
        self.assertEqual(self.run_bin("secrets", "production", "edit").returncode, 0)
        prod = os.path.join(self.infra, "production")
        self.assertEqual(self.log(self.sops_log), [
            "--config", f"{prod}/.sops-infra.yaml", "edit", f"{prod}/secrets.enc.env"])

    def test_secrets_exec_preserves_arguments(self):
        odd = "it's $HOME `x` \"q\""
        result = self.run_bin("secrets", "stage", "exec", "--", "terraform", "plan", odd)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.log(self.tf_log)[1:], ["secret=1", "arg=plan", f"arg={odd}"])

    def test_secrets_usage(self):
        self.assertEqual(self.run_bin("secrets").returncode, 2)
        self.assertEqual(self.run_bin("secrets", "stage", "exec", "--").returncode, 2)

    # --- tf ------------------------------------------------------------

    def test_tf_in_an_environment_loads_its_secrets(self):
        app = os.path.join(self.infra, "stage", "app")
        result = self.run_bin("tf", "plan", "-out=p", cwd=app)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("exec-env", self.log(self.sops_log))
        self.assertIn(f"{self.infra}/stage/secrets.enc.env", self.log(self.sops_log))
        self.assertEqual(self.log(self.tf_log), [f"cwd={app}", "secret=1", "arg=plan", "arg=-out=p"])

    def test_tf_outside_an_environment_is_plain_terraform(self):
        for cwd in (self.tmp, os.path.join(self.infra, "modules", "net"), self.infra):
            with self.subTest(cwd=cwd):
                result = self.run_bin("tf", "validate", cwd=cwd)
                self.assertEqual(result.returncode, 0, result.stderr)
                self.assertFalse(os.path.exists(self.sops_log))
                self.assertEqual(self.log(self.tf_log)[1:], ["secret=", "arg=validate"])

    def test_tf_refuses_secrets_without_a_sops_config(self):
        # secrets.enc.env but no .sops-infra.yaml is a broken environment, not a shared
        # dir: falling back to plain terraform would run without (or with the wrong) credentials.
        qa = os.path.join(self.infra, "qa")
        os.makedirs(os.path.join(qa, "app"))
        open(os.path.join(qa, "secrets.enc.env"), "w").close()
        for cwd in (qa, os.path.join(qa, "app")):
            with self.subTest(cwd=cwd):
                result = self.run_bin("tf", "plan", cwd=cwd)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(f"{qa}/.sops-infra.yaml", result.stderr)
                self.assertFalse(os.path.exists(self.tf_log))
                self.assertFalse(os.path.exists(self.sops_log))

    def test_tf_leaves_mutating_commands_to_the_guard(self):
        # terraform-guard.js recognises `tf`, so the wrapper must not second-guess
        # ALLOW_TERRAFORM_MODIFY by refusing apply itself under Claude Code.
        self.env["CLAUDECODE"] = "1"
        result = self.run_bin("tf", "apply", cwd=os.path.join(self.infra, "stage", "app"))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn("arg=apply", self.log(self.tf_log))


class DockerfileTest(unittest.TestCase):
    def test_bin_is_installed_on_path(self):
        with open(os.path.join(IMAGE_DIR, "Dockerfile")) as fh:
            dockerfile = fh.read()
        self.assertRegex(dockerfile, r"(?m)^COPY bin/ /usr/local/bin/$")

    def test_bin_scripts_are_executable(self):
        for name in ("secrets", "tf"):
            self.assertTrue(os.access(os.path.join(BIN_DIR, name), os.X_OK), name)


if __name__ == "__main__":
    unittest.main()
