#!/usr/bin/env python3
"""Tests for stdio_utf8, the UTF-8 stream policy this project's entry point applies.

This file holds no tests of its own beyond its guards: it is the one entry point for the
grouped suites in the sibling ``tests/`` directory, which is the only way they run. A
runner that launches each folder's top-level ``test_*.py`` reaches ``tests/`` through
the ``load_tests`` hook below, and through nothing else.

- ``tests/test_streams.py``: forcing a tool's own streams to UTF-8 (stdout strict,
  stderr escaping) and the constants that decode a child's capture.
- ``tests/test_escaping.py``: the escaping boundary for a message quoting argv or a
  file name (``printable``, ``print_escaped``, and ``EscapingArgumentParser``).
- ``tests/test_spell_unsafe.py``: ``spell_unsafe``, which makes a character a terminal
  would act on visible.
- ``tests/test_bootstrap.py``: this project's own ``sys.path`` bootstrap, which puts
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

# What each grouped file must yield, floored separately rather than as one combined total.
# The guard exists because an empty or half-failed discovery is otherwise a passing run:
# "no tests ran" and "every test passed" are the same exit code, and nothing else in the
# run distinguishes them. One combined floor decays by growth, though. Once the suite
# exceeds it by more than the smallest file holds, that file can be lost whole while the
# total still clears the floor, and the guard goes on passing while guarding nothing. A
# per-file floor closes that by construction, and the key-set comparison below means a file
# added later fails as unfloored instead of being absorbed into a combined number.
#
# Restated, not derived: a floor computed from discovery would compare discovery against
# itself and pass for an empty suite, which is the defect this guard exists to catch.
_MIN_TESTS_BY_FILE: dict[str, int] = {
    "test_bootstrap.py": 8,
    "test_escaping.py": 17,
    "test_spell_unsafe.py": 10,
    "test_streams.py": 15,
}

# This module's own tests: the guards below, which are the only reason the file exists.
# Kept apart from the grouped floors rather than summed into one total. A combined number
# carries the grouped suites' growth as slack, and these guards are small enough to vanish
# inside it: two added grouped tests would buy the silent deletion of both. Counted
# apart, no amount of grouped growth pays for a deleted guard.
#
# Restated, not counted off the class below: a count taken from that class falls by one
# when a guard is deleted, so it would pass for exactly the deletion it exists to catch.
# Exact rather than a floor, for the same reason the total was dropped: under ``>=`` an
# added guard becomes room a later deletion can hide in.
_OWN_GUARD_TESTS = 2


def _grouped_suite() -> unittest.TestSuite:
    """Every ``test_*.py`` under ``tests/``, loaded as top-level modules.

    ``top_level_dir`` is the suite directory itself, so the modules need no ``__init__.py`` and
    no package name; unittest puts that directory on ``sys.path`` while loading, so a bare
    import of anything beside them resolves too.
    """
    loader = unittest.TestLoader()
    return loader.discover(str(_SUITE_DIR), top_level_dir=str(_SUITE_DIR))


def _tests_by_module(suite: unittest.TestSuite) -> dict[str, int]:
    """Flatten a (possibly nested) suite into a per-file test count.

    ``discover`` returns a suite of suites (one layer per directory or module it walked),
    so this recurses rather than assuming a fixed depth, and keeps working if that
    internal shape ever changes. Each leaf's ``__module__`` becomes a ``<name>.py`` key,
    matching ``_MIN_TESTS_BY_FILE`` and, for this module's own tests, ``__name__``.
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
    """Fail loudly when a grouped suite is lost, or when a guard in here is."""

    def test_module_load_includes_the_grouped_suites(self) -> None:
        """Guards the ``load_tests`` wiring, which a discovery-based check cannot see.

        A guard calling ``_grouped_suite()`` directly shares its subject's failure mode:
        deleting or renaming ``load_tests`` stops every grouped suite running in the real
        invocation, while such a guard re-derives the same count fresh off disk and stays
        green. ``loadTestsFromModule`` is what a bare run and a test runner actually use,
        and it honors the ``load_tests`` protocol, so this depends on that wiring.

        The two populations are counted apart, never summed: a single total carries the
        grouped suites' growth as slack, and a deleted guard here disappears into it.
        ``__name__`` is what separates them, and it holds under both invocations
        (``__main__`` when run bare, the module name under a runner) because a class
        takes its ``__module__`` from the module that defines it.
        """
        loaded = _tests_by_module(
            unittest.TestLoader().loadTestsFromModule(sys.modules[__name__])
        )
        own_found = loaded.pop(f"{__name__}.py", 0)
        self.assertEqual(
            own_found,
            _OWN_GUARD_TESTS,
            f"this module contributed {own_found} of its own tests, expected exactly "
            f"{_OWN_GUARD_TESTS}: a guard above may have been deleted, or a new one "
            f"added without raising _OWN_GUARD_TESTS",
        )
        # Derived from the per-file floors rather than restated as a second total: that
        # dict is a written-down literal, not a count taken from discovery, so summing it
        # still measures the suite against a number from outside it.
        grouped_found = sum(loaded.values())
        grouped_floor = sum(_MIN_TESTS_BY_FILE.values())
        self.assertGreaterEqual(
            grouped_found,
            grouped_floor,
            f"loading this module yielded {grouped_found} grouped tests, expected at "
            f"least {grouped_floor}: load_tests may have been removed or renamed, "
            f"silently dropping a tests/ suite from the real run",
        )

    def test_grouped_suite_meets_its_own_per_file_floor(self) -> None:
        """Fail when one grouped file has lost tests, and when a new file arrives with no
        floor entry at all."""
        found_by_file = _tests_by_module(_grouped_suite())
        # Compared as full key sets, both ways: a new file with no entry here would
        # otherwise be silently unfloored, its whole-file loss caught only by the combined
        # total, which a gain elsewhere can mask. An unmapped new file, or a mapped file that
        # no longer exists, fails here before any count is compared.
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
                f"tests/{filename} yielded {found} tests, expected at least "
                f"{floor}: a test may have been removed from it",
            )


def load_tests(
    loader: unittest.TestLoader, standard_tests: unittest.TestSuite, pattern: str | None
) -> unittest.TestSuite:
    """unittest's own hook: fold the grouped suites in beside this file's guards."""
    standard_tests.addTests(_grouped_suite())
    return standard_tests


if __name__ == "__main__":
    unittest.main()
