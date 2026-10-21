"""Tests for the two steps that keep Spotlight off a new volume: the marker and the remount.

macOS honors an empty ``.metadata_never_index`` at a volume's root only when it is there
as the volume mounts, so setup and resize create it, then unmount and mount the volume
again. Each case runs on a ``FakeHost`` whose mounts are fake devices on a real folder;
no case reaches diskutil or Spotlight.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from unittest import mock

import sf_spotlight
import sf_unmount
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_fstab import is_mount_point
from sf_host import COMMAND_TIMEOUT, RunResult
from sf_plan import ConfirmFailed, Slot, run_plan
from sf_walk import NEVER_INDEX
from tests import fake_host
from tests.fake_host import FAKE_UUID_1, FAKE_UUID_2

_REFUSED = 1
_DISSENT = b"Volume work on disk9s5 failed to unmount: dissented by PID 4242 (mds)\n"


class _VolumeCase(unittest.TestCase):
    """A folder with a fake volume mounted on it, and that volume's UUID known."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.where = self.tmp / "work"
        self.where.mkdir()
        self.marker = self.where / NEVER_INDEX
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.mount(self.where)
        self.uuid = Slot("<new-volume-uuid>")
        self.uuid.set(FAKE_UUID_2)
        self.unmount = sf_unmount.unmount_argv(self.where)
        self.mount = [str(item) for item in self.mount_argv()]

    def mount_argv(self) -> list[str]:
        """The nobrowse mount of the folder's volume, by its UUID."""
        mount = ["sudo", "diskutil", "mount", "-mountOptions", "nobrowse"]
        return [*mount, "-mountPoint", str(self.where), FAKE_UUID_2]

    def mount_line(self) -> str:
        """That mount as the tool prints it."""
        return " ".join(self.mount_argv())


class MarkerStepTest(_VolumeCase):
    def test_it_creates_an_empty_file_owned_by_the_user(self) -> None:
        step = sf_spotlight.marker_step(self.where)
        step.action()
        found = os.lstat(self.marker)
        self.assertEqual(found.st_size, 0)
        self.assertEqual(found.st_uid, os.stat(self.where).st_uid)
        self.assertIsNone(step.recovery_line())

    def test_its_display_names_the_file_and_why(self) -> None:
        shown = sf_spotlight.marker_step(self.where).shown()
        self.assertTrue(shown.startswith(f"touch {self.marker} "), shown)
        self.assertIn("Spotlight", shown)

    def test_an_empty_marker_already_there_is_kept(self) -> None:
        self.marker.write_bytes(b"")
        sf_spotlight.marker_step(self.where).action()
        self.assertEqual(self.marker.read_bytes(), b"")

    def test_a_marker_with_content_is_refused_never_overwritten(self) -> None:
        self.marker.write_bytes(b"notes")
        with self.assertRaisesRegex(ToolError, "is not the empty file"):
            sf_spotlight.marker_step(self.where).action()
        self.assertEqual(self.marker.read_bytes(), b"notes")

    def test_a_marker_that_does_not_appear_fails_its_confirm(self) -> None:
        real_open = os.open

        def elsewhere(_path: object, _flags: int, _mode: int = 0o777) -> int:
            # An open that "succeeds" without making the file, as a confirm must catch.
            return real_open(os.devnull, os.O_WRONLY)

        with mock.patch.object(sf_spotlight.os, "open", side_effect=elsewhere):
            with self.assertRaisesRegex(ConfirmFailed, "is not there after"):
                sf_spotlight.marker_step(self.where).action()


