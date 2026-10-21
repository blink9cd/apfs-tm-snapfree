"""Tests for the typed readers over diskutil, tmutil and mdutil output.

Every reply is a plist built by the scripted host's helpers from obvious fakes, so no
test reads the real disk layout.
"""

import os
import plistlib
import tempfile
import unittest
from pathlib import Path
from typing import Any

import sf_macos
from sf_errors import ToolError
from tests import fake_host

UUID_A = fake_host.FAKE_UUID_1
UUID_B = fake_host.FAKE_UUID_2
TARGET = Path("/fake/target")
#: The device the fake stat names for a parse test's volume.
DEVICE = "disk9s5"


class _FakeCase(unittest.TestCase):
    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.fake = fake_host.FakeHost(self.tmp)


class ArgvTest(unittest.TestCase):
    def test_the_commands_are_the_documented_ones(self) -> None:
        self.assertEqual(
            sf_macos.device_argv(TARGET),
            ["/usr/bin/stat", "-L", "-f", "%Sd", str(TARGET)],
        )
        self.assertEqual(
            sf_macos.info_argv(DEVICE), ["diskutil", "info", "-plist", DEVICE]
        )
        self.assertEqual(
            sf_macos.APFS_LIST_ARGV, ("diskutil", "apfs", "list", "-plist")
        )
        self.assertEqual(
            sf_macos.isexcluded_argv(TARGET),
            ["tmutil", "isexcluded", "-X", str(TARGET)],
        )
        self.assertEqual(
            sf_macos.mdutil_status_argv(TARGET), ["mdutil", "-s", str(TARGET)]
        )


