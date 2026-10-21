"""Unit tests for memory_store.

Every case builds its own tempfile.mkdtemp() CLAUDE_HOME: delete_memory()
unlinks files and rewrites MEMORY.md, and the real home holds this box's own
memories.
"""

import os
import shutil
import stat
import sys
import tempfile
import unittest
from unittest import mock

sys.path.insert(
    0, os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "scripts")
)

import memory_store


def memory_text(name="a-memory", description="What it is", type_="feedback",
                modified="2026-10-05T13:09:42.361Z", body="The fact.\n"):
    """A memory file in the shape Claude Code writes."""
    lines = ["---", "name: %s" % name, 'description: "%s"' % description,
             "metadata:", "  node_type: memory"]
    if type_ is not None:
        lines.append("  type: %s" % type_)
    if modified is not None:
        lines.append("  modified: %s" % modified)
    lines += ["---", "", body]
    return "\n".join(lines)


class HomeTestCase(unittest.TestCase):
    def setUp(self):
        self.home = tempfile.mkdtemp()
        self.addCleanup(shutil.rmtree, self.home, ignore_errors=True)

    def memory_dir(self, project="-workspace"):
        path = os.path.join(self.home, "projects", project, "memory")
        os.makedirs(path, exist_ok=True)
        return path

    def write(self, filename, content, project="-workspace", mode="w"):
        path = os.path.join(self.memory_dir(project), filename)
        with open(path, mode, **({} if "b" in mode else {"encoding": "utf-8"})) as handle:
            handle.write(content)
        return path

    def read_bytes(self, filename, project="-workspace"):
        with open(os.path.join(self.memory_dir(project), filename), "rb") as handle:
            return handle.read()


class FrontmatterTest(unittest.TestCase):
    def test_top_level_and_nested_keys(self):
        fields = memory_store.parse_frontmatter(memory_text())
        self.assertEqual("a-memory", fields["name"])
        self.assertEqual("What it is", fields["description"])
        self.assertEqual("feedback", fields["metadata.type"])
        self.assertEqual("2026-10-05T13:09:42.361Z", fields["metadata.modified"])

    def test_a_trailing_space_after_a_parent_key_still_nests(self):
        # Claude Code writes "metadata: " with a trailing space.
        text = "---\nname: x\nmetadata: \n  type: user\n---\n"
        self.assertEqual("user", memory_store.parse_frontmatter(text)["metadata.type"])

    def test_double_quoted_values_are_unescaped(self):
        text = '---\ndescription: "say \\"hi\\": now"\n---\n'
        self.assertEqual('say "hi": now', memory_store.parse_frontmatter(text)["description"])

    def test_single_quoted_values_are_unquoted(self):
        text = "---\ndescription: 'it''s fine'\n---\n"
        self.assertEqual("it's fine", memory_store.parse_frontmatter(text)["description"])

    def test_a_colon_inside_an_unquoted_value_is_kept(self):
        text = "---\ndescription: memory dir vs CLAUDE.md: by kind\n---\n"
        self.assertEqual("memory dir vs CLAUDE.md: by kind",
                         memory_store.parse_frontmatter(text)["description"])

    def test_no_frontmatter_is_an_empty_dict(self):
        self.assertEqual({}, memory_store.parse_frontmatter("# Just a heading\n"))
        self.assertEqual({}, memory_store.parse_frontmatter(""))

    def test_an_unclosed_fence_is_not_frontmatter(self):
        self.assertEqual({}, memory_store.parse_frontmatter("---\nname: x\nbody\n"))

    def test_crlf_files_parse(self):
        text = memory_text().replace("\n", "\r\n")
        self.assertEqual("feedback", memory_store.parse_frontmatter(text)["metadata.type"])


