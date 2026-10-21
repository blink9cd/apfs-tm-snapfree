"""Tests for resize's failures: each step refused, and each step's confirm not satisfied.

A refused step must stop the run at exit 1, run nothing after it, and print the
recovery of every completed step most recent first, naming the real UUIDs by then
known. A step whose command exits zero but whose effect cannot be read back fails the
same way: an action only dispatched is not done.
"""

from __future__ import annotations

import contextlib
import dataclasses
import io
import shlex
import unittest
from pathlib import Path
from typing import Callable, List, Optional, Tuple
from unittest import mock

import sf_macos
import sf_plan
from sf_fstab import is_mount_point
from sf_host import COMMAND_TIMEOUT, RunResult
import sf_resize
import sf_resize_confirm
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from tests.resize_fixture import NEW, NEW_NAME, OLD, OTHER, ResizeCase

_LISTING = ("diskutil", "apfs", "list", "-plist")
_EXCLUDED_READ = tuple(sf_macos.EXCLUDED_UUIDS_ARGV)
_MV = ("sudo", "mv", "-f")


@dataclasses.dataclass(frozen=True)
class _Failure:
    """One injected failure: the step it hits, and what must be printed and run."""

    name: str
    number: int
    inject: Callable[[contextlib.ExitStack], object]
    recovery: List[str]
    #: The last command that may have run; None means the refused command itself.
    last: Optional[Tuple[str, ...]] = None


class ResizeFailureTest(ResizeCase):
    """Each mutating step refused in turn, via subTest."""

    def _refusing(self, name: str) -> Callable[[contextlib.ExitStack], object]:
        return lambda _stack: self.refuse(self.step_argvs()[name])

    @staticmethod
    def _raising(target: str) -> Callable[[contextlib.ExitStack], object]:
        patcher = mock.patch(target, side_effect=PermissionError("denied"))
        return lambda stack: stack.enter_context(patcher)

    def _refusing_second(self, name: str) -> Callable[[contextlib.ExitStack], object]:
        """Let the remount's run of *name* through, and refuse the one after it."""
        argv = self.step_argvs()[name]
        refused = RunResult(tuple(argv), 1, b"", b"boom")
        return lambda _stack: self.then(argv, lambda _argv: refused)

    def _refusing_with_spotlight_on(self, _stack: contextlib.ExitStack) -> None:
        """Refuse mdutil -i off, its read after saying Spotlight still indexes.

        The step is judged by that read, not the exit code alone: a refusal on a
        volume read "disabled" passes.
        """
        self.refuse(self.step_argvs()["mdutil"])
        self.fake.respond_mdutil(self.temp, "/:\n\tIndexing enabled.\n", once=True)

    def _refuse_fstab(self, _stack: contextlib.ExitStack) -> None:
        self.fake.respond(list(_MV), returncode=1, prefix=True, once=True)

    def _cases(self) -> List[_Failure]:
        temp, path = self.temp, self.path
        delete_new = f"sudo diskutil apfs deleteVolume {NEW}"
        rmdir_temp = f"rmdir {temp}"
        unmount_temp = f"sudo diskutil unmount {temp}"
        remount_old = (
            f"sudo diskutil mount -mountOptions nobrowse -mountPoint {path} {OLD}"
        )
        unmount_path = f"sudo diskutil unmount {path}"
        mounted = [unmount_temp, rmdir_temp, delete_new]
        refusing = self._refusing
        argvs = self.step_argvs()
        unmounted_temp = tuple(argvs["unmount temp"])
        return [
            _Failure("addVolume", 1, refusing("addVolume"), []),
            _Failure(
                "mkdir", 2, self._raising("pathlib.Path.mkdir"), [delete_new], _LISTING
            ),
            _Failure("mount temp", 3, refusing("mount temp"), [rmdir_temp, delete_new]),
            _Failure("chown", 5, refusing("chown"), mounted),
            _Failure("chmod", 6, refusing("chmod"), mounted),
            _Failure("exclude", 7, refusing("exclude"), mounted),
            _Failure("marker", 8, self._raising("os.open"), mounted, _EXCLUDED_READ),
            _Failure(
                "remount unmount", 9, refusing("unmount temp"), mounted, unmounted_temp
            ),
            _Failure(
                "remount mount",
                9,
                refusing("remount temp"),
                # Stuck unmounted: every undo ends in deleteVolume, so the remount
                # prints no mount of its own, and the folder is free to remove.
                [rmdir_temp, delete_new],
                tuple(argvs["remount temp"]),
            ),
            _Failure(
                "mdutil",
                10,
                self._refusing_with_spotlight_on,
                mounted,
                ("mdutil", "-s", str(temp)),
            ),
            _Failure("ditto", 11, refusing("ditto"), mounted),
            _Failure(
                "unmount temp",
                12,
                self._refusing_second("unmount temp"),
                mounted,
                unmounted_temp,
            ),
            _Failure(
                "rmdir",
                13,
                self._raising("pathlib.Path.rmdir"),
                [rmdir_temp, delete_new],
                unmounted_temp,
            ),
            _Failure("unmount path", 14, refusing("unmount path"), [delete_new]),
            _Failure(
                "mount path", 15, refusing("mount path"), [remount_old, delete_new]
            ),
            _Failure(
                "fstab", 16, self._refuse_fstab, [unmount_path, remount_old, delete_new]
            ),
        ]

    def test_each_refused_step_stops_the_run_and_prints_its_recovery(self) -> None:
        for index in range(len(self._cases())):
            self.setUp()  # a fresh folder and machine for each step's failure
            case = self._cases()[index]
            with self.subTest(step=case.name), contextlib.ExitStack() as stack:
                case.inject(stack)
                out = io.StringIO()
                code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
                stack.close()
                text = out.getvalue()
                self.assertEqual(code, EXIT_FAIL, text)
                self.assertIn(f"FAILED at step {case.number}:", text)
                self.assertIn(_recovery_block(case.recovery), text)
                self.assertNotIn(f"  {case.number + 1}. ", text)
                self.assertEqual(self.fake.calls[-1], self._last(case))

    def _last(self, case: _Failure) -> Tuple[str, ...]:
        """The command that must have run last when *case* is injected."""
        if case.last is not None:
            return case.last
        if case.name == "fstab":
            return (*_MV, str(self.fstab) + ".apfs_tm_snapfree.new", str(self.fstab))
        return tuple(self.step_argvs()[case.name])


