"""Tests for an applied teardown: the whole run, the hidden-folder branch, and failures.

A failure is injected at every step that changes something, by a failing command or
by a command that exits zero without taking effect. Each must stop the run there,
print the completed steps' recovery commands most recent first, and leave the data
somewhere whole; above all, the volume is never deleted unless every earlier step
completed.
"""

import contextlib
import os
import stat
from pathlib import Path
from typing import Callable, ContextManager, List, NamedTuple, Optional
from unittest import mock

import sf_fstab
import sf_macos
import sf_teardown
from sf_errors import EXIT_FAIL, EXIT_OK, ToolError
from sf_host import COMMAND_TIMEOUT, COPY_TIMEOUT
from sf_walk import NEVER_INDEX, tree_stats
from tests import fake_host, tm_prefs
from tests.test_sf_teardown import (
    FSTAB_HEADER,
    OTHER_LINE,
    VOLUME_UUID,
    TeardownFixture,
    step_2_undo_line,
)

_DELETE = ("sudo", "diskutil", "apfs", "deleteVolume", VOLUME_UUID)
_NOTHING_TO_UNDO = "(no completed step needs undoing)"
#: The volume's deletion, and the check after it that the plain folder is backed up:
#: final steps, each of which prints only its own retry.
_DELETE_STEP = 7
_FINAL_RETRY = {_DELETE_STEP: " ".join(_DELETE), 8: "(nothing to retry)"}
#: Steps up to this one fail with the data still in the folder, on its volume.
_DATA_STILL_MOUNTED = 1
#: From this step on, a failure finds the data back in the folder, on the parent.
_DATA_BACK_IN_PLACE = 6


def recovery_lines(text: str) -> List[str]:
    """The recovery block's lines in run output *text*, stripped, header and footer cut."""
    block = text.split("\nRecovery", 1)[1].split("\n", 1)[1]
    body = block.split("Nothing was undone automatically", 1)[0]
    return [line.strip() for line in body.splitlines()]


class _Injection(NamedTuple):
    """One failure: what it is, how to cause it, and the step it must stop at."""

    name: str
    inject: Callable[[], ContextManager[object]]
    step: int
    #: Whether the step's command ran before it failed, so its own undo prints too.
    ran: bool = False
    #: Text the failure line must carry.
    hint: str = ""


