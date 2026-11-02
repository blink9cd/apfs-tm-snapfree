"""Tests for the command line: options, mode rules, refusals and dispatch.

The last class runs the entry point as a real child process. It reaches no host command:
``--help`` exits inside argparse, and ``status`` against a temporary fstab with no line
for the folder ends at "not configured" before any host command.
"""

import argparse
import contextlib
import io
import os
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path
from typing import Any, List, TextIO, Tuple
from unittest import mock

import apfs_tm_snapfree as cli
import sf_options
from sf_errors import EXIT_OK, EXIT_USAGE, ToolError
from sf_host import Host
from tests import fake_host

_ENTRY = Path(cli.__file__).resolve()
# The live runs start a fresh interpreter; nothing they do waits on a device.
_LIVE_TIMEOUT = 60.0
_WIDE_COLUMNS = "200"


class _CliCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.folder = self.tmp / "work"
        self.folder.mkdir()
        self.fake = fake_host.FakeHost(self.tmp)

    def _main(self, *argv: str) -> Tuple[int, str, str]:
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            code = cli.main(list(argv), host=self.fake)
        return code, out.getvalue(), err.getvalue()

    def _base(self, mode: str) -> List[str]:
        return ["--mode", mode, "--path", str(self.folder)]


class ParseOptionsTest(_CliCase):
    def test_defaults(self) -> None:
        opts = cli.parse_options(self._base("status"))
        assert isinstance(opts, sf_options.Options)
        self.assertEqual(
            opts,
            sf_options.Options(
                mode="status",
                path=self.folder,
                quota=None,
                name=None,
                fstab=Path("/etc/fstab"),
                warn_percent=90,
                finish=False,
                apply=False,
            ),
        )

    def test_every_option_is_read(self) -> None:
        opts = cli.parse_options(
            [
                *self._base("setup"),
                "--quota",
                "40g",
                "--name",
                "vol",
                "--fstab",
                str(self.tmp / "fstab"),
                "--apply",
            ]
        )
        assert isinstance(opts, sf_options.Options)
        self.assertEqual(opts.quota, 40 * 10**9)
        self.assertEqual(opts.name, "vol")
        self.assertEqual(opts.fstab, self.tmp / "fstab")
        self.assertTrue(opts.apply)

    def test_path_is_resolved_through_symlinks(self) -> None:
        link = self.tmp / "link"
        link.symlink_to(self.folder)
        opts = cli.parse_options(["--mode", "status", "--path", str(link)])
        assert isinstance(opts, sf_options.Options)
        self.assertEqual(opts.path, self.folder)

    def test_resize_finish_and_warn_percent(self) -> None:
        opts = cli.parse_options([*self._base("resize"), "--finish"])
        assert isinstance(opts, sf_options.Options)
        self.assertTrue(opts.finish)
        opts = cli.parse_options([*self._base("status"), "--warn-percent", "75"])
        assert isinstance(opts, sf_options.Options)
        self.assertEqual(opts.warn_percent, 75)

    def test_help_exits_zero(self) -> None:
        with contextlib.redirect_stdout(io.StringIO()):
            self.assertEqual(cli.parse_options(["--help"]), EXIT_OK)


