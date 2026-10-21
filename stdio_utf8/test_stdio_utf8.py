#!/usr/bin/env python3
"""Tests for stdio_utf8, the UTF-8 stream policy this project's entry point applies.

This file holds no tests of its own beyond its guards: it is the one entry point for the
grouped suites in the sibling ``tests/`` directory, which is the only way they run. A
runner that launches each folder's top-level ``test_*.py`` reaches ``tests/`` through
the ``load_tests`` hook below, and through nothing else.

- ``tests/test_streams.py`` -- forcing a tool's own streams to UTF-8: stdout strict,
  stderr escaping, and the constants that decode a child's capture.
- ``tests/test_escaping.py`` -- the escaping boundary for a message quoting argv or a
  file name: ``printable``, ``print_escaped`` and ``EscapingArgumentParser``.
- ``tests/test_bootstrap.py`` -- this project's own ``sys.path`` bootstrap, which puts
  this library on the path from one fixed place, the folder beside the entry point, and
  never searches the ancestors.

Entry point assumptions: run bare, from any directory; the insert below puts this folder
on ``sys.path`` explicitly rather than inheriting it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

_HERE = Path(__file__).resolve().parent
_SUITE_DIR = _HERE / "tests"
sys.path.insert(0, str(_HERE))

# What each grouped file must yield, floored SEPARATELY rather than as one combined total.
# An empty or half-failed discovery is otherwise a PASSING run: "no tests ran" and "every
# test passed" are the same exit code. One combined floor decays by growth -- once the
# suite exceeds it by more than the smallest file holds, that file can be lost whole while
# the total still clears it. A per-file floor closes that, and the key-set comparison
# below means a file added later arrives unfloored LOUDLY.
#
# Restated, not derived: a floor computed from discovery would compare discovery against
# itself and pass for an empty suite, which is the defect this guard exists to catch.
_MIN_TESTS_BY_FILE: dict[str, int] = {
    "test_bootstrap.py": 8,
    "test_escaping.py": 17,
    "test_streams.py": 15,
}

# This module's OWN tests: the guards below. Counted apart from the grouped floors, and
# exactly: a combined number would carry the grouped suites' growth as slack a deleted
# guard could hide in, and so would a floor.
_OWN_GUARD_TESTS = 2


def _grouped_suite() -> unittest.TestSuite:
    """Every ``test_*.py`` under ``tests/``, loaded as top-level modules.

    ``top_level_dir`` is the suite directory itself, so the modules need no ``__init__.py``
    and no package name.
    """
    loader = unittest.TestLoader()
    return loader.discover(str(_SUITE_DIR), top_level_dir=str(_SUITE_DIR))


def _tests_by_module(suite: unittest.TestSuite) -> dict[str, int]:
    """Flatten a (possibly nested) suite into a per-file test count.

    ``discover`` returns a suite of suites, so this recurses rather than assuming a fixed
    depth. Each leaf's ``__module__`` becomes a ``<name>.py`` key, matching
    ``_MIN_TESTS_BY_FILE`` -- and, for this module's own tests, ``__name__``.
    """
    counts: dict[str, int] = {}
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            for module, count in _tests_by_module(item).items():
                counts[module] = counts.get(module, 0) + count
        else:
            filename = f"{type(item).__module__}.py"
            counts[filename] = counts.get(filename, 0) + 1
    return counts


class GroupedSuiteDiscoveryTest(unittest.TestCase):
    """Fail loudly when a grouped suite is lost -- or when a guard in here is."""

    def test_module_load_includes_the_grouped_suites(self) -> None:
        """Guards the ``load_tests`` WIRING, which a discovery-based check cannot see.

        Deleting or renaming ``load_tests`` stops every grouped suite running, while a
        guard calling ``_grouped_suite()`` directly would re-derive the same count off disk
        and stay green. ``loadTestsFromModule`` honors the ``load_tests`` protocol, as the
        real run does. ``__name__`` separates the two populations under every invocation,
        because a class takes its ``__module__`` from the module that defines it.
        """
        loaded = _tests_by_module(
            unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
        )
        own_found = loaded.pop(f"{__name__}.py", 0)
        self.assertEqual(
            own_found,
            _OWN_GUARD_TESTS,
            f"this module contributed {own_found} of its own tests, expected exactly "
            f"{_OWN_GUARD_TESTS}",
        )
        grouped_found = sum(loaded.values())
        grouped_floor = sum(_MIN_TESTS_BY_FILE.values())
        self.assertGreaterEqual(
            grouped_found,
            grouped_floor,
            f"loading this module yielded {grouped_found} grouped tests, expected at "
            f"least {grouped_floor} -- load_tests may have been removed or renamed",
        )

    def test_grouped_suite_meets_its_own_per_file_floor(self) -> None:
        """Fail when ONE grouped file has lost tests, or a file arrives unfloored."""
        found_by_file = _tests_by_module(_grouped_suite())
        self.assertEqual(
            found_by_file.keys(),
            _MIN_TESTS_BY_FILE.keys(),
            f"tests/ discovery and _MIN_TESTS_BY_FILE disagree on which files "
            f"exist: found {sorted(found_by_file)}, mapped {sorted(_MIN_TESTS_BY_FILE)}",
        )
        for filename, floor in _MIN_TESTS_BY_FILE.items():
            found = found_by_file.get(filename, 0)
            self.assertGreaterEqual(
                found,
                floor,
                f"tests/{filename} yielded {found} tests, expected at least {floor}",
            )


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """unittest's own hook: fold the grouped suites in beside this file's guards."""
    standard_tests.addTests(_grouped_suite())
    return standard_tests


if __name__ == "__main__":
    unittest.main()
