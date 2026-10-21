"""Tests for ``--mode status``: the eight read-only checks, their lines and exit codes.

Every host reply is scripted on the fake host from obvious fakes, the fstab is a file in
the test's own temporary folder, and a mount is the fake's device map, so no test reads
the real disk layout or runs a real command.
"""

import io
import os
import shlex
import tempfile
import unittest
from pathlib import Path
from typing import List, Optional, Tuple

import apfs_tm_snapfree as cli
import sf_commands
import sf_fstab
import sf_macos
import sf_status
from sf_errors import EXIT_FAIL, EXIT_OK, EXIT_USAGE, ToolError
from sf_host import COMMAND_TIMEOUT
from sf_options import DEFAULT_WARN_PERCENT, Options
from tests import fake_host

UUID = fake_host.FAKE_UUID_1
OTHER_UUID = fake_host.FAKE_UUID_2
GB = 10**9
#: Every command status may run: each a read, none a change, none through sudo.
READ_ONLY_PREFIXES = (
    sf_macos.STAT_DEVICE_ARGV,
    ("diskutil", "info", "-plist"),
    ("diskutil", "apfs", "list", "-plist"),
    ("tmutil", "isexcluded", "-X"),
    ("mdutil", "-s"),
)
SPOTLIGHT_OFF = "/fake:\n\tIndexing disabled.\n"
SPOTLIGHT_ON = "/fake:\n\tIndexing enabled.\n"
VOLUME_CHECKS = (
    "right volume",
    "Time Machine excluded",
    "Spotlight off",
    "ownership honored",
    "use against quota",
)


class _StatusCase(unittest.TestCase):
    """A folder, an fstab mounting a volume there, and a fake host answering for it."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.folder = self.tmp / "work tree"
        self.folder.mkdir()
        self.fstab = self.tmp / "fstab"
        self.write_fstab(sf_fstab.render_line(UUID, self.folder))
        self.fake = fake_host.FakeHost(self.tmp)

    def write_fstab(self, *lines: str) -> None:
        """Make the test's fstab hold *lines*."""
        text = "".join(f"{line}\n" for line in lines)
        self.fstab.write_bytes(text.encode("utf-8"))

    def healthy(
        self,
        *,
        quota: int = 10 * GB,
        in_use: int = 1 * GB,
        info_uuid: str = UUID,
        excluded: bool = True,
        spotlight: str = SPOTLIGHT_OFF,
        ownership: bool = True,
    ) -> None:
        """Mount the folder and script every read status makes, healthy by default."""
        self.fake.mount(self.folder)
        self.fake.respond_info(
            self.folder,
            mount_point=str(self.folder),
            volume_uuid=info_uuid,
            ownership=ownership,
            name="snapfree-work",
        )
        self.fake.respond_isexcluded(self.folder, excluded=excluded)
        self.fake.respond_mdutil(self.folder, spotlight)
        volume = fake_host.volume_entry(uuid=info_uuid, quota=quota, in_use=in_use)
        self.fake.respond_apfs_list([fake_host.container_entry(volumes=[volume])])

    def run_status(
        self, warn_percent: int = DEFAULT_WARN_PERCENT, path: Optional[Path] = None
    ) -> Tuple[int, List[str]]:
        """Run status; return its exit code and its output lines."""
        opts = Options(
            mode="status",
            path=self.folder if path is None else path,
            quota=None,
            name=None,
            fstab=self.fstab,
            warn_percent=warn_percent,
            finish=False,
            apply=False,
        )
        out = io.StringIO()
        code = sf_status.run_status(opts, self.fake, out)
        return code, out.getvalue().splitlines()

    def line_for(self, lines: List[str], check: str) -> str:
        """The one output line for *check*."""
        found = [line for line in lines if line.split(" ", 1)[1].startswith(check)]
        self.assertEqual(len(found), 1, lines)
        return found[0]

    def assert_read_only(self) -> None:
        """Every command run was a read from the allowlist, with the standard timeout."""
        for argv in self.fake.calls:
            self.assertTrue(
                any(argv[: len(prefix)] == prefix for prefix in READ_ONLY_PREFIXES),
                f"status ran a command outside the read-only allowlist: {argv}",
            )
            self.assertNotIn("sudo", argv)
        self.assertTrue(all(t == COMMAND_TIMEOUT for t in self.fake.timeouts))


