"""Suite aggregator: runs every grouped suite under tests/.

The suites live in tests/, one per module; this file owns only the wiring and one check
on the suite's own shape: every grouped file is discovered and yields tests, so a suite
emptied by a broken import or a renamed class fails loudly instead of passing as zero
tests. The subjects in src/ are put on sys.path by the tests package itself.

Run it directly, or as ``python3 -m unittest discover`` from this folder: both run every
suite exactly once, on any Python from 3.9 up, with nothing installed.
"""

import sys
import unittest
from pathlib import Path
from typing import Iterator, Optional

_ROOT = Path(__file__).resolve().parent
_TESTS = _ROOT / "tests"
# Discovery's top-level dir is this folder, so the suites import as `tests.test_x`.
# import-alone: .
if str(_ROOT) not in sys.path:
    sys.path.insert(0, str(_ROOT))


def _grouped_suite() -> unittest.TestSuite:
    """Discover every test module under tests/."""
    return unittest.TestLoader().discover(str(_TESTS), top_level_dir=str(_ROOT))


def _iter_cases(suite: unittest.TestSuite) -> Iterator[unittest.TestCase]:
    """Yield every TestCase leaf of a nested suite, however deep discovery nests it."""
    for item in suite:
        if isinstance(item, unittest.TestSuite):
            yield from _iter_cases(item)
        else:
            yield item


class SuiteDiscoveryTest(unittest.TestCase):
    """Every grouped suite file is discovered and contributes tests."""

    def test_every_suite_file_yields_tests(self) -> None:
        found = set()
        for case in _iter_cases(_grouped_suite()):
            # A module that fails to import shows up as a _FailedTest stand-in, which
            # is named for the module but lives in unittest itself.
            module = sys.modules[type(case).__module__]
            found.add(Path(str(module.__file__)).name)
        expected = {path.name for path in _TESTS.glob("test_*.py")}
        self.assertTrue(expected, "no grouped suite files were found at all")
        self.assertEqual(found, expected)


def load_tests(
    _loader: unittest.TestLoader, tests: unittest.TestSuite, pattern: Optional[str]
) -> unittest.TestSuite:
    """unittest's hook: this module's own tests, plus the grouped suites when needed.

    Loaded as a module (this file run directly, or named to ``unittest``), the pattern
    is None and nothing else reaches tests/, so it is added here. Under ``discover`` a
    pattern is passed and discovery walks into the tests package itself, so adding it
    here as well would run every suite twice.
    """
    extra = [_grouped_suite()] if pattern is None else []
    return unittest.TestSuite([tests, *extra])


if __name__ == "__main__":
    unittest.main()
