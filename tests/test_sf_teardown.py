"""Tests for teardown's validation, its dry run and its dispatch from the command line.

The folder, its fstab and the copies teardown makes are real files in the test's own
temporary directory; every diskutil, tmutil, ditto and sudo command is answered by the
scripted host, which also stands in for the volume's mount. The applied runs, and a
failure injected at each step, are in ``test_sf_teardown_apply``, which reuses the
fixture defined here.
"""

import contextlib
import dataclasses
import functools
import io
import os
import shutil
import stat
import tempfile
import unittest
from pathlib import Path
from typing import Sequence, Tuple
from unittest import mock

import apfs_tm_snapfree as cli
import sf_commands
import sf_fstab
import sf_macos
import sf_teardown
import sf_walk
from sf_errors import EXIT_OK, ToolError
from sf_host import RunResult
from sf_options import Options
from sf_size import SPACE_MARGIN
from sf_walk import NEVER_INDEX, VOLUME_PRIVATE, tree_stats
from tests import fake_host, tm_prefs

VOLUME_UUID = fake_host.FAKE_UUID_1
PARENT_UUID = fake_host.FAKE_UUID_2
#: Another volume's entry in Time Machine's exclusions, which teardown leaves alone.
STALE_UUID = fake_host.FAKE_UUID_3
STAMP = fake_host.FAKE_STAMP
#: The volume teardown deletes, and the parent volume it stays beside in the container.
VOLUME_DEVICE = "disk9s7"
PARENT_DEVICE = fake_host.FAKE_DEVICE
#: What the fake fstab holds besides teardown's line, which must survive the rewrite.
FSTAB_HEADER = "# a comment that stays\n"
OTHER_LINE = "UUID=00000000-0000-4000-8000-000000000009 /elsewhere apfs rw,nobrowse\n"


def step_2_undo_line(folder: Path) -> str:
    """Step 2's recovery line: a comment, since no command can undo it while it matters.

    While the folder's fstab line exists, every mount of the volume is nobrowse, which
    Time Machine skips with or without an exclusion, and on which ``addexclusion -v``
    adds nothing; so the line explains instead of printing a command that does nothing.
    """
    return (
        f"# step 2 needs no undo: while the fstab line mounts {folder} nobrowse, Time "
        "Machine skips the volume without an exclusion of its own, and addexclusion -v "
        "adds nothing on a nobrowse mount"
    )


