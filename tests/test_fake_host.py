"""Tests for the scripted host every mode's suite runs against.

The fake stands in for the machine in every other suite, so a defect here would let
those suites pass against behavior the real host never has.
"""

import os
import plistlib
import tempfile
import unittest
from pathlib import Path

import sf_host
import sf_macos
from tests import fake_host, tm_prefs


class _FakeCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.fake = fake_host.FakeHost(self.tmp)


class MatchingTest(_FakeCase):
    def test_an_unscripted_command_fails_the_test_loudly(self) -> None:
        with self.assertRaises(fake_host.UnscriptedCommand) as caught:
            self.fake.run(["diskutil", "eraseDisk"], 1.0)
        self.assertIsInstance(caught.exception, AssertionError)
        self.assertIn("diskutil eraseDisk", str(caught.exception))

    def test_exact_reply_carries_every_field_and_the_call_is_logged(self) -> None:
        self.fake.respond(["a", "b"], returncode=3, stdout=b"o", stderr=b"e")
        result = self.fake.run(["a", "b"], 4.0)
        self.assertEqual(result, sf_host.RunResult(("a", "b"), 3, b"o", b"e"))
        self.assertEqual(self.fake.calls, [("a", "b")])
        self.assertEqual(self.fake.timeouts, [4.0])

    def test_an_exact_reply_does_not_match_a_longer_argv(self) -> None:
        self.fake.respond(["a"])
        with self.assertRaises(fake_host.UnscriptedCommand):
            self.fake.run(["a", "b"], 1.0)

    def test_a_prefix_reply_matches_any_continuation(self) -> None:
        self.fake.respond(["sudo", "diskutil"], prefix=True, stdout=b"p")
        self.assertEqual(self.fake.run(["sudo", "diskutil", "x"], 1.0).stdout, b"p")

    def test_exact_beats_prefix_and_longer_prefix_beats_shorter(self) -> None:
        self.fake.respond(["a"], prefix=True, stdout=b"short")
        self.fake.respond(["a", "b"], prefix=True, stdout=b"long")
        self.fake.respond(["a", "b", "c"], stdout=b"exact")
        self.assertEqual(self.fake.run(["a", "b", "c"], 1.0).stdout, b"exact")
        self.assertEqual(self.fake.run(["a", "b", "d"], 1.0).stdout, b"long")
        self.assertEqual(self.fake.run(["a", "z"], 1.0).stdout, b"short")

    def test_once_replies_are_used_up_in_registration_order(self) -> None:
        self.fake.respond(["a"], stdout=b"first", once=True)
        self.fake.respond(["a"], stdout=b"second", once=True)
        self.fake.respond(["a"], stdout=b"standing")
        replies = [self.fake.run(["a"], 1.0).stdout for _ in range(4)]
        self.assertEqual(replies, [b"first", b"second", b"standing", b"standing"])

    def test_a_once_reply_queued_later_answers_before_a_standing_one(self) -> None:
        self.fake.respond(["a"], stdout=b"standing")
        self.fake.respond(["a"], stdout=b"standing too")
        self.fake.respond(["a"], stdout=b"next", once=True)
        replies = [self.fake.run(["a"], 1.0).stdout for _ in range(3)]
        self.assertEqual(replies, [b"next", b"standing", b"standing"])

    def test_an_effect_runs_and_may_supply_the_whole_result(self) -> None:
        seen: list[tuple[str, ...]] = []
        self.fake.respond(["a"], effect=seen.append, stdout=b"static")
        self.assertEqual(self.fake.run(["a"], 1.0).stdout, b"static")
        self.assertEqual(seen, [("a",)])
        self.fake.respond(
            ["b"], effect=lambda argv: sf_host.RunResult(argv, 5, b"dyn", b"")
        )
        self.assertEqual(self.fake.run(["b"], 1.0).returncode, 5)


