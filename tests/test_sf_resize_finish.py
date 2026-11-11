"""Tests for ``--mode resize --finish``: deleting the volume a resize retired.

The fixture's machine starts in the state a completed resize leaves (``retire_old``):
NEW mounted at the folder and named by its fstab line, OLD unmounted and named by the
retired-volume marker.
"""

from __future__ import annotations

import io
import unittest

import sf_macos
import sf_resize_finish
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_fstab import entry_for, marker_for
from sf_host import format_argv
from tests import fake_host
from tests.fake_host import FAKE_CONTAINER
from tests.resize_fixture import NEW, NEW_NAME, OLD, OTHER, ResizeCase, write_file

_DRY_FOOTER = (
    "Dry run: nothing was changed. Re-run with --apply to perform these steps.\n"
)


class FinishCase(ResizeCase):
    def setUp(self) -> None:
        super().setUp()
        self.retire_old()

    def _run(self, *, apply: bool) -> tuple[int, str]:
        out = io.StringIO()
        opts = self.options(finish=True, apply=apply)
        return sf_resize_finish.run_finish(opts, self.fake, out), out.getvalue()

    def _check_line(self) -> str:
        """Step 3 as it prints: the read of Time Machine's exclusions."""
        return (
            f"check Time Machine's exclusions for the deleted volume {OLD}: "
            f"{format_argv(sf_macos.EXCLUDED_UUIDS_ARGV)}"
        )

    def _rewrite_lines(self) -> str:
        fstab = self.fstab
        return (
            f"sudo cp {fstab} {fstab}.apfs_tm_snapfree.bak; "
            f"sudo install -m 0644 -o root -g wheel <temp> "
            f"{fstab}.apfs_tm_snapfree.new; "
            f"sudo mv -f {fstab}.apfs_tm_snapfree.new {fstab}"
        )