class RemountStepTest(_VolumeCase):
    def script(self, *, uuid: str = FAKE_UUID_2) -> None:
        """An unmount and a mount that both take, and the volume described after."""
        self.fake.respond(self.unmount, effect=self.fake.unmount_effect(self.where))
        self.fake.respond(self.mount, effect=self.fake.mount_effect(self.where))
        self.fake.respond_info(
            self.where, mount_point=str(self.where), volume_uuid=uuid
        )

    def run_alone(self) -> tuple[int, str]:
        """Run the remount as a one-step plan; its exit code and what it printed."""
        out = io.StringIO()
        step = sf_spotlight.remount_step(self.fake, self.where, self.uuid)
        return run_plan([step], True, out), out.getvalue()

    def test_it_unmounts_then_mounts_nobrowse_and_confirms(self) -> None:
        self.script()
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake.calls[:2], [tuple(self.unmount), tuple(self.mount)])
        self.assertTrue(is_mount_point(self.where, self.fake.device_of))

    def test_its_display_is_the_two_commands_with_the_uuid_once_known(self) -> None:
        unset = Slot("<new-volume-uuid>")
        step = sf_spotlight.remount_step(self.fake, self.where, unset)
        unmount = " ".join(self.unmount)
        shown = step.shown()
        self.assertTrue(shown.startswith(f"{unmount} && "), shown)
        self.assertIn("-mountPoint " + str(self.where) + " <new-volume-uuid>", shown)
        unset.set(FAKE_UUID_2)
        self.assertIn(self.mount_line(), step.shown())

    def test_a_refused_mount_after_the_unmount_prints_how_to_remount(self) -> None:
        self.script()
        self.fake.respond(self.mount, returncode=_REFUSED, stderr=b"no", once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"  {self.mount_line()}  # step 1's command ran", out)
        self.assertFalse(is_mount_point(self.where, self.fake.device_of))

    def test_the_wrong_volume_mounted_fails_its_confirm(self) -> None:
        self.script(uuid=FAKE_UUID_1)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"is volume {FAKE_UUID_1}, not {FAKE_UUID_2}", out)
        # Something is mounted there, so mounting the volume again would not help.
        self.assertIn("(no completed step needs undoing)", out)

    def test_a_mount_that_did_not_take_fails_its_confirm(self) -> None:
        self.script()
        self.fake.respond(self.mount, once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn("is not mounted after diskutil mount", out)
        self.assertIn(f"  {self.mount_line()}  # step 1's command ran", out)

    def test_a_refused_unmount_leaves_it_mounted_with_nothing_to_remount(self) -> None:
        self.script()
        self.fake.respond(self.unmount, returncode=_REFUSED, stderr=b"no", once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn("(no completed step needs undoing)", out)
        self.assertNotIn(tuple(self.mount), self.fake.calls)

    def test_an_unmount_that_did_not_take_stops_before_the_mount(self) -> None:
        self.script()
        self.fake.respond(self.unmount, once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn("is still mounted after diskutil unmount", out)
        self.assertNotIn(tuple(self.mount), self.fake.calls)

    def test_a_busy_unmount_is_waited_out(self) -> None:
        self.script()
        self.fake.respond(self.unmount, returncode=_REFUSED, stderr=_DISSENT, once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake.sleeps, [sf_unmount.UNMOUNT_RETRY_WAITS[0]])

    def test_a_failed_unmount_that_took_anyway_goes_on_to_the_mount(self) -> None:
        self.script()

        def unmounted_but_failed(argv: "tuple[str, ...]") -> RunResult:
            self.fake.unmount(self.where)
            return RunResult(argv, _REFUSED, b"", b"boom")

        self.fake.respond(self.unmount, effect=unmounted_but_failed, once=True)
        self.fake.respond(self.mount, returncode=_REFUSED, stderr=b"no", once=True)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        # Caught between the two, so the recovery names the mount back.
        self.assertIn(f"  {self.mount_line()}  # step 1's command ran", out)

    def test_without_mount_back_a_stopped_remount_prints_no_mount(self) -> None:
        # Resize's every undo ends in deleting the volume, so it asks for none.
        self.script()
        self.fake.respond(self.mount, returncode=_REFUSED, stderr=b"no", once=True)
        step = sf_spotlight.remount_step(
            self.fake, self.where, self.uuid, mount_back=False
        )
        out = io.StringIO()
        self.assertEqual(run_plan([step], True, out), EXIT_FAIL)
        self.assertIn("(no completed step needs undoing)", out.getvalue())

    def test_a_check_before_the_mount_that_fails_prints_the_mount_back(self) -> None:
        self.script()

        def refuse() -> None:
            raise ToolError("not empty")

        step = sf_spotlight.remount_step(
            self.fake, self.where, self.uuid, before_mount=refuse
        )
        out = io.StringIO()
        self.assertEqual(run_plan([step], True, out), EXIT_FAIL)
        self.assertIn("FAILED at step 1: not empty", out.getvalue())
        self.assertIn(f"  {self.mount_line()}  # step 1's command ran", out.getvalue())
        self.assertNotIn(tuple(self.mount), self.fake.calls)

    def test_a_completed_remount_has_no_recovery_even_once_unmounted(self) -> None:
        # Resize unmounts its temporary mount point on purpose later on; the remount
        # that completed earlier must not then ask for the volume to be mounted again.
        self.script()
        step = sf_spotlight.remount_step(self.fake, self.where, self.uuid)
        self.assertIsNone(step.recovery_line())
        step.action()
        self.fake.unmount(self.where)
        self.assertIsNone(step.recovery_line())


#: What ``mdutil -i off`` printed, to stderr, as it exited 1 on a volume the marker had
#: already kept out of Spotlight (the shape seen live; the values are fakes).
_LIVE_STDERR = (
    "2026-01-02 03:04:05.678 mdutil[4242:424242] mdutil disabling Spotlight: "
    "{where} -> kMDConfigSearchLevelFSSearchOnly\n"
)
_DISABLED = "{where}:\n\tIndexing and searching disabled.\n"
_ENABLED = "{where}:\n\tIndexing enabled.\n"
_UNCLEAR = "{where}:\n\tsomething new\n"


class OffStepTest(_VolumeCase):
    """``mdutil -i off``, judged by what ``mdutil -s`` reads after it, not its exit."""

    def script(self, status: str, *, returncode: int = 0) -> None:
        """``mdutil -i off`` exiting *returncode*, then ``mdutil -s`` reading *status*."""
        stderr = _LIVE_STDERR.format(where=self.where) if returncode else ""
        self.fake.respond(
            self.off_argv(), returncode=returncode, stderr=stderr.encode("utf-8")
        )
        self.fake.respond_mdutil(self.where, status.format(where=self.where))

    def off_argv(self) -> list[str]:
        """The command the step runs."""
        return ["sudo", "mdutil", "-i", "off", str(self.where)]

    def run_alone(self) -> tuple[int, str]:
        """Run the step as a one-step plan; its exit code and what it printed."""
        out = io.StringIO()
        step = sf_spotlight.off_step(self.fake, self.where)
        return run_plan([step], True, out), out.getvalue()

    def test_its_display_is_the_command(self) -> None:
        step = sf_spotlight.off_step(self.fake, self.where)
        self.assertEqual(step.shown(), " ".join(self.off_argv()))
        self.assertIsNone(step.recovery_line())

    def test_a_failed_exit_on_a_volume_read_disabled_passes(self) -> None:
        # The live case: the marker had already turned Spotlight off, mdutil -i off
        # exited 1 saying it was disabling it, and mdutil -s read "disabled".
        self.script(_DISABLED, returncode=_REFUSED)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(
            self.fake.calls[-2:],
            [tuple(self.off_argv()), ("mdutil", "-s", str(self.where))],
        )

    def test_a_failed_exit_on_a_volume_read_enabled_names_both(self) -> None:
        self.script(_ENABLED, returncode=_REFUSED)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"Spotlight still indexes {self.where}", out)
        self.assertIn(f"exited {_REFUSED}", out)
        self.assertIn("kMDConfigSearchLevelFSSearchOnly", out)

    def test_a_failed_exit_on_an_unclear_read_fails_with_both_outputs(self) -> None:
        self.script(_UNCLEAR, returncode=_REFUSED)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"exited {_REFUSED}", out)
        self.assertIn("kMDConfigSearchLevelFSSearchOnly", out)
        self.assertIn("something new", out)

    def test_a_clean_exit_on_an_unclear_read_is_let_through(self) -> None:
        # mdutil's prose has no structured form: text it cannot read is left to
        # --mode status to report, when nothing else says the command failed.
        self.script(_UNCLEAR)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_OK, out)

    def test_a_clean_exit_on_a_volume_read_enabled_fails(self) -> None:
        self.script(_ENABLED)
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"Spotlight still indexes {self.where} after mdutil -i off", out)
        self.assertNotIn("exited", out)

    def test_a_failed_exit_prints_its_stdout_too(self) -> None:
        self.fake.respond(self.off_argv(), returncode=_REFUSED, stdout=b"said this")
        self.fake.respond_mdutil(self.where, _UNCLEAR.format(where=self.where))
        code, out = self.run_alone()
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn("said this", out)

    def test_it_runs_both_commands_with_the_per_call_timeout(self) -> None:
        self.script(_DISABLED)
        sf_spotlight.off_step(self.fake, self.where).action()
        self.assertEqual(self.fake.timeouts[-2:], [COMMAND_TIMEOUT, COMMAND_TIMEOUT])


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