class UsageErrorTest(_CliCase):
    def _assert_usage(self, argv: List[str], fragment: str) -> None:
        code, _, err = self._main(*argv)
        self.assertEqual(code, EXIT_USAGE, err)
        self.assertIn(fragment, err)
        self.assertEqual(self.fake.calls, [])

    def test_mode_and_path_are_required(self) -> None:
        self._assert_usage(["--path", str(self.folder)], "--mode")
        self._assert_usage(["--mode", "status"], "--path")

    def test_unknown_mode(self) -> None:
        self._assert_usage(["--mode", "erase", "--path", "/x"], "invalid choice")

    def test_quota_is_required_for_setup_and_resize(self) -> None:
        self._assert_usage(self._base("setup"), "--quota is required")
        self._assert_usage(self._base("resize"), "--quota is required")

    def test_a_malformed_quota(self) -> None:
        self._assert_usage([*self._base("setup"), "--quota", "40"], "--quota")

    def test_quota_is_refused_where_it_means_nothing(self) -> None:
        for mode in ("status", "teardown"):
            with self.subTest(mode=mode):
                self._assert_usage([*self._base(mode), "--quota", "1g"], "--quota")
        self._assert_usage(
            [*self._base("resize"), "--finish", "--quota", "1g"],
            "--finish takes no --quota",
        )

    def test_name_is_setup_only(self) -> None:
        self._assert_usage([*self._base("status"), "--name", "v"], "--name")

    def test_finish_is_resize_only(self) -> None:
        self._assert_usage([*self._base("teardown"), "--finish"], "--finish")

    def test_apply_is_refused_for_status(self) -> None:
        self._assert_usage([*self._base("status"), "--apply"], "--apply")

    def test_warn_percent_is_status_only(self) -> None:
        cases = {
            "setup": ["--quota", "1g"],
            "resize": ["--quota", "1g"],
            "teardown": [],
        }
        for mode, extra in cases.items():
            with self.subTest(mode=mode):
                self._assert_usage(
                    [*self._base(mode), *extra, "--warn-percent", "50"],
                    "--warn-percent applies only to --mode status",
                )

    def test_warn_percent_bounds(self) -> None:
        for value in ("0", "101", "x"):
            with self.subTest(value=value):
                self._assert_usage(
                    [*self._base("status"), "--warn-percent", value], "--warn-percent"
                )