class TeardownFixture(unittest.TestCase):
    """A mounted, configured folder and a scripted host; no test methods of its own.

    ``fresh()`` builds the whole fixture in a new temporary directory, so a loop of
    subtests can start each case from a clean one.
    """

    def setUp(self) -> None:
        self.fresh()

    def fresh(self) -> None:
        """A new temporary directory holding a mounted folder, its data and its fstab."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.folder = self.tmp / "work"
        self.store = self.tmp / "volume-store"
        self.restore = self.tmp / f".work.snapfree-restore-{STAMP}"
        self.hidden = self.tmp / f"work.snapfree-hidden-{STAMP}"
        self.fstab = self.tmp / "fstab"
        self._write_data()
        self.original = tree_stats(self.folder)
        line = sf_fstab.render_line(VOLUME_UUID, self.folder)
        self.fstab.write_bytes(f"{FSTAB_HEADER}{line}\n{OTHER_LINE}".encode())
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.mount(self.folder)
        self.fake.respond_info(
            self.folder,
            mount_point=str(self.folder),
            volume_uuid=VOLUME_UUID,
            name="snapfree-work",
            device=VOLUME_DEVICE,
        )
        self.fake.respond_info(self.tmp, mount_point="/", volume_uuid=PARENT_UUID)
        self.out = io.StringIO()

    def _write_data(self) -> None:
        (self.folder / "sub").mkdir(parents=True)
        (self.folder / "a.txt").write_bytes(b"alpha")
        (self.folder / "sub" / "b.txt").write_bytes(b"beta beta")
        (self.folder / "link").symlink_to("a.txt")

    def opts(self, *, apply: bool = False) -> Options:
        """Teardown's options for the fixture folder."""
        return Options(
            mode="teardown",
            path=self.folder,
            quota=None,
            name=None,
            fstab=self.fstab,
            warn_percent=90,
            finish=False,
            apply=apply,
        )

    def teardown_run(self, *, apply: bool = False) -> int:
        """Run the mode under test against the fake host."""
        return sf_teardown.run_teardown(self.opts(apply=apply), self.fake, self.out)

    # -- the scripted host's answers for an applied run --------------------------------

    def _ditto(self, argv: Tuple[str, ...]) -> None:
        """``ditto SRC DST`` for one entry: a folder copied whole, a file as a file."""
        source, target = (Path(item.rstrip("/")) for item in argv[1:])
        for path in (source, target):
            self.assertIn(self.tmp, path.parents)
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True)
        else:
            shutil.copy2(source, target)

    def as_macos_made(self) -> None:
        """Make validation read the volume-private entries as root-owned, as macOS's.

        The suites never run as root, so the check is handed an ``lstat`` that reports
        root as the owner; everything else about each entry is read for real.
        """
        checker = functools.partial(
            sf_walk.check_private_entries, lstat=fake_host.root_owned_lstat
        )
        patcher = mock.patch.object(sf_teardown, "check_private_entries", checker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_volume_private(self) -> None:
        """Give the volume root a ``.fseventsd`` the user cannot list, as macOS does.

        Mode 000 stands in for its real drwx------ root ownership, and validation is
        made to read it as root's. The unmount moves it with the volume's data, so the
        cleanup unlocks it wherever it ended up.
        """
        self.as_macos_made()
        locked = self.folder / ".fseventsd"
        locked.mkdir()
        (locked / "0000000000000001").write_bytes(b"journal")
        locked.chmod(0)

        def unlock() -> None:
            for holder in (self.folder, self.store, self.restore):
                candidate = holder / ".fseventsd"
                if candidate.is_dir():
                    candidate.chmod(0o700)

        self.addCleanup(unlock)

    def _unmount(self, late: Sequence[str]) -> fake_host.Effect:
        """An unmount that takes the volume's data away, uncovering the bare folder.

        *late* names files left in the uncovered folder, as if written while the volume
        was not mounted.
        """

        def effect(_argv: Tuple[str, ...]) -> None:
            # os.replace, not a rename: a test refuses os.rename to fail step 5.
            os.replace(self.folder, self.store)
            self.folder.mkdir()
            for name in late:
                (self.folder / name).write_bytes(b"late")
            self.fake.unmount(self.folder)

        return effect

    def script_apply(self, *, late: Sequence[str] = ()) -> None:
        """Answer every command an applied teardown runs, each succeeding."""
        folder = str(self.folder)
        self.fake.respond(["ditto"], prefix=True, effect=self._ditto)
        self.fake.respond(["sudo", "tmutil", "removeexclusion", "-v", folder])
        self.fake.respond(
            sf_macos.isexcluded_argv(self.folder), effect=self._isexcluded
        )
        tm_prefs.respond_excluded_uuids(self.fake, [STALE_UUID])
        self.fake.respond(
            ["sudo", "diskutil", "unmount", folder], effect=self._unmount(late)
        )
        self.fake.emulate_file_commands()
        self.fake.respond(["sudo", "diskutil", "apfs", "deleteVolume", VOLUME_UUID])
        self.fake.respond_apfs_list([self.container(with_volume=False)])

    def _isexcluded(self, argv: Tuple[str, ...]) -> RunResult:
        """``tmutil isexcluded``, as macOS answers it: excluded while mounted nobrowse.

        Time Machine treats a nobrowse volume as excluded whether or not anything
        excluded it, so the folder reads excluded until it is a plain folder again.
        """
        mounted = sf_fstab.is_mount_point(self.folder, self.fake.device_of)
        plist = fake_host.isexcluded_plist(self.folder, excluded=mounted)
        return RunResult(argv, 0, plist, b"")

    def container(self, *, with_volume: bool) -> "dict[str, object]":
        """The fake container, holding the parent volume and maybe the folder's."""
        volumes = [fake_host.volume_entry(uuid=PARENT_UUID, device=PARENT_DEVICE)]
        if with_volume:
            volumes.append(
                fake_host.volume_entry(uuid=VOLUME_UUID, device=VOLUME_DEVICE)
            )
        return fake_host.container_entry(volumes=volumes)

    def info_calls(self) -> "list[Tuple[str, ...]]":
        """The read-only commands validation runs: the folder's and parent's volume."""
        return [*self.fake.volume_reads(self.folder), *self.fake.volume_reads(self.tmp)]


class DryRunTest(TeardownFixture):
    def test_lists_every_step_in_order(self) -> None:
        self.assertEqual(self.teardown_run(), EXIT_OK)
        folder, restore, fstab = self.folder, self.restore, self.fstab
        mode = stat.S_IMODE(os.stat(folder).st_mode)
        private = ", ".join(sorted(VOLUME_PRIVATE))
        rewrite = " && ".join(sf_fstab.describe_rewrite(fstab, backup=True))
        expected_steps = [
            f"  1. copy each entry at the top of {folder} into {restore}, all but "
            f"the volume's own {private} and this tool's {NEVER_INDEX} (as listed "
            "when this step prints):",
            f"       mkdir -m {mode:04o} {restore}",
            f"       ditto {folder}/a.txt {restore}/a.txt",
            f"       ln -s -- a.txt {restore}/link",
            f"       ditto {folder}/sub {restore}/sub",
            f"  2. sudo tmutil removeexclusion -v {folder}",
            f"  3. sudo diskutil unmount {folder}",
            f"  4. rmdir {folder} (or move it to {self.hidden} if not empty)",
            f"  5. mv {restore} {folder}",
            f"  6. remove the fstab line for {folder}: {rewrite}",
            f"  7. sudo diskutil apfs deleteVolume {VOLUME_UUID}",
            f"  8. tmutil isexcluded -X {folder}",
        ]
        lines = self.out.getvalue().splitlines()
        self.assertIn(VOLUME_UUID, lines[0])
        self.assertIn(str(folder), lines[0])
        self.assertTrue(lines[1].startswith(f"Quit anything that writes to {folder}"))
        self.assertEqual(lines[2:14], expected_steps)
        self.assertIn("Dry run: nothing was changed", lines[14])
        self.assertEqual(len(lines), 15)

    def test_runs_only_the_read_only_commands(self) -> None:
        fstab_before = self.fstab.read_text(encoding="utf-8")
        self.teardown_run()
        self.assertEqual(self.fake.calls, self.info_calls())
        self.assertTrue(sf_fstab.is_mount_point(self.folder, self.fake.device_of))
        self.assertFalse(self.restore.exists())
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), fstab_before)
        self.assertNotIn(
            sf_teardown.DONE_NOTE.format(path=self.folder), self.out.getvalue()
        )

    def test_data_that_fits_exactly_with_the_margin_is_allowed(self) -> None:
        self.fake.respond_info(
            self.tmp,
            once=True,
            mount_point="/",
            volume_uuid=PARENT_UUID,
            container_free=self.original.bytes + SPACE_MARGIN,
        )
        self.assertEqual(self.teardown_run(), EXIT_OK)

    def test_an_uppercase_fstab_uuid_names_the_same_volume(self) -> None:
        line = sf_fstab.render_line(VOLUME_UUID.upper(), self.folder)
        self.fstab.write_bytes(f"{line}\n".encode())
        self.assertEqual(self.teardown_run(), EXIT_OK)