class VolumeInfoTest(_FakeCase):
    def test_reads_every_field(self) -> None:
        self.fake.respond_info(
            TARGET,
            mount_point=str(TARGET),
            volume_uuid=UUID_A,
            container="disk9",
            container_free=5 * 10**9,
            ownership=False,
            filevault=True,
            name="snapfree-w",
            device="disk9s7",
        )
        info = sf_macos.volume_info(self.fake, TARGET)
        self.assertEqual(
            info,
            sf_macos.VolumeInfo(
                mount_point=str(TARGET),
                volume_uuid=UUID_A,
                container="disk9",
                container_free=5 * 10**9,
                filesystem="apfs",
                ownership_enabled=False,
                filevault=True,
                name="snapfree-w",
                device="disk9s7",
            ),
        )
        # The path goes to stat, never to diskutil, which refuses a folder; diskutil
        # gets the device stat named.
        self.assertEqual(
            self.fake.calls,
            [
                tuple(sf_macos.device_argv(TARGET)),
                tuple(sf_macos.info_argv("disk9s7")),
            ],
        )
        self.assertEqual(self.fake.timeouts, [120.0, 120.0])

    def test_a_folder_that_is_no_mount_point_is_read_through_its_device(
        self,
    ) -> None:
        # The regression: the real diskutil exits 1 for a folder inside a volume, and
        # the fake refuses one too, so only the stat-then-device read gets an answer.
        folder = self.tmp / "folder"
        folder.mkdir()
        self.fake.respond(sf_macos.info_argv(str(folder)), stdout=b"never used")
        self.fake.respond_info(folder, mount_point="/", name="Data")
        info = sf_macos.volume_info(self.fake, folder)
        self.assertEqual((info.mount_point, info.name), ("/", "Data"))
        self.assertEqual(info.device, self.fake.device_name(folder))

    def test_an_identifier_goes_to_diskutil_as_it_is(self) -> None:
        self.fake.respond_info_for(UUID_A, mount_point="")
        info = sf_macos.volume_info_for(self.fake, UUID_A)
        self.assertEqual(info.mount_point, "")
        self.assertEqual(self.fake.calls, [tuple(sf_macos.info_argv(UUID_A))])

    def test_a_missing_key_names_the_command_and_the_key(self) -> None:
        for key in ("VolumeUUID", "APFSContainerFree", "FileVault"):
            with self.subTest(key=key):
                self.fake.respond(
                    sf_macos.info_argv(DEVICE),
                    stdout=fake_host.info_plist(mount_point="/", omit=[key]),
                    once=True,
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.volume_info_for(self.fake, DEVICE)
                message = str(caught.exception)
                self.assertIn("diskutil info -plist", message)
                self.assertIn(key, message)

    def test_the_filesystem_is_read_first(self) -> None:
        self.fake.respond(
            sf_macos.info_argv(DEVICE), stdout=plistlib.dumps({"MountPoint": "/"})
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertIn("FilesystemType", str(caught.exception))

    def test_a_non_apfs_volume_reads_without_its_apfs_fields(self) -> None:
        apfs_only = [
            "VolumeUUID",
            "APFSContainerReference",
            "APFSContainerFree",
            "GlobalPermissionsEnabled",
            "FileVault",
        ]
        self.fake.respond(
            sf_macos.info_argv(DEVICE),
            stdout=fake_host.info_plist(
                mount_point="/", filesystem="hfs", omit=apfs_only
            ),
        )
        info = sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertEqual(info.filesystem, "hfs")
        self.assertEqual(
            (
                info.volume_uuid,
                info.container,
                info.container_free,
                info.ownership_enabled,
                info.filevault,
            ),
            (None, None, None, None, None),
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.require_apfs(info, TARGET)
        self.assertEqual(
            str(caught.exception), f"{TARGET} is on a hfs filesystem, not APFS"
        )

    def test_a_non_apfs_volume_keeps_the_fields_it_reports(self) -> None:
        self.fake.respond(
            sf_macos.info_argv(DEVICE),
            stdout=fake_host.info_plist(
                mount_point="/", filesystem="hfs", omit=["APFSContainerFree"]
            ),
        )
        info = sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertEqual(info.volume_uuid, fake_host.FAKE_UUID_1)
        self.assertIsNone(info.container_free)

    def test_a_non_apfs_field_of_the_wrong_type_is_still_refused(self) -> None:
        reply = plistlib.loads(fake_host.info_plist(mount_point="/", filesystem="hfs"))
        reply["FileVault"] = "maybe"
        self.fake.respond(sf_macos.info_argv(DEVICE), stdout=plistlib.dumps(reply))
        with self.assertRaises(ToolError) as caught:
            sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertIn("FileVault", str(caught.exception))

    def test_require_apfs_refuses_a_hand_built_info_missing_a_field(self) -> None:
        info = sf_macos.VolumeInfo(
            mount_point="/",
            volume_uuid=UUID_A,
            container=None,
            container_free=1,
            filesystem="apfs",
            ownership_enabled=True,
            filevault=False,
            name="n",
            device="disk9s1",
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.require_apfs(info, TARGET)
        self.assertIn("APFSContainerReference", str(caught.exception))

    def test_require_apfs_hands_back_the_apfs_fields(self) -> None:
        self.fake.respond_info(
            TARGET, mount_point="/", container="disk9", container_free=7
        )
        details = sf_macos.require_apfs(sf_macos.volume_info(self.fake, TARGET), TARGET)
        self.assertEqual(
            details,
            sf_macos.ApfsInfo(
                volume_uuid=fake_host.FAKE_UUID_1,
                container="disk9",
                container_free=7,
                ownership_enabled=True,
                filevault=False,
            ),
        )

    def test_a_wrongly_typed_value_is_refused(self) -> None:
        reply = plistlib.loads(fake_host.info_plist(mount_point="/"))
        reply["APFSContainerFree"] = "lots"
        self.fake.respond(sf_macos.info_argv(DEVICE), stdout=plistlib.dumps(reply))
        with self.assertRaises(ToolError) as caught:
            sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertIn("APFSContainerFree", str(caught.exception))

    def test_a_bool_is_not_accepted_as_an_integer(self) -> None:
        reply = plistlib.loads(fake_host.info_plist(mount_point="/"))
        reply["APFSContainerFree"] = True
        self.fake.respond(sf_macos.info_argv(DEVICE), stdout=plistlib.dumps(reply))
        with self.assertRaises(ToolError):
            sf_macos.volume_info_for(self.fake, DEVICE)

    def test_a_top_level_that_is_not_a_dict_is_refused(self) -> None:
        self.fake.respond(sf_macos.info_argv(DEVICE), stdout=plistlib.dumps([1]))
        with self.assertRaises(ToolError):
            sf_macos.volume_info_for(self.fake, DEVICE)

    def test_bytes_that_are_not_a_plist_are_refused(self) -> None:
        for junk in (b"not a plist", b"<?xml version='1.0'?><plist><dict><key>"):
            with self.subTest(junk=junk):
                self.fake.respond(sf_macos.info_argv(DEVICE), stdout=junk, once=True)
                with self.assertRaises(ToolError) as caught:
                    sf_macos.volume_info_for(self.fake, DEVICE)
                self.assertIn("unreadable plist", str(caught.exception))

    def test_a_failed_command_is_a_tool_error(self) -> None:
        self.fake.respond(
            sf_macos.info_argv(DEVICE), returncode=1, stderr=b"Could not find disk"
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.volume_info_for(self.fake, DEVICE)
        self.assertIn("Could not find disk", str(caught.exception))


class ContainerTest(_FakeCase):
    def setUp(self) -> None:
        super().setUp()
        self.vol_a = fake_host.volume_entry(
            uuid=UUID_A, name="Data", device="disk9s1", quota=0, in_use=7, reserve=0
        )
        self.vol_b = fake_host.volume_entry(
            uuid=UUID_B,
            name="snapfree-w",
            device="disk9s7",
            quota=40 * 10**9,
            in_use=10**9,
            reserve=0,
        )
        self.fake.respond_apfs_list(
            [
                fake_host.container_entry(
                    reference="disk9", free=50, volumes=[self.vol_a, self.vol_b]
                ),
                fake_host.container_entry(reference="disk8", free=1, volumes=[]),
            ]
        )

    def test_reads_every_container_and_volume(self) -> None:
        containers = sf_macos.apfs_containers(self.fake)
        self.assertEqual([c.reference for c in containers], ["disk9", "disk8"])
        self.assertEqual(containers[0].free, 50)
        self.assertEqual(
            containers[0].volumes[1],
            sf_macos.ApfsVolume(
                uuid=UUID_B,
                name="snapfree-w",
                device="disk9s7",
                quota=40 * 10**9,
                in_use=10**9,
                reserve=0,
            ),
        )

    def test_container_volumes_finds_one_by_reference(self) -> None:
        container = sf_macos.container_volumes(self.fake, "disk9")
        self.assertEqual(len(container.volumes), 2)

    def test_an_unknown_container_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError) as caught:
            sf_macos.container_volumes(self.fake, "disk3")
        self.assertIn("disk3", str(caught.exception))

    def test_lookups_by_uuid_and_name(self) -> None:
        container = sf_macos.container_volumes(self.fake, "disk9")
        found = container.volume_with_uuid(UUID_B)
        assert found is not None
        self.assertEqual(found.name, "snapfree-w")
        self.assertIsNone(
            container.volume_with_uuid("00000000-0000-4000-8000-00000000ffff")
        )
        self.assertEqual([v.uuid for v in container.volumes_named("Data")], [UUID_A])
        self.assertEqual(container.volumes_named("absent"), ())

    def test_uuid_lookups_ignore_case(self) -> None:
        # diskutil prints upper case; an fstab or marker written by hand may not.
        lettered = "00000000-0000-4000-8000-00000000ABCD"
        self.fake.respond_apfs_list(
            [
                fake_host.container_entry(
                    volumes=[dict(self.vol_a, APFSVolumeUUID=lettered)]
                )
            ],
            once=True,
        )
        containers = sf_macos.apfs_containers(self.fake)
        self.assertIsNotNone(containers[0].volume_with_uuid(lettered.lower()))
        hit = sf_macos.find_volume(containers, lettered.lower())
        assert hit is not None
        self.assertEqual(hit[1].uuid, lettered)

    def test_roles_are_read_when_reported(self) -> None:
        with_roles = dict(self.vol_a, Roles=["Data"])
        self.fake.respond_apfs_list(
            [fake_host.container_entry(volumes=[with_roles, self.vol_b])], once=True
        )
        volumes = sf_macos.apfs_containers(self.fake)[0].volumes
        self.assertEqual([v.roles for v in volumes], [("Data",), None])

    def test_roles_that_are_not_a_list_of_names_are_refused(self) -> None:
        for roles in ("Data", [1]):
            with self.subTest(roles=roles):
                broken = dict(self.vol_a, Roles=roles)
                self.fake.respond_apfs_list(
                    [fake_host.container_entry(volumes=[broken])], once=True
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.apfs_containers(self.fake)
                self.assertIn("Roles", str(caught.exception))

    def test_find_volume_searches_every_container(self) -> None:
        containers = sf_macos.apfs_containers(self.fake)
        hit = sf_macos.find_volume(containers, UUID_B)
        assert hit is not None
        self.assertEqual((hit[0].reference, hit[1].device), ("disk9", "disk9s7"))
        self.assertIsNone(sf_macos.find_volume(containers, "nope"))

    def test_containers_must_be_a_list_of_dicts(self) -> None:
        for reply in ({"Containers": "x"}, {"Containers": ["x"]}, {}):
            with self.subTest(reply=reply):
                self.fake.respond(
                    list(sf_macos.APFS_LIST_ARGV),
                    stdout=plistlib.dumps(reply),
                    once=True,
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.apfs_containers(self.fake)
                self.assertIn("diskutil apfs list -plist", str(caught.exception))

    def test_a_volume_missing_a_key_is_refused(self) -> None:
        broken = dict(self.vol_a)
        del broken["CapacityQuota"]
        self.fake.respond_apfs_list(
            [fake_host.container_entry(reference="disk9", free=1, volumes=[broken])],
            once=True,
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.apfs_containers(self.fake)
        self.assertIn("CapacityQuota", str(caught.exception))


class IsExcludedTest(_FakeCase):
    def test_reads_the_flag_both_ways(self) -> None:
        for excluded in (True, False):
            with self.subTest(excluded=excluded):
                self.fake.respond_isexcluded(TARGET, excluded=excluded, once=True)
                self.assertIs(sf_macos.is_excluded(self.fake, TARGET), excluded)

    def test_a_malformed_reply_is_refused(self) -> None:
        replies: tuple[Any, ...] = (
            [],
            {"IsExcluded": 1},
            ["x"],
            [{"Path": "/"}],
            [{"IsExcluded": True}],
        )
        for reply in replies:
            with self.subTest(reply=reply):
                self.fake.respond(
                    sf_macos.isexcluded_argv(TARGET),
                    stdout=plistlib.dumps(reply),
                    once=True,
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.is_excluded(self.fake, TARGET)
                self.assertIn("tmutil isexcluded -X", str(caught.exception))


class SpotlightTest(_FakeCase):
    def test_reads_disabled_enabled_and_unknown(self) -> None:
        cases = (
            (f"{TARGET}:\n\tIndexing disabled.\n", True),
            (f"{TARGET}:\n\tIndexing enabled. \n", False),
            (f"{TARGET}:\n\tError: unknown indexing state.\n", None),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.fake.respond_mdutil(TARGET, text, once=True)
                self.assertIs(sf_macos.spotlight_disabled(self.fake, TARGET), expected)

    def test_only_the_indented_verdict_is_read_never_the_path_line(self) -> None:
        # mdutil's first line is the volume's path, which may itself contain "disabled"
        # or "enabled"; reading it once reported a folder as unindexed while it was not.
        cases = (
            ("/Volumes/x/disabled-builds:\n\tIndexing enabled.\n", False),
            ("/Volumes/x/enabled-cache:\n\tIndexing disabled.\n", True),
            ("/Volumes/x/disabled:\n", None),
            ("/System/Volumes/Data/p:\n\tIndexing and searching disabled.\n", True),
        )
        for text, expected in cases:
            with self.subTest(text=text):
                self.fake.respond_mdutil(TARGET, text, once=True)
                self.assertIs(sf_macos.spotlight_disabled(self.fake, TARGET), expected)

    def test_a_failed_mdutil_is_a_tool_error(self) -> None:
        self.fake.respond(sf_macos.mdutil_status_argv(TARGET), returncode=1)
        with self.assertRaises(ToolError):
            sf_macos.spotlight_disabled(self.fake, TARGET)


if __name__ == "__main__":
    unittest.main()


class VolumeDeviceTest(_FakeCase):
    def test_reads_the_device_stat_names(self) -> None:
        self.fake.respond(sf_macos.device_argv(TARGET), stdout=b"disk3s5\n")
        self.assertEqual(sf_macos.volume_device(self.fake, TARGET), "disk3s5")
        self.assertEqual(self.fake.timeouts, [120.0])

    def test_a_volume_with_no_disk_behind_it_is_refused(self) -> None:
        # stat prints "???" for devfs, and a network share names no disk at all.
        for reply in (b"???\n", b"\n", b"//server/share\n", b"disk3s5 extra\n"):
            with self.subTest(reply=reply):
                self.fake.respond(sf_macos.device_argv(TARGET), stdout=reply, once=True)
                with self.assertRaises(ToolError) as caught:
                    sf_macos.volume_device(self.fake, TARGET)
                self.assertIn(str(TARGET), str(caught.exception))
                self.assertIn("not on a disk", str(caught.exception))

    def test_a_failed_stat_is_a_tool_error(self) -> None:
        self.fake.respond(
            sf_macos.device_argv(TARGET), returncode=1, stderr=b"No such file"
        )
        with self.assertRaises(ToolError) as caught:
            sf_macos.volume_info(self.fake, TARGET)
        self.assertIn("No such file", str(caught.exception))
