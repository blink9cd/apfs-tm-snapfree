"""Tests for setup mode with --apply: each confirm, and the Spotlight marker's remount.

A command that exits zero but did not take is a failed step, and the empty
``.metadata_never_index`` setup creates must be on the volume when it is mounted
again. Shares ``ApplyFixture`` with ``test_sf_setup_apply.py``.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, List

import sf_fstab
from sf_errors import EXIT_FAIL, EXIT_OK
from sf_walk import NEVER_INDEX
from tests import tm_prefs
from tests.fake_host import FAKE_UUID_1, FAKE_UUID_2, container_entry, volume_entry
from tests.test_sf_setup import VOLUME, q
from tests.test_sf_setup_apply import ApplyFixture, Argv


class SpotlightMarkerTest(ApplyFixture):
    """The empty marker that keeps Spotlight off the volume, there when it remounts."""

    def test_the_marker_is_on_the_volume_before_it_remounts(self) -> None:
        self.script_apply()
        seen: List[bytes] = []

        def unmount(argv: Argv) -> None:
            seen.append((self.folder / NEVER_INDEX).read_bytes())
            self.unmount_volume(argv)

        self.fake.respond(self.argv("unmount"), effect=unmount, once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(seen, [b""])
        self.assertEqual((self.folder / NEVER_INDEX).read_bytes(), b"")
        self.assertFalse((self.aside / NEVER_INDEX).exists())
        self.assertIn(f"  7. touch {self.folder / NEVER_INDEX} ", out)
        self.assertIn(
            f"  8. sudo diskutil unmount {q(self.folder)} && sudo diskutil mount "
            f"-mountOptions nobrowse -mountPoint {q(self.folder)} {FAKE_UUID_2}",
            out,
        )

    def test_an_empty_marker_the_folder_already_held_is_copied_as_it_is(self) -> None:
        (self.folder / NEVER_INDEX).write_bytes(b"")
        self.script_apply()
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual((self.aside / NEVER_INDEX).read_bytes(), b"")
        self.assertEqual((self.folder / NEVER_INDEX).read_bytes(), b"")

    def test_a_refused_remount_prints_how_to_mount_the_volume_again(self) -> None:
        self.script_apply()
        self.fail_next("remount")
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn("FAILED at step 8:", out)
        recovery = out.split("Recovery, most recent first:\n", 1)[1].splitlines()
        self.assertEqual(
            [line.strip() for line in recovery[:4]],
            [
                f"sudo diskutil mount -mountOptions nobrowse -mountPoint "
                f"{q(self.folder)} {FAKE_UUID_2}  # step 8's command ran",
                f"sudo diskutil unmount {q(self.folder)}",
                f"rmdir {q(self.folder)} && mv {q(self.aside)} {q(self.folder)}",
                f"sudo diskutil apfs deleteVolume {FAKE_UUID_2}",
            ],
        )
        self.assertNotIn(self.argv("mdutil"), self.fake.calls)

    def test_entries_written_while_unmounted_stop_the_remount(self) -> None:
        # The mount would hide what reached the bare folder in between, and the copy
        # reads only the aside: refuse, as step 3 does, and name what is there.
        self.script_apply()

        def unmount_then_write(argv: Argv) -> None:
            self.unmount_volume(argv)
            (self.folder / "late.txt").write_bytes(b"late")

        self.fake.respond(self.argv("unmount"), effect=unmount_then_write, once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(
            f"FAILED at step 8: {self.folder} holds late.txt, written while the "
            f"volume was unmounted, which the mount would hide: move it into "
            f"{self.aside}, then run the recovery commands below",
            out,
        )
        self.assertNotIn(self.argv("remount"), self.fake.calls)
        recovery = out.split("Recovery, most recent first:\n", 1)[1].splitlines()
        self.assertEqual(
            recovery[0].strip(),
            f"sudo diskutil mount -mountOptions nobrowse -mountPoint "
            f"{q(self.folder)} {FAKE_UUID_2}  # step 8's command ran",
        )


class ConfirmTest(ApplyFixture):
    """A command that exits 0 but did not take is a failed step."""

    def run_failing(self) -> str:
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_FAIL, out)
        return out

    def test_no_new_volume_listed(self) -> None:
        self.script_validation()
        self.fake.respond(self.argv("add"))
        self.fake.respond_apfs_list([container_entry(volumes=[volume_entry()])])
        out = self.run_failing()
        self.assertIn("found 0 volumes", out)
        # addVolume ran, so its undo prints, with the UUID it could not learn.
        self.assertIn(
            "  sudo diskutil apfs deleteVolume <new-volume-uuid>  # step 1's command ran",
            out.splitlines(),
        )

    def test_two_volumes_of_the_name_listed(self) -> None:
        self.script_validation()
        self.fake.respond(self.argv("add"))
        twins = [
            volume_entry(uuid=FAKE_UUID_2, name=VOLUME),
            volume_entry(uuid=FAKE_UUID_1, name=VOLUME),
        ]
        self.fake.respond_apfs_list([container_entry(volumes=twins)])
        self.assertIn("found 2 volumes", self.run_failing())

    def test_a_mount_that_did_not_take(self) -> None:
        self.script_apply()
        self.fake.respond(self.argv("mount"))
        self.assertIn("is not mounted", self.run_failing())

    def test_a_mount_of_the_wrong_volume(self) -> None:
        self.script_apply(mounted=FAKE_UUID_1)
        self.assertIn(f"is volume {FAKE_UUID_1}", self.run_failing())

    def test_ownership_that_stayed_ignored(self) -> None:
        self.script_apply(enabled=False)
        self.assertIn("still ignores ownership", self.run_failing())

    def test_an_owner_or_mode_that_did_not_take(self) -> None:
        self.script_apply()

        def wrong_mode(_argv: Argv) -> None:
            os.chmod(self.folder, 0o700)

        self.fake.respond(self.argv("chmod"), effect=wrong_mode, once=True)
        self.assertIn("mode 0700 after chown and chmod", self.run_failing())

    def test_spotlight_still_on(self) -> None:
        self.script_apply()
        self.fake.respond_mdutil(self.folder, "/x:\n\tIndexing enabled.\n", once=True)
        self.assertIn("Spotlight", self.run_failing())

    def test_a_failed_mdutil_off_on_a_volume_read_disabled_passes(self) -> None:
        # Seen live: with the marker already keeping Spotlight off, mdutil -i off
        # exited 1, printing only a "disabling Spotlight" log line (fake values here).
        self.script_apply()
        stderr = (
            f"2026-01-02 03:04:05.678 mdutil[4242:424242] mdutil disabling Spotlight: "
            f"{self.folder} -> kMDConfigSearchLevelFSSearchOnly\n"
        )
        self.fake.respond(
            self.argv("mdutil"), returncode=1, stderr=stderr.encode(), once=True
        )
        self.fake.respond_mdutil(self.folder, "/x:\n\tIndexing disabled.\n", once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)

    def test_a_failed_mdutil_off_on_an_unclear_read_fails(self) -> None:
        self.script_apply()
        self.fake.respond(self.argv("mdutil"), returncode=1, stderr=b"boom", once=True)
        self.fake.respond_mdutil(self.folder, "/x: something new", once=True)
        out = self.run_failing()
        self.assertIn("FAILED at step 9:", out)
        self.assertIn("something new", out)

    def test_spotlight_status_that_reads_neither_way_is_let_through(self) -> None:
        # As in resize: mdutil's prose has no structured form, and text it cannot
        # read is left to --mode status to report rather than stopping the run.
        self.script_apply()
        self.fake.respond_mdutil(self.folder, "/x: something new", once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)

    def test_an_exclusion_that_did_not_take(self) -> None:
        # tmutil isexcluded reads a nobrowse volume excluded whatever addexclusion did,
        # so only Time Machine's own list proves the volume was excluded.
        self.script_apply()
        self.fake.respond_isexcluded(self.folder, excluded=True)
        tm_prefs.respond_excluded_uuids(
            self.fake, [FAKE_UUID_1], reads=tm_prefs.EVERY_POLL_READ
        )
        out = self.run_failing()
        self.assertIn("FAILED at step 6:", out)
        self.assertIn(f"ExcludedVolumeUUIDs does not list {FAKE_UUID_2}", out)

    def test_the_exclusion_runs_while_the_volume_is_mounted_visible(self) -> None:
        # Seen live on macOS 27: addexclusion -v on a nobrowse mount exits 0 and adds
        # nothing, so it must run before the remount that hides the volume.
        self.script_apply()
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        calls = self.fake.calls
        exclude = calls.index(self.argv("exclude"))
        self.assertLess(calls.index(self.argv("mount")), exclude)
        self.assertLess(exclude, calls.index(self.argv("remount")))
        self.assertIn(FAKE_UUID_2, self.exclusions.uuids)

    def test_an_fstab_that_did_not_change(self) -> None:
        self.script_apply()
        self.fake.respond(["sudo", "mv", "-f"], prefix=True, once=True)
        out = self.run_failing()
        self.assertIn("does not mount", out)
        restore = f"sudo cp {sf_fstab.backup_path(self.fstab)} {self.fstab}"
        self.assertIn(f"  {restore}  # step 10's command ran", out.splitlines())

    def test_a_short_copy(self) -> None:
        self.script_apply()
        self.fake.respond(["ditto"], prefix=True, once=True)
        self.assertIn("does not match", self.run_failing())

    def copy_then(self, change: Callable[[Path], None]) -> Callable[[Argv], None]:
        """A ``ditto`` that copies, then applies *change* to the copy."""

        def effect(argv: Argv) -> None:
            self.copy_tree(argv)
            change(self.fake._inside(argv[-1]))

        return effect

    def test_a_renamed_file_of_the_same_size_fails(self) -> None:
        # Counts and byte totals agree here; only the names show the copy is wrong.
        self.script_apply()

        def rename_one(dst: Path) -> None:
            (dst / "a.txt").rename(dst / "z.txt")

        rename = self.copy_then(rename_one)
        self.fake.respond(self.argv("ditto"), effect=rename, once=True)
        out = self.run_failing()
        self.assertIn("not in the copy: a.txt (file, 5 bytes)", out)

    def test_a_lost_empty_folder_fails(self) -> None:
        (self.folder / "empty").mkdir()
        self.script_apply()
        drop = self.copy_then(lambda dst: (dst / "empty").rmdir())
        self.fake.respond(self.argv("ditto"), effect=drop, once=True)
        self.assertIn("not in the copy: empty (folder)", self.run_failing())


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    import unittest

    unittest.main()
