"""Tests for setup mode: its refusals, its dry run and the FileVault note.

The applied runs are in ``test_sf_setup_apply.py``, which shares ``SetupFixture``. Every
case runs on a ``FakeHost``; the folder, its aside and the fstab are real files in the
test's own temporary folder, and no case reaches diskutil, tmutil, mdutil, sudo, ditto
or the machine's fstab.
"""

from __future__ import annotations

import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple
from unittest import mock

import sf_commands
import sf_fstab
import sf_macos
import sf_setup
from sf_errors import EXIT_OK, ToolError
from sf_macos import APFS_LIST_ARGV
from sf_options import Options
from sf_size import SPACE_MARGIN
from sf_walk import NEVER_INDEX
from tests import fake_host
from tests.fake_host import (
    FAKE_CONTAINER,
    FAKE_UUID_1,
    container_entry,
    volume_entry,
)

#: The quota every case asks for unless it says otherwise: 40g.
QUOTA = 40 * 10**9
#: The name setup derives for the folder "work".
VOLUME = "snapfree-work"
#: The folder's mode, distinctive so a recreated folder is seen to keep it.
FOLDER_MODE = 0o750
#: A line the fstab already holds, so a rewrite is seen to keep it.
EXISTING_LINE = "# an existing comment\n"


def q(path: object) -> str:
    """*path* shell-quoted, as the tool prints it."""
    return shlex.quote(str(path))


class SetupFixture(unittest.TestCase):
    """A temporary folder holding the folder to move, an fstab and a scripted host."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.folder = self.tmp / "work"
        (self.folder / "sub").mkdir(parents=True)
        (self.folder / "a.txt").write_bytes(b"hello")
        (self.folder / "sub" / "b.txt").write_bytes(b"world!")
        os.symlink("a.txt", self.folder / "link")
        os.chmod(self.folder, FOLDER_MODE)
        self.fstab = self.tmp / "fstab"
        self.fstab.write_bytes(EXISTING_LINE.encode())
        self.aside = self.tmp / f"work.snapfree-aside-{fake_host.FAKE_STAMP}"
        owner = os.stat(self.folder)
        # The test user's own IDs, read from a folder it made: os.getuid is POSIX-only.
        self.uid, self.gid = owner.st_uid, owner.st_gid
        self.fake = fake_host.FakeHost(self.tmp, uid=self.uid, gid=self.gid)

    def options(self, *, apply: bool = False, **changes: object) -> Options:
        """Setup's options for the folder, with *changes* applied."""
        fields = {
            "mode": "setup",
            "path": self.folder,
            "quota": QUOTA,
            "name": None,
            "fstab": self.fstab,
            "warn_percent": 90,
            "finish": False,
            "apply": apply,
        }
        fields.update(changes)
        return Options(**fields)  # type: ignore[arg-type]

    def script_validation(
        self,
        *,
        filesystem: str = "apfs",
        filevault: bool = False,
        free: int = fake_host.FAKE_FREE,
        volumes: Tuple[str, ...] = ("Data",),
    ) -> None:
        """Answer the two reads validation makes: the folder's volume and its container."""
        self.fake.respond_info(
            self.folder,
            once=True,
            mount_point="/",
            volume_uuid=FAKE_UUID_1,
            filesystem=filesystem,
            filevault=filevault,
            container_free=free,
        )
        entries = [volume_entry(uuid=FAKE_UUID_1, name=name) for name in volumes]
        self.fake.respond_apfs_list([container_entry(volumes=entries)], once=True)

    def run_setup(self, opts: Options) -> Tuple[int, str]:
        """Run setup; its exit code and what it printed."""
        out = io.StringIO()
        code = sf_setup.run_setup(opts, self.fake, out)
        return code, out.getvalue()

    def refusal(self, opts: Optional[Options] = None) -> str:
        """The ToolError message setup refuses with."""
        with self.assertRaises(ToolError) as caught:
            self.run_setup(opts if opts is not None else self.options())
        return str(caught.exception)

    def read_only_calls(self) -> List[Tuple[str, ...]]:
        """The calls validation makes: nothing else may run in a dry run."""
        return [*self.fake.volume_reads(self.folder), APFS_LIST_ARGV]


