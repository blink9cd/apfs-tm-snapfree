"""Tests for counting the files and bytes under a folder."""

import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sf_walk
from sf_errors import ToolError


class TreeStatsTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))

    def _write(self, relative: str, size: int) -> Path:
        path = self.root / relative
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_bytes(b"x" * size)
        return path

    def test_empty_folder_is_zero(self) -> None:
        self.assertEqual(sf_walk.tree_stats(self.root), sf_walk.TreeStats(0, 0))

    def test_counts_nested_files_and_their_sizes(self) -> None:
        self._write("a.txt", 10)
        self._write("sub/b.txt", 20)
        self._write("sub/deeper/c.txt", 5)
        self.assertEqual(sf_walk.tree_stats(self.root), sf_walk.TreeStats(3, 35))

    def test_symlinks_count_as_entries_and_are_not_followed(self) -> None:
        target = self._write("outside/big.bin", 1000)
        tree = self.root / "tree"
        tree.mkdir()
        (tree / "to_file").symlink_to(target)
        (tree / "to_dir").symlink_to(target.parent, target_is_directory=True)
        stats = sf_walk.tree_stats(tree)
        # Two links, each its own lstat size (the length of its target string); the
        # 1000-byte file behind them is never counted.
        self.assertEqual(stats.files, 2)
        expected = len(str(target)) + len(str(target.parent))
        self.assertEqual(stats.bytes, expected)

    def test_a_missing_folder_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_walk.tree_stats(self.root / "absent")
        self.assertIn("absent", str(caught.exception))

    def test_an_unreadable_subfolder_is_a_tool_error_not_a_silent_skip(self) -> None:
        self._write("locked/inner.txt", 3)
        locked = self.root / "locked"
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        with self.assertRaises(ToolError):
            sf_walk.tree_stats(self.root)

    def test_a_file_vanishing_mid_walk_is_a_tool_error(self) -> None:
        self._write("gone.txt", 3)
        vanished = FileNotFoundError("gone")
        # Nested rather than one parenthesized `with`, a form Python 3.9 does not promise.
        with mock.patch.object(sf_walk.os, "lstat", side_effect=vanished):
            with self.assertRaises(ToolError) as caught:
                sf_walk.tree_stats(self.root)
        self.assertIn("gone.txt", str(caught.exception))


class VolumeRootTest(unittest.TestCase):
    """A volume root holds macOS's own root-only folders, which are not the data."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.root = Path(os.path.realpath(holder.name))
        (self.root / "a.txt").write_bytes(b"12345")
        (self.root / "sub").mkdir()
        (self.root / "sub" / "b.txt").write_bytes(b"xy")
        (self.root / "link").symlink_to("a.txt")
        # A top-level .fseventsd as macOS leaves one: drwx------ root, which the user
        # cannot list. Mode 000 stands in for that ownership (root ignores it, and
        # the suites never run as root).
        locked = self.root / ".fseventsd"
        locked.mkdir()
        (locked / "log").write_bytes(b"zz")
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        (self.root / ".Trashes").write_bytes(b"not a folder, still skipped")

    def test_every_volume_private_name_is_listed(self) -> None:
        self.assertEqual(
            sf_walk.VOLUME_PRIVATE,
            frozenset(
                {
                    ".fseventsd",
                    ".Spotlight-V100",
                    ".Trashes",
                    ".TemporaryItems",
                    ".DocumentRevisions-V100",
                }
            ),
        )

    def test_volume_stats_skips_the_private_entries_at_the_top(self) -> None:
        self.assertEqual(sf_walk.volume_stats(self.root), sf_walk.TreeStats(3, 12))

    def test_tree_stats_still_refuses_the_unreadable_folder(self) -> None:
        with self.assertRaises(ToolError):
            sf_walk.tree_stats(self.root)

    def test_a_private_name_below_the_top_is_data_and_counted(self) -> None:
        nested = self.root / "sub" / ".fseventsd"
        nested.mkdir()
        (nested / "kept").write_bytes(b"123")
        self.assertEqual(sf_walk.volume_stats(self.root), sf_walk.TreeStats(4, 15))

    def test_the_copyable_entries_are_the_rest_sorted(self) -> None:
        self.assertEqual(sf_walk.volume_entries(self.root), ["a.txt", "link", "sub"])

    def test_listing_a_missing_root_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_walk.volume_entries(self.root / "absent")
        self.assertIn("absent", str(caught.exception))


if __name__ == "__main__":
    unittest.main()