class ValidationTest(TeardownFixture):
    def _refused(self, *needles: str) -> None:
        with self.assertRaises(ToolError) as caught:
            self.teardown_run(apply=True)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        # A refusal changes nothing: only read-only commands ever ran.
        for argv in self.fake.calls:
            self.assertIn(argv, self.info_calls())

    def test_no_fstab_line(self) -> None:
        self.fstab.write_bytes((FSTAB_HEADER + OTHER_LINE).encode())
        self._refused("no line", str(self.folder))

    def test_no_fstab_at_all(self) -> None:
        self.fstab.unlink()
        self._refused("no line", str(self.folder))

    def test_a_folder_that_does_not_exist(self) -> None:
        shutil.rmtree(self.folder)
        self._refused("not a folder", str(self.folder))

    def test_not_mounted(self) -> None:
        self.fake.unmount(self.folder)
        self._refused("not mounted", str(self.folder))

    def test_mounted_volume_is_not_the_fstab_one(self) -> None:
        self.fake.respond_info(
            self.folder,
            once=True,
            mount_point=str(self.folder),
            volume_uuid=fake_host.FAKE_UUID_3,
        )
        self._refused(fake_host.FAKE_UUID_3, VOLUME_UUID)

    def test_mounted_volume_is_not_apfs(self) -> None:
        self.fake.respond_info(
            self.folder, once=True, mount_point=str(self.folder), filesystem="hfs"
        )
        self._refused("not APFS")

    def test_a_retired_volume_is_pending(self) -> None:
        marker = sf_fstab.render_marker(fake_host.FAKE_UUID_3, self.folder)
        before = self.fstab.read_bytes()
        self.fstab.write_bytes(before + f"{marker}\n".encode())
        self._refused(
            f"run {sf_commands.finish_command(self.folder, self.fstab)} first"
        )

    def test_the_parent_volume_is_not_apfs(self) -> None:
        self.fake.respond_info(self.tmp, once=True, mount_point="/", filesystem="hfs")
        self._refused("not APFS", str(self.tmp))

    def test_not_enough_room_beside_the_folder(self) -> None:
        self.fake.respond_info(
            self.tmp,
            once=True,
            mount_point="/",
            volume_uuid=PARENT_UUID,
            container_free=self.original.bytes + SPACE_MARGIN - 1,
        )
        self._refused("free", "margin")

    def test_the_restore_path_is_taken(self) -> None:
        self.restore.mkdir()
        self._refused(str(self.restore), "already exists")

    def test_a_user_made_trashes_folder_is_refused(self) -> None:
        # The copy leaves the name out and the last step deletes the volume, so a
        # folder the user made under that name would be lost with it.
        (self.folder / ".Trashes").mkdir()
        (self.folder / ".Trashes" / "keep.txt").write_bytes(b"user data")
        self._refused(
            str(self.folder / ".Trashes"), "not the macOS-managed folder of that name"
        )

    def test_a_trashes_that_is_a_plain_file_is_refused(self) -> None:
        self.as_macos_made()
        (self.folder / ".Trashes").write_bytes(b"user data")
        self._refused(str(self.folder / ".Trashes"), "move it aside or rename it")


