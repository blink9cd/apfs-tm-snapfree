"""Count the files and bytes under a folder, the measure a copy is confirmed against.

Symlinks are counted as entries in their own right and never followed, which is what
``ditto`` copies; sizes are ``lstat`` sizes. An unreadable folder or a file that vanishes
mid-walk is an error, never a silent skip, because a copy confirmed against an
undercount would pass with data missing.

A volume's root also holds folders macOS keeps for the volume itself, root-only and
churning, which are not the operator's data, and the empty ``.metadata_never_index``
this tool keeps there so Spotlight leaves the volume alone: ``volume_stats``,
``volume_entries`` and ``volume_manifest`` leave all of those out, at the root's top
level only. They go by name, so ``check_private_entries`` first proves each macOS name
present is the root-owned folder macOS made, and ``check_never_index`` that the marker
is the empty file the tool made, not something of the operator's that a copy would
silently leave behind.

``tree_manifest`` lists every entry with its kind, its size (a file) or target (a
symlink), so two trees compare entry by entry: a copy missing an empty folder, or
holding a file under another name, differs even where the counts agree.

Entry point assumptions: reads the filesystem directly; the trees it walks are the
operator's folder and the copies this tool made of it.
"""

from __future__ import annotations

import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import AbstractSet, Callable, NamedTuple, Optional, Tuple

from sf_errors import ToolError

#: The entries macOS creates at the top of a mounted volume for the volume's own use.
#: Each is root-only (the user cannot list it) and none is the operator's data, so a
#: copy of a volume's data, and the count confirming it, skip them. They are skipped
#: ONLY at a volume root's top level: the same name deeper down is ordinary data.
VOLUME_PRIVATE = frozenset(
    {
        # The FSEvents journal of changes on this volume; rewritten continually.
        ".fseventsd",
        # Spotlight's index of this volume (absent while indexing is off).
        ".Spotlight-V100",
        # The volume's Trash, one folder per user; deleted with the volume.
        ".Trashes",
        # Scratch space apps are given for safe saves on this volume.
        ".TemporaryItems",
        # The document version store (Versions, autosave) for this volume.
        ".DocumentRevisions-V100",
    }
)
#: The tool's own entry at a new volume's root: an empty file whose presence, when the
#: volume mounts, makes Spotlight leave the volume unindexed. The user owns it, so it is
#: no part of the root-owned proof above; each copy makes or skips its own.
NEVER_INDEX = ".metadata_never_index"
#: Every top-level entry of a volume root that is not the operator's data.
VOLUME_OWN = VOLUME_PRIVATE | {NEVER_INDEX}
_NO_SKIP: AbstractSet[str] = frozenset()
#: The owner macOS gives every one of those entries.
_ROOT_UID = 0

#: An entry's kind in a manifest.
KIND_DIR = "dir"
KIND_FILE = "file"
KIND_SYMLINK = "symlink"
#: A FIFO, socket or device node: none is the operator's data in practice.
KIND_OTHER = "other"


class ManifestEntry(NamedTuple):
    """One entry of a tree: where it is, what it is, and what it holds."""

    #: The path relative to the tree's root, ``/``-separated.
    path: str
    kind: str
    #: The size in bytes of a file; 0 for every other kind.
    size: int
    #: Where a symlink points; empty for every other kind.
    target: str


#: Every entry of a tree, sorted by path.
Manifest = Tuple[ManifestEntry, ...]


@dataclass(frozen=True)
class TreeStats:
    """How many non-directory entries a tree holds, and their total size in bytes."""

    files: int
    bytes: int


def _raise_walk_error(error: OSError) -> None:
    """``os.walk``'s error hook: stop the walk instead of skipping the folder."""
    raise ToolError(f"could not read {error.filename}: {error.strerror}") from error


def _lstat_size(path: str) -> int:
    """The ``lstat`` size of *path*, or a ``ToolError`` naming it."""
    try:
        return os.lstat(path).st_size
    except OSError as exc:
        raise ToolError(f"could not stat {path}: {exc}") from exc


def tree_stats(path: Path, skip_top: AbstractSet[str] = _NO_SKIP) -> TreeStats:
    """Count every file and symlink under *path*, and sum their ``lstat`` sizes.

    Entries of *path* itself named in *skip_top* are left out, and never opened;
    the same names deeper down are counted.
    """
    if not os.path.isdir(path):
        raise ToolError(f"{path} is not a folder that can be read")
    files = 0
    total = 0
    for folder, dirnames, filenames in os.walk(path, onerror=_raise_walk_error):
        if skip_top and folder == os.fspath(path):
            # Pruned in place, so os.walk never descends into a skipped folder.
            dirnames[:] = [name for name in dirnames if name not in skip_top]
            filenames = [name for name in filenames if name not in skip_top]
        # A symlink to a folder is listed among dirnames and, unfollowed, never
        # walked, so it is counted here as the entry it is.
        links = [
            name for name in dirnames if os.path.islink(os.path.join(folder, name))
        ]
        for name in (*filenames, *links):
            files += 1
            total += _lstat_size(os.path.join(folder, name))
    return TreeStats(files, total)


def volume_stats(root: Path) -> TreeStats:
    """``tree_stats`` of a volume's data: *root* without the volume's own entries."""
    return tree_stats(root, skip_top=VOLUME_OWN)


