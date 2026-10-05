"""Tests for the sops + age install — encrypted secrets kept in git repos.

The Dockerfile installs sops as a pinned release binary (checked against the
release's checksums file) and age from Debian. Two halves:

* DockerfileTest reads the Dockerfile, so it runs anywhere and catches an
  install that drops the checksum check or the pin.
* RoundTripTest runs the *installed* tools the way a repo uses them: a
  `.sops.yaml` creation rule naming an age recipient, `sops --encrypt` picking
  it up from the file's path, and `sops --decrypt` with the key supplied through
  SOPS_AGE_KEY_FILE / SOPS_AGE_KEY. It needs sops and age on PATH, i.e. a built
  image, and is skipped elsewhere. HOME points at a temp dir so a real
  ~/.config/sops/age/keys.txt can never satisfy a decrypt the test expects to fail.
"""

import os
import re
import shutil
import subprocess
import tempfile
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
IMAGE_DIR = os.path.abspath(os.path.join(TESTS_DIR, ".."))

SECRET = "hunter2-do-not-leak"
HAVE_TOOLS = all(shutil.which(t) for t in ("sops", "age", "age-keygen"))


def read(*parts):
    with open(os.path.join(*parts), encoding="utf-8") as handle:
        return handle.read()


class DockerfileTest(unittest.TestCase):
    def setUp(self):
        self.dockerfile = read(IMAGE_DIR, "Dockerfile")

    def test_sops_version_is_pinned(self):
        self.assertRegex(self.dockerfile, r"(?m)^ARG SOPS_VERSION=\d+\.\d+\.\d+$")

    def test_sops_download_is_checksum_verified(self):
        self.assertIn("sops-v${SOPS_VERSION}.checksums.txt", self.dockerfile)
        self.assertRegex(self.dockerfile, r"grep[^\n]*SOPS_BIN[^\n]*\| sha256sum --check")

    def test_sops_binary_follows_target_arch(self):
        self.assertIn('SOPS_BIN="sops-v${SOPS_VERSION}.linux.${TARGETARCH}"', self.dockerfile)

    def test_age_is_installed(self):
        self.assertRegex(self.dockerfile, r"apt-get install [^\n]*\bage\b")


@unittest.skipUnless(HAVE_TOOLS, "sops/age not installed (run inside the image)")
class RoundTripTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.dir, ignore_errors=True)
        self.env = {k: v for k, v in os.environ.items() if not k.startswith("SOPS_")}
        self.env["HOME"] = self.dir
        self.key_file, self.recipient = self.keygen("key.txt")
        self.write(".sops.yaml", "creation_rules:\n"
                   f"  - path_regex: secrets/.*\\.yaml$\n    age: {self.recipient}\n")
        os.mkdir(os.path.join(self.dir, "secrets"))
        self.write("secrets/app.yaml", f"db:\n  user: app\n  password: {SECRET}\n")
        self.sops("--encrypt", "--in-place", "secrets/app.yaml")

    # --- helpers -------------------------------------------------------

    def keygen(self, name):
        path = os.path.join(self.dir, name)
        subprocess.run(["age-keygen", "-o", path], check=True, capture_output=True)
        match = re.search(r"^# public key: (age1\w+)$", read(path), re.M)
        self.assertIsNotNone(match, "age-keygen wrote no public key comment")
        return path, match.group(1)

    def write(self, name, text):
        with open(os.path.join(self.dir, name), "w", encoding="utf-8") as fh:
            fh.write(text)

    def sops(self, *args, env=None, check=True):
        return subprocess.run(["sops", *args], cwd=self.dir, env=env or self.env,
                              capture_output=True, text=True, check=check)

    def with_env(self, **extra):
        return {**self.env, **extra}

    # --- tests ---------------------------------------------------------

    def test_encrypted_file_hides_values_but_keeps_keys(self):
        encrypted = read(self.dir, "secrets", "app.yaml")
        self.assertNotIn(SECRET, encrypted)
        self.assertIn("password: ENC[", encrypted)
        self.assertIn(self.recipient, encrypted)

    def test_decrypt_with_key_file(self):
        out = self.sops("--decrypt", "secrets/app.yaml",
                        env=self.with_env(SOPS_AGE_KEY_FILE=self.key_file))
        self.assertIn(f"password: {SECRET}", out.stdout)

    def test_decrypt_with_key_in_env(self):
        out = self.sops("--decrypt", "secrets/app.yaml",
                        env=self.with_env(SOPS_AGE_KEY=read(self.key_file)))
        self.assertIn(f"password: {SECRET}", out.stdout)

    def test_decrypt_without_key_fails(self):
        out = self.sops("--decrypt", "secrets/app.yaml", check=False)
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn(SECRET, out.stdout)

    def test_decrypt_with_wrong_key_fails(self):
        other, _ = self.keygen("other.txt")
        out = self.sops("--decrypt", "secrets/app.yaml", check=False,
                        env=self.with_env(SOPS_AGE_KEY_FILE=other))
        self.assertNotEqual(out.returncode, 0)
        self.assertNotIn(SECRET, out.stdout)

    def test_set_updates_one_value_without_an_editor(self):
        env = self.with_env(SOPS_AGE_KEY_FILE=self.key_file)
        self.sops("set", "secrets/app.yaml", '["db"]["password"]', '"rotated"', env=env)
        self.assertNotIn("rotated", read(self.dir, "secrets", "app.yaml"))
        out = self.sops("--decrypt", "--extract", '["db"]["password"]',
                        "secrets/app.yaml", env=env)
        self.assertEqual(out.stdout.strip(), "rotated")


if __name__ == "__main__":
    unittest.main()