class FinishValidateTest(FinishCase):
    def _refused(self, *needles: str) -> None:
        with self.assertRaises(ToolError) as caught:
            self._run(apply=True)
        for needle in needles:
            self.assertIn(needle, str(caught.exception))
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))

    def test_no_marker_means_nothing_to_finish(self) -> None:
        write_file(self.fstab, f"UUID={NEW} {self.path} apfs rw,nobrowse\n")
        self._refused("no retired volume", str(self.path))

    def test_a_marker_naming_the_mounted_volume_is_refused(self) -> None:
        write_file(
            self.fstab,
            f"UUID={OLD} {self.path} apfs rw,nobrowse\n"
            f"# apfs_tm_snapfree: retired UUID={OLD.upper()} for {self.path}\n",
        )
        self._refused(OLD, "fstab")

    def test_a_marker_with_no_fstab_line_is_refused(self) -> None:
        # Without the line, nothing proves the data now lives on another volume.
        write_file(
            self.fstab, f"# apfs_tm_snapfree: retired UUID={OLD} for {self.path}\n"
        )
        self._refused("no line", str(self.path))

    def test_a_folder_with_nothing_mounted_is_refused(self) -> None:
        self.fake.unmount(self.path)
        self._refused("not mounted", str(self.path))

    def test_a_mounted_volume_the_fstab_does_not_name_is_refused(self) -> None:
        self.infos[str(self.path)]["volume_uuid"] = OTHER
        self._refused(OTHER, NEW)

    def test_the_mounted_volume_being_the_retired_one_is_refused(self) -> None:
        # The fstab line was put back by hand, but the marker was left behind.
        write_file(
            self.fstab,
            f"UUID={OLD} {self.path} apfs rw,nobrowse\n"
            f"# apfs_tm_snapfree: retired UUID={OLD} for {self.path}\n",
        )
        self.infos[str(self.path)]["volume_uuid"] = OLD
        self._refused(OLD, "refusing")

    def test_a_retired_volume_in_another_container_is_refused(self) -> None:
        old = next(v for v in self.volumes if v["APFSVolumeUUID"] == OLD)
        here = [v for v in self.volumes if v is not old]
        self.fake.respond_apfs_list(
            [
                fake_host.container_entry(volumes=here),
                fake_host.container_entry(reference="disk8", volumes=[old]),
            ],
            once=True,
        )
        self._refused(OLD, "disk8", FAKE_CONTAINER)

    def test_a_retired_volume_named_unlike_the_folder_volume_is_refused(self) -> None:
        old = next(v for v in self.volumes if v["APFSVolumeUUID"] == OLD)
        old["Name"] = "Macintosh HD - Data"
        self._refused(OLD, "Macintosh HD - Data", NEW_NAME)

    def test_a_retired_volume_with_a_role_is_refused(self) -> None:
        old = next(v for v in self.volumes if v["APFSVolumeUUID"] == OLD)
        old["Roles"] = ["Data"]
        self._refused(OLD, "Data", "role")

    def test_a_retired_volume_with_no_roles_is_finished(self) -> None:
        for volume in self.volumes:
            volume["Roles"] = []
        code, _ = self._run(apply=False)
        self.assertEqual(code, EXIT_OK)

    def test_a_mounted_volume_missing_from_the_listing_is_refused(self) -> None:
        self.volumes[:] = [v for v in self.volumes if v["APFSVolumeUUID"] != NEW]
        self._refused(NEW, "reports no volume")

    def test_a_lower_case_marker_still_finds_the_volume(self) -> None:
        # diskutil lists the retired volume in upper case; the marker, edited by
        # hand, spells it in lower case.
        lettered = "00000000-0000-4000-8000-00000000ABCD"
        old = next(v for v in self.volumes if v["APFSVolumeUUID"] == OLD)
        old["APFSVolumeUUID"] = lettered
        write_file(
            self.fstab,
            f"UUID={NEW} {self.path} apfs rw,nobrowse\n"
            f"# apfs_tm_snapfree: retired UUID={lettered.lower()} for {self.path}\n",
        )
        self.infos[lettered.lower()] = self.infos[OLD]
        code, _ = self._run(apply=False)
        self.assertEqual(code, EXIT_OK)

    def test_a_retired_volume_that_no_longer_exists_is_refused(self) -> None:
        self.volumes[:] = [vol for vol in self.volumes if vol["APFSVolumeUUID"] != OLD]
        self._refused(OLD, "no longer exists")

    def test_a_retired_volume_that_is_mounted_is_refused(self) -> None:
        self.infos[OLD]["mount_point"] = "/Volumes/old"
        self._refused(OLD, "/Volumes/old")

    def test_an_fstab_line_mounting_the_retired_volume_elsewhere_is_refused(
        self,
    ) -> None:
        # Unmounted now, but the next boot would mount it there: someone uses it.
        elsewhere = self.tmp / "elsewhere"
        before = self.fstab.read_text(encoding="utf-8")
        write_file(
            self.fstab, before + f"UUID={OLD.upper()} {elsewhere} apfs rw,nobrowse\n"
        )
        self._refused(OLD, str(elsewhere), "refusing to delete it")

    def test_an_unreadable_fstab_is_refused(self) -> None:
        self.fstab.unlink()
        self.fstab.mkdir()
        self._refused("could not read")