class DeviceTest(_FakeCase):
    def test_unmounted_paths_report_their_real_device(self) -> None:
        self.assertEqual(self.fake.device_of(self.tmp), os.stat(self.tmp).st_dev)

    def test_mount_gives_a_fresh_device_and_unmount_restores_it(self) -> None:
        folder = self.tmp / "w"
        folder.mkdir()
        first = self.fake.mount(folder)
        self.assertNotEqual(first, os.stat(self.tmp).st_dev)
        self.assertEqual(self.fake.device_of(folder), first)
        other = self.tmp / "v"
        other.mkdir()
        self.assertNotEqual(self.fake.mount(other), first)
        self.fake.unmount(folder)
        self.assertEqual(self.fake.device_of(folder), os.stat(self.tmp).st_dev)

    def test_mount_and_unmount_effects_for_scripted_commands(self) -> None:
        folder = self.tmp / "w"
        folder.mkdir()
        self.fake.respond(["mount"], effect=self.fake.mount_effect(folder))
        self.fake.respond(["unmount"], effect=self.fake.unmount_effect(folder))
        real = os.stat(folder).st_dev
        self.assertEqual(self.fake.run(["mount"], 1.0).returncode, 0)
        self.assertNotEqual(self.fake.device_of(folder), real)
        self.fake.run(["unmount"], 1.0)
        self.assertEqual(self.fake.device_of(folder), real)

    def test_mount_resolves_symlinks(self) -> None:
        folder = self.tmp / "w"
        folder.mkdir()
        link = self.tmp / "link"
        link.symlink_to(folder)
        device = self.fake.mount(link)
        self.assertEqual(self.fake.device_of(folder), device)

    def test_unmounting_a_path_never_mounted_fails_the_test(self) -> None:
        with self.assertRaises(AssertionError):
            self.fake.unmount(self.tmp)

    def test_a_missing_path_raises_like_stat(self) -> None:
        with self.assertRaises(FileNotFoundError):
            self.fake.device_of(self.tmp / "absent")


class VolumeIdentityTest(_FakeCase):
    """diskutil info refuses a folder, as the real one does: the regression guard."""

    def test_diskutil_info_refuses_a_folder_even_with_a_reply_registered(self) -> None:
        folder = self.tmp / "folder"
        folder.mkdir()
        self.fake.respond(["diskutil", "info", "-plist", str(folder)], stdout=b"x")
        result = self.fake.run(["diskutil", "info", "-plist", str(folder)], 1.0)
        self.assertEqual(result.returncode, 1)
        self.assertIn(b"Could not find disk", result.stderr)
        self.assertEqual(self.fake.calls, [("diskutil", "info", "-plist", str(folder))])

    def test_diskutil_info_answers_a_mount_point_device_or_uuid(self) -> None:
        folder = self.tmp / "mounted"
        folder.mkdir()
        self.fake.mount(folder)
        for target in (str(folder), "disk9s1", fake_host.FAKE_UUID_1):
            with self.subTest(target=target):
                argv = ["diskutil", "info", "-plist", target]
                self.fake.respond(argv, stdout=b"ok")
                self.assertEqual(self.fake.run(argv, 1.0).stdout, b"ok")

    def test_other_diskutil_info_shapes_are_matched_as_usual(self) -> None:
        self.fake.respond(["diskutil", "info"], prefix=True, stdout=b"p")
        self.assertEqual(self.fake.run(["diskutil", "info", "/x"], 1.0).stdout, b"p")

    def test_each_path_has_its_own_stable_device_name(self) -> None:
        first = self.fake.device_name(self.tmp / "a")
        self.assertEqual(self.fake.device_name(self.tmp / "a"), first)
        self.assertNotEqual(self.fake.device_name(self.tmp / "b"), first)
        self.assertEqual(self.fake.path_for_device(first), str(self.tmp / "a"))
        self.assertIsNone(self.fake.path_for_device("disk1s1"))

    def test_respond_info_scripts_the_stat_then_the_device_read(self) -> None:
        self.fake.respond_info(self.tmp, mount_point="/", name="Data")
        device = self.fake.device_name(self.tmp)
        stat = self.fake.run(["/usr/bin/stat", "-L", "-f", "%Sd", str(self.tmp)], 1.0)
        self.assertEqual(stat.stdout, f"{device}\n".encode())
        reply = self.fake.run(["diskutil", "info", "-plist", device], 1.0)
        self.assertEqual(plistlib.loads(reply.stdout)["DeviceIdentifier"], device)

    def test_respond_info_with_a_named_device_uses_it_for_both_reads(self) -> None:
        self.fake.respond_info(self.tmp, mount_point="/", device="disk9s7")
        stat = self.fake.run(["/usr/bin/stat", "-L", "-f", "%Sd", str(self.tmp)], 1.0)
        self.assertEqual(stat.stdout, b"disk9s7\n")
        reply = self.fake.run(["diskutil", "info", "-plist", "disk9s7"], 1.0)
        self.assertEqual(plistlib.loads(reply.stdout)["DeviceIdentifier"], "disk9s7")

    def test_device_effect_answers_any_path(self) -> None:
        argv = ("/usr/bin/stat", "-L", "-f", "%Sd", str(self.tmp))
        result = self.fake.device_effect(argv)
        self.assertEqual(result.stdout, f"{self.fake.device_name(self.tmp)}\n".encode())


