"""Tests for setup mode with --apply: the happy path, each failure and each confirm.

Shares ``SetupFixture`` with ``test_sf_setup.py``. The folder's rename, its recreation,
the copy and the fstab rewrite act on real files in the test's temporary folder; every
command is a scripted reply, and ``ditto`` is emulated by a copy inside that folder.
"""

from __future__ import annotations

import os
import shutil
import stat
from typing import Callable, List, Optional, Tuple
from unittest import mock

import sf_fstab
from sf_errors import EXIT_FAIL, EXIT_OK
from sf_host import COMMAND_TIMEOUT, COPY_TIMEOUT
from sf_macos import APFS_LIST_ARGV, EXCLUDED_UUIDS_ARGV, mdutil_status_argv
from tests import tm_prefs
from tests.fake_host import (
    FAKE_CONTAINER,
    FAKE_UUID_1,
    FAKE_UUID_2,
    container_entry,
    volume_entry,
)
from tests.test_sf_setup import EXISTING_LINE, FOLDER_MODE, VOLUME, SetupFixture, q

Argv = Tuple[str, ...]
#: Where the new volume's root entries are kept while it is unmounted.
_VOLUME_STORE = "new-volume-store"


class ApplyFixture(SetupFixture):
    """Scripts every reply a successful applied setup needs."""

    def argv(self, name: str) -> Argv:
        """The argv of one named command of the plan."""
        folder, aside = str(self.folder), str(self.aside)
        table = {
            "list": APFS_LIST_ARGV,
            "add": (
                "sudo",
                "diskutil",
                "apfs",
                "addVolume",
                FAKE_CONTAINER,
                "APFS",
                VOLUME,
                "-quota",
                "40g",
                "-nomount",
            ),  # fmt: skip
            # The first mount is visible, so addexclusion -v takes (ruling D8).
            "mount": (
                "sudo",
                "diskutil",
                "mount",
                "-mountPoint",
                folder,
                FAKE_UUID_2,
            ),  # fmt: skip
            "remount": (
                "sudo",
                "diskutil",
                "mount",
                "-mountOptions",
                "nobrowse",
                "-mountPoint",
                folder,
                FAKE_UUID_2,
            ),  # fmt: skip
            "unmount": ("sudo", "diskutil", "unmount", folder),
            "own": ("sudo", "diskutil", "enableOwnership", folder),
            "chown": ("sudo", "chown", f"{self.uid}:{self.gid}", folder),
            "chmod": ("sudo", "chmod", "0750", folder),
            "mdutil": ("sudo", "mdutil", "-i", "off", folder),
            "mdstatus": tuple(mdutil_status_argv(self.folder)),
            "exclude": ("sudo", "tmutil", "addexclusion", "-v", folder),
            "excluded": EXCLUDED_UUIDS_ARGV,
            "ditto": ("ditto", f"{aside}/", f"{folder}/"),
        }
        return table[name]

    def rewrite(self, *, backup: bool = True) -> List[Argv]:
        """The fstab rewrite's commands, with the fake's first temporary file."""
        temp = str(self.tmp / "fake-temp-1.txt")
        argvs = sf_fstab.rewrite_argvs(self.fstab, temp, backup=backup)
        return [tuple(argv) for argv in argvs]

    def happy_calls(
        self, *, ownership: bool = False, backup: bool = True
    ) -> List[Argv]:
        """Every call a successful setup makes, in order."""
        a, info = self.argv, self.fake.volume_reads(self.folder)
        owning = [*info, a("own"), *info] if not ownership else info
        return [
            *info, a("list"), a("add"), a("list"), a("mount"), *info,
            *owning, a("chown"), a("chmod"), a("exclude"), a("excluded"),
            a("unmount"), a("remount"), *info, a("mdutil"), a("mdstatus"),
            *self.rewrite(backup=backup), a("ditto"),
        ]  # fmt: skip

    def new_volume_info(
        self, *, ownership: bool = True, uuid: str = FAKE_UUID_2
    ) -> None:
        """Queue one ``diskutil info`` reply describing the new volume at the folder."""
        self.fake.respond_info(
            self.folder,
            once=True,
            mount_point=str(self.folder),
            volume_uuid=uuid,
            ownership=ownership,
            name=VOLUME,
        )

    def script_apply(
        self,
        *,
        ownership: bool = False,
        mounted: str = FAKE_UUID_2,
        enabled: bool = True,
    ) -> None:
        """Script a setup that succeeds; a test overrides one reply to fail it.

        *ownership* is whether the new volume honors ownership when first mounted,
        *mounted* the UUID the folder reports once mounted, and *enabled* whether
        ``enableOwnership`` took.
        """
        self.script_validation()
        volumes = [
            volume_entry(uuid=FAKE_UUID_1, name="Data"),
            volume_entry(uuid=FAKE_UUID_2, name=VOLUME, device="disk9s7"),
        ]
        self.fake.respond_apfs_list([container_entry(volumes=volumes)])
        self.fake.respond(self.argv("add"))
        self.fake.respond(
            ["sudo", "diskutil", "mount"], prefix=True, effect=self.mount_volume
        )
        self.new_volume_info(ownership=ownership, uuid=mounted)
        self.new_volume_info(ownership=ownership)
        if not ownership:
            self.fake.respond(self.argv("own"))
            self.new_volume_info(ownership=enabled)
        # The remount's confirm: the volume back at the folder after the marker.
        self.new_volume_info()
        self.fake.respond(self.argv("unmount"), effect=self.unmount_volume)
        for name in ("chown", "chmod", "mdutil"):
            self.fake.respond(self.argv(name))
        self.fake.respond_mdutil(self.folder, "/x:\n\tIndexing disabled.\n")
        # Time Machine's own list, with an old volume's stale entry left in it; the
        # exclusion lists the new volume only while it is mounted visible, as live.
        self.exclusions = tm_prefs.ExcludedList(self.fake, [FAKE_UUID_1])
        self.fake.respond(self.argv("exclude"), effect=self.exclusions.add)
        self.fake.emulate_file_commands()
        self.fake.respond(["ditto"], prefix=True, effect=self.copy_tree)

    def mount_volume(self, _argv: Argv) -> None:
        """Mount the new volume at the folder, its root entries back in view."""
        self.fake.mount(self.folder)
        store = self.tmp / _VOLUME_STORE
        if store.is_dir():
            for child in store.iterdir():
                os.replace(child, self.folder / child.name)

    def unmount_volume(self, _argv: Argv) -> None:
        """Unmount it: its root entries go with it, leaving the folder empty."""
        self.fake.unmount(self.folder)
        store = self.tmp / _VOLUME_STORE
        store.mkdir(exist_ok=True)
        for child in self.folder.iterdir():
            os.replace(child, store / child.name)

    def copy_tree(self, argv: Argv) -> None:
        """``ditto SRC/ DST/``, done as a copy inside the temporary folder."""
        source, target = (self.fake._inside(raw) for raw in argv[-2:])
        shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)

    def exclude_then(self, change: Callable[[], None]) -> None:
        """Let the next exclusion take as usual, then apply *change*."""

        def effect(argv: Argv) -> None:
            self.exclusions.add(argv)
            change()

        self.fake.respond(self.argv("exclude"), effect=effect, once=True)

    def fail_next(self, name: str) -> None:
        """Make the next run of the named command exit 1."""
        self.fake.respond(self.argv(name), returncode=1, stderr=b"boom", once=True)


