"""Tests for the follow-up commands the modes print for the operator to paste."""

from __future__ import annotations

import unittest
from pathlib import Path
from typing import ContextManager
from unittest import mock

import sf_commands
import sf_fstab

_FOLDER = Path("/fake/my work")
_FSTAB = Path("/fake/other fstab")


def _patched_argv(argv0: str) -> ContextManager[object]:
    return mock.patch.object(sf_commands.sys, "argv", [argv0])


class ToolCommandTest(unittest.TestCase):
    def test_runs_the_file_by_its_own_name_under_python3(self) -> None:
        with _patched_argv("/some/where/tool.py"):
            line = sf_commands.status_command(_FOLDER, sf_fstab.DEFAULT_FSTAB)
        self.assertEqual(line, "python3 tool.py --mode status --path '/fake/my work'")

    def test_a_non_default_fstab_is_carried(self) -> None:
        with _patched_argv("apfs_tm_snapfree.py"):
            line = sf_commands.status_command(_FOLDER, _FSTAB)
        self.assertEqual(
            line,
            "python3 apfs_tm_snapfree.py --mode status --path '/fake/my work' "
            "--fstab '/fake/other fstab'",
        )

    def test_the_finish_command_applies(self) -> None:
        with _patched_argv("apfs_tm_snapfree.py"):
            line = sf_commands.finish_command(_FOLDER, sf_fstab.DEFAULT_FSTAB)
        self.assertEqual(
            line,
            "python3 apfs_tm_snapfree.py --mode resize --path '/fake/my work' "
            "--finish --apply",
        )

    def test_the_finish_command_carries_a_non_default_fstab(self) -> None:
        with _patched_argv("apfs_tm_snapfree.py"):
            line = sf_commands.finish_command(_FOLDER, _FSTAB)
        self.assertTrue(line.endswith("--finish --apply --fstab '/fake/other fstab'"))

    def test_a_program_name_needing_quotes_is_quoted(self) -> None:
        with _patched_argv("/x/my tool.py"):
            line = sf_commands.status_command(_FOLDER, sf_fstab.DEFAULT_FSTAB)
        self.assertTrue(line.startswith("python3 'my tool.py' --mode status"), line)


if __name__ == "__main__":  # pragma: no cover - direct run of this suite
    unittest.main()