class FileSeamTest(_FakeCase):
    def test_write_temp_lands_in_the_temp_dir_and_is_recorded(self) -> None:
        path = self.fake.write_temp("hello\n")
        self.assertEqual(path.parent, self.tmp)
        self.assertEqual(path.read_text(encoding="utf-8"), "hello\n")
        self.assertEqual(self.fake.temps, [path])
        self.assertNotEqual(self.fake.write_temp("again"), path)

    def test_read_text_reads_the_real_file(self) -> None:
        path = self.tmp / "f"
        path.write_bytes("x".encode("utf-8"))
        self.assertEqual(self.fake.read_text(path), "x")

    def test_identity_and_clock_are_the_configured_fakes(self) -> None:
        self.assertEqual(self.fake.platform, "darwin")
        self.assertEqual(self.fake.getuid(), fake_host.FAKE_UID)
        self.assertEqual(self.fake.getgid(), fake_host.FAKE_GID)
        self.assertEqual(self.fake.now(), fake_host.FAKE_STAMP)
        other = fake_host.FakeHost(self.tmp, platform="linux", uid=0, gid=0, stamp="1")
        self.assertEqual(
            (other.platform, other.getuid(), other.getgid(), other.now()),
            ("linux", 0, 0, "1"),
        )


class EmulatedFileCommandTest(_FakeCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake.emulate_file_commands()
        self.src = self.tmp / "src"
        self.src.write_bytes("data".encode("utf-8"))

    def test_cp_install_and_mv_act_on_the_temp_files(self) -> None:
        copy = self.tmp / "copy"
        self.fake.run(["sudo", "cp", str(self.src), str(copy)], 1.0)
        self.assertEqual(copy.read_text(encoding="utf-8"), "data")
        staged = self.tmp / "staged"
        install = ["sudo", "install", "-m", "0644", "-o", "root", "-g", "wheel"]
        self.fake.run([*install, str(copy), str(staged)], 1.0)
        self.assertEqual(staged.read_text(encoding="utf-8"), "data")
        moved = self.tmp / "moved"
        self.fake.run(["sudo", "mv", "-f", str(staged), str(moved)], 1.0)
        self.assertFalse(staged.exists())
        self.assertEqual(moved.read_text(encoding="utf-8"), "data")

    def test_a_path_outside_the_temp_dir_fails_the_test(self) -> None:
        with self.assertRaises(AssertionError):
            self.fake.run(["sudo", "cp", str(self.src), "/etc/fstab"], 1.0)


class PlistBuilderTest(unittest.TestCase):
    def test_info_plist_carries_the_real_key_names(self) -> None:
        reply = plistlib.loads(fake_host.info_plist(mount_point="/m"))
        self.assertEqual(
            sorted(reply),
            sorted(
                [
                    "MountPoint",
                    "VolumeUUID",
                    "APFSContainerReference",
                    "APFSContainerFree",
                    "FilesystemType",
                    "GlobalPermissionsEnabled",
                    "FileVault",
                    "VolumeName",
                    "DeviceIdentifier",
                ]
            ),
        )
        self.assertEqual(reply["VolumeUUID"], fake_host.FAKE_UUID_1)

    def test_info_plist_can_omit_keys(self) -> None:
        reply = plistlib.loads(
            fake_host.info_plist(mount_point="/m", omit=["FileVault", "VolumeUUID"])
        )
        self.assertNotIn("FileVault", reply)
        self.assertNotIn("VolumeUUID", reply)
        self.assertIn("MountPoint", reply)

    def test_apfs_list_and_isexcluded_shapes(self) -> None:
        volume = fake_host.volume_entry(uuid=fake_host.FAKE_UUID_2)
        listing = plistlib.loads(
            fake_host.apfs_list_plist([fake_host.container_entry(volumes=[volume])])
        )
        container = listing["Containers"][0]
        self.assertEqual(container["ContainerReference"], fake_host.FAKE_CONTAINER)
        self.assertEqual(
            container["Volumes"][0]["APFSVolumeUUID"], fake_host.FAKE_UUID_2
        )
        excluded = plistlib.loads(fake_host.isexcluded_plist(Path("/m"), excluded=True))
        self.assertEqual(excluded, [{"Path": "/m", "IsExcluded": 1}])

    def test_fake_uuids_are_obvious_and_distinct(self) -> None:
        self.assertTrue(fake_host.FAKE_UUID_1.startswith("00000000-0000-4000-8000-"))
        self.assertNotEqual(fake_host.FAKE_UUID_1, fake_host.FAKE_UUID_2)


class PrivacyEnvironmentClockTest(_FakeCase):
    def test_a_probe_is_recorded_and_granted_by_default(self) -> None:
        self.fake.read_probe(Path("/Library/x.plist"))
        self.assertEqual(self.fake.probes, [Path("/Library/x.plist")])

    def test_a_probe_raises_the_error_a_test_set(self) -> None:
        self.fake.probe_error = PermissionError(1, "Operation not permitted")
        with self.assertRaises(PermissionError):
            self.fake.read_probe(Path("/Library/x.plist"))

    def test_getenv_reads_only_the_fakes_own_environment(self) -> None:
        self.assertIsNone(self.fake.getenv("PATH"))
        self.fake.env["TERM_PROGRAM"] = "Apple_Terminal"
        self.assertEqual(self.fake.getenv("TERM_PROGRAM"), "Apple_Terminal")

    def test_sleep_records_each_wait(self) -> None:
        self.fake.sleep(1.0)
        self.fake.sleep(2.0)
        self.assertEqual(self.fake.sleeps, [1.0, 2.0])


class ExcludedListTest(_FakeCase):
    """``addexclusion -v`` lists a volume only while it is mounted visible (live)."""

    def setUp(self) -> None:
        super().setUp()
        self.where = self.tmp / "folder"
        self.where.mkdir()
        self.exclusions = tm_prefs.ExcludedList(self.fake, ["stale"])
        self.fake.respond(
            ["sudo", "diskutil", "mount"],
            prefix=True,
            effect=self.fake.mount_effect(self.where),
        )
        self.exclude = ("sudo", "tmutil", "addexclusion", "-v", str(self.where))

    def mount(self, *options: str) -> None:
        argv = ["sudo", "diskutil", "mount", *options, "-mountPoint"]
        self.fake.run([*argv, str(self.where), fake_host.FAKE_UUID_2], 1.0)

    def listed(self) -> list:
        result = self.fake.run(list(sf_macos.EXCLUDED_UUIDS_ARGV), 1.0)
        return plistlib.loads(result.stdout)

    def test_a_visible_mount_is_listed(self) -> None:
        self.mount()
        self.exclusions.add(self.exclude)
        self.assertEqual(self.listed(), ["stale", fake_host.FAKE_UUID_2])

    def test_a_nobrowse_mount_is_not_listed(self) -> None:
        self.mount()
        self.mount("-mountOptions", "nobrowse")
        self.exclusions.add(self.exclude)
        self.assertEqual(self.listed(), ["stale"])

    def test_nothing_mounted_there_is_not_listed(self) -> None:
        self.exclusions.add(self.exclude)
        self.assertEqual(self.exclusions.uuids, ["stale"])

    def test_a_mount_no_mount_command_at_the_folder_made_is_not_listed(self) -> None:
        # The effect mounts the folder, but the only mount logged names another place.
        self.fake.run(
            ["sudo", "diskutil", "mount", "-mountPoint", "/elsewhere", "X"], 1.0
        )
        self.fake.respond(["true"])
        self.fake.run(["true"], 1.0)
        self.assertIsNone(tm_prefs.visible_volume_at(self.fake, str(self.where)))


if __name__ == "__main__":
    unittest.main()