class PlainFolderConfirmTest(TeardownFixture):
    def test_a_mount_point_is_not_a_plain_folder(self) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_teardown.confirm_plain_folder(self.fake, self.folder)
        self.assertIn("still a mount point", str(caught.exception))

    def test_a_missing_folder_is_not_a_plain_folder(self) -> None:
        with self.assertRaises(ToolError):
            sf_teardown.confirm_plain_folder(self.fake, self.tmp / "absent")

    def test_an_unmounted_folder_is(self) -> None:
        self.fake.unmount(self.folder)
        sf_teardown.confirm_plain_folder(self.fake, self.folder)


class CommentRecoveryTest(TeardownFixture):
    """A folder named with a newline cannot break a recovery comment into a command."""

    def test_each_comment_recovery_stays_one_line(self) -> None:
        plan = sf_teardown.validate(self.opts(), self.fake)
        odd = self.tmp / "x\nrm -rf /tmp/y ;"
        plan = dataclasses.replace(plan, path=odd, restore=self.tmp / ".r\nid")
        steps = sf_teardown.build_steps(self.fake, plan, io.StringIO())
        comments = [
            line
            for line in (step.recovery_line() for step in steps)
            if line is not None and line.startswith("#")
        ]
        self.assertEqual(len(comments), 2)
        for line in comments:
            self.assertNotIn("\n", line)


class DispatchTest(TeardownFixture):
    def test_the_entry_point_dispatches_teardown_here(self) -> None:
        self.assertIs(cli.MODES["teardown"], sf_teardown.run_teardown)

    def test_a_dry_run_through_the_command_line(self) -> None:
        argv = ["--mode", "teardown", "--path", str(self.folder)]
        out = io.StringIO()
        with contextlib.redirect_stdout(out):
            code = cli.main([*argv, "--fstab", str(self.fstab)], host=self.fake)
        self.assertEqual(code, EXIT_OK)
        self.assertIn("deleteVolume", out.getvalue())
