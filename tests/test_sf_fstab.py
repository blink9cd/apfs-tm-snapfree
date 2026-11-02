"""Tests for reading, editing and safely replacing an fstab.

Every fstab here is a file in the test's own temporary directory, and the sudo commands
the rewrite runs are answered by the scripted host, which performs them on those files.
"""

import os
import random
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Tuple
from unittest import mock

import sf_fstab
from sf_errors import ToolError
from tests import fake_host

UUID_A = fake_host.FAKE_UUID_1
UUID_B = fake_host.FAKE_UUID_2
# A seed, so the randomized round trip below is the same run every time.
_ROUND_TRIP_SEED = 20261016
_ROUND_TRIP_CASES = 300


class _TempCase(unittest.TestCase):
    """A test with its own resolved temporary directory."""

    def setUp(self) -> None:
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))


class FieldCodingTest(unittest.TestCase):
    def test_decode_turns_octal_escapes_into_characters(self) -> None:
        self.assertEqual(sf_fstab.decode_field(r"/a\040b\011c"), "/a b\tc")

    def test_decode_leaves_a_backslash_without_three_octal_digits(self) -> None:
        self.assertEqual(sf_fstab.decode_field(r"/a\09b\x"), r"/a\09b\x")

    def test_encode_escapes_space_tab_newline_and_backslash(self) -> None:
        self.assertEqual(
            sf_fstab.encode_field("/a b\tc\nd\\e"), r"/a\040b\011c\012d\134e"
        )

    def test_encode_leaves_other_characters_alone(self) -> None:
        self.assertEqual(sf_fstab.encode_field("/café/x-y_z"), "/café/x-y_z")

    def test_decode_inverts_encode_over_random_text(self) -> None:
        rng = random.Random(_ROUND_TRIP_SEED)
        alphabet = "ab/ \t\n\\0471é"
        for _ in range(_ROUND_TRIP_CASES):
            text = "".join(rng.choice(alphabet) for _ in range(rng.randint(0, 12)))
            encoded = sf_fstab.encode_field(text)
            with self.subTest(text=text):
                self.assertEqual(sf_fstab.decode_field(encoded), text)
                # An encoded field is one whitespace-free token, as fstab needs.
                self.assertEqual(encoded.split(), [encoded] if encoded else [])


class ParseEntriesTest(unittest.TestCase):
    def test_comments_blanks_and_short_lines_are_skipped(self) -> None:
        text = (
            "# a comment\n\n   \nlonely\n"
            f"UUID={UUID_A} /Volumes/x\\040y apfs rw\n"
            "  #indented comment /z apfs rw\n"
        )
        self.assertEqual(
            sf_fstab.parse_entries(text),
            [sf_fstab.FstabEntry(f"UUID={UUID_A}", "/Volumes/x y")],
        )

    def test_entry_volume_id_drops_the_uuid_prefix(self) -> None:
        entry = sf_fstab.FstabEntry(f"uuid={UUID_A}", "/m")
        self.assertEqual(entry.volume_id, UUID_A)


class VolumeIdTest(unittest.TestCase):
    def test_prefix_is_case_insensitive(self) -> None:
        for spec in (f"UUID={UUID_A}", f"uuid={UUID_A}", f"Uuid={UUID_A}"):
            with self.subTest(spec=spec):
                self.assertEqual(sf_fstab.volume_id(spec), UUID_A)

    def test_other_specs_are_returned_whole(self) -> None:
        self.assertEqual(sf_fstab.volume_id("LABEL=data"), "LABEL=data")


