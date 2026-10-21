"""The read-backs resize's steps confirm with: an action only dispatched is not done.

Each function here builds the ``confirm`` callback of one resize step. It reads back,
from the authoritative source, the state its step should have changed (the mount
table's ``st_dev``, the folder's own ``stat``, Time Machine's list of excluded
volumes, a walk of the copy) and raises ``ToolError`` naming what it found when the change did not take.

Entry point assumptions: called by ``sf_resize`` while it builds a plan; the callbacks
run when their steps do, through the ``Host`` handed in or on the user's own folders.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Callable, Optional

import sf_macos
from sf_errors import ToolError
from sf_fstab import is_mount_point
from sf_host import Host
from sf_plan import Slot
from sf_volume_copy import require_same_data
from sf_walk import Manifest, manifest_difference, volume_manifest

#: The permission bits chmod sets, setuid, setgid and sticky included.
MODE_BITS = 0o7777


def unmounted(host: Host, where: Path) -> Callable[[], None]:
    """A confirm that *where* no longer has a volume mounted on it."""

    def confirm() -> None:
        if is_mount_point(where, host.device_of):
            raise ToolError(f"{where} is still mounted after diskutil unmount")

    return confirm


def owned(temp: Path, uid: int, gid: int) -> Callable[[], None]:
    """A confirm that *temp* is owned by *uid*:*gid*."""

    def confirm() -> None:
        found = os.stat(temp)
        if (found.st_uid, found.st_gid) != (uid, gid):
            raise ToolError(
                f"{temp} is owned by {found.st_uid}:{found.st_gid}, not {uid}:{gid}"
            )

    return confirm


def moded(temp: Path, mode: int) -> Callable[[], None]:
    """A confirm that *temp*'s permission bits are *mode*."""

    def confirm() -> None:
        found = os.stat(temp).st_mode & MODE_BITS
        if found != mode:
            raise ToolError(f"{temp} has mode {found:o}, not {mode:o}")

    return confirm


def excluded(host: Host, uuid: Slot) -> Callable[[], None]:
    """A confirm that Time Machine's ``ExcludedVolumeUUIDs`` lists the volume *uuid*.

    Not ``tmutil isexcluded``: it reports any nobrowse volume excluded whatever
    ``addexclusion`` did, so it could not catch an exclusion that did not take.
    """
    return lambda: sf_macos.require_explicitly_excluded(host, uuid.get())


class CopiedData:
    """The folder's data as the copy's confirm found it, held for the unmount's re-check.

    The copy lives on the new volume, which is unmounted before the folder is, so the
    folder cannot be compared with the copy itself by then: it is compared with this
    manifest, taken when the two were last proven equal.
    """

    def __init__(self) -> None:
        """Hold nothing until the copy's confirm records what it found."""
        self._manifest: Optional[Manifest] = None

    def record(self, manifest: Manifest) -> None:
        """Keep *manifest*, the folder's data as the copy matched it."""
        self._manifest = manifest

    def get(self) -> Manifest:
        """The recorded manifest; a ``ToolError`` before the copy is confirmed."""
        if self._manifest is None:
            raise ToolError("the folder was to be re-checked before its copy was")
        return self._manifest


def copied(source: Path, copy: Path, data: CopiedData) -> Callable[[], None]:
    """A confirm that *copy* holds exactly the entries of *source*, recorded in *data*.

    Both are volume roots, so each is compared without its volume-private entries.
    """
    advice = f"quit whatever writes to {source} and resize again"
    return lambda: data.record(require_same_data(source, copy, advice))


def require_unchanged(path: Path, data: CopiedData) -> None:
    """Refuse if the folder no longer holds exactly what was copied from it.

    Run just before the folder's unmount: anything written after the copy would
    otherwise be stranded on the old volume, which ``--finish`` deletes.
    """
    difference = manifest_difference(volume_manifest(path), data.get())
    if difference is not None:
        raise ToolError(
            f"{path} no longer matches what was copied to the new volume: something "
            f"wrote to {path} after the copy ({difference}); quit it, then run the "
            "recovery commands below and resize again"
        )
