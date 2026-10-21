"""Grouped suite: this project's sys.path bootstrap never reaches above its own root.

The entry point puts this library on ``sys.path`` from ONE fixed place: the
``stdio_utf8/`` folder beside it at the project root, derived from its own ``__file__``
and appended. Nothing here searches the ancestors, so no folder above a checkout can be
imported in place of the real one, and a missing copy fails at the import. The entry
point's bootstrap comment says why.

Run through ``../test_stdio_utf8.py``'s ``load_tests`` hook, which is how this file runs.
"""

from __future__ import annotations

import ast
import os
import shutil
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

#: A generous ceiling for the black-box child: a hung CLI must fail this suite
#: rather than hang it.
_SUBPROCESS_TIMEOUT = 120

#: This project's root: two levels above this file, which sits in stdio_utf8/tests/.
_PROJECT_ROOT = Path(__file__).resolve().parents[2]

#: The library's folder name, which is also the name an ancestor search looks for.
_LIBRARY = "stdio_utf8"

#: Every file whose bootstrap puts this library on sys.path, relative to the root, with
#: the arguments that run it to a clean exit. The entry point is the one shipped tool
#: that imports this library, and `--help` makes it import all it needs, then exit 0.
_BOOTSTRAPPED: tuple[tuple[str, tuple[str, ...]], ...] = (
    ("apfs_tm_snapfree.py", ("--help",)),
)


def _write_lf(path: Path, text: str) -> None:
    """Write *text* to *path* as UTF-8 with LF line endings, on any platform.

    ``Path.write_text`` takes ``newline`` only from Python 3.10, and this project runs on
    3.9, so the file is opened directly.
    """
    with open(path, "w", encoding="utf-8", newline="\n") as stream:
        stream.write(text)


def _shipped_modules(root: Path) -> list[Path]:
    """Every Python file under *root*, sorted, skipping anything inside a hidden folder.

    A clone's own ``.venv/`` holds installed packages rather than this project's
    modules, and some of those search their ancestors for reasons of their own.
    """
    return [
        path
        for path in sorted(root.rglob("*.py"))
        if not any(part.startswith(".") for part in path.relative_to(root).parts)
    ]


def _ancestor_walk_lines(source: str) -> list[int]:
    """Lines in *source* where a comprehension iterates a ``.parents`` sequence.

    That is the shape of an unbounded upward search. A single ``parents[N]`` index is a
    subscript rather than an iteration, so a bounded path reads as clean.
    """
    return [
        clause.iter.lineno
        for node in ast.walk(ast.parse(source))
        for clause in getattr(node, "generators", [])
        if isinstance(clause.iter, ast.Attribute) and clause.iter.attr == "parents"
    ]


