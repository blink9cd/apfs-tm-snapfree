"""Tests for resize: its refusals, its volume name, its dry run and its applied run.

Every run goes through ``tests.resize_fixture``'s scripted machine; the only real files
are the folder, its copy and the fstab in each test's own temporary folder.
"""

from __future__ import annotations

import dataclasses
import io
import os
import shlex
import sys
import unittest
from pathlib import Path

import apfs_tm_snapfree as cli
import sf_macos
import sf_resize
from sf_errors import EXIT_OK, ToolError
from sf_fstab import entry_for, marker_for
from sf_host import COPY_TIMEOUT
from sf_size import format_size
from sf_walk import NEVER_INDEX, VOLUME_PRIVATE
from tests.fake_host import FAKE_CONTAINER, FAKE_STAMP
from tests.resize_fixture import (
    GB,
    IN_USE,
    NEW,
    NEW_NAME,
    NEW_QUOTA,
    OLD,
    OTHER,
    ResizeCase,
    write_file,
)

_PRIVATE = ", ".join(sorted(VOLUME_PRIVATE))
_DRY_FOOTER = (
    "Dry run: nothing was changed. Re-run with --apply to perform these steps.\n"
)


def _finish_command(path: Path, fstab: Path) -> str:
    """The --finish command the last step names, under this test runner's argv[0]."""
    prog = os.path.basename(sys.argv[0])
    argv = ["python3", prog, "--mode", "resize", "--path", str(path)]
    return shlex.join([*argv, "--finish", "--apply", "--fstab", str(fstab)])


class NewVolumeNameTest(unittest.TestCase):
    def test_a_plain_name_gains_the_stamp(self) -> None:
        self.assertEqual(
            sf_resize.new_volume_name("snapfree-work", FAKE_STAMP),
            f"snapfree-work-{FAKE_STAMP}",
        )

    def test_a_previous_stamp_is_replaced_not_stacked(self) -> None:
        self.assertEqual(
            sf_resize.new_volume_name("snapfree-work-20250101000000", FAKE_STAMP),
            f"snapfree-work-{FAKE_STAMP}",
        )

    def test_only_a_whole_trailing_fourteen_digit_stamp_is_dropped(self) -> None:
        for name in ("w-2025010100000", "w-202501010000001", "w-20250101000000x"):
            with self.subTest(name=name):
                self.assertEqual(
                    sf_resize.new_volume_name(name, FAKE_STAMP), f"{name}-{FAKE_STAMP}"
                )

    def test_the_temporary_mount_point_is_hidden_beside_the_folder(self) -> None:
        self.assertEqual(
            sf_resize.temp_mount_point(Path("/a/b/work"), FAKE_STAMP),
            Path(f"/a/b/.work.snapfree-resize-{FAKE_STAMP}"),
        )


