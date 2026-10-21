"""Tests for the unmount that waits out a process holding a fresh volume open.

Spotlight's ``mds`` can dissent from unmounting a volume just made, even with indexing
turned off, and lets go a little later. Each case scripts diskutil's replies on a
``FakeHost``, whose ``sleep`` returns at once and records each wait, so the escalating
schedule is asserted exactly and no case waits or reaches diskutil.
"""

from __future__ import annotations

import io
import os
import tempfile
import unittest
from pathlib import Path
from typing import Callable

import sf_resize
import sf_unmount
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_fstab import is_mount_point
from sf_host import COMMAND_TIMEOUT, RunResult
from sf_plan import Slot, render, run_plan
from tests import fake_host
from tests.resize_fixture import ResizeCase
from tests.test_sf_teardown import TeardownFixture

#: What diskutil prints when Spotlight holds the volume: an obvious fake PID.
_MDS = (
    "PID 4242 (/System/Library/Frameworks/CoreServices.framework/Frameworks/"
    "Metadata.framework/Versions/A/Support/mds)"
)
_DISSENT = f"Volume work on disk9s5 failed to unmount: dissented by {_MDS}\n".encode()
_BUSY = b"Unmount of disk9s5 failed: Resource busy\n"
_REFUSED = 1


class _UnmountCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.where = self.tmp / "work"
        self.where.mkdir()
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.mount(self.where)
        self.argv = ["sudo", "diskutil", "unmount", str(self.where)]

    def succeed(self) -> None:
        """A standing reply that unmounts the folder."""
        self.fake.respond(self.argv, effect=self.fake.unmount_effect(self.where))

    def dissent(self, times: int, *, text: bytes = _DISSENT) -> None:
        """Queue *times* refusals, each saying something holds the volume."""
        for _ in range(times):
            self.fake.respond(self.argv, returncode=_REFUSED, stderr=text, once=True)

    def confirm(self) -> Callable[[], None]:
        def check() -> None:
            if is_mount_point(self.where, self.fake.device_of):
                raise ToolError(f"{self.where} is still mounted")

        return check

    def act(self, *, hint: str = "") -> None:
        step = sf_unmount.unmount_step(
            self.fake,
            self.where,
            recover=["sudo", "diskutil", "mount", str(self.where)],
            confirm=self.confirm(),
            hint=hint or None,
        )
        self.assertEqual(step.shown(), " ".join(self.argv))
        step.action()


class MountArgvTest(unittest.TestCase):
    def test_every_mount_keeps_the_volume_out_of_finder_and_spotlight(self) -> None:
        # nobrowse by default: a visible mount is what Spotlight's mds opens, and the
        # only mount it was ever seen to hold.
        uuid = Slot("<uuid>")
        argv = sf_unmount.mount_argv(Path("/w"), uuid)
        self.assertEqual(
            render(argv),
            "sudo diskutil mount -mountOptions nobrowse -mountPoint /w <uuid>",
        )
        uuid.set("U")
        self.assertEqual(render(argv), "sudo diskutil mount -mountOptions nobrowse "
                         "-mountPoint /w U")  # fmt: skip

    def test_a_first_mount_may_be_visible(self) -> None:
        # Ruling D8: tmutil addexclusion -v is a silent no-op on a nobrowse mount, so
        # a new volume's first mount is visible, for the exclusion to take.
        argv = sf_unmount.mount_argv(Path("/w"), "U", visible=True)
        self.assertEqual(render(argv), "sudo diskutil mount -mountPoint /w U")


class ScheduleTest(unittest.TestCase):
    def test_the_waits_escalate_and_total_about_half_a_minute(self) -> None:
        waits = sf_unmount.UNMOUNT_RETRY_WAITS
        self.assertEqual(waits, (1.0, 2.0, 4.0, 8.0, 16.0))
        self.assertTrue(all(b > a for a, b in zip(waits, waits[1:])))
        self.assertEqual(sum(waits), 31.0)

    def test_the_busy_matchers_are_dissent_and_resource_busy(self) -> None:
        self.assertEqual(
            sf_unmount.UNMOUNT_BUSY_MARKERS, ("dissented by", "Resource busy")
        )


