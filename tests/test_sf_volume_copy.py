"""Tests for copying a volume's data entry by entry, without macOS's own entries."""

import io
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Tuple

import sf_plan
import sf_volume_copy
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_host import COPY_TIMEOUT
from sf_walk import VOLUME_PRIVATE, volume_stats
from tests import fake_host


def _never() -> None:
    """A confirm with nothing to check."""


class CopyCase(unittest.TestCase):
    """A volume root with data, a symlink and a locked .fseventsd; a destination."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.root = self.tmp / "volume"
        (self.root / "sub").mkdir(parents=True)
        (self.root / "a.txt").write_bytes(b"alpha")
        (self.root / "sub" / "b.txt").write_bytes(b"beta")
        (self.root / "link").symlink_to("sub", target_is_directory=True)
        locked = self.root / ".fseventsd"
        locked.mkdir()
        locked.chmod(0)
        self.addCleanup(locked.chmod, 0o700)
        self.dest = self.tmp / "copy"
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.respond(["ditto"], prefix=True, effect=self._ditto)

    def _ditto(self, argv: Tuple[str, ...]) -> None:
        source, target = Path(argv[1]), Path(argv[2])
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)


class DescribeTest(CopyCase):
    def test_lists_each_command_under_one_summary(self) -> None:
        text = sf_volume_copy.describe_copy(self.root, self.dest, 0o750)
        lines = text.splitlines()
        self.assertIn(f"copy each entry at the top of {self.root} into", lines[0])
        for name in VOLUME_PRIVATE:
            self.assertIn(name, lines[0])
        self.assertEqual(
            [line.strip() for line in lines[1:]],
            [
                f"mkdir -m 0750 {self.dest}",
                f"ditto {self.root}/a.txt {self.dest}/a.txt",
                f"ln -s -- sub {self.dest}/link",
                f"ditto {self.root}/sub {self.dest}/sub",
            ],
        )

    def test_a_link_target_that_looks_like_an_option_is_quoted_after_dashes(
        self,
    ) -> None:
        (self.root / "link").unlink()
        (self.root / "link").symlink_to("-n my file")
        text = sf_volume_copy.describe_copy(self.root, self.dest, None)
        self.assertIn(f"ln -s -- '-n my file' {self.dest}/link", text)

    def test_an_empty_volume_says_there_is_nothing_to_copy(self) -> None:
        empty = self.tmp / "empty"
        empty.mkdir()
        text = sf_volume_copy.describe_copy(empty, self.dest, None)
        self.assertIn("nothing to copy", text.splitlines()[1])

    def test_a_root_that_cannot_be_listed_never_breaks_the_display(self) -> None:
        text = sf_volume_copy.describe_copy(self.tmp / "absent", self.dest, None)
        self.assertIn("could not list the entries now", text)


class CopyStepTest(CopyCase):
    def test_copies_the_data_and_recreates_the_symlink(self) -> None:
        step = sf_volume_copy.copy_step(
            self.fake, self.root, self.dest, confirm=_never, create_mode=0o750
        )
        out = io.StringIO()
        self.assertEqual(sf_plan.run_plan([step], apply=True, out=out), EXIT_OK)
        self.assertEqual(volume_stats(self.dest), volume_stats(self.root))
        self.assertFalse(os.path.lexists(self.dest / ".fseventsd"))
        self.assertEqual(os.readlink(self.dest / "link"), "sub")
        self.assertEqual(os.stat(self.dest).st_mode & 0o7777, 0o750)
        self.assertEqual(
            self.fake.calls,
            [
                ("ditto", f"{self.root}/a.txt", f"{self.dest}/a.txt"),
                ("ditto", f"{self.root}/sub", f"{self.dest}/sub"),
            ],
        )
        self.assertEqual(self.fake.timeouts, [COPY_TIMEOUT, COPY_TIMEOUT])

    def test_without_a_mode_the_destination_must_already_exist(self) -> None:
        self.dest.mkdir()
        step = sf_volume_copy.copy_step(self.fake, self.root, self.dest, confirm=_never)
        step.action()
        self.assertTrue((self.dest / "sub" / "b.txt").is_file())

    def test_the_confirm_runs_after_the_copy(self) -> None:
        seen = []

        def confirm() -> None:
            seen.append(volume_stats(self.dest))

        step = sf_volume_copy.copy_step(
            self.fake, self.root, self.dest, confirm=confirm, create_mode=0o700
        )
        step.action()
        self.assertEqual(seen, [volume_stats(self.root)])

    def test_a_refused_copy_fails_the_step(self) -> None:
        self.fake.respond(["ditto"], prefix=True, returncode=1, once=True)
        step = sf_volume_copy.copy_step(
            self.fake, self.root, self.dest, confirm=_never, create_mode=0o700
        )
        out = io.StringIO()
        self.assertEqual(sf_plan.run_plan([step], apply=True, out=out), EXIT_FAIL)
        self.assertIn("FAILED at step 1: ditto", out.getvalue())

    def test_a_mode_wider_than_the_umask_is_set_exactly(self) -> None:
        step = sf_volume_copy.copy_step(
            self.fake, self.root, self.dest, confirm=_never, create_mode=0o2775
        )
        step.action()
        self.assertEqual(os.stat(self.dest).st_mode & 0o7777, 0o2775)


class SameDataTest(CopyCase):
    """The copy's confirm compares every entry, not just a count of files and bytes."""

    _ADVICE = "quit whatever writes there"

    def setUp(self) -> None:
        super().setUp()
        (self.root / "sub" / "empty").mkdir()
        step = sf_volume_copy.copy_step(
            self.fake, self.root, self.dest, confirm=_never, create_mode=0o700
        )
        step.action()

    def _refused(self, *needles: str) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_volume_copy.require_same_data(self.root, self.dest, self._ADVICE)
        text = str(caught.exception)
        self.assertIn(f"the copy at {self.dest} does not match {self.root}: ", text)
        self.assertTrue(text.endswith(f": {self._ADVICE}"), text)
        for needle in needles:
            self.assertIn(needle, text)

    def test_an_equal_copy_passes(self) -> None:
        sf_volume_copy.require_same_data(self.root, self.dest, self._ADVICE)

    def test_a_missing_empty_folder_fails(self) -> None:
        (self.dest / "sub" / "empty").rmdir()
        self._refused("not in the copy: sub/empty (folder)")

    def test_a_renamed_file_of_equal_size_fails(self) -> None:
        (self.dest / "a.txt").rename(self.dest / "z.txt")
        self._refused("a.txt (file, 5 bytes)", "only in the copy: z.txt")


if __name__ == "__main__":
    unittest.main()