class ValidateResizeTest(ResizeCase):
    def _refused(self, *needles: str) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(self.options(), self.fake, io.StringIO())
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        self.assertFalse(
            any(call[0] == "sudo" for call in self.fake.calls), self.fake.calls
        )

    def test_a_missing_quota_is_refused(self) -> None:
        opts = dataclasses.replace(self.options(), quota=None)
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(opts, self.fake, io.StringIO())
        self.assertIn("--quota", str(caught.exception))

    def test_an_absent_fstab_means_not_configured(self) -> None:
        self.fstab.unlink()
        self._refused("has no line", str(self.path))

    def test_an_unreadable_fstab_is_refused(self) -> None:
        self.fstab.unlink()
        self.fstab.mkdir()
        self._refused("could not read", str(self.fstab))

    def test_a_folder_with_no_fstab_line_is_refused(self) -> None:
        write_file(self.fstab, "# nothing here\n")
        self._refused("has no line", str(self.path))

    def test_a_folder_with_nothing_mounted_is_refused(self) -> None:
        self.fake.unmount(self.path)
        self._refused("not mounted", str(self.path))

    def test_the_wrong_volume_at_the_folder_is_refused(self) -> None:
        self.infos[str(self.path)]["volume_uuid"] = OTHER
        self._refused(OTHER, OLD)

    def test_the_uuid_comparison_ignores_case(self) -> None:
        upper = "00000000-0000-4000-8000-00000000000A"
        write_file(self.fstab, f"UUID={upper.lower()} {self.path} apfs rw,nobrowse\n")
        self.infos[str(self.path)]["volume_uuid"] = upper
        self.volumes[0]["APFSVolumeUUID"] = upper
        code = sf_resize.run_resize(self.options(), self.fake, io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_a_pending_retired_volume_is_refused(self) -> None:
        with self.fstab.open("a", encoding="utf-8", newline="\n") as stream:
            stream.write(f"# apfs_tm_snapfree: retired UUID={OTHER} for {self.path}\n")
        self._refused(OTHER, f"run {_finish_command(self.path, self.fstab)} first")

    def test_a_volume_missing_from_its_container_is_refused(self) -> None:
        self.volumes.clear()
        self._refused(OLD, FAKE_CONTAINER)

    def test_a_quota_without_the_margin_above_the_data_is_refused(self) -> None:
        opts = self.options(quota=IN_USE)
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(opts, self.fake, io.StringIO())
        self.assertIn(format_size(IN_USE), str(caught.exception))

    def test_a_quota_of_exactly_the_data_plus_margin_is_enough(self) -> None:
        opts = self.options(quota=IN_USE + GB)
        self.assertEqual(sf_resize.run_resize(opts, self.fake, io.StringIO()), EXIT_OK)

    def test_a_container_without_room_for_the_copy_is_refused(self) -> None:
        self.free = IN_USE + GB - 1
        self._refused("free", FAKE_CONTAINER)

    def test_room_of_exactly_the_data_plus_margin_is_enough(self) -> None:
        self.free = IN_USE + GB
        code = sf_resize.run_resize(self.options(), self.fake, io.StringIO())
        self.assertEqual(code, EXIT_OK)

    def test_a_taken_volume_name_is_refused(self) -> None:
        self.volumes[0]["Name"] = NEW_NAME
        self._refused(NEW_NAME)

    def test_a_taken_temporary_mount_point_is_refused(self) -> None:
        self.temp.mkdir()
        self._refused(str(self.temp))

    def test_a_user_made_trashes_folder_is_refused(self) -> None:
        # The copy leaves the name out, so a folder the user made under it would
        # stay behind on the old volume, which --finish deletes.
        (self.path / ".Trashes").mkdir()
        self._refused(
            str(self.path / ".Trashes"), "not the macOS-managed folder of that name"
        )

    def test_a_trashes_that_is_a_plain_file_is_refused(self) -> None:
        self.as_macos_made()
        (self.path / ".Trashes").write_bytes(b"user data")
        self._refused(str(self.path / ".Trashes"), "move it aside or rename it")


class ResizeDryRunTest(ResizeCase):
    def _dry_run(self) -> str:
        out = io.StringIO()
        self.assertEqual(sf_resize.run_resize(self.options(), self.fake, out), EXIT_OK)
        return out.getvalue()

    def test_the_dry_run_prints_every_step_and_changes_nothing(self) -> None:
        temp, path, fstab = self.temp, self.path, self.fstab
        steps = [
            f"sudo diskutil apfs addVolume {FAKE_CONTAINER} APFS {NEW_NAME} "
            "-quota 20g -nomount",
            f"mkdir {temp}",
            f"sudo diskutil mount -mountPoint {temp} <new-volume-uuid>",
            f"if the new volume ignores ownership: sudo diskutil enableOwnership {temp}",
            f"sudo chown {self.owner} {temp}",
            f"sudo chmod 750 {temp}",
            f"sudo tmutil addexclusion -v {temp}",
            f"touch {temp}/{NEVER_INDEX} (as you: an empty file that keeps "
            "Spotlight off the volume once it mounts again)",
            f"sudo diskutil unmount {temp} && sudo diskutil mount -mountOptions "
            f"nobrowse -mountPoint {temp} <new-volume-uuid>",
            f"sudo mdutil -i off {temp}",
            f"copy each entry at the top of {path} into {temp}, all but the "
            f"volume's own {_PRIVATE} and this tool's {NEVER_INDEX} (as listed when "
            "this step prints):\n"
            f"       ditto {path}/a.txt {temp}/a.txt\n"
            f"       ln -s -- a.txt {temp}/link\n"
            f"       ditto {path}/sub {temp}/sub",
            f"sudo diskutil unmount {temp}",
            f"rmdir {temp}",
            f"sudo diskutil unmount {path}",
            f"sudo diskutil mount -mountOptions nobrowse -mountPoint {path} "
            "<new-volume-uuid>",
            f"in {fstab}, name the new volume on the line for {path} and add a "
            f"marker retiring {OLD}: sudo cp {fstab} {fstab}.apfs_tm_snapfree.bak; "
            f"sudo install -m 0644 -o root -g wheel <temp> "
            f"{fstab}.apfs_tm_snapfree.new; "
            f"sudo mv -f {fstab}.apfs_tm_snapfree.new {fstab}",
            f"keep the old volume {OLD}, its data intact, until the new one is "
            "checked; it has no fstab line, so a restart may mount it under "
            "/Volumes, and --finish then prints the command to unmount it; delete "
            f"it with: {_finish_command(path, fstab)}",
        ]
        header = (
            f"Resize {path}: copy volume {OLD} ({format_size(IN_USE)} in use) onto a "
            f"new volume {NEW_NAME} with a {format_size(NEW_QUOTA)} quota in "
            f"{FAKE_CONTAINER}.\n"
            f"Quit anything that writes to {path} first: whatever it writes once "
            "the copy starts stays on the old volume.\n"
        )
        numbered = "".join(f"  {n}. {s}\n" for n, s in enumerate(steps, start=1))
        self.assertEqual(self._dry_run(), header + numbered + _DRY_FOOTER)
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))
        self.assertFalse(os.path.lexists(self.temp))

    def test_no_filevault_note_when_the_parent_is_not_encrypted(self) -> None:
        self.assertNotIn("FileVault", self._dry_run())

    def test_a_filevault_parent_gets_the_unencrypted_note(self) -> None:
        self.infos[str(self.parent)]["filevault"] = True
        text = self._dry_run()
        self.assertIn(f"{sf_macos.FILEVAULT_NOTE.format(path=self.parent)}\n", text)
        self.assertLess(text.index("FileVault"), text.index("  1. "))

    def test_a_non_apfs_parent_gets_no_note(self) -> None:
        self.infos[str(self.parent)] = {
            "mount_point": "/Volumes/Other",
            "filesystem": "hfs",
            "omit": ("VolumeUUID", "FileVault"),
        }
        self.assertNotIn("FileVault", self._dry_run())