class HealthyTest(_StatusCase):
    def test_every_check_ok_is_exit_0_in_spec_order(self) -> None:
        self.healthy()
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            [line.split(":", 1)[0] for line in lines],
            [
                "ok fstab line",
                "ok mounted",
                "ok right volume",
                "ok Time Machine excluded",
                "ok Spotlight off",
                "ok ownership honored",
                "ok use against quota",
                "ok retired volume pending",
            ],
        )
        self.assertIn(UUID, self.line_for(lines, "fstab line"))
        self.assertIn(
            "1.0 GB of 10.0 GB (10%)", self.line_for(lines, "use against quota")
        )
        self.assert_read_only()
        # stat and diskutil info for the volume, then isexcluded, mdutil, apfs list.
        self.assertEqual(len(self.fake.calls), 5)

    def test_the_uuid_comparison_ignores_case(self) -> None:
        self.write_fstab(sf_fstab.render_line(UUID.lower(), self.folder))
        self.healthy(info_uuid=UUID.upper())
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_OK, lines)

    def test_status_is_registered_in_the_dispatch_table(self) -> None:
        self.assertIs(cli.MODES["status"], sf_status.run_status)


class NotConfiguredTest(_StatusCase):
    def test_no_line_for_the_path_is_exit_2_and_runs_nothing(self) -> None:
        self.write_fstab("# nothing here", sf_fstab.render_line(UUID, self.tmp))
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_USAGE)
        self.assertEqual(
            lines,
            [
                f"FAIL fstab line: not configured: no {self.fstab} line mounts a "
                f"volume at {self.folder}"
            ],
        )
        self.assertEqual(self.fake.calls, [])

    def test_a_missing_fstab_is_not_configured(self) -> None:
        self.fstab.unlink()
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_USAGE)
        self.assertIn("not configured", lines[0])

    def test_an_unreadable_fstab_is_a_tool_error_naming_it(self) -> None:
        self.fstab.unlink()
        self.fstab.mkdir()
        with self.assertRaises(ToolError) as caught:
            self.run_status()
        self.assertIn(str(self.fstab), str(caught.exception))

    def test_an_fstab_that_is_not_utf8_is_a_tool_error(self) -> None:
        self.fstab.write_bytes(b"\xff\xfe not text\n")
        with self.assertRaises(ToolError) as caught:
            self.run_status()
        self.assertIn(str(self.fstab), str(caught.exception))


class NotMountedTest(_StatusCase):
    def test_checks_3_to_7_are_skipped_and_nothing_runs(self) -> None:
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertEqual(len(lines), 8)
        self.assertTrue(self.line_for(lines, "mounted").startswith("FAIL mounted: "))
        for check in VOLUME_CHECKS:
            with self.subTest(check=check):
                self.assertEqual(
                    self.line_for(lines, check),
                    f"WARN {check}: skipped (not mounted)",
                )
        self.assertTrue(lines[-1].startswith("ok retired volume pending"))
        self.assertEqual(self.fake.calls, [])

    def test_a_missing_folder_is_a_fail_line_not_a_crash(self) -> None:
        gone = self.tmp / "gone"
        self.write_fstab(sf_fstab.render_line(UUID, gone))
        code, lines = self.run_status(path=gone)
        self.assertEqual(code, EXIT_FAIL)
        mounted = self.line_for(lines, "mounted")
        self.assertTrue(mounted.startswith("FAIL mounted: could not"), mounted)
        self.assertIn("WARN right volume: skipped (not mounted)", lines)


class RightVolumeTest(_StatusCase):
    def test_another_volume_mounted_there_fails_naming_both(self) -> None:
        self.healthy(info_uuid=OTHER_UUID)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "right volume")
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn(UUID, line)
        self.assertIn(OTHER_UUID, line)

    def test_a_non_apfs_volume_fails_not_apfs_and_skips_the_apfs_reads(self) -> None:
        self.fake.mount(self.folder)
        self.fake.respond_info(
            self.folder,
            mount_point=str(self.folder),
            filesystem="msdos",
            omit=("VolumeUUID", "APFSContainerReference", "APFSContainerFree"),
        )
        self.fake.respond_isexcluded(self.folder, excluded=True)
        self.fake.respond_mdutil(self.folder, SPOTLIGHT_OFF)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "right volume")
        self.assertTrue(line.startswith("FAIL right volume: "), line)
        self.assertIn("not APFS", line)
        for check in ("ownership honored", "use against quota"):
            self.assertEqual(
                self.line_for(lines, check),
                f"WARN {check}: skipped (no APFS volume info)",
            )
        self.assertTrue(self.line_for(lines, "Time Machine").startswith("ok "))
        self.assert_read_only()

    def test_a_failing_diskutil_info_is_a_fail_line_naming_it(self) -> None:
        self.healthy()
        self.fake.respond(
            sf_macos.info_argv(self.fake.device_name(self.folder)),
            returncode=1,
            stderr=b"boom",
            once=True,
        )
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "right volume")
        self.assertTrue(line.startswith("FAIL right volume: diskutil info"), line)
        self.assertIn("boom", line)