class HappyPathTest(ApplyFixture):
    def test_every_step_runs_in_order_and_the_original_is_kept(self) -> None:
        self.script_apply()
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake.calls, self.happy_calls())
        line = sf_fstab.render_line(FAKE_UUID_2, self.folder)
        self.assertEqual(
            self.fstab.read_text(encoding="utf-8"), f"{EXISTING_LINE}{line}\n"
        )
        self.assertEqual(
            (self.folder / "sub" / "b.txt").read_text(encoding="utf-8"), "world!"
        )
        self.assertTrue((self.aside / "a.txt").is_file())
        self.assertEqual(stat.S_IMODE(os.stat(self.folder).st_mode), FOLDER_MODE)
        self.assertIn(f"  3. sudo diskutil mount -mountPoint {q(self.folder)} "
                      f"{FAKE_UUID_2}", out)  # fmt: skip
        self.assertEqual(self.exclusions.uuids, [FAKE_UUID_1, FAKE_UUID_2])
        self.assertIn(f"add `{line}` to {self.fstab}:", out)
        self.assertIn(f"  rm -rf {q(self.aside)}", out)
        self.assertIn(f"--mode status --path {q(self.folder)}", out)
        self.assertIn("Done: all 12 steps completed.", out)

    def test_a_system_folder_on_the_new_volume_is_not_counted(self) -> None:
        self.script_apply()

        def copy_and_index(argv: Argv) -> None:
            self.copy_tree(argv)
            (self.folder / ".fseventsd").mkdir()
            (self.folder / ".fseventsd" / "log").write_bytes(b"x")

        self.fake.respond(self.argv("ditto"), effect=copy_and_index, once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)

    def test_a_root_only_system_folder_on_the_new_volume_is_not_read(self) -> None:
        # The real .fseventsd is drwx------ root; mode 000 stands in for that. The
        # confirm walks only the aside's own top-level names, so it never opens it.
        self.script_apply()
        locked = self.folder / ".fseventsd"

        def copy_and_lock(argv: Argv) -> None:
            self.copy_tree(argv)
            locked.mkdir()
            locked.chmod(0)

        def unlock() -> None:
            if locked.is_dir():
                locked.chmod(0o700)

        self.addCleanup(unlock)
        self.fake.respond(self.argv("ditto"), effect=copy_and_lock, once=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)

    def test_ditto_runs_as_the_user_with_the_copy_timeout(self) -> None:
        self.script_apply()
        self.run_setup(self.options(apply=True))
        index = self.fake.calls.index(self.argv("ditto"))
        self.assertEqual(self.fake.timeouts[index], COPY_TIMEOUT)
        others = self.fake.timeouts[:index]
        self.assertEqual(set(others), {COMMAND_TIMEOUT})

    def test_ownership_already_honored_skips_enable_ownership(self) -> None:
        self.script_apply(ownership=True)
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake.calls, self.happy_calls(ownership=True))

    def test_an_absent_fstab_is_created_without_a_backup(self) -> None:
        self.fstab.unlink()
        self.script_apply()
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_OK, out)
        self.assertEqual(self.fake.calls, self.happy_calls(backup=False))
        line = sf_fstab.render_line(FAKE_UUID_2, self.folder)
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), f"{line}\n")