class AppliedRunTest(TeardownFixture):
    def test_every_step_runs_in_order_and_the_folder_is_plain_again(self) -> None:
        fstab_before = self.fstab.read_text(encoding="utf-8")
        self.script_apply()
        self.assertEqual(self.teardown_run(apply=True), EXIT_OK)
        folder, fstab = str(self.folder), str(self.fstab)
        staged = str(sf_fstab.staged_path(self.fstab))
        self.assertEqual(
            self.fake.calls,
            [
                *self.info_calls(),
                ("ditto", f"{folder}/a.txt", f"{self.restore}/a.txt"),
                ("ditto", f"{folder}/sub", f"{self.restore}/sub"),
                ("sudo", "tmutil", "removeexclusion", "-v", folder),
                sf_macos.EXCLUDED_UUIDS_ARGV,
                ("sudo", "diskutil", "unmount", folder),
                ("sudo", "cp", fstab, str(sf_fstab.backup_path(self.fstab))),
                ("sudo", "install", "-m", "0644", "-o", "root", "-g", "wheel")
                + (str(self.fake.temps[0]), staged),
                ("sudo", "mv", "-f", staged, fstab),
                _DELETE,
                sf_macos.APFS_LIST_ARGV,
                tuple(sf_macos.isexcluded_argv(self.folder)),
            ],
        )
        first_copy = len(self.info_calls())
        self.assertEqual(
            self.fake.timeouts[first_copy : first_copy + 2], [COPY_TIMEOUT] * 2
        )
        self.assertEqual(set(self.fake.timeouts[first_copy + 2 :]), {COMMAND_TIMEOUT})
        self._assert_data_back_as_a_plain_folder()
        self.assertEqual(
            self.fstab.read_text(encoding="utf-8"), FSTAB_HEADER + OTHER_LINE
        )
        backup = sf_fstab.backup_path(self.fstab)
        self.assertEqual(backup.read_text(encoding="utf-8"), fstab_before)
        self.assertFalse(self.restore.exists())
        self.assertFalse(self.hidden.exists())
        text = self.out.getvalue()
        self.assertIn("Done: all 8 steps completed.", text)
        self.assertTrue(
            text.endswith(sf_teardown.DONE_NOTE.format(path=self.folder) + "\n")
        )

    def _assert_data_back_as_a_plain_folder(self) -> None:
        self.assertTrue(self.folder.is_dir())
        self.assertFalse(sf_fstab.is_mount_point(self.folder, self.fake.device_of))
        self.assertEqual(
            self.fake.device_of(self.folder), self.fake.device_of(self.tmp)
        )
        self.assertEqual(tree_stats(self.folder), self.original)
        self.assertEqual(
            (self.folder / "sub" / "b.txt").read_text(encoding="utf-8"), "beta beta"
        )
        self.assertTrue((self.folder / "link").is_symlink())

    def test_data_written_while_unmounted_is_moved_aside_not_lost(self) -> None:
        self.script_apply(late=["late.txt"])
        self.assertEqual(self.teardown_run(apply=True), EXIT_OK)
        self._assert_data_back_as_a_plain_folder()
        self.assertEqual((self.hidden / "late.txt").read_text(encoding="utf-8"), "late")
        text = self.out.getvalue()
        self.assertIn(str(self.hidden), text)
        self.assertIn("written while the volume was not mounted", text)

    def test_a_plain_folder_still_excluded_says_the_data_is_safe(self) -> None:
        self.script_apply()
        self.fake.respond_isexcluded(self.folder, excluded=True, once=True)
        self.assertEqual(self.teardown_run(apply=True), EXIT_FAIL)
        text = self.out.getvalue()
        self.assertIn(
            f"FAILED at step 8: {self.folder} is a plain folder again and its data is "
            "safe there, but Time Machine still excludes it.",
            text,
        )
        self.assertIn(f"tmutil isexcluded -X {self.folder}", text)
        self.assertIn(f"sudo tmutil removeexclusion -p {self.folder}", text)
        self.assertEqual(recovery_lines(text), ["(nothing to retry)"])
        self._assert_data_back_as_a_plain_folder()

    def test_a_failed_delete_after_the_hidden_branch_only_says_retry(self) -> None:
        # The data is restored by then: undoing steps 1 to 6 would take it away
        # again. The hidden folder was named when step 4 moved it.
        self.script_apply(late=["late.txt"])
        self.fake.respond(list(_DELETE), once=True, returncode=1)
        self.assertEqual(self.teardown_run(apply=True), EXIT_FAIL)
        text = self.out.getvalue()
        self.assertEqual(recovery_lines(text), [" ".join(_DELETE)])
        self.assertIn(f"moved it to {self.hidden}", text)

    def test_a_write_after_the_copy_stops_the_run_before_the_unmount(self) -> None:
        # Unmounting hides whatever reached the volume after the copy, and the last
        # step deletes the volume, so a late write must stop the run at step 3.
        self.script_apply()

        def late_write(_argv: "tuple[str, ...]") -> None:
            (self.folder / "sub" / "late.txt").write_bytes(b"written late")

        self.fake.respond(
            ["sudo", "tmutil", "removeexclusion", "-v", str(self.folder)],
            effect=late_write,
            once=True,
        )
        self.assertEqual(self.teardown_run(apply=True), EXIT_FAIL)
        text = self.out.getvalue()
        self.assertIn(
            f"FAILED at step 3: {self.folder} no longer matches the copy at "
            f"{self.restore}: something wrote to {self.folder} after the copy "
            "(not in the copy: sub/late.txt (file, 12 bytes))",
            text,
        )
        self.assertNotIn(
            ("sudo", "diskutil", "unmount", str(self.folder)), self.fake.calls
        )
        self.assertEqual(
            recovery_lines(text),
            [
                step_2_undo_line(self.folder),
                f"# the restore copy at {self.restore} can be removed once "
                f"{self.folder} is checked",
            ],
        )
        self.assertTrue(sf_fstab.is_mount_point(self.folder, self.fake.device_of))

    def test_the_warning_to_quit_writers_comes_before_the_steps(self) -> None:
        self.teardown_run()
        text = self.out.getvalue()
        warning = text.index(f"Quit anything that writes to {self.folder} first")
        self.assertLess(warning, text.index("  1. "))
        self.assertIn("which teardown deletes", text[warning : text.index("  1. ")])