class ExclusionAndSpotlightTest(_StatusCase):
    def test_not_excluded_from_time_machine_fails(self) -> None:
        self.healthy(excluded=False)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(
            self.line_for(lines, "Time Machine excluded").startswith("FAIL ")
        )

    def test_a_privacy_denial_stays_on_one_line_and_names_the_cause(self) -> None:
        self.healthy()
        self.fake.respond(
            ["tmutil", "isexcluded", "-X", str(self.folder)],
            returncode=1,
            stderr=b"Operation not permitted",
            once=True,
        )
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertEqual(len(lines), 8)
        line = self.line_for(lines, "Time Machine excluded")
        self.assertTrue(line.startswith("FAIL Time Machine excluded: tmutil"), line)
        self.assertIn("Full Disk Access", line)

    def test_spotlight_enabled_fails(self) -> None:
        self.healthy(spotlight=SPOTLIGHT_ON)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(self.line_for(lines, "Spotlight off").startswith("FAIL "))

    def test_unrecognized_mdutil_text_is_warn_unknown(self) -> None:
        self.healthy(spotlight="something new\n")
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "Spotlight off")
        self.assertTrue(line.startswith("WARN Spotlight off: unknown"), line)

    def test_a_failing_mdutil_is_a_fail_line(self) -> None:
        self.healthy()
        self.fake.respond(["mdutil", "-s", str(self.folder)], returncode=2, once=True)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(
            self.line_for(lines, "Spotlight off").startswith("FAIL Spotlight off: md")
        )


class OwnershipTest(_StatusCase):
    def test_ownership_ignored_fails(self) -> None:
        self.healthy(ownership=False)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(self.line_for(lines, "ownership honored").startswith("FAIL "))


class QuotaTest(_StatusCase):
    def test_no_quota_is_warn(self) -> None:
        self.healthy(quota=0)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "use against quota")
        self.assertTrue(line.startswith("WARN use against quota: no quota"), line)

    def test_at_the_warn_percent_is_warn_with_both_sizes(self) -> None:
        self.healthy(quota=10 * GB, in_use=9 * GB)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "use against quota")
        self.assertTrue(line.startswith("WARN "), line)
        self.assertIn("9.0 GB", line)
        self.assertIn("10.0 GB", line)
        self.assertIn("90%", line)

    def test_just_under_the_warn_percent_is_ok(self) -> None:
        self.healthy(quota=10 * GB, in_use=9 * GB - 1)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_OK, lines)

    def test_the_warn_percent_option_is_honored(self) -> None:
        self.healthy(quota=10 * GB, in_use=5 * GB)
        code, lines = self.run_status(warn_percent=50)
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(self.line_for(lines, "use against quota").startswith("WARN "))

    def test_a_volume_missing_from_apfs_list_fails(self) -> None:
        self.healthy()
        self.fake.respond_apfs_list([fake_host.container_entry()], once=True)
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "use against quota")
        self.assertTrue(line.startswith("FAIL "), line)
        self.assertIn(UUID, line)

    def test_a_failing_apfs_list_is_a_fail_line(self) -> None:
        self.healthy()
        self.fake.respond(
            ["diskutil", "apfs", "list", "-plist"], stdout=b"not a plist", once=True
        )
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "use against quota")
        self.assertTrue(line.startswith("FAIL use against quota: diskutil"), line)


class RetiredMarkerTest(_StatusCase):
    def test_a_pending_marker_warns_naming_the_finish_command(self) -> None:
        self.write_fstab(
            sf_fstab.render_line(UUID, self.folder),
            sf_fstab.render_marker(OTHER_UUID, self.folder),
        )
        self.healthy()
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        line = self.line_for(lines, "retired volume pending")
        self.assertTrue(line.startswith("WARN "), line)
        self.assertIn(OTHER_UUID, line)
        finish = sf_commands.finish_command(self.folder, self.fstab)
        self.assertIn(f"delete it with {finish}", line)
        self.assertIn(f"--finish --apply --fstab {shlex.quote(str(self.fstab))}", line)

    def test_a_marker_for_another_path_is_ignored(self) -> None:
        self.write_fstab(
            sf_fstab.render_line(UUID, self.folder),
            sf_fstab.render_marker(OTHER_UUID, self.tmp),
        )
        self.healthy()
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_OK, lines)

    def test_the_marker_is_reported_even_when_not_mounted(self) -> None:
        self.write_fstab(
            sf_fstab.render_line(UUID, self.folder),
            sf_fstab.render_marker(OTHER_UUID, self.folder),
        )
        code, lines = self.run_status()
        self.assertEqual(code, EXIT_FAIL)
        self.assertTrue(lines[-1].startswith("WARN retired volume pending"))


if __name__ == "__main__":
    unittest.main()
