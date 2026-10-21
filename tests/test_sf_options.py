"""Tests for the parsed-options record."""

import unittest
from pathlib import Path

import sf_options


def _options(name: "str | None") -> sf_options.Options:
    return sf_options.Options(
        mode="setup",
        path=Path("/fake/work"),
        quota=None,
        name=name,
        fstab=Path("/fake/fstab"),
        warn_percent=90,
        finish=False,
        apply=False,
    )


class VolumeNameTest(unittest.TestCase):
    def test_defaults_to_the_folder_name_with_the_prefix(self) -> None:
        self.assertEqual(_options(None).volume_name, "snapfree-work")

    def test_an_explicit_name_wins(self) -> None:
        self.assertEqual(_options("mine").volume_name, "mine")


class ConstantsTest(unittest.TestCase):
    def test_modes_and_default_percent(self) -> None:
        self.assertEqual(
            sf_options.MODE_NAMES, ("setup", "status", "resize", "teardown")
        )
        self.assertEqual(sf_options.DEFAULT_WARN_PERCENT, 90)


if __name__ == "__main__":
    unittest.main()
