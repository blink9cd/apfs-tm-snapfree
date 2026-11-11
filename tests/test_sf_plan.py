"""Tests for the step list every mode prints as a dry run and performs with --apply."""

import io
import tempfile
import unittest
from pathlib import Path
from typing import List

import sf_plan
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from tests import fake_host

UUID = fake_host.FAKE_UUID_2


def _noop() -> None:
    """A step action that does nothing."""


class CommandStepTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.fake = fake_host.FakeHost(Path(holder.name))

    def test_display_is_the_copy_pasteable_command(self) -> None:
        step = sf_plan.command_step(
            self.fake, ["sudo", "mdutil", "-i", "off", "/a b"], 120.0, recover="undo it"
        )
        self.assertEqual(step.shown(), "sudo mdutil -i off '/a b'")
        self.assertEqual(step.recover, "undo it")
        self.assertEqual(self.fake.calls, [], "building a step runs nothing")

    def test_action_runs_the_command_with_its_timeout_then_confirms(self) -> None:
        order: list[str] = []
        self.fake.respond(["true"], effect=lambda argv: order.append("ran"))
        step = sf_plan.command_step(
            self.fake, ["true"], 9.0, confirm=lambda: order.append("confirmed")
        )
        step.action()
        self.assertEqual(order, ["ran", "confirmed"])
        self.assertEqual(self.fake.timeouts, [9.0])
        self.assertIsNone(step.recover)

    def test_without_a_confirm_the_command_alone_is_the_step(self) -> None:
        self.fake.respond(["true"])
        sf_plan.command_step(self.fake, ["true"], 1.0).action()
        self.assertEqual(self.fake.calls, [("true",)])

    def test_a_failing_command_raises_and_skips_the_confirm(self) -> None:
        confirmed: list[bool] = []
        self.fake.respond(["false"], returncode=1)
        step = sf_plan.command_step(
            self.fake, ["false"], 1.0, confirm=lambda: confirmed.append(True)
        )
        with self.assertRaises(ToolError):
            step.action()
        self.assertEqual(confirmed, [])


class ConfirmFailedTest(CommandStepTest):
    """A command that ran but did not take still needs its own undo."""

    def test_a_failed_confirm_is_a_confirm_failed_tool_error(self) -> None:
        self.fake.respond(["true"])
        step = sf_plan.command_step(self.fake, ["true"], 1.0, confirm=_raise_tool_error)
        with self.assertRaises(sf_plan.ConfirmFailed) as caught:
            step.action()
        self.assertIsInstance(caught.exception, ToolError)
        self.assertEqual(str(caught.exception), "it failed")

    def test_an_os_error_in_a_confirm_is_one_too(self) -> None:
        def stat_fails() -> None:
            raise PermissionError("denied")

        with self.assertRaises(sf_plan.ConfirmFailed) as caught:
            sf_plan.confirmed(stat_fails)
        self.assertIn("denied", str(caught.exception))

    def test_a_passing_confirm_raises_nothing(self) -> None:
        sf_plan.confirmed(_noop)

    def test_the_step_that_ran_prints_its_own_recovery_first(self) -> None:
        self.fake.respond(["true"])
        steps = [
            sf_plan.Step("one", _noop, "undo one"),
            sf_plan.Step("two", _noop),
            sf_plan.command_step(
                self.fake,
                ["true"],
                1.0,
                recover="undo three",
                confirm=_raise_tool_error,
            ),
        ]
        out = io.StringIO()
        self.assertEqual(sf_plan.run_plan(steps, apply=True, out=out), EXIT_FAIL)
        self.assertIn(
            "FAILED at step 3: it failed\n"
            "Recovery, most recent first:\n"
            "  undo three  # step 3's command ran\n"
            "  undo one\n",
            out.getvalue(),
        )

    def test_a_step_that_ran_with_no_recovery_adds_no_line(self) -> None:
        self.fake.respond(["true"])
        step = sf_plan.command_step(self.fake, ["true"], 1.0, confirm=_raise_tool_error)
        out = io.StringIO()
        sf_plan.run_plan([step], apply=True, out=out)
        self.assertIn("  (no completed step needs undoing)\n", out.getvalue())

    def test_a_failed_command_prints_no_recovery_of_its_own(self) -> None:
        self.fake.respond(["false"], returncode=1)
        step = sf_plan.command_step(self.fake, ["false"], 1.0, recover="undo it")
        out = io.StringIO()
        sf_plan.run_plan([step], apply=True, out=out)
        self.assertNotIn("undo it", out.getvalue())