class FinishRunTest(FinishCase):
    def test_the_dry_run_prints_both_steps_and_changes_nothing(self) -> None:
        before = self.fstab.read_text()
        code, text = self._run(apply=False)
        self.assertEqual(code, EXIT_OK)
        self.assertEqual(
            text,
            f"Finish the resize of {self.path}: delete the retired volume {OLD}.\n"
            f"  1. sudo diskutil apfs deleteVolume {OLD}\n"
            f"  2. in {self.fstab}, remove the marker retiring {OLD}: "
            f"{self._rewrite_lines()}\n"
            f"  3. {self._check_line()}\n" + _DRY_FOOTER,
        )
        self.assertEqual(self.fstab.read_text(), before)
        self.assertFalse(any(call[0] == "sudo" for call in self.fake.calls))

    def test_apply_deletes_the_volume_then_the_marker(self) -> None:
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_OK, text)
        fstab, staged = str(self.fstab), f"{self.fstab}.apfs_tm_snapfree.new"
        listing = ("diskutil", "apfs", "list", "-plist")
        self.assertEqual(
            self.fake.calls,
            [
                *self.fake.volume_reads(self.path),
                listing,
                ("diskutil", "info", "-plist", OLD),
                ("sudo", "diskutil", "apfs", "deleteVolume", OLD),
                listing,
                ("sudo", "cp", fstab, f"{fstab}.apfs_tm_snapfree.bak"),
                ("sudo", "install", "-m", "0644", "-o", "root", "-g", "wheel")
                + (str(self.fake.temps[0]), staged),
                ("sudo", "mv", "-f", staged, fstab),
                sf_macos.EXCLUDED_UUIDS_ARGV,
            ],
        )
        text_after = self.fstab.read_text()
        entry = entry_for(text_after, self.path)
        assert entry is not None
        self.assertEqual(entry.volume_id, NEW)
        self.assertIsNone(marker_for(text_after, self.path))
        self.assertIn("Done: all 3 steps completed.", text)

    def test_a_refused_delete_stops_before_the_fstab(self) -> None:
        self.refuse(self.step_argvs()["deleteVolume"])
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 1:", text)
        self.assertIn("(no completed step needs undoing)", text)
        self.assertEqual(
            self.fake.calls[-1][:4], ("sudo", "diskutil", "apfs", "deleteVolume")
        )

    def test_a_volume_still_listed_after_the_delete(self) -> None:
        self.ignore(self.step_argvs()["deleteVolume"])
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 1:", text)
        self.assertIn("still", text)
        # The hand edit of the marker is for a volume known to be gone; this one
        # is still listed, so nothing is offered.
        self.assertIn("(no completed step needs undoing)", text)
        self.assertNotIn("by hand", text)

    def test_a_refused_marker_removal_names_the_hand_edit(self) -> None:
        self.fake.respond(["sudo", "mv", "-f"], returncode=1, prefix=True, once=True)
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 2:", text)
        self.assertIn(f"\n  # remove the marker retiring {OLD} from {self.fstab}", text)

    def test_a_marker_still_present_after_the_rewrite(self) -> None:
        self.ignore(["sudo", "mv", "-f"], prefix=True)
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 2:", text)
        self.assertIn("marker", text)

    def test_a_deleted_volume_still_excluded_is_noted_never_edited(self) -> None:
        # tmutil removeexclusion -v needs a mounted volume, so nothing the tool may
        # run unlists a deleted one; it says so, and leaves Time Machine's file alone.
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_OK, text)
        note = next(line for line in text.splitlines() if "Note:" in line)
        self.assertIn(f"still lists the deleted volume {OLD}", note)
        self.assertIn("harmless", note)
        self.assertIn("System Settings > General > Time Machine > Options", note)
        self.assertFalse(any("PlistBuddy" in " ".join(c) for c in self.fake.calls))
        self.assertFalse(any("defaults" in c for c in self.fake.calls))

    def test_no_note_when_the_deleted_volume_is_not_listed(self) -> None:
        self.excluded_uuids[:] = [NEW]
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_OK, text)
        self.assertNotIn("Note:", text)

    def test_a_failed_exclusions_read_prints_only_its_own_retry(self) -> None:
        # The volume is deleted and the marker gone by then: nothing to undo.
        self.fake.respond(
            list(sf_macos.EXCLUDED_UUIDS_ARGV), returncode=1, stderr=b"boom", once=True
        )
        code, text = self._run(apply=True)
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn("FAILED at step 3:", text)
        self.assertIn("retry this one:", text)
        self.assertIn(f"  {format_argv(sf_macos.EXCLUDED_UUIDS_ARGV)}\n", text)
        self.assertIsNone(marker_for(self.fstab.read_text(), self.path))

    def test_an_unrelated_volume_is_never_touched(self) -> None:
        self._run(apply=True)
        self.assertFalse(any(OTHER in call for call in self.fake.calls))


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
