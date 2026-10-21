"""Tests for the checks a copy of a volume's data rests on.

``check_private_entries`` proves each name the copy leaves out really is the folder
macOS keeps there; ``tree_manifest`` and ``manifest_difference`` compare two trees
entry by entry, so a copy that lost an empty folder or renamed a file never passes.
"""

import os
import stat
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sf_walk
from sf_errors import ToolError
from tests import fake_host

#: Where ``st_mode`` sits in an ``os.stat_result`` read as a sequence.
_MODE_INDEX = 0


class _TreeCase(unittest.TestCase):
    """A fresh temporary folder, resolved, for each test."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))


class PrivateEntriesTest(_TreeCase):
    def _check(self) -> "tuple[str, ...]":
        return sf_walk.check_private_entries(
            self.root, lstat=fake_host.root_owned_lstat
        )

    def test_a_root_with_none_of_them_passes_with_none_present(self) -> None:
        (self.root / "data").mkdir()
        self.assertEqual(self._check(), ())

    def test_root_owned_folders_pass_and_are_listed_sorted(self) -> None:
        for name in (".Trashes", ".fseventsd", ".DocumentRevisions-V100"):
            (self.root / name).mkdir()
        self.assertEqual(
            self._check(), (".DocumentRevisions-V100", ".Trashes", ".fseventsd")
        )

    def test_a_user_owned_trashes_folder_is_refused(self) -> None:
        (self.root / ".Trashes").mkdir()
        with self.assertRaises(ToolError) as caught:
            sf_walk.check_private_entries(self.root)
        text = str(caught.exception)
        self.assertIn(str(self.root / ".Trashes"), text)
        self.assertIn("is not the macOS-managed folder of that name", text)
        self.assertIn("move it aside or rename it first", text)

    def test_a_root_owned_plain_file_of_that_name_is_refused(self) -> None:
        (self.root / ".Trashes").write_bytes(b"a file, not the folder")
        with self.assertRaises(ToolError) as caught:
            self._check()
        self.assertIn(".Trashes", str(caught.exception))

    def test_a_symlink_of_that_name_is_refused_even_to_a_folder(self) -> None:
        (self.root / "real").mkdir()
        (self.root / ".TemporaryItems").symlink_to("real")
        with self.assertRaises(ToolError) as caught:
            self._check()
        self.assertIn(".TemporaryItems", str(caught.exception))

    def test_an_entry_that_cannot_be_read_is_a_tool_error(self) -> None:
        denied = PermissionError(13, "Permission denied")
        with self.assertRaises(ToolError) as caught:
            sf_walk.check_private_entries(
                self.root, lstat=mock.Mock(side_effect=denied)
            )
        self.assertIn("could not stat", str(caught.exception))


class NeverIndexTest(_TreeCase):
    """The tool's own Spotlight marker: an empty file at a volume's root, or nothing."""

    def _refused(self) -> str:
        with self.assertRaises(ToolError) as caught:
            sf_walk.check_never_index(self.root)
        return str(caught.exception)

    def test_its_name_is_the_one_spotlight_reads(self) -> None:
        self.assertEqual(sf_walk.NEVER_INDEX, ".metadata_never_index")
        self.assertEqual(
            sf_walk.VOLUME_OWN, sf_walk.VOLUME_PRIVATE | {sf_walk.NEVER_INDEX}
        )

    def test_none_there_reads_absent(self) -> None:
        self.assertFalse(sf_walk.check_never_index(self.root))

    def test_an_empty_file_reads_present(self) -> None:
        (self.root / sf_walk.NEVER_INDEX).write_bytes(b"")
        self.assertTrue(sf_walk.check_never_index(self.root))

    def test_a_file_with_content_is_refused_rather_than_overwritten(self) -> None:
        (self.root / sf_walk.NEVER_INDEX).write_bytes(b"notes")
        text = self._refused()
        self.assertIn(str(self.root / sf_walk.NEVER_INDEX), text)
        self.assertIn("is not the empty file", text)
        self.assertIn("move it aside or rename it first", text)

    def test_a_folder_of_that_name_is_refused(self) -> None:
        (self.root / sf_walk.NEVER_INDEX).mkdir()
        self.assertIn("is not the empty file", self._refused())

    def test_a_symlink_to_an_empty_file_is_refused(self) -> None:
        (self.root / "empty").write_bytes(b"")
        (self.root / sf_walk.NEVER_INDEX).symlink_to("empty")
        self.assertIn("is not the empty file", self._refused())

    def test_an_entry_that_cannot_be_read_is_a_tool_error(self) -> None:
        denied = PermissionError(13, "Permission denied")
        with mock.patch.object(sf_walk.os, "lstat", side_effect=denied):
            self.assertIn("could not stat", self._refused())

    def test_the_volume_readers_leave_it_out_at_the_top_only(self) -> None:
        (self.root / sf_walk.NEVER_INDEX).write_bytes(b"")
        (self.root / "sub").mkdir()
        (self.root / "sub" / sf_walk.NEVER_INDEX).write_bytes(b"")
        self.assertEqual(sf_walk.volume_entries(self.root), ["sub"])
        paths = [entry.path for entry in sf_walk.volume_manifest(self.root)]
        self.assertEqual(paths, ["sub", f"sub/{sf_walk.NEVER_INDEX}"])
        self.assertEqual(sf_walk.volume_stats(self.root), sf_walk.TreeStats(1, 0))


class ManifestTest(_TreeCase):
    def _tree(self, top: Path) -> Path:
        """A small tree: a file, a folder with a file, an empty folder and a link."""
        (top / "sub" / "empty").mkdir(parents=True)
        (top / "a.txt").write_bytes(b"alpha")
        (top / "sub" / "b.txt").write_bytes(b"beta")
        (top / "link").symlink_to("a.txt")
        return top

    def _two(self) -> "tuple[Path, Path]":
        return self._tree(self.root / "one"), self._tree(self.root / "two")

    def test_lists_every_entry_with_its_kind_size_and_target(self) -> None:
        tree = self._tree(self.root / "one")
        entry = sf_walk.ManifestEntry
        self.assertEqual(
            sf_walk.tree_manifest(tree),
            (
                entry("a.txt", sf_walk.KIND_FILE, 5, ""),
                entry("link", sf_walk.KIND_SYMLINK, 0, "a.txt"),
                entry("sub", sf_walk.KIND_DIR, 0, ""),
                entry("sub/b.txt", sf_walk.KIND_FILE, 4, ""),
                entry("sub/empty", sf_walk.KIND_DIR, 0, ""),
            ),
        )

    def test_equal_trees_have_no_difference(self) -> None:
        one, two = self._two()
        self.assertIsNone(
            sf_walk.manifest_difference(
                sf_walk.tree_manifest(one), sf_walk.tree_manifest(two)
            )
        )

    def test_a_missing_empty_folder_is_a_difference(self) -> None:
        one, two = self._two()
        (two / "sub" / "empty").rmdir()
        found = sf_walk.manifest_difference(
            sf_walk.tree_manifest(one), sf_walk.tree_manifest(two)
        )
        self.assertEqual(found, "not in the copy: sub/empty (folder)")

    def test_a_renamed_file_of_equal_size_is_a_difference(self) -> None:
        one, two = self._two()
        (two / "a.txt").rename(two / "c.txt")
        found = sf_walk.manifest_difference(
            sf_walk.tree_manifest(one), sf_walk.tree_manifest(two)
        )
        self.assertEqual(
            found,
            "not in the copy: a.txt (file, 5 bytes); "
            "only in the copy: c.txt (file, 5 bytes)",
        )

    def test_a_changed_link_target_is_a_difference(self) -> None:
        one, two = self._two()
        (two / "link").unlink()
        (two / "link").symlink_to("sub")
        found = sf_walk.manifest_difference(
            sf_walk.tree_manifest(one), sf_walk.tree_manifest(two)
        )
        self.assertIn("link (symlink to a.txt)", str(found))
        self.assertIn("only in the copy: link (symlink to sub)", str(found))

    def test_several_differences_name_the_first_and_count_the_rest(self) -> None:
        one, two = self._two()
        (one / "x").write_bytes(b"")
        (one / "y").write_bytes(b"")
        found = sf_walk.manifest_difference(
            sf_walk.tree_manifest(one), sf_walk.tree_manifest(two)
        )
        self.assertEqual(found, "not in the copy: x (file, 0 bytes) and 1 more")

    def test_an_entry_neither_file_nor_folder_nor_link_is_listed_as_other(
        self,
    ) -> None:
        # A FIFO stands in for every such kind: made as a file, then reported as a
        # FIFO by lstat, so the test needs no platform's mkfifo.
        pipe = self.root / "pipe"
        pipe.write_bytes(b"")
        real_lstat = os.lstat

        def as_fifo(path: "str | os.PathLike[str]") -> os.stat_result:
            fields = list(real_lstat(path))
            fields[_MODE_INDEX] = stat.S_IFIFO | 0o600
            return os.stat_result(fields)

        with mock.patch.object(sf_walk.os, "lstat", side_effect=as_fifo):
            manifest = sf_walk.tree_manifest(self.root)
        self.assertEqual(
            manifest, (sf_walk.ManifestEntry("pipe", sf_walk.KIND_OTHER, 0, ""),)
        )
        found = sf_walk.manifest_difference(manifest, ())
        self.assertEqual(found, "not in the copy: pipe (other)")

    def test_a_volume_manifest_skips_private_names_at_the_top_only(self) -> None:
        tree = self._tree(self.root / "one")
        locked = tree / ".fseventsd"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        (tree / "sub" / ".Trashes").write_bytes(b"data")
        (tree / ".Trashes").write_bytes(b"skipped")
        names = [entry.path for entry in sf_walk.volume_manifest(tree)]
        self.assertEqual(
            names, ["a.txt", "link", "sub", "sub/.Trashes", "sub/b.txt", "sub/empty"]
        )

    def test_a_missing_folder_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            sf_walk.tree_manifest(self.root / "absent")

    def test_an_unreadable_subfolder_is_a_tool_error(self) -> None:
        tree = self._tree(self.root / "one")
        (tree / "sub").chmod(0)
        self.addCleanup((tree / "sub").chmod, 0o700)
        with self.assertRaises(ToolError):
            sf_walk.tree_manifest(tree)

    def test_an_entry_vanishing_mid_walk_is_a_tool_error(self) -> None:
        (self.root / "gone.txt").write_bytes(b"x")
        vanished = FileNotFoundError(2, "No such file or directory")
        with mock.patch.object(sf_walk.os, "lstat", side_effect=vanished):
            with self.assertRaises(ToolError) as caught:
                sf_walk.tree_manifest(self.root)
        self.assertIn("gone.txt", str(caught.exception))


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