class ResizeApplyTest(ResizeCase):
    def _apply(self) -> str:
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        return out.getvalue()

    def _expected_calls(self, *, enable_ownership: bool) -> list[tuple[str, ...]]:
        argv = self.step_argvs()
        reads = self.fake.volume_reads
        listing = ("diskutil", "apfs", "list", "-plist")
        temp, fstab = str(self.temp), str(self.fstab)
        ownership = [tuple(argv["enableOwnership"]), *reads(self.temp)]
        staged = f"{fstab}.apfs_tm_snapfree.new"
        return [
            *reads(self.path),
            listing,
            *reads(self.parent),
            tuple(argv["addVolume"]),
            listing,
            tuple(argv["mount temp"]),
            *reads(self.temp),
            *reads(self.temp),
            *(ownership if enable_ownership else []),
            tuple(argv["chown"]),
            tuple(argv["chmod"]),
            tuple(argv["exclude"]),
            sf_macos.EXCLUDED_UUIDS_ARGV,
            tuple(argv["unmount temp"]),
            tuple(argv["remount temp"]),
            *reads(self.temp),
            tuple(argv["mdutil"]),
            ("mdutil", "-s", temp),
            tuple(argv["ditto"]),
            tuple(argv["ditto sub"]),
            tuple(argv["unmount temp"]),
            tuple(argv["unmount path"]),
            tuple(argv["mount path"]),
            *reads(self.path),
            ("sudo", "cp", fstab, f"{fstab}.apfs_tm_snapfree.bak"),
            ("sudo", "install", "-m", "0644", "-o", "root", "-g", "wheel")
            + (str(self.fake.temps[0]), staged),
            ("sudo", "mv", "-f", staged, fstab),
        ]

    def test_apply_runs_every_step_in_order_and_retires_the_old_volume(self) -> None:
        text = self._apply()
        self.assertEqual(self.fake.calls, self._expected_calls(enable_ownership=False))
        fstab = self.fstab.read_text()
        entry = entry_for(fstab, self.path)
        marker = marker_for(fstab, self.path)
        assert entry is not None and marker is not None
        self.assertEqual((entry.volume_id, marker.uuid), (NEW, OLD))
        self.assertTrue(fstab.startswith("# local\n"))
        self.assertFalse(os.path.lexists(self.temp))
        self.assertIn(
            f"sudo diskutil mount -mountOptions nobrowse -mountPoint {self.temp} {NEW}",
            text,
        )
        self.assertIn(f"sudo diskutil mount -mountPoint {self.temp} {NEW}", text)
        self.assertEqual(self.excluded_uuids, [OLD, NEW])
        self.assertIn(_finish_command(self.path, self.fstab), text)
        self.assertIn("Done: all 17 steps completed.", text)

    def test_the_copy_runs_without_sudo_under_the_copy_timeout(self) -> None:
        self._apply()
        index = self.fake.calls.index(tuple(self.step_argvs()["ditto"]))
        self.assertEqual(self.fake.timeouts[index], COPY_TIMEOUT)

    def test_a_volume_ignoring_ownership_has_it_enabled_and_confirmed(self) -> None:
        self.new_ownership = False
        self._apply()
        self.assertEqual(self.fake.calls, self._expected_calls(enable_ownership=True))