class FailureTest(ApplyFixture):
    """A failure at each mutating step: exit 1, recovery in reverse, nothing after."""

    def recoveries(self, *, backup: bool = True) -> List[str]:
        """Every recovery line, most recent first, as a failure at the end prints them."""
        restore = (
            f"sudo cp {sf_fstab.backup_path(self.fstab)} {self.fstab}"
            if backup
            else f"sudo rm {self.fstab}"
        )
        return [
            restore,
            f"sudo diskutil unmount {q(self.folder)}",
            f"rmdir {q(self.folder)} && mv {q(self.aside)} {q(self.folder)}",
            f"sudo diskutil apfs deleteVolume {FAKE_UUID_2}",
        ]

    def assert_failed(self, last: Optional[Argv], undone: int, step: int) -> str:
        """Exit 1 at *step*, the last *undone* recoveries, nothing run after *last*."""
        code, out = self.run_setup(self.options(apply=True))
        self.assertEqual(code, EXIT_FAIL, out)
        self.assertIn(f"FAILED at step {step}:", out)
        happy = self.happy_calls()
        # With no *last*, the run stopped once the added volume was listed.
        stop = (
            happy.index(last) if last is not None else happy.index(self.argv("add")) + 1
        )
        expected = happy[: stop + 1]
        self.assertEqual(self.fake.calls, expected)
        recovery = out.split("Recovery, most recent first:\n", 1)[1].splitlines()
        wanted = self.recoveries()[-undone:] if undone else []
        self.assertEqual([line.strip() for line in recovery[: len(wanted)]], wanted)
        self.assertNotIn(f"  {step + 1}. ", out)
        return out

    def test_a_failure_at_each_command_step(self) -> None:
        cases: List[Tuple[int, str, int]] = [
            (1, "add", 0),
            (3, "mount", 2),
            (4, "own", 3),
            (5, "chown", 3),
            (5, "chmod", 3),
            (6, "exclude", 3),
            (8, "unmount", 3),
            (11, "ditto", 4),
        ]
        for step, name, undone in cases:
            with self.subTest(step=step, command=name):
                self.setUp()
                self.script_apply()
                self.fail_next(name)
                out = self.assert_failed(self.argv(name), undone, step)
                if not undone:
                    self.assertIn("(no completed step needs undoing)", out)

    def test_a_failed_mdutil_off_that_left_spotlight_on(self) -> None:
        # mdutil -i off is judged by the mdutil -s read after it, not its exit code
        # alone, so a failure is a refused command whose read says "enabled".
        self.script_apply()
        self.fail_next("mdutil")
        self.fake.respond_mdutil(self.folder, "/x:\n\tIndexing enabled.\n", once=True)
        out = self.assert_failed(self.argv("mdstatus"), 3, 9)
        self.assertIn(" exited 1: boom", out)

    def test_a_failed_fstab_rewrite(self) -> None:
        self.script_apply()
        self.fake.respond(["sudo", "install", "-m"], prefix=True, returncode=1)
        self.assert_failed(self.rewrite()[1], 3, 10)
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), EXISTING_LINE)

    def test_a_failed_copy_with_no_fstab_before_says_to_remove_it(self) -> None:
        self.fstab.unlink()
        self.script_apply()
        self.fail_next("ditto")
        _, out = self.run_setup(self.options(apply=True))
        self.assertIn(f"  sudo rm {self.fstab}", out)

    def test_a_failed_rename_keeps_the_folder(self) -> None:
        self.script_apply()
        with mock.patch.object(os, "rename", side_effect=OSError("denied")):
            self.assert_failed(None, 1, 2)
        self.assertTrue((self.folder / "a.txt").is_file())

    def put_back(self) -> str:
        """The one command that undoes a rename whether or not the folder came back."""
        return f"rmdir {q(self.folder)} && mv {q(self.aside)} {q(self.folder)}"

    def recreate_failure(self) -> str:
        """The FAILED line a recreate that raised "denied" prints."""
        return (
            f"FAILED at step 2: {self.folder} was moved to {self.aside} but could not "
            f"be recreated (denied); to put it back: {self.put_back()}"
        )

    def test_a_failed_recreate_names_the_aside(self) -> None:
        self.script_apply()
        with mock.patch.object(os, "mkdir", side_effect=OSError("denied")):
            out = self.assert_failed(None, 1, 2)
        self.assertIn(self.recreate_failure(), out.splitlines())
        self.assertFalse(self.folder.exists())
        self.assertTrue((self.aside / "a.txt").is_file())

    def test_a_failed_chmod_says_to_remove_the_recreated_folder_first(self) -> None:
        # The folder exists again, so a bare mv would nest the aside inside it.
        self.script_apply()
        with mock.patch.object(os, "chmod", side_effect=OSError("denied")):
            out = self.assert_failed(None, 1, 2)
        self.assertIn(self.recreate_failure(), out.splitlines())
        self.assertEqual(os.listdir(self.folder), [])
        self.assertTrue((self.aside / "a.txt").is_file())

    def test_a_mount_point_refilled_after_the_recreate_is_not_mounted_over(
        self,
    ) -> None:
        self.script_apply()
        real_chmod = os.chmod

        def chmod_then_refill(path: object, mode: int, **kwargs: object) -> None:
            real_chmod(path, mode, **kwargs)  # type: ignore[arg-type]
            # Only the recreate's own chmod refills: later copies chmod too.
            if path == self.folder and not os.listdir(self.folder):
                for name in ("late-1.txt", "late-2.txt"):
                    (self.folder / name).write_bytes(b"late")

        with mock.patch.object(os, "chmod", side_effect=chmod_then_refill):
            out = self.assert_failed(None, 2, 3)
        wanted = (
            f"FAILED at step 3: {self.folder} holds 2 entries written after it was "
            f"recreated, which the mount would hide: move them back into "
            f"{self.aside}, then {self.put_back()}"
        )
        self.assertIn(wanted, out.splitlines())
        self.assertNotIn(self.argv("mount"), self.fake.calls)

    def test_the_fstab_recovery_follows_an_fstab_removed_after_validation(
        self,
    ) -> None:
        self.script_apply()
        self.exclude_then(self.fstab.unlink)
        self.fail_next("ditto")
        _, out = self.run_setup(self.options(apply=True))
        self.assertIn(f"  sudo rm {self.fstab}", out.splitlines())
        self.assertNotIn("sudo cp", out.split("Recovery", 1)[1])

    def test_the_fstab_recovery_follows_an_fstab_created_after_validation(
        self,
    ) -> None:
        self.fstab.unlink()
        self.script_apply()

        def create() -> None:
            self.fstab.write_bytes(EXISTING_LINE.encode())

        self.exclude_then(create)
        self.fail_next("ditto")
        _, out = self.run_setup(self.options(apply=True))
        restore = f"  sudo cp {sf_fstab.backup_path(self.fstab)} {self.fstab}"
        self.assertIn(restore, out.splitlines())
        self.assertNotIn("sudo rm", out)


if __name__ == "__main__":
    import unittest

    unittest.main()
