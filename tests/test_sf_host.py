"""Tests for the host seams: the result type, the checked run, and the real host.

The real host is exercised against this interpreter as its child process and the test's
own temporary directory, never against diskutil, tmutil, mdutil or sudo.
"""

import os
import re
import stat
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest import mock

import sf_host
from sf_errors import ToolError
from tests import fake_host

_ECHO = "import sys; sys.stdout.write('out'); sys.stderr.write('err'); sys.exit(3)"
_IGNORE_TERM = (
    "import signal, time; signal.signal(signal.SIGTERM, signal.SIG_IGN); time.sleep(30)"
)
# The grandchild inherits the pipes and outlives its parent by a few seconds, then
# exits on its own, so no process is left behind for long.
_SPAWN_GRANDCHILD = (
    "import subprocess, sys, time; "
    "subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(4)']); "
    "time.sleep(30)"
)
# Well under the grandchild's lifetime: a call that waited for it would take longer.
_PROMPT = 3.0


class RunResultTest(unittest.TestCase):
    def test_text_views_decode_utf8_and_replace_bad_bytes(self) -> None:
        result = sf_host.RunResult(("x",), 0, "café".encode(), b"\xff!")
        self.assertEqual(result.stdout_text(), "café")
        self.assertEqual(result.stderr_text(), "�!")


class FormatArgvTest(unittest.TestCase):
    def test_quotes_what_a_shell_would_split(self) -> None:
        self.assertEqual(
            sf_host.format_argv(["sudo", "mv", "/a b", "c"]), "sudo mv '/a b' c"
        )


class RunCheckedTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.fake = fake_host.FakeHost(Path(holder.name))

    def test_success_returns_the_result_and_passes_the_timeout(self) -> None:
        self.fake.respond(["true"], stdout=b"fine")
        result = sf_host.run_checked(self.fake, ["true"], 7.0)
        self.assertEqual(result.stdout, b"fine")
        self.assertEqual(self.fake.timeouts, [7.0])

    def test_non_zero_exit_names_the_command_and_its_stderr(self) -> None:
        self.fake.respond(["diskutil", "x y"], returncode=4, stderr=b"  no such disk\n")
        with self.assertRaises(ToolError) as caught:
            sf_host.run_checked(self.fake, ["diskutil", "x y"], 1.0)
        message = str(caught.exception)
        self.assertIn("diskutil 'x y' exited 4", message)
        self.assertIn("no such disk", message)
        self.assertNotIn("Full Disk Access", message)

    def test_empty_stderr_still_reads_cleanly(self) -> None:
        self.fake.respond(["false"], returncode=1)
        with self.assertRaises(ToolError) as caught:
            sf_host.run_checked(self.fake, ["false"], 1.0)
        self.assertEqual(str(caught.exception), "false exited 1")

    def test_a_privacy_denial_adds_the_full_disk_access_hint(self) -> None:
        self.fake.respond(
            ["tmutil", "isexcluded"],
            prefix=True,
            returncode=1,
            stderr=b"tmutil: Operation not permitted",
        )
        with self.assertRaises(ToolError) as caught:
            sf_host.run_checked(self.fake, ["tmutil", "isexcluded", "-X", "/p"], 1.0)
        self.assertIn("Full Disk Access", str(caught.exception))
        self.assertIn("never changes privacy settings", str(caught.exception))


class TimeoutConstantTest(unittest.TestCase):
    def test_command_and_copy_bounds(self) -> None:
        self.assertEqual(sf_host.COMMAND_TIMEOUT, 120.0)
        self.assertEqual(sf_host.COPY_TIMEOUT, 6 * 60 * 60.0)


class RealHostTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.host = sf_host.RealHost(temp_dir=self.tmp)

    def test_run_captures_both_streams_and_the_exit_code(self) -> None:
        result = self.host.run([sys.executable, "-c", _ECHO], 30.0)
        self.assertEqual(
            (result.returncode, result.stdout, result.stderr), (3, b"out", b"err")
        )
        self.assertEqual(result.argv, (sys.executable, "-c", _ECHO))

    def test_run_leaves_stdin_attached_and_never_uses_a_shell(self) -> None:
        process = mock.Mock(returncode=0)
        process.communicate.return_value = (b"", b"")
        with mock.patch.object(
            sf_host.subprocess, "Popen", return_value=process
        ) as popen:
            self.host.run(["x"], 5.0)
        kwargs = popen.call_args.kwargs
        for kept in ("stdin", "shell", "start_new_session", "process_group"):
            self.assertNotIn(kept, kwargs)
        self.assertEqual(kwargs["stdout"], subprocess.PIPE)
        self.assertEqual(kwargs["stderr"], subprocess.PIPE)
        process.communicate.assert_called_once_with(timeout=5.0)

    def test_a_command_past_its_timeout_is_a_tool_error(self) -> None:
        argv = [sys.executable, "-c", "import time; time.sleep(30)"]
        with self.assertRaises(ToolError) as caught:
            self.host.run(argv, 0.5)
        self.assertIn("did not finish within 0.5 s", str(caught.exception))

    def test_a_child_ignoring_sigterm_is_killed(self) -> None:
        argv = [sys.executable, "-c", _IGNORE_TERM]
        started = time.monotonic()
        with mock.patch.object(sf_host, "_STOP_GRACE", 0.2):
            with self.assertRaises(ToolError):
                self.host.run(argv, 0.5)
        self.assertLess(time.monotonic() - started, _PROMPT)

    def test_a_grandchild_holding_the_pipes_does_not_hold_the_call(self) -> None:
        # The direct child exits on SIGTERM, but a grandchild keeps the captured pipes
        # open: the call must return without reading them to the end.
        argv = [sys.executable, "-c", _SPAWN_GRANDCHILD]
        started = time.monotonic()
        with self.assertRaises(ToolError):
            self.host.run(argv, 0.5)
        self.assertLess(time.monotonic() - started, _PROMPT)

    def test_a_killed_child_is_reaped_with_a_bounded_wait(self) -> None:
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(["x"], 1.0)
        process.wait.side_effect = [subprocess.TimeoutExpired(["x"], 1.0), 0]
        with mock.patch.object(sf_host.subprocess, "Popen", return_value=process):
            with self.assertRaises(ToolError) as caught:
                self.host.run(["x"], 1.0)
        process.terminate.assert_called_once_with()
        process.kill.assert_called_once_with()
        self.assertEqual(
            process.wait.call_args_list,
            [mock.call(timeout=sf_host._STOP_GRACE)] * 2,
        )
        self.assertNotIn("did not exit", str(caught.exception))
        process.stdout.close.assert_called_once_with()

    def test_a_child_surviving_sigkill_is_named_in_the_error(self) -> None:
        process = mock.Mock()
        process.communicate.side_effect = subprocess.TimeoutExpired(["x"], 1.0)
        process.wait.side_effect = subprocess.TimeoutExpired(["x"], 1.0)
        with mock.patch.object(sf_host.subprocess, "Popen", return_value=process):
            with self.assertRaises(ToolError) as caught:
                self.host.run(["x"], 1.0)
        self.assertIn("did not finish within 1 s", str(caught.exception))
        self.assertIn("did not exit even after SIGKILL", str(caught.exception))

    def test_a_missing_program_is_a_tool_error(self) -> None:
        missing = str(self.tmp / "no-such-program")
        with self.assertRaises(ToolError) as caught:
            self.host.run([missing], 5.0)
        self.assertIn("could not start", str(caught.exception))

    def test_device_of_is_st_dev(self) -> None:
        self.assertEqual(self.host.device_of(self.tmp), os.stat(self.tmp).st_dev)

    def test_read_text_reads_utf8(self) -> None:
        path = self.tmp / "fstab"
        path.write_bytes("UUID=x /café apfs rw\n".encode())
        self.assertEqual(self.host.read_text(path), "UUID=x /café apfs rw\n")

    def test_read_text_lets_a_missing_file_raise(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.host.read_text(self.tmp / "absent")

    def test_write_temp_writes_a_private_file_in_its_folder(self) -> None:
        path = self.host.write_temp("line one\n")
        self.assertEqual(path.parent, self.tmp)
        self.assertEqual(path.read_bytes(), b"line one\n")
        self.assertEqual(stat.S_IMODE(path.stat().st_mode), 0o600)

    def test_ids_and_platform_are_the_processes_own(self) -> None:
        if sys.platform == "win32":  # pragma: only-on win32
            self.skipTest("user and group IDs exist only on a POSIX system")
        self.assertEqual(self.host.getuid(), os.getuid())
        self.assertEqual(self.host.getgid(), os.getgid())
        self.assertEqual(self.host.platform, sys.platform)

    def test_now_is_a_fourteen_digit_stamp(self) -> None:
        self.assertRegex(self.host.now(), re.compile(r"^[0-9]{14}$"))

    def test_read_probe_reads_a_readable_file(self) -> None:
        path = self.tmp / "prefs.plist"
        path.write_bytes(b"x")
        self.host.read_probe(path)  # raises on any refusal

    def test_read_probe_reads_an_empty_file(self) -> None:
        path = self.tmp / "empty.plist"
        path.write_bytes(b"")
        self.host.read_probe(path)  # raises on any refusal

    def test_read_probe_lets_a_missing_file_raise(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.host.read_probe(self.tmp / "absent")

    def test_getenv_reads_the_process_environment(self) -> None:
        with mock.patch.dict(os.environ, {"SF_PROBE_NAME": "value"}):
            self.assertEqual(self.host.getenv("SF_PROBE_NAME"), "value")
        with mock.patch.dict(os.environ, {}, clear=True):
            self.assertIsNone(self.host.getenv("SF_PROBE_NAME"))

    def test_sleep_waits_through_time_sleep(self) -> None:
        with mock.patch.object(sf_host.time, "sleep") as waited:
            self.host.sleep(4.0)
        waited.assert_called_once_with(4.0)


class RealHostFactoryTest(unittest.TestCase):
    def test_real_host_leaves_temps_to_the_system_default(self) -> None:
        host = sf_host.real_host()
        self.assertIsInstance(host, sf_host.RealHost)
        # None hands the choice to tempfile, which honors TMPDIR: the user's own
        # temporary folder, readable by the sudo install that copies from it.
        self.assertIsNone(host.temp_dir)


if __name__ == "__main__":
    unittest.main()
