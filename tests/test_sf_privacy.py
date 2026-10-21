"""Tests for the Full Disk Access preflight, alone and in each mode that needs it.

The probe goes through ``FakeHost.read_probe``, which opens nothing: a test sets the
error macOS would raise, so no case reads the machine's Time Machine preferences or
depends on whether the terminal running the suite holds the grant.
"""

from __future__ import annotations

import errno
import io
import unittest
from pathlib import Path

import sf_privacy
import sf_resize
from sf_errors import EXIT_OK, ToolError
from tests import fake_host
from tests.resize_fixture import ResizeCase
from tests.test_sf_setup import SetupFixture
from tests.test_sf_teardown import TeardownFixture

#: What macOS raises for a read its privacy controls refuse, whatever the file mode.
_DENIED = PermissionError(errno.EPERM, "Operation not permitted")
#: A POSIX refusal, which says nothing about Full Disk Access.
_POSIX_DENIED = PermissionError(errno.EACCES, "Permission denied")
#: What a Mac whose Time Machine was never set up raises: no preferences file yet.
_MISSING = FileNotFoundError(errno.ENOENT, "No such file or directory")
_SETTINGS = "System Settings > Privacy & Security > Full Disk Access"


class _ProbeCase(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = fake_host.FakeHost(Path("."))


class ProbeTest(_ProbeCase):
    def test_a_readable_preferences_file_means_granted(self) -> None:
        self.assertIs(sf_privacy.full_disk_access(self.fake), True)
        self.assertEqual(self.fake.probes, [sf_privacy.TIME_MACHINE_PREFERENCES])

    def test_the_probe_reads_time_machines_preferences(self) -> None:
        self.assertEqual(
            str(sf_privacy.TIME_MACHINE_PREFERENCES),
            "/Library/Preferences/com.apple.TimeMachine.plist",
        )

    def test_a_privacy_refusal_means_not_granted(self) -> None:
        self.fake.probe_error = _DENIED
        self.assertIs(sf_privacy.full_disk_access(self.fake), False)

    def test_a_missing_file_is_unknown(self) -> None:
        self.fake.probe_error = _MISSING
        self.assertIsNone(sf_privacy.full_disk_access(self.fake))

    def test_a_posix_refusal_is_unknown(self) -> None:
        self.fake.probe_error = _POSIX_DENIED
        self.assertIsNone(sf_privacy.full_disk_access(self.fake))


class TerminalAppTest(_ProbeCase):
    def test_apples_terminal_is_named_terminal(self) -> None:
        self.fake.env["TERM_PROGRAM"] = "Apple_Terminal"
        self.assertEqual(sf_privacy.terminal_app(self.fake), "Terminal")

    def test_another_terminal_is_named_as_it_names_itself(self) -> None:
        self.fake.env["TERM_PROGRAM"] = "iTerm.app"
        self.assertEqual(sf_privacy.terminal_app(self.fake), "iTerm.app")

    def test_no_terminal_name_is_described(self) -> None:
        self.assertEqual(
            sf_privacy.terminal_app(self.fake), "the app running this terminal"
        )

    def test_an_empty_terminal_name_is_described(self) -> None:
        self.fake.env["TERM_PROGRAM"] = ""
        self.assertEqual(
            sf_privacy.terminal_app(self.fake), "the app running this terminal"
        )


class CheckTest(_ProbeCase):
    def check(self, *, apply: bool) -> str:
        out = io.StringIO()
        sf_privacy.check_full_disk_access(
            self.fake, "sudo tmutil addexclusion -v", apply=apply, out=out
        )
        return out.getvalue()

    def test_granted_says_nothing(self) -> None:
        self.assertEqual(self.check(apply=True), "")
        self.assertEqual(self.check(apply=False), "")

    def test_unknown_says_nothing(self) -> None:
        self.fake.probe_error = _MISSING
        self.assertEqual(self.check(apply=True), "")

    def test_an_applied_run_without_the_grant_is_refused(self) -> None:
        self.fake.probe_error = _DENIED
        self.fake.env["TERM_PROGRAM"] = "Apple_Terminal"
        with self.assertRaises(ToolError) as caught:
            self.check(apply=True)
        message = str(caught.exception)
        for needle in (
            "Full Disk Access",
            "Terminal",
            _SETTINGS,
            "sudo tmutil addexclusion -v",
            "never changes privacy settings",
        ):
            self.assertIn(needle, message)

    def test_a_dry_run_without_the_grant_warns(self) -> None:
        self.fake.probe_error = _DENIED
        text = self.check(apply=False)
        self.assertTrue(text.startswith("Warning: "), text)
        self.assertIn("the app running this terminal", text)
        self.assertIn(_SETTINGS, text)
        self.assertIn("--apply", text)


class SetupPreflightTest(SetupFixture):
    def test_an_applied_setup_without_the_grant_is_refused_before_step_1(self) -> None:
        self.script_validation()
        self.fake.probe_error = _DENIED
        message = self.refusal(self.options(apply=True))
        self.assertIn("Full Disk Access", message)
        self.assertEqual(self.fake.calls, self.read_only_calls())

    def test_a_dry_run_without_the_grant_warns_and_lists_the_steps(self) -> None:
        self.script_validation()
        self.fake.probe_error = _DENIED
        code, text = self.run_setup(self.options())
        self.assertEqual(code, EXIT_OK)
        self.assertIn("Warning: ", text)
        self.assertIn("  1. sudo diskutil apfs addVolume", text)

    def test_a_granted_setup_probes_once_and_does_not_warn(self) -> None:
        self.script_validation()
        code, text = self.run_setup(self.options())
        self.assertEqual(code, EXIT_OK)
        self.assertNotIn("Warning", text)
        self.assertEqual(self.fake.probes, [sf_privacy.TIME_MACHINE_PREFERENCES])


class ResizePreflightTest(ResizeCase):
    def test_an_applied_resize_without_the_grant_is_refused(self) -> None:
        self.fake.probe_error = _DENIED
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(self.options(apply=True), self.fake, io.StringIO())
        self.assertIn("Full Disk Access", str(caught.exception))
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))

    def test_a_dry_run_resize_without_the_grant_warns(self) -> None:
        self.fake.probe_error = _DENIED
        out = io.StringIO()
        self.assertEqual(sf_resize.run_resize(self.options(), self.fake, out), EXIT_OK)
        self.assertIn("Warning: ", out.getvalue())

    def test_an_applied_finish_without_the_grant_is_refused(self) -> None:
        # Its last step reads Time Machine's exclusions, which needs the grant too.
        self.retire_old()
        self.fake.probe_error = _DENIED
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(
                self.options(finish=True, apply=True), self.fake, io.StringIO()
            )
        self.assertIn(sf_privacy.EXCLUSIONS_READ_COMMAND, str(caught.exception))
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))

    def test_a_dry_run_finish_without_the_grant_warns(self) -> None:
        self.retire_old()
        self.fake.probe_error = _DENIED
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(finish=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        self.assertIn("Warning: ", out.getvalue())
        self.assertEqual(self.fake.probes, [sf_privacy.TIME_MACHINE_PREFERENCES])


class TeardownPreflightTest(TeardownFixture):
    def test_an_applied_teardown_without_the_grant_is_refused(self) -> None:
        self.fake.respond_apfs_list([self.container(with_volume=True)])
        self.fake.probe_error = _DENIED
        with self.assertRaises(ToolError) as caught:
            self.teardown_run(apply=True)
        self.assertIn("tmutil removeexclusion -v", str(caught.exception))
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))

    def test_a_dry_run_teardown_without_the_grant_warns(self) -> None:
        self.fake.probe_error = _DENIED
        self.assertEqual(self.teardown_run(), EXIT_OK)
        self.assertIn("Warning: ", self.out.getvalue())


if __name__ == "__main__":
    unittest.main()