class TestBootstrapStaysInsideTheProject(unittest.TestCase):
    """No bootstrap may import a module from above this project's root.

    The entry point puts this library on sys.path at one fixed place: the stdio_utf8/
    folder beside it. A search of its ancestors instead would also take a same-named
    folder planted ABOVE the checkout -- a shared temporary directory, say -- and would
    take it outright when the project's own copy is missing. These copy the project
    under a parent holding exactly such a folder and run the entry point in a real
    subprocess; the planted modules record that they ran.
    """

    def _plant_decoys(self, parent: Path) -> Path:
        """Plant the folder an ancestor search would take, holding two recording modules.

        ``stdio_utf8.py`` is what makes a search take the folder, and it records if it is
        imported in place of the project's own. ``json.py`` records if the folder lands
        ahead of the standard library: this project's modules import ``json`` after the
        bootstrap has run, so a stranger's folder left early on the path captures that
        import even when the library itself still comes from the right place.
        Returns the path either decoy creates when it runs.
        """
        marker = parent / "decoy-ran"
        folder = parent / _LIBRARY
        folder.mkdir()
        record = f"from pathlib import Path\nPath({str(marker)!r}).touch()\n"
        for name in (f"{_LIBRARY}.py", "json.py"):
            _write_lf(folder / name, record)
        return marker

    def _run(self, script: Path, *args: str) -> tuple[int, str]:
        """Run *script* to exit with no inherited PYTHON* setting, bounded.

        Its output goes to a file rather than a pipe, so the timeout holds whatever the
        child does, and comes back as text so a failure here can name its own cause.
        """
        env = {k: v for k, v in os.environ.items() if not k.startswith("PYTHON")}
        with tempfile.TemporaryDirectory() as folder:
            log = Path(folder) / "child.log"
            with log.open("wb") as sink:
                # reliability: ignore[RL005] a file, not a pipe: nothing is left to read
                done = subprocess.run(
                    [sys.executable, str(script), *args],
                    stdout=sink,
                    stderr=sink,
                    env=env,
                    timeout=_SUBPROCESS_TIMEOUT,
                    check=False,
                )
            output = log.read_text(encoding="utf-8", errors="replace")
        return done.returncode, output

    def _staged_copy(self, parent: Path, *, with_library: bool = True) -> Path:
        """This project copied to ``<parent>/project``, without caches or hidden files.

        *with_library* False leaves out the project's own copy of this library, which
        is the one case where an ancestor search takes the planted folder outright.
        """
        project = parent / "project"
        shutil.copytree(
            _PROJECT_ROOT, project, ignore=shutil.ignore_patterns(".*", "__pycache__")
        )
        if not with_library:
            shutil.rmtree(project / _LIBRARY)
        return project

    def test_no_bootstrap_imports_a_module_planted_above_the_root(self) -> None:
        for script, args in _BOOTSTRAPPED:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as td:
                parent = Path(td)
                marker = self._plant_decoys(parent)
                code, output = self._run(self._staged_copy(parent) / script, *args)
                self.assertFalse(marker.exists(), f"{script} ran a planted module")
                # Also proves the tool really ran: a broken bootstrap exits non-zero.
                self.assertEqual(code, 0, output)

    def test_a_missing_library_fails_at_the_import(self) -> None:
        """With its own copy gone, the entry point stops rather than looking upward.

        A search of the parent directories would find the planted folder and import it;
        a fixed path finds nothing, and the import names the module it could not load.
        """
        for script, args in _BOOTSTRAPPED:
            with self.subTest(script=script), tempfile.TemporaryDirectory() as td:
                parent = Path(td)
                marker = self._plant_decoys(parent)
                project = self._staged_copy(parent, with_library=False)
                code, output = self._run(project / script, *args)
                self.assertFalse(marker.exists(), f"{script} ran a planted module")
                self.assertNotEqual(code, 0, output)
                self.assertIn(f"No module named '{_LIBRARY}'", output)

    def _probe(self, project: Path, bootstrap: str) -> Path:
        """A script inside *project* that runs *bootstrap*, then imports as the tool does."""
        script = project / "probe" / "probe.py"
        script.parent.mkdir()
        program = (
            "import sys\nfrom pathlib import Path\n"
            + bootstrap
            + f"import {_LIBRARY}  # noqa: E402, F401\n"
            + "import json  # noqa: E402, F401\n"
        )
        _write_lf(script, program)
        return script

    def test_the_decoy_fires_for_a_search_past_a_missing_library(self) -> None:
        """The control for a missing library: a nearest-match search takes the decoy.

        Without it a clean result above could mean a dead decoy rather than a fixed path.
        """
        search = (
            "sys.path += [\n"
            "    str(folder)\n"
            "    for ancestor in Path(__file__).resolve().parents\n"
            f"    for folder in (ancestor / {_LIBRARY!r},)\n"
            f"    if (folder / {_LIBRARY + '.py'!r}).is_file()\n"
            "][:1]\n"
        )
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            marker = self._plant_decoys(parent)
            project = self._staged_copy(parent, with_library=False)
            self._run(self._probe(project, search))
            self.assertTrue(marker.exists(), "the decoy never ran; the test is blind")

    def test_the_decoy_fires_for_a_search_that_prepends(self) -> None:
        """The control for a present library: a search that prepends every match.

        The project's own copy still wins its own name there, so this is the ``json``
        decoy firing: the planted folder, left ahead of the standard library, captures
        the next import.
        """
        search = (
            "sys.path[:0] = [\n"
            "    str(folder)\n"
            "    for ancestor in Path(__file__).resolve().parents\n"
            f"    for folder in (ancestor / {_LIBRARY!r},)\n"
            f"    if (folder / {_LIBRARY + '.py'!r}).is_file()\n"
            "]\n"
        )
        with tempfile.TemporaryDirectory() as td:
            parent = Path(td)
            marker = self._plant_decoys(parent)
            self._run(self._probe(self._staged_copy(parent), search))
            self.assertTrue(marker.exists(), "the decoy never ran; the test is blind")

    def test_no_shipped_module_searches_every_ancestor(self) -> None:
        """Structural, so a walk added later fails here and not in a stranger's tree.

        The subprocess tests above cover the bootstrap that exists today; this one reads
        every shipped module.
        """
        offenders = [
            f"{path.relative_to(_PROJECT_ROOT)}:{line}"
            for path in _shipped_modules(_PROJECT_ROOT)
            for line in _ancestor_walk_lines(path.read_text(encoding="utf-8"))
        ]
        self.assertEqual(offenders, [], "an unbounded ancestor search reached a module")

    def test_the_module_scan_skips_a_hidden_folder(self) -> None:
        """The scan's own control: a module in a ``.venv/`` is not this project's."""
        with tempfile.TemporaryDirectory() as td:
            root = Path(td)
            nested = root / ".venv" / "lib"
            nested.mkdir(parents=True)
            for path in (root / "tool.py", nested / "installed.py", root / "src.py"):
                _write_lf(path, "")
            self.assertEqual(
                _shipped_modules(root), [root / "src.py", root / "tool.py"]
            )

    def test_the_walk_detector_names_an_unbounded_search(self) -> None:
        """The detector's own control: it must fire on the shape it exists to refuse."""
        source = (
            "import sys\n"
            "from pathlib import Path\n"
            "sys.path[:0] = [\n"
            "    str(a / 'x') for a in Path(__file__).resolve().parents\n"
            "]\n"
        )
        self.assertEqual(_ancestor_walk_lines(source), [4])

    def test_the_walk_detector_passes_a_bounded_index(self) -> None:
        """``parents[N]`` is the bounded form, and must not read as a walk."""
        source = (
            "from pathlib import Path\nroot = Path(__file__).resolve().parents[1]\n"
        )
        self.assertEqual(_ancestor_walk_lines(source), [])


if __name__ == "__main__":
    unittest.main()