class ResizeVolumePrivateTest(ResizeCase):
    """macOS's own root-only folders on either volume root are not the data."""

    def test_they_are_neither_copied_nor_counted(self) -> None:
        self.add_volume_private(self.path)
        mount_temp = self.step_argvs()["mount temp"]

        def mount_and_journal(argv: tuple[str, ...]) -> None:
            self._mount_temp(argv)
            self.add_volume_private(self.temp)

        self.fake.respond(mount_temp, effect=mount_and_journal, once=True)
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        copies = [call for call in self.fake.calls if call[0] == "ditto"]
        self.assertEqual(
            copies, [tuple(self.step_argvs()[name]) for name in ("ditto", "ditto sub")]
        )

    def test_a_macos_made_trashes_is_accepted_and_named_in_the_warning(self) -> None:
        self.as_macos_made()
        (self.path / ".Trashes").mkdir()
        out = io.StringIO()
        self.assertEqual(sf_resize.run_resize(self.options(), self.fake, out), EXIT_OK)
        lines = out.getvalue().splitlines()
        self.assertEqual(
            lines[2],
            f"macOS's own .Trashes at the top of {self.path} are not carried to the "
            "new volume: they and their contents, Versions history "
            "(.DocumentRevisions-V100) and the volume's Trash included, stay on the "
            "old volume, which --finish deletes.",
        )


class ResizeSpotlightMarkerTest(ResizeCase):
    """The new volume gets its own marker, there when it remounts; the old one stays."""

    def _apply(self) -> str:
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(apply=True), self.fake, out)
        self.assertEqual(code, EXIT_OK, out.getvalue())
        return out.getvalue()

    def test_the_new_volume_has_the_marker_when_it_remounts(self) -> None:
        seen: list[bytes] = []

        def unmount(argv: tuple[str, ...]) -> None:
            seen.append((self.temp / NEVER_INDEX).read_bytes())
            self._unmount_temp(argv)

        self.fake.respond(self.step_argvs()["unmount temp"], effect=unmount, once=True)
        self._apply()
        self.assertEqual(seen, [b""])
        self.assertEqual((self.new_store / NEVER_INDEX).read_bytes(), b"")

    def test_the_old_volume_s_marker_is_not_copied(self) -> None:
        (self.path / NEVER_INDEX).write_bytes(b"")
        self._apply()
        copies = [call for call in self.fake.calls if call[0] == "ditto"]
        self.assertEqual(
            copies, [tuple(self.step_argvs()[name]) for name in ("ditto", "ditto sub")]
        )
        self.assertEqual(
            sorted(os.listdir(self.new_store)),
            sorted([NEVER_INDEX, "a.txt", "link", "sub"]),
        )

    def test_a_marker_with_content_on_the_old_volume_is_refused(self) -> None:
        (self.path / NEVER_INDEX).write_bytes(b"notes")
        with self.assertRaises(ToolError) as caught:
            sf_resize.run_resize(self.options(), self.fake, io.StringIO())
        self.assertIn(
            f"{self.path / NEVER_INDEX} is not the empty file", str(caught.exception)
        )


class DispatchTest(ResizeCase):
    def test_the_cli_routes_resize_here(self) -> None:
        self.assertIs(cli.MODES["resize"], sf_resize.run_resize)

    def test_finish_dispatches_to_the_finish_half(self) -> None:
        self.retire_old()
        out = io.StringIO()
        code = sf_resize.run_resize(self.options(finish=True), self.fake, out)
        self.assertEqual(code, EXIT_OK)
        self.assertIn(f"sudo diskutil apfs deleteVolume {OLD}", out.getvalue())


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