class ResizeRemountRecoveryTest(ResizeCase):
    """The recovery printed after a failed remount, run in order, undoes the resize."""

    def _run_recovery(self, text: str) -> None:
        """Run each printed recovery line in order against the fake machine.

        ``rmdir`` is the real one, except that it refuses a mount point as macOS's
        does; every other line goes to the fake, which must accept it.
        """
        block = text.split("Recovery, most recent first:\n", 1)[1]
        for line in block.split("Nothing was undone", 1)[0].splitlines():
            argv = shlex.split(line, comments=True)
            if argv[0] == "rmdir":
                folder = Path(argv[1])
                if is_mount_point(folder, self.fake.device_of):
                    self.fail(f"rmdir {folder}: a volume is still mounted there")
                folder.rmdir()
                continue
            result = self.fake.run(argv, COMMAND_TIMEOUT)
            self.assertEqual(result.returncode, 0, line)

    def _delete_new(self, _argv: Tuple[str, ...]) -> None:
        self.volumes[:] = [vol for vol in self.volumes if vol["APFSVolumeUUID"] != NEW]

    def test_the_printed_block_leaves_no_temp_folder_and_no_volume(self) -> None:
        for stuck in ("unmounted", "mounted"):
            self.setUp()  # a fresh folder and machine for each
            with self.subTest(stuck=stuck):
                argvs = self.step_argvs()
                if stuck == "unmounted":
                    self.refuse(argvs["remount temp"])
                else:
                    self.refuse(argvs["unmount temp"])
                delete = ["sudo", "diskutil", "apfs", "deleteVolume", NEW]
                self.fake.respond(delete, effect=self._delete_new)
                out = io.StringIO()
                code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
                self.assertEqual(code, EXIT_FAIL, out.getvalue())
                self.assertIn("FAILED at step 9:", out.getvalue())
                self._run_recovery(out.getvalue())
                self.assertFalse(self.temp.exists())
                uuids = [vol["APFSVolumeUUID"] for vol in self.volumes]
                self.assertNotIn(NEW, uuids)


