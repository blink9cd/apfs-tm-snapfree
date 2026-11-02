# Changelog

Notable changes to this project, newest first.

## 0.1.1 - 2026-11-02

### Fixed

- `--help` no longer prints `(default: False)` after `--finish` and `--apply`. Both are off
  unless given, and `--help` now shows them as it shows the other options with no default.

### Changed

- The bundled `stdio_utf8` library gains `spell_unsafe`, which prints each character a
  terminal would act on (control characters, direction marks) as a visible escape, with
  its own suite. The tool itself does not call it.

### Documentation

- The README no longer calls teardown's `removeexclusion -v` on a `nobrowse` mount unproven.
  A live run on macOS 27 showed it removing the volume from Time Machine's list of excluded
  volumes.
- The trial on a throwaway folder now also runs a resize (its dry run first) and `--finish`,
  with what `status` reports between them and a check that the data came across before the
  old volume is deleted.
- *Recovery is printed, never run* now says that teardown's printed
  `sudo tmutil addexclusion -v P` adds nothing while the volume is mounted `nobrowse`, and
  gives the commands that remount it visible first.
- *Running the tests* gives the library's suite its own command,
  `python3 stdio_utf8/test_stdio_utf8.py`. `python3 -m unittest discover` never ran it,
  because `stdio_utf8/` is not a Python package.
- Comments, docstrings, and the library's guide are reworded throughout for a reader new to
  the project. Dashes used as punctuation and all-caps emphasis are gone, and the code
  comments state the reason for a new volume's visible first mount rather than citing a
  decision recorded elsewhere.
- `pyproject.toml` keeps only the tables that `pip`, ruff, mypy, and coverage.py read.

## 0.1.0 - 2026-10-21

- Initial release.