class EntryForTest(_TempCase):
    def test_matches_with_both_sides_resolved(self) -> None:
        real = self.tmp / "real"
        real.mkdir()
        link = self.tmp / "link"
        link.symlink_to(real)
        text = f"UUID={UUID_A} {link}/ apfs rw\n"
        entry = sf_fstab.entry_for(text, real)
        self.assertIsNotNone(entry)
        assert entry is not None
        self.assertEqual(entry.volume_id, UUID_A)

    def test_first_match_wins(self) -> None:
        target = self.tmp / "w"
        text = f"UUID={UUID_A} {target} apfs rw\nUUID={UUID_B} {target} apfs rw\n"
        entry = sf_fstab.entry_for(text, target)
        assert entry is not None
        self.assertEqual(entry.volume_id, UUID_A)

    def test_no_line_for_the_path_is_none(self) -> None:
        text = f"UUID={UUID_A} {self.tmp / 'other'} apfs rw\n"
        self.assertIsNone(sf_fstab.entry_for(text, self.tmp / "w"))

    def test_escaped_mount_points_match_their_decoded_path(self) -> None:
        target = self.tmp / "with space"
        text = sf_fstab.render_line(UUID_A, target) + "\n"
        entry = sf_fstab.entry_for(text, target)
        assert entry is not None
        self.assertEqual(entry.mount_point, str(target))


class RenderAddTest(_TempCase):
    def test_render_line_is_the_documented_shape(self) -> None:
        self.assertEqual(
            sf_fstab.render_line(UUID_A, Path("/w t")),
            f"UUID={UUID_A} /w\\040t apfs rw,nobrowse",
        )

    def test_add_line_to_empty_text(self) -> None:
        self.assertEqual(sf_fstab.add_line("", "L"), "L\n")

    def test_add_line_keeps_a_final_newline(self) -> None:
        self.assertEqual(sf_fstab.add_line("a\n", "L"), "a\nL\n")

    def test_add_line_supplies_a_missing_final_newline(self) -> None:
        self.assertEqual(sf_fstab.add_line("a", "L"), "a\nL\n")

    def test_add_line_refuses_an_embedded_newline(self) -> None:
        with self.assertRaises(ValueError):
            sf_fstab.add_line("", "a\nb")