class FinalCleanupTest(CommandStepTest):
    """A last step that only tidies up: the steps before it need no undo."""

    def _steps(self) -> List[sf_plan.Step]:
        return [
            sf_plan.Step("one", _noop, "undo one"),
            sf_plan.command_step(
                self.fake, ["rm-volume"], 1.0, recover="rm-volume", final_cleanup=True
            ),
        ]

    def _failed_output(self) -> str:
        out = io.StringIO()
        self.assertEqual(sf_plan.run_plan(self._steps(), True, out), EXIT_FAIL)
        return out.getvalue()

    def test_a_failed_final_cleanup_offers_only_its_retry(self) -> None:
        self.fake.respond(["rm-volume"], returncode=1)
        text = self._failed_output()
        self.assertIn(
            "FAILED at step 2: rm-volume exited 1\n"
            "Recovery: every step before this one completed and needs no undo; "
            "retry this one:\n"
            "  rm-volume\n",
            text,
        )
        self.assertNotIn("undo one", text)

    def test_one_whose_command_ran_offers_the_same_retry(self) -> None:
        self.fake.respond(["rm-volume"])
        steps = self._steps()
        steps[1] = sf_plan.command_step(
            self.fake,
            ["rm-volume"],
            1.0,
            recover="rm-volume",
            confirm=_raise_tool_error,
            final_cleanup=True,
        )
        out = io.StringIO()
        sf_plan.run_plan(steps, True, out)
        self.assertIn("retry this one:\n  rm-volume\n", out.getvalue())
        self.assertNotIn("undo one", out.getvalue())

    def test_one_with_no_recovery_says_there_is_nothing_to_retry(self) -> None:
        step = sf_plan.Step("tidy", _raise_tool_error, final_cleanup=True)
        out = io.StringIO()
        sf_plan.run_plan([step], True, out)
        self.assertIn("retry this one:\n  (nothing to retry)\n", out.getvalue())

    def test_the_flag_is_off_by_default(self) -> None:
        self.assertFalse(sf_plan.Step("x", _noop).final_cleanup)


class CommandHintTest(CommandStepTest):
    def test_a_hint_follows_a_failed_command(self) -> None:
        self.fake.respond(["umount"], returncode=1, stderr=b"busy")
        step = sf_plan.command_step(self.fake, ["umount"], 1.0, hint="quit it first")
        with self.assertRaises(ToolError) as caught:
            step.action()
        self.assertEqual(str(caught.exception), "umount exited 1: busy\nquit it first")

    def test_a_hint_is_not_added_to_a_failed_confirm(self) -> None:
        self.fake.respond(["umount"])
        step = sf_plan.command_step(
            self.fake, ["umount"], 1.0, confirm=_raise_tool_error, hint="quit it"
        )
        with self.assertRaises(sf_plan.ConfirmFailed) as caught:
            step.action()
        self.assertEqual(str(caught.exception), "it failed")


class RunPlanTest(unittest.TestCase):
    def setUp(self) -> None:
        self.out = io.StringIO()
        self.ran: list[str] = []

    def _step(self, name: str, recover: "str | None" = None) -> sf_plan.Step:
        return sf_plan.Step(name, lambda: self.ran.append(name), recover)

    def _failing(self, error: Exception) -> sf_plan.Step:
        def fail() -> None:
            raise error

        return sf_plan.Step("explode", fail, "never printed")

    def test_dry_run_numbers_every_step_and_runs_none(self) -> None:
        steps = [self._step("first"), self._step("second")]
        self.assertEqual(sf_plan.run_plan(steps, apply=False, out=self.out), EXIT_OK)
        self.assertEqual(self.ran, [])
        self.assertEqual(
            self.out.getvalue(),
            "  1. first\n  2. second\n"
            "Dry run: nothing was changed. Re-run with --apply to perform these steps.\n",
        )

    def test_apply_runs_every_step_in_order(self) -> None:
        steps = [self._step("first"), self._step("second")]
        self.assertEqual(sf_plan.run_plan(steps, apply=True, out=self.out), EXIT_OK)
        self.assertEqual(self.ran, ["first", "second"])
        self.assertEqual(
            self.out.getvalue(),
            "  1. first\n  2. second\nDone: all 2 steps completed.\n",
        )

    def test_a_failure_stops_and_prints_recovery_most_recent_first(self) -> None:
        steps = [
            self._step("one", "undo one"),
            self._step("two"),
            self._step("three", "undo three"),
            self._failing(ToolError("diskutil said no")),
            self._step("never"),
        ]
        self.assertEqual(sf_plan.run_plan(steps, apply=True, out=self.out), EXIT_FAIL)
        self.assertEqual(self.ran, ["one", "two", "three"])
        text = self.out.getvalue()
        self.assertIn("FAILED at step 4: diskutil said no\n", text)
        self.assertIn("Recovery, most recent first:\n  undo three\n  undo one\n", text)
        self.assertNotIn("never printed", text)
        self.assertNotIn("never\n", text.split("FAILED")[0])
        self.assertIn("Nothing was undone automatically", text)

    def test_an_os_error_is_caught_the_same_way(self) -> None:
        steps = [self._failing(PermissionError("denied"))]
        self.assertEqual(sf_plan.run_plan(steps, apply=True, out=self.out), EXIT_FAIL)
        text = self.out.getvalue()
        self.assertIn("FAILED at step 1: denied", text)
        self.assertIn("  (no completed step needs undoing)\n", text)

    def test_other_exceptions_are_not_swallowed(self) -> None:
        with self.assertRaises(KeyError):
            sf_plan.run_plan([self._failing(KeyError("bug"))], apply=True, out=self.out)

    def test_step_defaults_to_no_recovery(self) -> None:
        self.assertIsNone(sf_plan.Step("x", _noop).recover)