class ValidationTest(SetupFixture):
    def test_a_missing_folder_is_refused(self) -> None:
        message = self.refusal(self.options(path=self.tmp / "absent"))
        self.assertIn("does not exist", message)
        self.assertEqual(self.fake.calls, [])

    def test_a_file_is_refused(self) -> None:
        plain = self.tmp / "plain.txt"
        plain.write_bytes(b"x")
        self.assertIn("not a folder", self.refusal(self.options(path=plain)))

    def test_a_folder_owned_by_someone_else_is_refused(self) -> None:
        self.fake = fake_host.FakeHost(self.tmp, uid=self.uid + 1, gid=self.gid)
        self.assertIn("not owned by", self.refusal())

    def test_a_mount_point_is_refused(self) -> None:
        self.fake.mount(self.folder)
        self.assertIn("already a mount point", self.refusal())

    def test_a_folder_the_fstab_already_names_is_refused(self) -> None:
        line = sf_fstab.render_line(FAKE_UUID_1, self.folder)
        self.fstab.write_bytes(f"{line}\n".encode())
        self.assertIn("already has a line", self.refusal())
        self.assertEqual(self.fake.calls, [])

    def test_a_folder_off_apfs_is_refused(self) -> None:
        self.script_validation(filesystem="hfs")
        self.assertIn("not APFS", self.refusal())

    def test_a_volume_of_that_name_already_present_is_refused(self) -> None:
        self.script_validation(volumes=("Data", VOLUME))
        message = self.refusal()
        self.assertIn(VOLUME, message)
        self.assertIn("already", message)

    def test_data_at_the_quota_is_refused(self) -> None:
        self.script_validation()
        data = sf_setup.tree_stats(self.folder).bytes
        self.assertIn("quota", self.refusal(self.options(quota=data)))

    def test_data_just_under_the_quota_is_accepted(self) -> None:
        self.script_validation()
        data = sf_setup.tree_stats(self.folder).bytes
        code, _ = self.run_setup(self.options(quota=data + 1))
        self.assertEqual(code, EXIT_OK)

    def test_data_and_margin_past_the_free_space_is_refused(self) -> None:
        data = sf_setup.tree_stats(self.folder).bytes
        self.script_validation(free=data + SPACE_MARGIN - 1)
        self.assertIn("free", self.refusal())

    def test_data_and_margin_exactly_the_free_space_is_accepted(self) -> None:
        data = sf_setup.tree_stats(self.folder).bytes
        self.script_validation(free=data + SPACE_MARGIN)
        code, _ = self.run_setup(self.options())
        self.assertEqual(code, EXIT_OK)

    def test_a_taken_aside_path_is_refused(self) -> None:
        self.aside.mkdir()
        self.script_validation()
        self.assertIn(str(self.aside), self.refusal())

    def test_a_spotlight_marker_with_content_is_refused(self) -> None:
        # Setup's own marker is an empty file; one holding something is the
        # operator's, and the copy would put it where the tool keeps its own.
        (self.folder / NEVER_INDEX).write_bytes(b"notes")
        self.script_validation()
        text = self.refusal()
        self.assertIn(str(self.folder / NEVER_INDEX), text)
        self.assertIn("is not the empty file", text)

    def test_no_quota_is_refused(self) -> None:
        self.assertIn("--quota", self.refusal(self.options(quota=None)))


class DryRunTest(SetupFixture):
    def test_every_step_prints_and_nothing_changes(self) -> None:
        self.script_validation()
        code, out = self.run_setup(self.options())
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(self.fake.calls, self.read_only_calls())
        placeholder = "<new-volume-uuid>"
        for expected in (
            f"  1. sudo diskutil apfs addVolume {FAKE_CONTAINER} APFS {VOLUME} "
            "-quota 40g -nomount",
            f"  2. mv {q(self.folder)} {q(self.aside)} && mkdir -m 0750 {q(self.folder)}",
            f"  3. sudo diskutil mount -mountPoint {q(self.folder)} {placeholder}",
            f"  4. sudo diskutil enableOwnership {q(self.folder)} "
            "(if ownership is ignored)",
            f"  5. sudo chown {self.uid}:{self.gid} {q(self.folder)} "
            f"&& sudo chmod 0750 {q(self.folder)}",
            f"  6. sudo tmutil addexclusion -v {q(self.folder)}",
            f"  7. touch {self.folder / NEVER_INDEX} (as you: an empty file that "
            "keeps Spotlight off the volume once it mounts again)",
            f"  8. sudo diskutil unmount {q(self.folder)} && sudo diskutil mount "
            f"-mountOptions nobrowse -mountPoint {q(self.folder)} {placeholder}",
            f"  9. sudo mdutil -i off {q(self.folder)}",
            f"  10. add `UUID={placeholder} {self.folder} apfs rw,nobrowse` "
            f"to {self.fstab}:",
            f"  11. ditto {q(f'{self.aside}/')} {q(f'{self.folder}/')}",
            f"  12. keep {self.aside}, and print how to delete it after checking",
        ):
            self.assertIn(expected, out)
        for line in sf_fstab.describe_rewrite(self.fstab, backup=True):
            self.assertIn(f"       {line}", out)
        self.assertIn("Dry run: nothing was changed.", out)
        self.assertTrue(self.folder.is_dir())
        self.assertFalse(self.aside.exists())
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), EXISTING_LINE)
        self.assertNotIn("not be encrypted", out)

    def test_an_absent_fstab_prints_no_backup(self) -> None:
        self.fstab.unlink()
        self.script_validation()
        _, out = self.run_setup(self.options())
        self.assertNotIn("sudo cp", out)
        self.assertFalse(self.fstab.exists())

    def test_name_overrides_the_volume_name(self) -> None:
        self.script_validation(volumes=("Data", VOLUME))
        _, out = self.run_setup(self.options(name="scratch"))
        self.assertIn("APFS scratch -quota 40g", out)

    def test_filevault_prints_the_note_before_the_steps(self) -> None:
        self.script_validation(filevault=True)
        _, out = self.run_setup(self.options())
        self.assertIn(f"{sf_macos.FILEVAULT_NOTE.format(path=self.folder)}\n", out)
        self.assertLess(out.index("not be encrypted"), out.index("  1. "))


class HelpersTest(SetupFixture):
    def test_the_status_command_names_a_non_default_fstab(self) -> None:
        with mock.patch.object(sf_commands.sys, "argv", ["apfs_tm_snapfree.py"]):
            line = sf_setup.status_command(self.options())
        self.assertEqual(
            line,
            f"python3 apfs_tm_snapfree.py --mode status --path {q(self.folder)} "
            f"--fstab {q(self.fstab)}",
        )

    def test_the_status_command_leaves_out_the_default_fstab(self) -> None:
        opts = self.options(fstab=sf_fstab.DEFAULT_FSTAB)
        with mock.patch.object(sf_commands.sys, "argv", ["/some/where/tool.py"]):
            line = sf_setup.status_command(opts)
        self.assertEqual(line, f"python3 tool.py --mode status --path {q(self.folder)}")


if __name__ == "__main__":
    unittest.main()