class RemoveSwapTest(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.target = self.tmp / "w"
        self.line = f"UUID={UUID_A}  {self.target}\tapfs rw,nobrowse"
        self.text = f"# keep me\n{self.line}\nUUID={UUID_B} /elsewhere apfs rw\n"

    def test_remove_entry_drops_only_that_line(self) -> None:
        self.assertEqual(
            sf_fstab.remove_entry(self.text, self.target),
            f"# keep me\nUUID={UUID_B} /elsewhere apfs rw\n",
        )

    def test_remove_entry_without_a_line_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            sf_fstab.remove_entry("# nothing\n", self.target)

    def test_swap_uuid_replaces_only_the_spec_and_keeps_the_spacing(self) -> None:
        swapped = sf_fstab.swap_uuid(self.text, self.target, UUID_B)
        self.assertEqual(
            swapped,
            f"# keep me\nUUID={UUID_B}  {self.target}\tapfs rw,nobrowse\n"
            f"UUID={UUID_B} /elsewhere apfs rw\n",
        )

    def test_swap_uuid_keeps_leading_indentation(self) -> None:
        text = f"   UUID={UUID_A} {self.target} apfs rw"
        self.assertEqual(
            sf_fstab.swap_uuid(text, self.target, UUID_B),
            f"   UUID={UUID_B} {self.target} apfs rw",
        )

    def test_swap_uuid_without_a_line_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            sf_fstab.swap_uuid("", self.target, UUID_B)


class RetiredMarkerTest(_TempCase):
    def test_render_is_the_documented_comment(self) -> None:
        self.assertEqual(
            sf_fstab.render_marker(UUID_A, Path("/w t")),
            f"# apfs_tm_snapfree: retired UUID={UUID_A} for /w\\040t",
        )

    def test_markers_parse_back_and_are_not_entries(self) -> None:
        target = self.tmp / "w t"
        text = sf_fstab.add_line(
            "# other comment\n", sf_fstab.render_marker(UUID_A, target)
        )
        self.assertEqual(
            sf_fstab.parse_markers(text),
            [sf_fstab.RetiredMarker(UUID_A, str(target))],
        )
        self.assertEqual(sf_fstab.parse_entries(text), [])

    def test_marker_for_matches_resolved_paths_and_misses_others(self) -> None:
        target = self.tmp / "w"
        text = sf_fstab.render_marker(UUID_A, target) + "\n"
        marker = sf_fstab.marker_for(text, Path(str(target) + "/"))
        assert marker is not None
        self.assertEqual(marker.uuid, UUID_A)
        self.assertIsNone(sf_fstab.marker_for(text, self.tmp / "other"))

    def test_remove_marker_keeps_every_other_line(self) -> None:
        target = self.tmp / "w"
        line = sf_fstab.render_line(UUID_B, target)
        marker = sf_fstab.render_marker(UUID_A, target)
        text = f"{line}\n{marker}\n# tail\n"
        self.assertEqual(sf_fstab.remove_marker(text, target), f"{line}\n# tail\n")

    def test_remove_marker_without_one_is_a_tool_error(self) -> None:
        with self.assertRaises(ToolError):
            sf_fstab.remove_marker("# none\n", self.tmp / "w")


class SharedHelpersTest(_TempCase):
    """The helpers every mode shares: reading the fstab, comparing UUIDs, restoring."""

    def test_same_uuid_ignores_case(self) -> None:
        lettered = "00000000-0000-4000-8000-00000000abcd"
        self.assertTrue(sf_fstab.same_uuid(lettered, lettered.upper()))
        self.assertFalse(sf_fstab.same_uuid(UUID_A, UUID_B))

    def test_read_fstab_reads_an_absent_one_as_empty(self) -> None:
        host = fake_host.FakeHost(self.tmp)
        fstab = self.tmp / "fstab"
        self.assertEqual(sf_fstab.read_fstab(host, fstab), "")
        fstab.write_bytes(b"# x\n")
        self.assertEqual(sf_fstab.read_fstab(host, fstab), "# x\n")

    def test_read_fstab_refuses_an_unreadable_one(self) -> None:
        host = fake_host.FakeHost(self.tmp)
        folder = self.tmp / "a-folder"
        folder.mkdir()
        garbled = self.tmp / "garbled"
        garbled.write_bytes(b"\xff\xfe")
        for fstab in (folder, garbled):
            with self.subTest(fstab=fstab.name), self.assertRaises(ToolError) as caught:
                sf_fstab.read_fstab(host, fstab)
            self.assertIn(f"could not read {fstab}", str(caught.exception))

    def test_restore_command_copies_the_backup_back(self) -> None:
        fstab = self.tmp / "fs tab"
        self.assertEqual(
            sf_fstab.restore_command(fstab),
            f"sudo cp '{fstab}.apfs_tm_snapfree.bak' '{fstab}'",
        )


class IsMountPointTest(_TempCase):
    def test_same_device_as_the_parent_is_not_a_mount_point(self) -> None:
        folder = self.tmp / "f"
        folder.mkdir()
        devices = {str(folder): 7, str(self.tmp): 7}
        self.assertFalse(
            sf_fstab.is_mount_point(folder, lambda path: devices[str(path)])
        )

    def test_a_different_device_is_a_mount_point(self) -> None:
        folder = self.tmp / "f"
        folder.mkdir()
        devices = {str(folder): 9, str(self.tmp): 7}
        self.assertTrue(
            sf_fstab.is_mount_point(folder, lambda path: devices[str(path)])
        )

    def test_the_path_is_resolved_before_its_parent_is_taken(self) -> None:
        inner = self.tmp / "a" / "inner"
        inner.mkdir(parents=True)
        link = self.tmp / "link"
        link.symlink_to(inner)
        seen: list[str] = []

        def device_of(path: Path) -> int:
            seen.append(str(path))
            return 1

        sf_fstab.is_mount_point(link, device_of)
        self.assertEqual(seen, [str(inner), str(inner.parent)])


class ReplaceFstabTest(_TempCase):
    def setUp(self) -> None:
        super().setUp()
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.emulate_file_commands()
        self.fstab = self.tmp / "fstab"

    def test_existing_fstab_is_backed_up_staged_and_renamed(self) -> None:
        self.fstab.write_bytes("old\n".encode("utf-8"))
        sf_fstab.replace_fstab(self.fake, self.fstab, "new\n")
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), "new\n")
        backup = sf_fstab.backup_path(self.fstab)
        self.assertEqual(backup.read_text(encoding="utf-8"), "old\n")
        temp = self.fake.temps[0]
        self.assertEqual(
            self.fake.calls,
            [
                ("sudo", "cp", str(self.fstab), str(backup)),
                (
                    "sudo",
                    "install",
                    "-m",
                    "0644",
                    "-o",
                    "root",
                    "-g",
                    "wheel",
                    str(temp),
                    str(sf_fstab.staged_path(self.fstab)),
                ),
                (
                    "sudo",
                    "mv",
                    "-f",
                    str(sf_fstab.staged_path(self.fstab)),
                    str(self.fstab),
                ),
            ],
        )
        self.assertFalse(temp.exists(), "the user temp file is removed once used")
        self.assertFalse(sf_fstab.staged_path(self.fstab).exists())

    def test_an_absent_fstab_gets_no_backup(self) -> None:
        sf_fstab.replace_fstab(self.fake, self.fstab, "first\n")
        self.assertEqual(self.fstab.read_text(encoding="utf-8"), "first\n")
        self.assertEqual([call[1] for call in self.fake.calls], ["install", "mv"])
        self.assertFalse(sf_fstab.backup_path(self.fstab).exists())

    def test_the_staged_file_sits_beside_the_fstab(self) -> None:
        self.assertEqual(sf_fstab.staged_path(self.fstab).parent, self.fstab.parent)

    def test_a_failed_command_stops_the_rewrite_and_still_removes_the_temp(
        self,
    ) -> None:
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.respond(["sudo", "install"], prefix=True, returncode=1, stderr=b"no")
        with self.assertRaises(ToolError):
            sf_fstab.replace_fstab(self.fake, self.fstab, "x\n")
        self.assertEqual(len(self.fake.calls), 1)
        self.assertFalse(self.fake.temps[0].exists())

    def test_a_failing_temp_cleanup_never_masks_the_real_error(self) -> None:
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.respond(["sudo", "install"], prefix=True, returncode=1, stderr=b"no")
        failing_unlink = mock.patch.object(
            Path, "unlink", side_effect=PermissionError("locked")
        )
        with failing_unlink, self.assertRaises(ToolError) as caught:
            sf_fstab.replace_fstab(self.fake, self.fstab, "x\n")
        self.assertIn("sudo install", str(caught.exception))

    def test_a_temp_already_gone_is_not_an_error(self) -> None:
        def install_then_remove_temp(argv: Tuple[str, ...]) -> None:
            # Stage the copy as install would, then lose the temp before cleanup.
            shutil.copyfile(argv[-2], argv[-1])
            Path(argv[-2]).unlink()

        self.fake.respond(
            ["sudo", "install"], prefix=True, effect=install_then_remove_temp, once=True
        )
        sf_fstab.replace_fstab(self.fake, self.fstab, "x\n")
        self.assertFalse(self.fake.temps[0].exists())

    def test_describe_rewrite_shows_each_command_with_a_temp_placeholder(self) -> None:
        described = sf_fstab.describe_rewrite(self.fstab, backup=True)
        self.assertEqual(
            described,
            [
                f"sudo cp {self.fstab} {sf_fstab.backup_path(self.fstab)}",
                "sudo install -m 0644 -o root -g wheel <temp> "
                f"{sf_fstab.staged_path(self.fstab)}",
                f"sudo mv -f {sf_fstab.staged_path(self.fstab)} {self.fstab}",
            ],
        )
        self.assertEqual(len(sf_fstab.describe_rewrite(self.fstab, backup=False)), 2)

    def test_describe_rewrite_still_quotes_a_path_that_needs_it(self) -> None:
        spaced = self.tmp / "fs tab"
        install = sf_fstab.describe_rewrite(spaced, backup=False)[0]
        self.assertTrue(install.endswith(f" <temp> '{sf_fstab.staged_path(spaced)}'"))


if __name__ == "__main__":
    unittest.main()