class SlotTest(unittest.TestCase):
    def test_unset_slot_shows_its_placeholder_and_refuses_a_read(self) -> None:
        slot = sf_plan.Slot("<new-volume-uuid>")
        self.assertFalse(slot.is_set)
        self.assertEqual(slot.text(), "<new-volume-uuid>")
        with self.assertRaises(ToolError) as caught:
            slot.get()
        self.assertIn("<new-volume-uuid>", str(caught.exception))

    def test_a_set_slot_reads_its_value(self) -> None:
        slot = sf_plan.Slot("<x>")
        slot.set(UUID)
        self.assertTrue(slot.is_set)
        self.assertEqual((slot.get(), slot.text()), (UUID, UUID))

    def test_render_quotes_strings_and_values_but_not_placeholders(self) -> None:
        slot = sf_plan.Slot("<new-volume-uuid>")
        argv: List[sf_plan.Arg] = ["sudo", "diskutil", "mount", "-mountPoint", "/a b"]
        argv.append(slot)
        self.assertEqual(
            sf_plan.render(argv),
            "sudo diskutil mount -mountPoint '/a b' <new-volume-uuid>",
        )
        slot.set("x y")
        self.assertEqual(
            sf_plan.render(argv), "sudo diskutil mount -mountPoint '/a b' 'x y'"
        )

    def test_resolve_reads_every_slot(self) -> None:
        slot = sf_plan.Slot("<u>")
        with self.assertRaises(ToolError):
            sf_plan.resolve(["a", slot])
        slot.set(UUID)
        self.assertEqual(sf_plan.resolve(["a", slot]), ["a", UUID])


class StepRenderingTest(unittest.TestCase):
    def test_display_may_be_a_string_or_slot_bearing_argv(self) -> None:
        slot = sf_plan.Slot("<u>")
        self.assertEqual(sf_plan.Step("plain", _noop).shown(), "plain")
        step = sf_plan.Step(["rm", slot], _noop)
        self.assertEqual(step.shown(), "rm <u>")
        slot.set(UUID)
        self.assertEqual(step.shown(), f"rm {UUID}")

    def test_display_may_be_worked_out_each_time_it_prints(self) -> None:
        seen = ["first"]
        step = sf_plan.Step(lambda: seen[-1], _noop)
        self.assertEqual(step.shown(), "first")
        seen.append("second")
        self.assertEqual(step.shown(), "second")

    def test_recover_forms(self) -> None:
        slot = sf_plan.Slot("<u>")
        self.assertIsNone(sf_plan.Step("x", _noop).recovery_line())
        self.assertEqual(sf_plan.Step("x", _noop, "undo").recovery_line(), "undo")
        argv_step = sf_plan.Step("x", _noop, ["sudo", "rm", slot])
        self.assertEqual(argv_step.recovery_line(), "sudo rm <u>")
        slot.set(UUID)
        self.assertEqual(argv_step.recovery_line(), f"sudo rm {UUID}")
        lazy = sf_plan.Step("x", _noop, lambda: f"undo {slot.get()}")
        self.assertEqual(lazy.recovery_line(), f"undo {UUID}")
        self.assertIsNone(sf_plan.Step("x", _noop, lambda: None).recovery_line())

    def test_a_callable_recover_that_fails_never_breaks_the_line(self) -> None:
        unset = sf_plan.Slot("<u>")
        step = sf_plan.Step("x", _noop, lambda: f"undo {unset.get()}")
        line = step.recovery_line()
        assert line is not None
        self.assertTrue(line.startswith("# a recovery command could not be worked out"))
        self.assertIn("<u>", line)

    def test_a_callable_recover_that_cannot_read_the_disk_never_breaks_the_line(
        self,
    ) -> None:
        def unreadable() -> str:
            raise FileNotFoundError(2, "No such file or directory", "/w")

        line = sf_plan.Step("x", _noop, unreadable).recovery_line()
        assert line is not None
        self.assertTrue(line.startswith("# a recovery command could not be worked out"))
        self.assertIn("No such file or directory", line)

    def test_a_path_in_the_failure_note_cannot_end_its_line(self) -> None:
        # An OSError shows its filename by repr, already escaped; a ToolError does not.
        def unreadable() -> str:
            raise ToolError("cannot read /w\nrm -rf /y")

        line = sf_plan.Step("x", _noop, unreadable).recovery_line()
        assert line is not None
        self.assertNotIn("\n", line)
        self.assertTrue(line.startswith("# a recovery command could not be worked out"))