def _recovery_block(lines: List[str]) -> str:
    """The recovery block run_plan prints for *lines*, most recent first."""
    body = "".join(f"  {line}\n" for line in lines)
    if not lines:
        body = "  (no completed step needs undoing)\n"
    return f"Recovery, most recent first:\n{body}Nothing was undone"


class ResizeConfirmTest(ResizeCase):
    """Each step whose command exits zero but whose effect is not there."""

    def _fails_at(self, number: int, *needles: str) -> str:
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        text = out.getvalue()
        self.assertEqual(code, EXIT_FAIL, text)
        failure = next(line for line in text.splitlines() if "FAILED" in line)
        self.assertTrue(failure.startswith(f"FAILED at step {number}:"), failure)
        for needle in needles:
            self.assertIn(needle, text)
        return text

    def test_an_added_volume_that_does_not_appear(self) -> None:
        self.ignore(self.add_volume_argv())
        self._fails_at(1, NEW_NAME, "found 0")

    def test_an_added_name_that_appears_twice(self) -> None:
        self.volumes.append(dict(self.volumes[0], Name=NEW_NAME, APFSVolumeUUID=OTHER))
        # Validation reads the list before that duplicate could exist; only the
        # confirm's read, after addVolume, may see it.
        self.fake.respond(
            list(_LISTING), effect=self._listing_without_duplicate, once=True
        )
        self._fails_at(1, NEW_NAME, "found 2")

    def _listing_without_duplicate(self, argv: Tuple[str, ...]) -> RunResult:
        saved = list(self.volumes)
        self.volumes[:] = saved[:1]
        try:
            return self._apfs_list(argv)
        finally:
            self.volumes[:] = saved

    def test_a_mount_that_did_not_take(self) -> None:
        self.ignore(self.step_argvs()["mount temp"])
        self._fails_at(3, str(self.temp))

    def test_ownership_still_ignored_after_enabling_it(self) -> None:
        self.new_ownership = False
        self.ignore(self.step_argvs()["enableOwnership"])
        self._fails_at(4, "ownership")

    def test_an_owner_that_did_not_change(self) -> None:
        job = sf_resize.validate_resize(self.options(apply=True), self.fake)
        other = dataclasses.replace(job, uid=job.uid + 1)
        self.fake.respond(["sudo", "chown"], prefix=True)
        out = io.StringIO()
        code = sf_plan.run_plan(sf_resize.plan_resize(other, self.fake), True, out)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 5:", out.getvalue())

    def test_a_mode_that_did_not_change(self) -> None:
        self.ignore(self.step_argvs()["chmod"])
        self._fails_at(6, "750")

    def test_a_remount_unmount_that_did_not_take(self) -> None:
        self.ignore(self.step_argvs()["unmount temp"])
        self._fails_at(9, "still mounted after diskutil unmount")

    def test_a_remount_that_did_not_take(self) -> None:
        self.ignore(self.step_argvs()["remount temp"])
        self._fails_at(9, "is not mounted after diskutil mount")

    def test_spotlight_still_indexing(self) -> None:
        self.fake.respond_mdutil(self.temp, "/:\n\tIndexing enabled.\n", once=True)
        self._fails_at(10, "Spotlight")

    def test_a_failed_mdutil_off_on_a_volume_read_disabled_passes(self) -> None:
        # Seen live on macOS 27: with the marker already keeping Spotlight off,
        # mdutil -i off exited 1, printing only this log line (fake values here).
        stderr = (
            f"2026-01-02 03:04:05.678 mdutil[4242:424242] mdutil disabling Spotlight: "
            f"{self.temp} -> kMDConfigSearchLevelFSSearchOnly\n"
        )
        argv = self.step_argvs()["mdutil"]
        self.fake.respond(argv, returncode=1, stderr=stderr.encode(), once=True)
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())

    def test_a_failed_mdutil_off_on_an_unclear_read_fails(self) -> None:
        self.refuse(self.step_argvs()["mdutil"])
        self.fake.respond_mdutil(self.temp, "/: something new", once=True)
        self._fails_at(10, "exited 1: boom", "something new")

    def test_spotlight_status_that_reads_neither_way_is_let_through(self) -> None:
        self.fake.respond_mdutil(self.temp, "/: something new", once=True)
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())

    def test_an_exclusion_that_did_not_take(self) -> None:
        # The fixture's tmutil isexcluded reads excluded regardless, as a nobrowse
        # volume does on macOS: only Time Machine's own list can show this.
        self.ignore(self.step_argvs()["exclude"])
        self._fails_at(7, f"ExcludedVolumeUUIDs does not list {NEW}")

    def test_the_exclusion_runs_while_the_new_volume_is_mounted_visible(self) -> None:
        # Seen live on macOS 27: addexclusion -v on a nobrowse mount exits 0 and adds
        # nothing, so it must run before the remount that hides the volume (D8).
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        argvs, calls = self.step_argvs(), self.fake.calls
        exclude = calls.index(tuple(argvs["exclude"]))
        self.assertLess(calls.index(tuple(argvs["mount temp"])), exclude)
        self.assertLess(exclude, calls.index(tuple(argvs["remount temp"])))
        self.assertIn(NEW, self.excluded_uuids)

    def test_a_copy_that_does_not_match(self) -> None:
        self.ignore(self.step_argvs()["ditto"])
        # Only a.txt's copy is ignored; the symlink and sub/ still arrive.
        self._fails_at(11, "not in the copy: a.txt (file, 5 bytes)")

    def test_an_unmount_of_the_temporary_mount_that_did_not_take(self) -> None:
        self.then(self.step_argvs()["unmount temp"], lambda _argv: None)
        self._fails_at(12, "still mounted")

    def test_an_unmount_of_the_folder_that_did_not_take(self) -> None:
        self.ignore(self.step_argvs()["unmount path"])
        self._fails_at(14, "still mounted")

    def test_the_wrong_volume_at_the_folder_after_mounting(self) -> None:
        self.ignore(self.step_argvs()["mount path"])
        self._fails_at(15, OTHER, NEW)

    def test_an_fstab_rewrite_that_did_not_take(self) -> None:
        self.ignore(["sudo", "mv", "-f"], prefix=True)
        text = self._fails_at(16, "does not show the change")
        restore = f"sudo cp {self.fstab}.apfs_tm_snapfree.bak {self.fstab}"
        self.assertIn(
            f"Recovery, most recent first:\n  {restore}  # step 16's command ran\n",
            text,
        )


