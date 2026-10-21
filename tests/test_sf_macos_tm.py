"""Tests for the read of Time Machine's explicit volume exclusions, ``ExcludedVolumeUUIDs``.

``tmutil isexcluded`` reports a nobrowse-mounted volume excluded whether or not anything
excluded it, so the steps that add or remove an exclusion confirm it by this list. Every
reply is scripted; no test reads the machine's own preferences.
"""

from __future__ import annotations

import plistlib
import unittest
from pathlib import Path

import sf_macos
import sf_privacy
from sf_errors import ToolError
from tests import fake_host, tm_prefs

UUID_A = fake_host.FAKE_UUID_1
UUID_B = fake_host.FAKE_UUID_2


class _Case(unittest.TestCase):
    def setUp(self) -> None:
        self.fake = fake_host.FakeHost(Path("."))


class ArgvTest(unittest.TestCase):
    def test_reads_the_key_from_time_machines_preferences_under_sudo(self) -> None:
        self.assertEqual(
            sf_macos.EXCLUDED_UUIDS_ARGV,
            (
                "sudo",
                "/usr/bin/plutil",
                "-extract",
                "ExcludedVolumeUUIDs",
                "xml1",
                "-o",
                "-",
                str(sf_privacy.TIME_MACHINE_PREFERENCES),
            ),
        )


class ExcludedUuidsTest(_Case):
    def test_reads_the_list_in_upper_case(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_A.lower(), UUID_B])
        self.assertEqual(
            sf_macos.excluded_volume_uuids(self.fake), [UUID_A.upper(), UUID_B]
        )
        self.assertEqual(self.fake.timeouts, [120.0])

    def test_an_empty_list_reads_empty(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [])
        self.assertEqual(sf_macos.excluded_volume_uuids(self.fake), [])

    def test_a_missing_key_reads_as_an_empty_list(self) -> None:
        self.fake.respond(
            list(sf_macos.EXCLUDED_UUIDS_ARGV),
            returncode=1,
            stderr=tm_prefs.NO_KEY_STDERR,
        )
        self.assertEqual(sf_macos.excluded_volume_uuids(self.fake), [])

    def test_a_missing_file_reads_as_an_empty_list(self) -> None:
        self.fake.respond(
            list(sf_macos.EXCLUDED_UUIDS_ARGV),
            returncode=1,
            stderr=tm_prefs.NO_FILE_STDERR,
        )
        self.assertEqual(sf_macos.excluded_volume_uuids(self.fake), [])

    def test_any_other_failure_is_a_tool_error(self) -> None:
        # A privacy refusal above all: read as "nothing excluded", it would let
        # teardown's confirm pass on a list it never saw.
        for stderr in (b"couldn't be opened because you don't have permission", b""):
            with self.subTest(stderr=stderr):
                self.fake.respond(
                    list(sf_macos.EXCLUDED_UUIDS_ARGV),
                    returncode=1,
                    stderr=stderr,
                    once=True,
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.excluded_volume_uuids(self.fake)
                self.assertIn("plutil -extract", str(caught.exception))

    def test_a_reply_that_is_not_a_list_of_uuids_is_refused(self) -> None:
        for reply in (plistlib.dumps({"a": 1}), plistlib.dumps([1]), b"not a plist"):
            with self.subTest(reply=reply):
                self.fake.respond(
                    list(sf_macos.EXCLUDED_UUIDS_ARGV), stdout=reply, once=True
                )
                with self.assertRaises(ToolError) as caught:
                    sf_macos.excluded_volume_uuids(self.fake)
                self.assertIn("plutil -extract", str(caught.exception))


class RequireTest(_Case):
    """Both checks poll the list, since tmutil's write may reach the file late."""

    def _reads(self) -> int:
        return self.fake.calls.count(sf_macos.EXCLUDED_UUIDS_ARGV)

    def test_listed_on_the_first_read_returns_without_waiting(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_A.lower()])
        sf_macos.require_explicitly_excluded(self.fake, UUID_A)
        self.assertEqual((self._reads(), self.fake.sleeps), (1, []))

    def test_listed_after_three_reads_waits_on_the_schedule(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_B], reads=2)
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_A])
        sf_macos.require_explicitly_excluded(self.fake, UUID_A)
        self.assertEqual(self._reads(), 3)
        self.assertEqual(self.fake.sleeps, list(sf_macos.EXCLUSION_POLL_WAITS[:2]))

    def test_never_listed_fails_after_the_whole_schedule(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_B])
        with self.assertRaises(ToolError) as caught:
            sf_macos.require_explicitly_excluded(self.fake, UUID_A)
        self.assertIn(
            f"ExcludedVolumeUUIDs does not list {UUID_A}", str(caught.exception)
        )
        self.assertEqual(self.fake.sleeps, list(sf_macos.EXCLUSION_POLL_WAITS))
        self.assertEqual(self._reads(), len(sf_macos.EXCLUSION_POLL_WAITS) + 1)

    def test_unlisted_on_the_first_read_returns_without_waiting(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_B])
        sf_macos.require_not_explicitly_excluded(self.fake, UUID_A)
        self.assertEqual((self._reads(), self.fake.sleeps), (1, []))

    def test_unlisted_after_three_reads_waits_on_the_schedule(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_A.lower()], reads=2)
        tm_prefs.respond_excluded_uuids(self.fake, [])
        sf_macos.require_not_explicitly_excluded(self.fake, UUID_A)
        self.assertEqual(self._reads(), 3)
        self.assertEqual(self.fake.sleeps, list(sf_macos.EXCLUSION_POLL_WAITS[:2]))

    def test_still_listed_fails_after_the_whole_schedule(self) -> None:
        tm_prefs.respond_excluded_uuids(self.fake, [UUID_A.lower()])
        with self.assertRaises(ToolError) as caught:
            sf_macos.require_not_explicitly_excluded(self.fake, UUID_A)
        self.assertIn(
            f"ExcludedVolumeUUIDs still lists {UUID_A}", str(caught.exception)
        )
        self.assertEqual(self.fake.sleeps, list(sf_macos.EXCLUSION_POLL_WAITS))
        self.assertEqual(self._reads(), len(sf_macos.EXCLUSION_POLL_WAITS) + 1)

    def test_a_failed_read_fails_at_once(self) -> None:
        self.fake.respond(
            list(sf_macos.EXCLUDED_UUIDS_ARGV), returncode=1, stderr=b"boom"
        )
        with self.assertRaises(ToolError):
            sf_macos.require_explicitly_excluded(self.fake, UUID_A)
        self.assertEqual((self._reads(), self.fake.sleeps), (1, []))

    def test_the_schedule_escalates(self) -> None:
        waits = sf_macos.EXCLUSION_POLL_WAITS
        self.assertEqual(waits, (0.5, 1.0, 2.0, 4.0, 8.0))


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