class ParseIndexTest(unittest.TestCase):
    def test_entries_carry_line_number_and_targets(self):
        data = b"# Index\n- [One](one.md) \xe2\x80\x94 hook\n\n- [Two](two.md)\n"
        entries = memory_store.parse_index(data)
        self.assertEqual([2, 4], [entry["line"] for entry in entries])
        self.assertEqual([["one.md"], ["two.md"]], [entry["targets"] for entry in entries])
        self.assertEqual("- [One](one.md) — hook", entries[0]["text"])
        self.assertTrue(all(entry["withinCap"] for entry in entries))

    def test_links_that_are_not_bare_md_filenames_are_ignored(self):
        data = (b"- [web](https://example.com/a.md)\n- [sub](dir/a.md)\n"
                b"- [txt](a.txt)\n- [ok](a.md)\n")
        self.assertEqual([4], [e["line"] for e in memory_store.parse_index(data)])

    def test_line_200_is_within_the_cap_and_line_201_is_not(self):
        data = b"".join(b"- [m](m%d.md)\n" % n for n in range(1, 202))
        entries = memory_store.parse_index(data)
        self.assertTrue(entries[199]["withinCap"])
        self.assertFalse(entries[200]["withinCap"])

    def test_the_byte_cap_counts_up_to_the_end_of_the_line(self):
        # Line 1 ends exactly at byte 25,000; line 2 ends after it.
        first = b"- [a](a.md) " + b"x" * (25000 - len(b"- [a](a.md) "))
        data = first + b"\n- [b](b.md)\n"
        entries = memory_store.parse_index(data)
        self.assertTrue(entries[0]["withinCap"])
        self.assertFalse(entries[1]["withinCap"])

    def test_crlf_line_endings_count_as_one_line_each(self):
        data = b"- [a](a.md)\r\n- [b](b.md)\r\n"
        self.assertEqual([1, 2], [e["line"] for e in memory_store.parse_index(data)])

    def test_invalid_utf8_does_not_raise(self):
        self.assertEqual(1, len(memory_store.parse_index(b"- [\xff](a.md)\n")))


