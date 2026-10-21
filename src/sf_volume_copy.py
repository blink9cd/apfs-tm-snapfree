"""Copy a volume's data into a folder, entry by entry, leaving macOS's own entries out.

A mounted volume's root holds folders macOS keeps for the volume itself
(``sf_walk.VOLUME_PRIVATE``): root-only, so the user cannot read them, and churning,
so a count of them never matches; and it holds the empty ``.metadata_never_index``
this tool keeps there (``sf_walk.NEVER_INDEX``), which a copy never carries: a new
volume gets its own, and a plain folder none. A whole-root ``ditto <root>/ <dest>/``
would fail on them or carry them into the copy, so each top-level entry of the data is
copied on its
own instead: ``ditto <root>/<entry> <dest>/<entry>``. A top-level symlink is recreated
with ``os.symlink`` rather than handed to ditto, because ditto follows a symlink named
as its source (it keeps the ones it finds inside a folder), which would copy whatever
the link points at in its place.

The entries are listed when the step prints and again when it runs, never at plan
time, so the copy takes what is there then. ``require_same_data`` is the confirm the
plans give the step: it compares the two sides entry by entry (``sf_walk``'s manifest:
every path, its kind, each file's size, each link's target) with the same entries left
out, so a copy missing an empty folder or holding a renamed file fails.

Entry point assumptions: called by the resize and teardown plans; ditto runs through
the ``Host`` as the user, and the destination folder and symlinks are made directly.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

from sf_errors import ToolError
from sf_host import COPY_TIMEOUT, Host, format_argv, run_checked
from sf_plan import Recover, Step, confirmed
from sf_walk import (
    NEVER_INDEX,
    VOLUME_PRIVATE,
    Manifest,
    manifest_difference,
    volume_entries,
    volume_manifest,
)

#: How far each command is indented under its step's first line, clear of the number.
_INDENT = " " * 7
_NOTHING = "(nothing to copy: the volume holds no entries of its own)"
#: The mode a destination folder is made with, before it is given its own: only the
#: user can reach it while it is not yet the copy it will be.
_PRIVATE_MODE = 0o700


def _ditto_argv(root: Path, dest: Path, name: str) -> list[str]:
    """The command copying one top-level entry *name* of *root* into *dest*."""
    return ["ditto", str(root / name), str(dest / name)]


def _entry_command(root: Path, dest: Path, name: str) -> str:
    """How the copy of one entry reads: ``ln -s --`` for a symlink, else ditto.

    The ``--`` keeps a link target that starts with a dash from reading as an option.
    """
    source = root / name
    if source.is_symlink():
        return format_argv(["ln", "-s", "--", os.readlink(source), str(dest / name)])
    return format_argv(_ditto_argv(root, dest, name))


def _mkdir_command(dest: Path, mode: int) -> str:
    """``mkdir -m <mode> <dest>``, which sets the mode exactly, umask aside."""
    return format_argv(["mkdir", "-m", f"{mode:04o}", str(dest)])


def describe_copy(root: Path, dest: Path, create_mode: Optional[int]) -> str:
    """The copy step's display: a summary line, then each command it runs now."""
    private = ", ".join(sorted(VOLUME_PRIVATE))
    head = (
        f"copy each entry at the top of {root} into {dest}, all but the volume's own "
        f"{private} and this tool's {NEVER_INDEX} (as listed when this step prints):"
    )
    try:
        commands = [_entry_command(root, dest, name) for name in volume_entries(root)]
    except (OSError, ToolError) as exc:
        # The display must never raise: the step's own run reports the failure.
        commands = [f"(could not list the entries now: {exc})"]
    if create_mode is not None:
        commands.insert(0, _mkdir_command(dest, create_mode))
    return "\n".join([head, *(f"{_INDENT}{line}" for line in commands or [_NOTHING])])


def copy_entries(host: Host, root: Path, dest: Path) -> None:
    """Copy every top-level entry of the volume *root* into the folder *dest*."""
    for name in volume_entries(root):
        source = root / name
        if source.is_symlink():
            os.symlink(os.readlink(source), dest / name)
        else:
            run_checked(host, _ditto_argv(root, dest, name), COPY_TIMEOUT)


def require_same_data(source: Path, copy: Path, advice: str) -> Manifest:
    """Raise unless the volume *copy* holds exactly the data of the volume *source*.

    Both are compared without their volume-private entries; the ``ToolError`` names
    the first difference each way and ends with *advice*, what the operator should do.
    Returns *source*'s manifest, so a later step can tell whether it has changed since.
    """
    want = volume_manifest(source)
    difference = manifest_difference(want, volume_manifest(copy))
    if difference is not None:
        raise ToolError(
            f"the copy at {copy} does not match {source}: {difference}: {advice}"
        )
    return want


def _copy_then_confirm(
    host: Host, root: Path, dest: Path, confirm: Callable[[], None]
) -> None:
    """Copy every entry, then run *confirm*: the part of the step that changes *dest*."""
    copy_entries(host, root, dest)
    confirm()


def copy_step(
    host: Host,
    root: Path,
    dest: Path,
    *,
    confirm: Callable[[], None],
    create_mode: Optional[int] = None,
    recover: Recover = None,
) -> Step:
    """A step copying the volume *root*'s data into *dest*, then running *confirm*.

    With *create_mode*, *dest* is created first with exactly that mode; without it,
    *dest* must already exist (a mounted volume's root). Once copying has begun, any
    failure is a ``ConfirmFailed``: *dest* may hold part of the data by then, so the
    step's own recovery applies.
    """

    def act() -> None:
        if create_mode is not None:
            # Made private first, so nobody else can reach it before it has its mode.
            os.mkdir(dest, _PRIVATE_MODE)
            os.chmod(dest, create_mode)
        confirmed(lambda: _copy_then_confirm(host, root, dest, confirm))

    return Step(lambda: describe_copy(root, dest, create_mode), act, recover)