class VolumePrivateEntriesTest(TeardownFixture):
    """macOS's own root-only folders at the volume root are not the data."""

    def setUp(self) -> None:
        super().setUp()
        self.folder.chmod(0o750)
        self.add_volume_private()
        (self.folder / ".Trashes").mkdir()

    def test_the_dry_run_validates_and_lists_only_the_data(self) -> None:
        self.assertEqual(self.teardown_run(), EXIT_OK)
        text = self.out.getvalue()
        self.assertIn(f"ditto {self.folder}/a.txt {self.restore}/a.txt", text)
        self.assertNotIn(f"{self.folder}/.fseventsd", text)

    def test_the_warning_names_the_ones_present_and_says_they_go(self) -> None:
        self.teardown_run()
        lines = self.out.getvalue().splitlines()
        self.assertEqual(
            lines[2],
            f"macOS's own .Trashes, .fseventsd at the top of {self.folder} are not "
            "copied: they and their contents, Versions history "
            "(.DocumentRevisions-V100) and the volume's Trash included, go with the "
            "volume when teardown deletes it.",
        )
        self.assertTrue(lines[3].startswith("  1. "), lines[3])

    def test_an_applied_run_restores_the_data_without_them(self) -> None:
        self.script_apply()
        self.assertEqual(self.teardown_run(apply=True), EXIT_OK, self.out.getvalue())
        self.assertFalse(os.path.lexists(self.folder / ".fseventsd"))
        self.assertEqual(tree_stats(self.folder), self.original)
        self.assertTrue((self.folder / "link").is_symlink())
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), 0o750)
        copies = [call for call in self.fake.calls if call[0] == "ditto"]
        self.assertEqual(
            copies,
            [
                ("ditto", f"{self.folder}/{name}", f"{self.restore}/{name}")
                for name in ("a.txt", "sub")
            ],
        )


class SpotlightMarkerTest(TeardownFixture):
    """The tool's own empty marker at the volume root goes with the volume."""

    def test_it_is_named_in_the_notes_and_not_carried_back(self) -> None:
        (self.folder / NEVER_INDEX).write_bytes(b"")
        self.script_apply()
        self.assertEqual(self.teardown_run(apply=True), EXIT_OK, self.out.getvalue())
        lines = self.out.getvalue().splitlines()
        self.assertEqual(
            lines[2],
            f"This tool's empty {NEVER_INDEX} at the top of {self.folder}, which "
            "keeps Spotlight off the volume, is not copied: it goes with the volume, "
            f"and Spotlight then treats {self.folder} as the parent volume says.",
        )
        self.assertFalse(os.path.lexists(self.folder / NEVER_INDEX))
        self.assertEqual(tree_stats(self.folder), self.original)

    def test_no_note_when_the_volume_has_none(self) -> None:
        self.teardown_run()
        self.assertNotIn(NEVER_INDEX, self.out.getvalue().split("  1. ", 1)[0])

    def test_one_with_content_is_refused_before_anything_changes(self) -> None:
        (self.folder / NEVER_INDEX).write_bytes(b"notes")
        with self.assertRaisesRegex(ToolError, "is not the empty file"):
            self.teardown_run(apply=True)
        self.assertEqual(self.fake.calls[-2:], self.fake.volume_reads(self.folder))