class SlotPlanTest(unittest.TestCase):
    """The setup shape: addVolume finds its UUID, and later steps use it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.fake = fake_host.FakeHost(Path(holder.name))
        self.out = io.StringIO()
        self.uuid = sf_plan.Slot("<new-volume-uuid>")
        self.fake.respond(["sudo", "diskutil", "apfs", "addVolume"], prefix=True)
        self.fake.respond(["sudo", "diskutil", "mount"], prefix=True)

    def _steps(self) -> List[sf_plan.Step]:
        def found() -> None:
            # What setup's confirm does: read the volume back from apfs list.
            self.uuid.set(UUID)

        add = sf_plan.command_step(
            self.fake,
            ["sudo", "diskutil", "apfs", "addVolume", "disk9", "APFS", "v"],
            1.0,
            recover=["sudo", "diskutil", "apfs", "deleteVolume", self.uuid],
            confirm=found,
        )
        mount = sf_plan.command_step(
            self.fake,
            ["sudo", "diskutil", "mount", "-mountPoint", "/w", self.uuid],
            1.0,
            recover=["sudo", "diskutil", "unmount", "/w"],
        )
        return [add, mount]

    def test_dry_run_shows_the_placeholder(self) -> None:
        sf_plan.run_plan(self._steps(), apply=False, out=self.out)
        self.assertIn(
            "  2. sudo diskutil mount -mountPoint /w <new-volume-uuid>\n",
            self.out.getvalue(),
        )
        self.assertEqual(self.fake.calls, [])

    def test_apply_runs_and_shows_the_real_uuid(self) -> None:
        self.assertEqual(
            sf_plan.run_plan(self._steps(), apply=True, out=self.out), EXIT_OK
        )
        self.assertIn(
            f"  2. sudo diskutil mount -mountPoint /w {UUID}\n", self.out.getvalue()
        )
        self.assertEqual(
            self.fake.calls[1], ("sudo", "diskutil", "mount", "-mountPoint", "/w", UUID)
        )

    def test_a_later_failure_names_the_real_uuid_in_recovery(self) -> None:
        self.fake.respond(
            ["sudo", "diskutil", "mount"], prefix=True, returncode=1, once=True
        )
        self.assertEqual(
            sf_plan.run_plan(self._steps(), apply=True, out=self.out), EXIT_FAIL
        )
        self.assertIn(
            f"Recovery, most recent first:\n  sudo diskutil apfs deleteVolume {UUID}\n",
            self.out.getvalue(),
        )

    def test_an_unset_slot_at_recovery_prints_its_placeholder(self) -> None:
        steps = self._steps()
        never = sf_plan.Step(
            "noop",
            _noop,
            ["sudo", "diskutil", "apfs", "deleteVolume", sf_plan.Slot("<u>")],
        )
        fail = sf_plan.Step("boom", _raise_tool_error)
        self.assertEqual(
            sf_plan.run_plan([never, fail, *steps], apply=True, out=self.out), EXIT_FAIL
        )
        self.assertIn("  sudo diskutil apfs deleteVolume <u>\n", self.out.getvalue())

    def test_a_step_reading_an_unset_slot_fails_cleanly(self) -> None:
        mount = self._steps()[1]
        self.assertEqual(sf_plan.run_plan([mount], apply=True, out=self.out), EXIT_FAIL)
        self.assertIn("<new-volume-uuid>", self.out.getvalue())
        self.assertEqual(self.fake.calls, [])


def _raise_tool_error() -> None:
    """A step action that fails."""
    raise ToolError("it failed")


class CommentTextTest(unittest.TestCase):
    """A value printed inside a ``#`` recovery comment can never end the comment."""

    def test_a_newline_is_escaped_so_the_comment_stays_one_line(self) -> None:
        text = sf_plan.comment_text(Path("/Volumes/x\nrm -rf /tmp/y ;"))
        self.assertEqual(text, "/Volumes/x\\x0arm -rf /tmp/y ;")
        self.assertNotIn("\n", text)

    def test_every_control_character_is_escaped(self) -> None:
        self.assertEqual(
            sf_plan.comment_text("a\rb\tc\x7fd\x00"), "a\\x0db\\x09c\\x7fd\\x00"
        )

    def test_plain_text_is_unchanged_spaces_and_quotes_included(self) -> None:
        self.assertEqual(
            sf_plan.comment_text("/Volumes/it's here"), "/Volumes/it's here"
        )


if __name__ == "__main__":
    unittest.main()