class ResizeLateWriteTest(ResizeCase):
    """A write to the folder after the copy is confirmed, before the folder unmounts."""

    def _write_late(self, argv: Tuple[str, ...]) -> None:
        self._unmount_temp(argv)
        (self.path / "sub" / "late.txt").write_bytes(b"written late")

    def test_a_write_after_the_copy_stops_the_run_before_the_unmount(self) -> None:
        # Unmounting the folder strands whatever reached the old volume after the
        # copy, and --finish later deletes that volume, so step 14 must refuse.
        unmount_temp = self.step_argvs()["unmount temp"]
        self.then(unmount_temp, self._write_late)
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        text = out.getvalue()
        self.assertEqual(code, EXIT_FAIL, text)
        self.assertIn(
            f"FAILED at step 14: {self.path} no longer matches what was copied to "
            f"the new volume: something wrote to {self.path} after the copy "
            "(not in the copy: sub/late.txt (file, 12 bytes)); quit it, then run "
            "the recovery commands below and resize again",
            text,
        )
        self.assertIn(_recovery_block([f"sudo diskutil apfs deleteVolume {NEW}"]), text)
        self.assertNotIn(tuple(self.step_argvs()["unmount path"]), self.fake.calls)
        self.assertEqual(self.fake.calls[-1], tuple(unmount_temp))

    def test_a_re_check_before_the_copy_is_recorded_is_a_plan_defect(self) -> None:
        with self.assertRaisesRegex(ToolError, "before its copy was"):
            sf_resize_confirm.require_unchanged(
                self.path, sf_resize_confirm.CopiedData()
            )


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