class ListMemoriesTest(HomeTestCase):
    def listing(self):
        return memory_store.list_memories(self.home)

    def test_an_empty_home_lists_nothing(self):
        listing = self.listing()
        self.assertEqual([], listing["memories"])
        self.assertEqual([], listing["dangling"])
        self.assertEqual(0, listing["totals"]["count"])
        self.assertEqual(25000, listing["totals"]["indexByteCap"])
        self.assertEqual(200, listing["totals"]["indexLineCap"])

    def test_a_memory_carries_its_frontmatter_and_size(self):
        content = memory_text(name="no-branches", description="Never branch")
        self.write("no-branches.md", content)
        self.write("MEMORY.md", "- [No branches](no-branches.md) — hook\n")
        [memory] = self.listing()["memories"]
        self.assertEqual("-workspace/no-branches.md", memory["id"])
        self.assertEqual("-workspace", memory["project"])
        self.assertEqual("no-branches.md", memory["file"])
        self.assertEqual("no-branches", memory["name"])
        self.assertEqual("Never branch", memory["description"])
        self.assertEqual("feedback", memory["type"])
        self.assertEqual(len(content.encode("utf-8")), memory["bytes"])
        self.assertEqual(1791205782, memory["modified"])
        self.assertEqual("loaded", memory["status"])
        self.assertEqual(1, memory["indexLine"])

    def test_the_index_itself_is_not_a_memory(self):
        self.write("MEMORY.md", "# nothing\n")
        self.assertEqual([], self.listing()["memories"])

    def test_an_unindexed_file_is_orphaned(self):
        self.write("lost.md", memory_text())
        [memory] = self.listing()["memories"]
        self.assertEqual("orphaned", memory["status"])
        self.assertIsNone(memory["indexLine"])

    def test_a_file_linked_only_past_the_cap_is_cut_off(self):
        self.write("late.md", memory_text())
        filler = "".join("- line %d\n" % n for n in range(1, 201))
        self.write("MEMORY.md", filler + "- [Late](late.md)\n")
        self.assertEqual("cutoff", self.listing()["memories"][0]["status"])

    def test_the_first_index_line_decides(self):
        self.write("twice.md", memory_text())
        filler = "".join("- line %d\n" % n for n in range(1, 200))
        self.write("MEMORY.md", "- [T](twice.md)\n" + filler + "- [T again](twice.md)\n")
        memory = self.listing()["memories"][0]
        self.assertEqual("loaded", memory["status"])
        self.assertEqual(1, memory["indexLine"])

    def test_index_lines_without_a_file_are_dangling(self):
        self.write("MEMORY.md", "# Index\n- [Gone](gone.md) — deleted by hand\n")
        listing = self.listing()
        self.assertEqual(
            [{"project": "-workspace", "file": "gone.md", "line": 2,
              "text": "- [Gone](gone.md) — deleted by hand"}],
            listing["dangling"])
        self.assertEqual(1, listing["totals"]["dangling"])

    def test_unknown_or_missing_type_is_other(self):
        self.write("a.md", memory_text(type_="opinion"))
        self.write("b.md", memory_text(type_=None))
        self.assertEqual(["other", "other"],
                         [m["type"] for m in self.listing()["memories"]])

    def test_a_file_without_frontmatter_is_named_after_its_file(self):
        self.write("plain-note.md", "Just text.\n")
        memory = self.listing()["memories"][0]
        self.assertEqual("plain-note", memory["name"])
        self.assertEqual("", memory["description"])
        self.assertEqual("other", memory["type"])

    def test_a_bad_modified_falls_back_to_the_mtime(self):
        path = self.write("a.md", memory_text(modified="yesterday-ish"))
        os.utime(path, (1700000000, 1700000000))
        self.assertEqual(1700000000, self.listing()["memories"][0]["modified"])

    def test_a_missing_modified_falls_back_to_the_mtime(self):
        path = self.write("a.md", memory_text(modified=None))
        os.utime(path, (1700000000, 1700000000))
        self.assertEqual(1700000000, self.listing()["memories"][0]["modified"])

    def test_names_that_are_not_memory_filenames_are_skipped(self):
        self.write(".hidden.md", memory_text())
        self.write("notes.txt", "x")
        os.makedirs(os.path.join(self.memory_dir(), "sub.md"))
        self.assertEqual([], self.listing()["memories"])

    def test_a_symlink_is_not_listed(self):
        target = os.path.join(self.home, "elsewhere.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write(memory_text())
        os.symlink(target, os.path.join(self.memory_dir(), "link.md"))
        self.assertEqual([], self.listing()["memories"])

    def test_memories_sort_by_name_ignoring_case(self):
        self.write("b.md", memory_text(name="beta"))
        self.write("a.md", memory_text(name="Alpha"))
        self.write("c.md", memory_text(name="charlie"))
        self.assertEqual(["Alpha", "beta", "charlie"],
                         [m["name"] for m in self.listing()["memories"]])

    def test_totals(self):
        self.write("a.md", "x" * 100)
        self.write("b.md", "y" * 50)
        index = "- [A](a.md)\n- [Gone](gone.md)\n"
        self.write("MEMORY.md", index)
        totals = self.listing()["totals"]
        self.assertEqual(2, totals["count"])
        self.assertEqual(150, totals["bytes"])
        self.assertEqual(len(index), totals["indexBytes"])
        self.assertEqual(2, totals["indexLines"])
        self.assertEqual((1, 0, 1, 1), (totals["loaded"], totals["cutoff"],
                                        totals["orphaned"], totals["dangling"]))

    def test_every_memory_dir_is_merged_and_the_largest_index_counts(self):
        self.write("a.md", memory_text(name="a"), project="-workspace")
        self.write("b.md", memory_text(name="b"), project="-srv-app")
        self.write("MEMORY.md", "- [A](a.md)\n", project="-workspace")
        self.write("MEMORY.md", "- [B](b.md)\n- [x](x.md)\n", project="-srv-app")
        listing = self.listing()
        self.assertEqual({"-workspace/a.md", "-srv-app/b.md"},
                         {m["id"] for m in listing["memories"]})
        self.assertEqual(2, listing["totals"]["indexLines"])

    def test_a_project_without_a_memory_dir_is_ignored(self):
        os.makedirs(os.path.join(self.home, "projects", "-other"))
        self.assertEqual([], self.listing()["memories"])

    def test_long_and_multiline_strings_are_bounded_to_one_line(self):
        self.write("a.md", memory_text(name="x" * 500, description="one\\ntwo  three"))
        memory = self.listing()["memories"][0]
        self.assertLessEqual(len(memory["name"]), 120)
        self.assertEqual("one two three", memory["description"])

    def test_a_link_to_an_unlisted_file_that_exists_is_not_dangling(self):
        self.write("_under.md", memory_text())  # not a listable memory name
        self.write("\u00e9t\u00e9.md", memory_text())  # non-ASCII
        target = os.path.join(self.home, "elsewhere.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("x")
        os.symlink(target, os.path.join(self.memory_dir(), "link.md"))
        self.write("MEMORY.md", "- [u](_under.md)\n- [e](\u00e9t\u00e9.md)\n- [l](link.md)\n")
        listing = self.listing()
        self.assertEqual([], listing["dangling"])
        self.assertEqual(0, listing["totals"]["dangling"])

    def test_a_symlinked_memory_dir_is_not_listed(self):
        real = os.path.join(self.home, "real-memory")
        os.makedirs(real)
        with open(os.path.join(real, "a.md"), "w", encoding="utf-8") as handle:
            handle.write(memory_text())
        os.makedirs(os.path.join(self.home, "projects", "-linked"))
        os.symlink(real, os.path.join(self.home, "projects", "-linked", "memory"))
        self.assertEqual([], self.listing()["memories"])

    def test_a_symlinked_index_counts_as_absent(self):
        self.write("a.md", memory_text())
        target = os.path.join(self.home, "idx.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("- [A](a.md)\n")
        os.symlink(target, os.path.join(self.memory_dir(), "MEMORY.md"))
        listing = self.listing()
        self.assertEqual("orphaned", listing["memories"][0]["status"])
        self.assertEqual(0, listing["totals"]["indexBytes"])

    def test_a_project_dir_that_is_not_utf8_is_skipped(self):
        projects = os.path.join(self.home, "projects")
        os.makedirs(projects)
        try:
            bad = os.path.join(os.fsencode(projects), b"\xff")
            os.mkdir(bad)
            os.mkdir(os.path.join(bad, b"memory"))
        except OSError:
            self.skipTest("filesystem refuses non-UTF-8 names")
        with open(os.path.join(bad, b"memory", b"a.md"), "w", encoding="utf-8") as handle:
            handle.write(memory_text())
        self.write("b.md", memory_text())
        self.assertEqual(["-workspace/b.md"], [m["id"] for m in self.listing()["memories"]])

    def test_the_index_is_opened_without_following_symlinks(self):
        self.write("a.md", memory_text())
        self.write("MEMORY.md", "- [A](a.md)\n")
        real_open = os.open
        flags = []

        def spy(path, flag, *args, **kwargs):
            flags.append(flag)
            return real_open(path, flag, *args, **kwargs)

        with mock.patch("memory_store.os.open", spy):
            self.assertEqual("loaded", self.listing()["memories"][0]["status"])
        self.assertTrue(flags and all(f & os.O_NOFOLLOW for f in flags))

    def test_a_byte_order_mark_does_not_hide_the_frontmatter(self):
        self.write("a.md", b"\xef\xbb\xbf" + memory_text(name="bom-name").encode("utf-8"), mode="wb")
        memory = self.listing()["memories"][0]
        self.assertEqual("bom-name", memory["name"])
        self.assertEqual("feedback", memory["type"])


class ResolveTest(HomeTestCase):
    def assertStatus(self, status, project, filename):
        with self.assertRaises(memory_store.MemoryStoreError) as caught:
            memory_store.read_memory(self.home, project, filename)
        self.assertEqual(status, caught.exception.status)
        return caught.exception

    def test_reading_returns_the_whole_file(self):
        content = memory_text(body="line one\nline two\n")
        self.write("a.md", content)
        self.assertEqual(content, memory_store.read_memory(self.home, "-workspace", "a.md"))

    def test_bad_ids_are_400(self):
        self.write("a.md", memory_text())
        for project, filename in [
            ("-workspace", "MEMORY.md"),
            ("-workspace", "../a.md"),
            ("-workspace", "a.txt"),
            ("-workspace", ".a.md"),
            ("-workspace", "/etc/passwd.md"),
            ("..", "a.md"),
            (".", "a.md"),
            ("", "a.md"),
            ("a/b", "a.md"),
            ("a\\b", "a.md"),
            ("-work\x00space", "a.md"),
        ]:
            with self.subTest(project=project, filename=filename):
                error = self.assertStatus(400, project, filename)
                self.assertEqual("not a memory id", str(error))

    def test_an_unknown_file_or_project_is_404(self):
        self.memory_dir()
        self.assertEqual("no such memory", str(self.assertStatus(404, "-workspace", "nope.md")))
        self.assertStatus(404, "-nowhere", "a.md")

    def test_a_directory_or_symlink_is_404(self):
        os.makedirs(os.path.join(self.memory_dir(), "dir.md"))
        self.assertStatus(404, "-workspace", "dir.md")
        target = os.path.join(self.home, "outside.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("secret")
        os.symlink(target, os.path.join(self.memory_dir(), "link.md"))
        self.assertStatus(404, "-workspace", "link.md")

    def test_a_symlinked_memory_dir_is_404(self):
        real = os.path.join(self.home, "real-memory")
        os.makedirs(real)
        with open(os.path.join(real, "a.md"), "w", encoding="utf-8") as handle:
            handle.write("secret")
        os.makedirs(os.path.join(self.home, "projects", "-linked"))
        os.symlink(real, os.path.join(self.home, "projects", "-linked", "memory"))
        self.assertStatus(404, "-linked", "a.md")

    def test_a_project_that_does_not_encode_is_400(self):
        self.write("a.md", memory_text())
        error = self.assertStatus(400, "\ud800", "a.md")
        self.assertEqual("not a memory id", str(error))

    def test_a_file_swapped_for_a_symlink_after_resolve_is_404(self):
        target = os.path.join(self.home, "outside.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("secret")
        path = self.write("a.md", memory_text())
        real_resolve = memory_store.resolve

        def swapping(*args):
            result = real_resolve(*args)
            os.unlink(path)
            os.symlink(target, path)
            return result

        with mock.patch("memory_store.resolve", swapping):
            self.assertStatus(404, "-workspace", "a.md")

    def test_invalid_utf8_is_replaced_not_raised(self):
        self.write("a.md", b"ok \xff\n", mode="wb")
        self.assertEqual("ok \ufffd\n", memory_store.read_memory(self.home, "-workspace", "a.md"))


class ErrorPublicMessageTest(unittest.TestCase):
    def test_public_defaults_to_none_and_can_be_set(self):
        self.assertIsNone(memory_store.MemoryStoreError(500, "x").public)
        self.assertEqual("p", memory_store.MemoryStoreError(500, "x", public="p").public)


class DeleteMemoryTest(HomeTestCase):
    def delete(self, filename, project="-workspace"):
        memory_store.delete_memory(self.home, project, filename)

    def test_the_file_and_its_index_line_go_and_nothing_else_changes(self):
        self.write("a.md", memory_text())
        self.write("b.md", memory_text())
        self.write("MEMORY.md", b"# Index\r\n- [A](a.md) \xe2\x80\x94 x\r\n- [B](b.md)", mode="wb")
        self.delete("a.md")
        self.assertFalse(os.path.exists(os.path.join(self.memory_dir(), "a.md")))
        # CRLF kept, missing trailing newline kept, other lines byte-identical.
        self.assertEqual(b"# Index\r\n- [B](b.md)", self.read_bytes("MEMORY.md"))

    def test_every_line_linking_to_the_file_is_removed(self):
        self.write("a.md", memory_text())
        self.write("MEMORY.md", "- [A](a.md)\n- [keep](b.md)\n- [A again](a.md)\n")
        self.delete("a.md")
        self.assertEqual(b"- [keep](b.md)\n", self.read_bytes("MEMORY.md"))

    def test_a_line_that_only_mentions_the_name_without_linking_stays(self):
        self.write("a.md", memory_text())
        self.write("MEMORY.md", "see a.md for details\n- [A](a.md)\n- [aa](aa.md)\n")
        self.delete("a.md")
        self.assertEqual(b"see a.md for details\n- [aa](aa.md)\n", self.read_bytes("MEMORY.md"))

    def test_a_line_linking_to_it_and_to_others_goes_whole(self):
        self.write("a.md", memory_text())
        self.write("b.md", memory_text())
        self.write("MEMORY.md", "- [A](a.md) and [B](b.md)\n- [keep](c.md)\n")
        self.delete("a.md")
        self.assertEqual(b"- [keep](c.md)\n", self.read_bytes("MEMORY.md"))
        # The other file the line linked to is now unindexed.
        [memory] = memory_store.list_memories(self.home)["memories"]
        self.assertEqual("orphaned", memory["status"])

    def test_a_symlinked_index_is_refused_with_a_500_and_left_alone(self):
        self.write("a.md", memory_text())
        target = os.path.join(self.home, "idx.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("- [A](a.md)\n")
        os.symlink(target, os.path.join(self.memory_dir(), "MEMORY.md"))
        with self.assertRaises(memory_store.MemoryStoreError) as caught:
            self.delete("a.md")
        self.assertEqual(500, caught.exception.status)
        self.assertIn("could not update", str(caught.exception))
        # The file is gone, so the browser is told the true, path-free state.
        self.assertEqual("deleted the memory, but could not update MEMORY.md",
                         caught.exception.public)
        self.assertFalse(os.path.exists(os.path.join(self.memory_dir(), "a.md")))
        self.assertTrue(os.path.islink(os.path.join(self.memory_dir(), "MEMORY.md")))
        with open(target, encoding="utf-8") as handle:
            self.assertEqual("- [A](a.md)\n", handle.read())

    def test_an_index_swapped_for_a_symlink_after_the_check_is_refused(self):
        self.write("a.md", memory_text())
        path = self.write("MEMORY.md", "- [A](a.md)\n")
        target = os.path.join(self.home, "idx.md")
        with open(target, "w", encoding="utf-8") as handle:
            handle.write("- [A](a.md)\n")
        real_lstat = os.lstat

        def swapping(p, *args, **kwargs):
            result = real_lstat(p, *args, **kwargs)
            if p == path:
                os.unlink(path)
                os.symlink(target, path)
            return result

        with mock.patch("memory_store.os.lstat", swapping):
            with self.assertRaises(memory_store.MemoryStoreError) as caught:
                self.delete("a.md")
        self.assertEqual(500, caught.exception.status)
        with open(target, encoding="utf-8") as handle:
            self.assertEqual("- [A](a.md)\n", handle.read())

    def test_the_new_index_is_synced_before_the_rename(self):
        self.write("a.md", memory_text())
        self.write("MEMORY.md", "- [A](a.md)\n- [B](b.md)\n")
        with mock.patch("memory_store.os.fsync") as fsync:
            self.delete("a.md")
        fsync.assert_called_once()

    def test_no_index_is_fine(self):
        self.write("a.md", memory_text())
        self.delete("a.md")
        self.assertFalse(os.path.exists(os.path.join(self.memory_dir(), "MEMORY.md")))

    def test_an_index_without_the_line_is_left_untouched(self):
        self.write("a.md", memory_text())
        path = self.write("MEMORY.md", "- [B](b.md)\n")
        before = os.stat(path)
        self.delete("a.md")
        self.assertEqual(before.st_ino, os.stat(path).st_ino)  # not rewritten

    def test_the_rewrite_keeps_the_index_mode_and_leaves_no_temp_file(self):
        self.write("a.md", memory_text())
        path = self.write("MEMORY.md", "- [A](a.md)\n- [B](b.md)\n")
        os.chmod(path, 0o640)
        self.delete("a.md")
        self.assertEqual(0o640, stat.S_IMODE(os.stat(path).st_mode))
        self.assertEqual(["MEMORY.md"], sorted(os.listdir(self.memory_dir())))

    def test_bad_ids_and_missing_files_are_refused_and_change_nothing(self):
        self.write("MEMORY.md", "- [A](a.md)\n")
        for filename, status in (("MEMORY.md", 400), ("../x.md", 400), ("a.md", 404)):
            with self.subTest(filename=filename):
                with self.assertRaises(memory_store.MemoryStoreError) as caught:
                    self.delete(filename)
                self.assertEqual(status, caught.exception.status)
        self.assertEqual(b"- [A](a.md)\n", self.read_bytes("MEMORY.md"))

    def test_an_unlink_failure_is_a_500_that_names_the_path(self):
        path = self.write("a.md", memory_text())
        with mock.patch("memory_store.os.unlink", side_effect=PermissionError("denied")):
            with self.assertRaises(memory_store.MemoryStoreError) as caught:
                self.delete("a.md")
        self.assertEqual(500, caught.exception.status)
        self.assertIn(path, str(caught.exception))
        self.assertTrue(os.path.exists(path))

    def test_an_index_rewrite_failure_is_a_500_and_leaves_the_old_index(self):
        self.write("a.md", memory_text())
        self.write("MEMORY.md", "- [A](a.md)\n")
        with mock.patch("memory_store.os.replace", side_effect=OSError("disk full")):
            with self.assertRaises(memory_store.MemoryStoreError) as caught:
                self.delete("a.md")
        self.assertEqual(500, caught.exception.status)
        self.assertEqual(b"- [A](a.md)\n", self.read_bytes("MEMORY.md"))
        self.assertEqual(["MEMORY.md"], sorted(os.listdir(self.memory_dir())))


if __name__ == "__main__":
    unittest.main()