class RetryTest(_UnmountCase):
    def test_a_plain_success_runs_once_and_never_waits(self) -> None:
        self.succeed()
        self.act()
        self.assertEqual(self.fake.calls, [tuple(self.argv)])
        self.assertEqual(self.fake.sleeps, [])
        self.assertEqual(self.fake.timeouts, [COMMAND_TIMEOUT])

    def test_dissent_then_success_on_attempt_n(self) -> None:
        for attempt in range(2, len(sf_unmount.UNMOUNT_RETRY_WAITS) + 2):
            self.setUp()
            with self.subTest(attempt=attempt):
                self.succeed()
                self.dissent(attempt - 1)
                self.act()
                self.assertEqual(self.fake.calls, [tuple(self.argv)] * attempt)
                self.assertEqual(
                    self.fake.sleeps,
                    list(sf_unmount.UNMOUNT_RETRY_WAITS[: attempt - 1]),
                )
                self.assertEqual(self.fake.timeouts, [COMMAND_TIMEOUT] * attempt)

    def test_resource_busy_is_retried_too(self) -> None:
        self.succeed()
        self.dissent(1, text=_BUSY)
        self.act()
        self.assertEqual(self.fake.sleeps, [1.0])

    def test_dissent_on_stdout_is_retried_too(self) -> None:
        self.succeed()
        self.fake.respond(self.argv, returncode=_REFUSED, stdout=_DISSENT, once=True)
        self.act()
        self.assertEqual(len(self.fake.calls), 2)

    def test_dissent_every_time_fails_naming_the_process(self) -> None:
        self.fake.respond(self.argv, returncode=_REFUSED, stderr=_DISSENT)
        with self.assertRaises(ToolError) as caught:
            self.act(hint="the step's own hint")
        message = str(caught.exception)
        tries = len(sf_unmount.UNMOUNT_RETRY_WAITS) + 1
        self.assertEqual(self.fake.calls, [tuple(self.argv)] * tries)
        self.assertEqual(self.fake.sleeps, list(sf_unmount.UNMOUNT_RETRY_WAITS))
        self.assertIn(_MDS, message)
        self.assertIn(f"{tries} tries over 31 s", message)
        self.assertIn("Wait for it to let go, or quit it", message)
        self.assertTrue(message.endswith("\nthe step's own hint"), message)

    def test_resource_busy_every_time_points_at_lsof(self) -> None:
        self.fake.respond(self.argv, returncode=_REFUSED, stderr=_BUSY)
        with self.assertRaises(ToolError) as caught:
            self.act()
        self.assertIn(f"lsof +D {self.where}", str(caught.exception))

    def test_any_other_failure_is_not_retried(self) -> None:
        self.fake.respond(self.argv, returncode=_REFUSED, stderr=b"boom")
        with self.assertRaises(ToolError) as caught:
            self.act(hint="hint")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.sleeps, [])
        self.assertEqual(
            str(caught.exception),
            f"sudo diskutil unmount {self.where} exited 1: boom\nhint",
        )

    def test_a_refusal_the_poll_sees_unmounted_anyway_succeeds(self) -> None:
        def unmounted_but_refused(argv: "tuple[str, ...]") -> RunResult:
            self.fake.unmount(self.where)
            return RunResult(argv, _REFUSED, b"", _DISSENT)

        self.fake.respond(self.argv, effect=unmounted_but_refused, once=True)
        self.act()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.sleeps, [])

    def test_any_other_failure_that_took_anyway_succeeds(self) -> None:
        # The mount state is read after every failure, busy or not: a folder seen
        # unmounted is an unmount that took, whatever diskutil's exit said.
        def unmounted_but_failed(argv: "tuple[str, ...]") -> RunResult:
            self.fake.unmount(self.where)
            return RunResult(argv, _REFUSED, b"", b"boom")

        self.fake.respond(self.argv, effect=unmounted_but_failed, once=True)
        self.act()
        self.assertEqual(len(self.fake.calls), 1)
        self.assertEqual(self.fake.sleeps, [])

    def test_an_unmount_that_exits_zero_without_taking_fails_its_confirm(
        self,
    ) -> None:
        self.fake.respond(self.argv)
        out = io.StringIO()
        step = sf_unmount.unmount_step(
            self.fake, self.where, recover="undo it", confirm=self.confirm()
        )
        self.assertEqual(run_plan([step], True, out), EXIT_FAIL)
        self.assertIn("undo it  # step 1's command ran", out.getvalue())


class ResizeRetryTest(ResizeCase):
    def test_the_remount_and_the_folder_unmount_wait_out_a_dissent(self) -> None:
        for name in ("unmount temp", "unmount path"):
            self.fake.respond(
                self.step_argvs()[name],
                returncode=_REFUSED,
                stderr=_DISSENT,
                once=True,
            )
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        self.assertEqual(self.fake.sleeps, [1.0, 1.0])


class TeardownRetryTest(TeardownFixture):
    def unmount_argv(self) -> "list[str]":
        return ["sudo", "diskutil", "unmount", str(self.folder)]

    def test_step_3_waits_out_a_dissent(self) -> None:
        self.script_apply()
        self.fake.respond(
            self.unmount_argv(), returncode=_REFUSED, stderr=_DISSENT, once=True
        )
        self.assertEqual(self.teardown_run(apply=True), EXIT_OK, self.out.getvalue())
        self.assertEqual(self.fake.sleeps, [1.0])

    def test_step_3_names_the_process_when_it_never_lets_go(self) -> None:
        self.script_apply()
        for _ in range(len(sf_unmount.UNMOUNT_RETRY_WAITS) + 1):
            self.fake.respond(
                self.unmount_argv(), returncode=_REFUSED, stderr=_DISSENT, once=True
            )
        self.assertEqual(self.teardown_run(apply=True), EXIT_FAIL)
        text = self.out.getvalue()
        self.assertIn("FAILED at step 3:", text)
        self.assertIn(_MDS, text)
        self.assertIn(
            f"Recovery, most recent first:\n  sudo tmutil addexclusion -v {self.folder}",
            text,
        )


if __name__ == "__main__":
    unittest.main()
