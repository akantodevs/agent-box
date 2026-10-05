"""Tests for release-agent-box.yml — version tags for already-published images.

publish-agent-box.yml builds on pushes to main and tags the image `latest` and
`sha-<commit>`. A version tag (v1.8.1) must not rebuild: a second build of the
same commit can bake a newer Claude Code CLI, so `1.8.1` would differ from the
image main published. The release workflow instead points `1.8.1` at the
existing `sha-<commit>` manifest. These checks read the workflow text, so they
run anywhere; the end-to-end proof is a run on a real tag.
"""

import os
import re
import unittest

TESTS_DIR = os.path.dirname(os.path.abspath(__file__))
REPO_DIR = os.path.abspath(os.path.join(TESTS_DIR, "..", ".."))
WORKFLOWS = os.path.join(REPO_DIR, ".github", "workflows")


def read(name):
    with open(os.path.join(WORKFLOWS, name), encoding="utf-8") as handle:
        return handle.read()


class ReleaseWorkflowTest(unittest.TestCase):
    def setUp(self):
        self.workflow = read("release-agent-box.yml")

    def test_triggers_on_version_tags(self):
        self.assertRegex(self.workflow, r"(?ms)^on:\n  push:\n    tags: \[\"v\*\.\*\.\*\"\]")

    def test_tag_push_is_the_only_trigger(self):
        # Releasing is pushing a tag; nothing has to start the workflow by hand.
        self.assertNotIn("workflow_dispatch", self.workflow)
        self.assertNotIn("inputs.", self.workflow)

    def test_source_is_the_tagged_commit(self):
        # The tag push checks out the tag, so HEAD is the tagged commit — an
        # annotated tag's own object id never reaches the image name.
        self.assertIn('commit="$(git rev-parse HEAD)"', self.workflow)

    def test_retags_instead_of_building(self):
        self.assertIn("docker buildx imagetools create", self.workflow)
        self.assertNotIn("docker/build-push-action", self.workflow)

    def test_source_is_the_sha_tag_main_published(self):
        # metadata-action's `type=sha` tag is `sha-` + the first 7 hex digits.
        self.assertRegex(self.workflow, r"sha-\$\{commit:0:7\}")

    def test_waits_for_the_main_build(self):
        self.assertRegex(self.workflow, r"docker buildx imagetools inspect")

    def test_version_tag_keeps_the_v(self):
        # Published images are tagged like the git tags: v1.8.0, v1.8.1, ...
        self.assertIn('echo "TARGET=$image:$TAG"', self.workflow)

    def test_refuses_to_overwrite_a_version(self):
        self.assertRegex(self.workflow, r'(?s)if docker buildx imagetools inspect "\$TARGET".*already exists')

    def test_is_the_only_tag_workflow(self):
        # tag-semver.yml promoted :latest, racing main's build of the tagged commit.
        self.assertFalse(os.path.exists(os.path.join(WORKFLOWS, "tag-semver.yml")))

    def test_never_moves_latest(self):
        self.assertNotRegex(self.workflow, r":latest\b")


class PublishWorkflowTest(unittest.TestCase):
    def test_sha_tag_format_is_what_release_expects(self):
        publish = read("publish-agent-box.yml")
        self.assertRegex(publish, r"(?m)^\s+type=sha$")
        self.assertNotIn("DOCKER_METADATA_SHORT_SHA_LENGTH", publish)

    def test_publish_does_not_build_tags(self):
        self.assertNotRegex(read("publish-agent-box.yml"), re.compile(r"^\s+tags: \[", re.M))


if __name__ == "__main__":
    unittest.main()