def volume_entries(root: Path) -> list[str]:
    """The names at the top of the volume *root* a copy of its data takes, sorted."""
    try:
        names = os.listdir(root)
    except OSError as exc:
        raise ToolError(f"could not list {root}: {exc.strerror or exc}") from exc
    return sorted(name for name in names if name not in VOLUME_OWN)


def check_private_entries(
    root: Path, lstat: Callable[[str], os.stat_result] = os.lstat
) -> tuple[str, ...]:
    """The ``VOLUME_PRIVATE`` names present at *root*, once each is proven macOS's.

    Each must be a real folder (not a symlink or a file) owned by root: anything else
    of that name is the operator's, and a copy that skips the name would lose it. A
    refusal is a ``ToolError`` naming the entry. *lstat* is injectable because the
    suites cannot make a root-owned folder.
    """
    present = []
    for name in sorted(VOLUME_PRIVATE):
        entry = root / name
        try:
            found = lstat(os.fspath(entry))
        except FileNotFoundError:
            continue
        except OSError as exc:
            raise ToolError(f"could not stat {entry}: {exc.strerror or exc}") from exc
        if not stat.S_ISDIR(found.st_mode) or found.st_uid != _ROOT_UID:
            raise ToolError(
                f"{entry} is not the macOS-managed folder of that name, so the copy "
                "would leave it behind: move it aside or rename it first"
            )
        present.append(name)
    return tuple(present)


def check_never_index(root: Path) -> bool:
    """Whether *root* holds the Spotlight marker, once it is proven the empty file.

    Anything else of that name (a file with content, a folder, a symlink) is the
    operator's: resize and teardown leave the name out of their copies, and setup's
    copy would land it where the tool keeps its own, so it is a ``ToolError`` rather
    than something to lose or overwrite.
    """
    entry = root / NEVER_INDEX
    try:
        found = os.lstat(entry)
    except FileNotFoundError:
        return False
    except OSError as exc:
        raise ToolError(f"could not stat {entry}: {exc.strerror or exc}") from exc
    if not stat.S_ISREG(found.st_mode) or found.st_size != 0:
        raise ToolError(
            f"{entry} is not the empty file this tool keeps there to keep Spotlight "
            "off the volume: move it aside or rename it first"
        )
    return True


def _manifest_entry(full: str, relative: str) -> ManifestEntry:
    """The manifest entry of the filesystem entry at *full*, never following a link."""
    try:
        found = os.lstat(full)
        if stat.S_ISLNK(found.st_mode):
            return ManifestEntry(relative, KIND_SYMLINK, 0, os.readlink(full))
    except OSError as exc:
        raise ToolError(f"could not stat {full}: {exc.strerror or exc}") from exc
    if stat.S_ISDIR(found.st_mode):
        return ManifestEntry(relative, KIND_DIR, 0, "")
    if stat.S_ISREG(found.st_mode):
        return ManifestEntry(relative, KIND_FILE, found.st_size, "")
    return ManifestEntry(relative, KIND_OTHER, 0, "")


def tree_manifest(path: Path, skip_top: AbstractSet[str] = _NO_SKIP) -> Manifest:
    """Every entry under *path*, sorted; *path*'s own entries in *skip_top* left out.

    Symlinks are listed, never followed; an unreadable folder or a vanished entry is
    a ``ToolError``, as in ``tree_stats``.
    """
    if not os.path.isdir(path):
        raise ToolError(f"{path} is not a folder that can be read")
    top = os.fspath(path)
    entries = []
    for folder, dirnames, filenames in os.walk(top, onerror=_raise_walk_error):
        if folder == top:
            # Pruned in place, so os.walk never descends into a skipped folder.
            dirnames[:] = [name for name in dirnames if name not in skip_top]
            filenames = [name for name in filenames if name not in skip_top]
        relative = os.path.relpath(folder, top)
        for name in (*dirnames, *filenames):
            inside = name if relative == os.curdir else f"{relative}/{name}"
            entries.append(_manifest_entry(os.path.join(folder, name), inside))
    return tuple(sorted(entries))


def volume_manifest(root: Path) -> Manifest:
    """``tree_manifest`` of a volume's data: *root* without the volume's own entries."""
    return tree_manifest(root, skip_top=VOLUME_OWN)


def _describe_entry(entry: ManifestEntry) -> str:
    """One entry as a difference names it: its path, kind, and size or target."""
    if entry.kind == KIND_FILE:
        return f"{entry.path} (file, {entry.size} bytes)"
    if entry.kind == KIND_SYMLINK:
        return f"{entry.path} (symlink to {entry.target})"
    if entry.kind == KIND_DIR:
        return f"{entry.path} (folder)"
    return f"{entry.path} (other)"


def _first_and_count(entries: list[ManifestEntry]) -> str:
    """The first of *entries* described, and how many more there are."""
    more = len(entries) - 1
    return _describe_entry(entries[0]) + (f" and {more} more" if more else "")


def manifest_difference(want: Manifest, got: Manifest) -> Optional[str]:
    """None when the copy *got* matches the original *want*, else what differs.

    Names the first entry of the original the copy lacks as it is there, and the
    first entry of the copy the original lacks, each with a count of the rest.
    """
    missing = sorted(set(want) - set(got))
    extra = sorted(set(got) - set(want))
    parts = []
    if missing:
        parts.append(f"not in the copy: {_first_and_count(missing)}")
    if extra:
        parts.append(f"only in the copy: {_first_and_count(extra)}")
    return "; ".join(parts) or None