class InjectedFailureTest(TeardownFixture):
    def _recovery_lines(self) -> List[str]:
        """Every step's recovery line, step 1's first, for the empty-folder path."""
        folder, restore = self.folder, self.restore
        return [
            f"# the restore copy at {restore} can be removed once {folder} is checked",
            step_2_undo_line(folder),
            f"sudo diskutil mount -mountOptions nobrowse -mountPoint {folder} {VOLUME_UUID}",
            f"mkdir {folder}",
            f"mv {folder} {restore}",
            f"sudo cp {sf_fstab.backup_path(self.fstab)} {self.fstab}",
        ]

    def _respond_once(
        self,
        argv: List[str],
        *,
        prefix: bool = False,
        fail: bool = False,
        effect: Optional[fake_host.Effect] = None,
    ) -> ContextManager[object]:
        """Queue one reply for *argv*: a refusal with *fail*, else a bare success."""
        self.fake.respond(
            argv,
            once=True,
            prefix=prefix,
            returncode=1 if fail else 0,
            stderr=b"refused" if fail else b"",
            effect=effect,
        )
        return contextlib.nullcontext()

    def _injections(self) -> List[_Injection]:
        folder = str(self.folder)
        unmount = ["sudo", "diskutil", "unmount", folder]

        def still_excluded() -> ContextManager[object]:
            tm_prefs.respond_excluded_uuids(
                self.fake, [VOLUME_UUID.lower()], reads=tm_prefs.EVERY_POLL_READ
            )
            return contextlib.nullcontext()

        def plain_folder_excluded() -> ContextManager[object]:
            # Step 2 reads Time Machine's list, not isexcluded, so only the check
            # after the volume is gone takes this reply.
            self.fake.respond_isexcluded(self.folder, excluded=True, once=True)
            return contextlib.nullcontext()

        def hidden_taken() -> ContextManager[object]:
            self.hidden.mkdir()
            return self._respond_once(unmount, effect=self._unmount(["late.txt"]))

        def still_listed() -> ContextManager[object]:
            self.fake.respond_apfs_list([self.container(with_volume=True)], once=True)
            return contextlib.nullcontext()

        return [
            _Injection(
                "ditto fails",
                lambda: self._respond_once(["ditto"], prefix=True, fail=True),
                1,
                ran=True,
            ),
            _Injection(
                "ditto copies nothing",
                lambda: self._respond_once(["ditto"], prefix=True),
                1,
                ran=True,
            ),
            _Injection(
                "removeexclusion fails",
                lambda: self._respond_once(
                    ["sudo", "tmutil", "removeexclusion", "-v", folder], fail=True
                ),
                2,
            ),
            _Injection("still excluded", still_excluded, 2, ran=True),
            _Injection(
                "unmount fails", lambda: self._respond_once(unmount, fail=True), 3
            ),
            _Injection(
                "unmount busy, with its hint",
                lambda: self._respond_once(unmount, fail=True),
                3,
                hint=f"lsof +D {folder}",
            ),
            _Injection(
                "unmount does not take", lambda: self._respond_once(unmount), 3, True
            ),
            _Injection("hidden path taken", hidden_taken, 4),
            _Injection(
                "rename refused",
                lambda: mock.patch("os.rename", side_effect=PermissionError("denied")),
                5,
            ),
            _Injection(
                "fstab install fails",
                lambda: self._respond_once(["sudo", "install"], prefix=True, fail=True),
                6,
            ),
            _Injection(
                "fstab rename does not take",
                lambda: self._respond_once(["sudo", "mv", "-f"], prefix=True),
                6,
                ran=True,
            ),
            _Injection(
                "deleteVolume fails",
                lambda: self._respond_once(list(_DELETE), fail=True),
                7,
            ),
            _Injection("volume still listed", still_listed, 7),
            _Injection(
                "the plain folder is still excluded",
                plain_folder_excluded,
                8,
                hint=f"sudo tmutil removeexclusion -p {folder}",
            ),
        ]

    def test_each_failure_stops_there_and_prints_the_recovery(self) -> None:
        for index in range(len(self._injections())):
            self.fresh()
            self.script_apply()
            injection = self._injections()[index]
            with self.subTest(injection.name), injection.inject():
                code = self.teardown_run(apply=True)
                self._assert_stopped_at(injection.step, code, ran=injection.ran)
                failed = next(
                    line
                    for line in self.out.getvalue().splitlines()
                    if "FAILED" in line
                )
                self.assertIn(injection.hint, self.out.getvalue().split(failed)[1])

    def _assert_stopped_at(self, step: int, code: int, *, ran: bool) -> None:
        text = self.out.getvalue()
        self.assertEqual(code, EXIT_FAIL)
        self.assertIn(f"FAILED at step {step}:", text)
        self.assertNotIn(f"  {step + 1}. ", text)
        expected = list(reversed(self._recovery_lines()[: step - 1]))
        if step in _FINAL_RETRY:
            # The data is restored by then: only the failed step itself is retried.
            expected = [_FINAL_RETRY[step]]
        elif ran:
            own = self._recovery_lines()[step - 1]
            expected.insert(0, f"{own}  # step {step}'s command ran")
        self.assertEqual(recovery_lines(text), expected or [_NOTHING_TO_UNDO])
        if step < _DELETE_STEP:
            self.assertNotIn(_DELETE, self.fake.calls)
        self.assertEqual(tree_stats(self._where_the_data_is(step)), self.original)

    def _where_the_data_is(self, step: int) -> Path:
        if step <= _DATA_STILL_MOUNTED or step >= _DATA_BACK_IN_PLACE:
            return self.folder
        return self.restore