class RefusalTest(_CliCase):
    def test_off_macos_is_refused(self) -> None:
        self.fake = fake_host.FakeHost(self.tmp, platform="linux")
        code, _, err = self._main(*self._base("status"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("macOS", err)
        self.assertEqual(self.fake.calls, [])

    def test_root_is_refused(self) -> None:
        self.fake = fake_host.FakeHost(self.tmp, uid=0)
        code, _, err = self._main(*self._base("status"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not as root", err)


class DispatchTest(_CliCase):
    def test_every_mode_has_an_entry(self) -> None:
        self.assertEqual(sorted(cli.MODES), ["resize", "setup", "status", "teardown"])

    def test_the_handler_gets_the_options_host_and_stdout(self) -> None:
        seen: list[object] = []

        def handler(opts: sf_options.Options, host: Host, out: TextIO) -> int:
            seen.extend([opts.mode, host, out])
            out.write("handled\n")
            return 1

        with mock.patch.dict(cli.MODES, {"status": handler}):
            code, out, _ = self._main(*self._base("status"))
        self.assertEqual((code, out), (1, "handled\n"))
        self.assertEqual(seen[:2], ["status", self.fake])

    def test_a_tool_error_from_a_mode_is_exit_2_with_its_message(self) -> None:
        def handler(opts: sf_options.Options, host: Host, out: TextIO) -> int:
            raise ToolError("diskutil exploded")

        with mock.patch.dict(cli.MODES, {"status": handler}):
            code, _, err = self._main(*self._base("status"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("diskutil exploded", err)

    def test_an_os_error_from_a_mode_is_one_line_and_exit_2(self) -> None:
        def handler(opts: sf_options.Options, host: Host, out: TextIO) -> int:
            raise PermissionError(13, "Permission denied", "/a/locked")

        with mock.patch.dict(cli.MODES, {"status": handler}):
            code, _, err = self._main(*self._base("status"))
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(
            err, f"{cli.PROG}: [Errno 13] Permission denied: '/a/locked'\n"
        )

    def test_any_other_exception_from_a_mode_is_not_swallowed(self) -> None:
        def handler(opts: sf_options.Options, host: Host, out: TextIO) -> int:
            raise RuntimeError("a defect")

        with mock.patch.dict(cli.MODES, {"status": handler}):
            with self.assertRaises(RuntimeError):
                self._main(*self._base("status"))

    def test_the_real_host_is_used_when_none_is_given(self) -> None:
        with mock.patch.object(
            cli.sf_host, "real_host", return_value=self.fake
        ) as made:
            with contextlib.redirect_stdout(io.StringIO()):
                # A temporary fstab, so the status run never reads the machine's own.
                cli.main([*self._base("status"), "--fstab", str(self.tmp / "fstab")])
        made.assert_called_once_with()


class EpilogTest(unittest.TestCase):
    def test_the_exit_codes_say_where_a_failing_host_command_lands(self) -> None:
        epilog = " ".join((cli.build_parser().epilog or "").split())
        self.assertIn("a host command failing during a check is a FAIL line", epilog)
        self.assertIn("2 = a usage error, a folder that is not configured", epilog)
        self.assertIn("a run refused before any check or step", epilog)
        self.assertNotIn("a host command that could not run", epilog)


class HelpFormatterTest(unittest.TestCase):
    def _help_for(self, **kwargs: Any) -> str:
        parser = argparse.ArgumentParser(formatter_class=cli.HelpFormatter)
        action = parser.add_argument("--x", help="the x", **kwargs)
        formatter = cli.HelpFormatter("prog")
        return str(formatter._get_help_string(action))

    def test_a_real_default_is_shown(self) -> None:
        self.assertEqual(self._help_for(default=90), "the x (default: %(default)s)")

    def test_a_none_default_is_left_unstated(self) -> None:
        self.assertEqual(self._help_for(), "the x")

    def test_a_required_option_is_never_stamped(self) -> None:
        # Python before 3.13 stamps one; the suite runs on 3.9 as well, which pins this.
        self.assertEqual(self._help_for(required=True, default="x"), "the x")

    def test_an_unset_flag_is_not_stamped_in_the_tool_s_help(self) -> None:
        # --finish and --apply are off unless given, as --quota and --name are unset;
        # none of the four shows a default.
        text = cli.build_parser().format_help()
        self.assertNotIn("(default: False)", text)
        self.assertNotIn("(default: None)", text)

    def test_an_unset_flag_reaches_options_as_false(self) -> None:
        # The flags default to None so --help stamps nothing; Options still holds a bool.
        opts = cli.parse_options(["--mode", "status", "--path", os.curdir])
        assert isinstance(opts, sf_options.Options)
        self.assertIs(opts.finish, False)
        self.assertIs(opts.apply, False)


class LiveEntryTest(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))

    def _run(self, *argv: str) -> "subprocess.CompletedProcess[str]":
        # The child is this tool, which starts no process of its own for --help or the
        # stub, so no grandchild can hold the pipes past the timeout.
        return subprocess.run(  # reliability: ignore[RL005] the child spawns nothing
            [sys.executable, str(_ENTRY), *argv],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            timeout=_LIVE_TIMEOUT,
            check=False,
            # A wide terminal, so argparse never wraps a default across two lines.
            env={**os.environ, "COLUMNS": _WIDE_COLUMNS},
        )

    def test_help_exits_zero_and_shows_defaults(self) -> None:
        done = self._run("--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn("(default: 90)", done.stdout)
        self.assertIn("(default: /etc/fstab)", done.stdout)
        # The epilog wraps at the terminal width, which may split the phrase.
        self.assertIn("Exit codes", " ".join(done.stdout.split()))
        self.assertIn(_ENTRY.name, done.stdout)

    def test_status_against_a_temporary_fstab(self) -> None:
        # Exit 2 either way: an fstab with no line for the path is "not configured",
        # and a host this suite may run on off macOS or as root is refused first.
        fstab = self.tmp / "fstab"
        fstab.write_bytes("# no lines\n".encode("utf-8"))
        done = self._run(
            "--mode", "status", "--path", str(self.tmp), "--fstab", str(fstab)
        )
        self.assertEqual(done.returncode, EXIT_USAGE, done.stderr)


if __name__ == "__main__":
    unittest.main()
