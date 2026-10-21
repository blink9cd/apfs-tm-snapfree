"""Keep Spotlight off a volume the tool makes: the marker, then the remount that reads it.

``mdutil -i off`` alone left a fresh volume reading "unknown indexing state" on current
macOS, through remounts and ``mdutil -X``. An empty ``.metadata_never_index`` at the
volume's root is still honored, but only when it is there as the volume mounts: created
on a mounted volume it changes nothing, while present at mount time it makes ``mdutil -s``
read "Indexing and searching disabled.", a state that then holds even without it. So
setup and resize, once the new volume is mounted and given the folder's owner, create
the marker as the user (``marker_step``) and mount the volume again at the same place
(``remount_step``), before ``mdutil -i off`` (``off_step``).

``mdutil -i off`` is judged by the ``mdutil -s`` read after it, not by its exit code:
on a volume the marker has already kept out of Spotlight it exits 1, printing only a
"disabling Spotlight ... -> kMDConfigSearchLevelFSSearchOnly" log line, while
``mdutil -s`` reads "disabled". A read of "enabled" fails the step; a read that says
neither passes only when ``-i off`` exited zero, since nothing else then says it failed.

The marker stays: it is the tool's own entry at the root (``sf_walk.NEVER_INDEX``),
which no copy carries and no manifest compares.

Entry point assumptions: called by setup and resize while they build a plan; the marker
is made directly in a folder the user owns by then, and every command goes through the
``Host`` handed in.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

import sf_macos
from sf_errors import ToolError
from sf_fstab import is_mount_point
from sf_host import (
    COMMAND_TIMEOUT,
    Host,
    RunResult,
    failure_message,
    format_argv,
    run_checked,
)
from sf_plan import Slot, Step, confirmed, render, resolve
from sf_unmount import mount_argv, unmount_argv, unmount_waiting
from sf_walk import NEVER_INDEX, check_never_index

#: An empty file anyone may read, as ``touch`` would make it under the usual umask.
_MARKER_MODE = 0o644
#: Created only if absent: with O_EXCL, open fails on anything already of that name,
#: a symlink planted there included, which it never follows.
_CREATE_FLAGS = os.O_WRONLY | os.O_CREAT | os.O_EXCL
_WHY = (
    "(as you: an empty file that keeps Spotlight off the volume once it mounts again)"
)


def _create_marker(root: Path) -> None:
    """Make the empty marker at *root*, keeping one already there; confirm it."""
    if not check_never_index(root):
        os.close(os.open(root / NEVER_INDEX, _CREATE_FLAGS, _MARKER_MODE))

    def there() -> None:
        if not check_never_index(root):
            raise ToolError(f"{root / NEVER_INDEX} is not there after creating it")

    confirmed(there)


def marker_step(root: Path) -> Step:
    """Create the empty ``.metadata_never_index`` at the volume root *root*, as the user.

    No recovery of its own: every undo after a later failure ends in deleting the new
    volume, and the marker goes with it.
    """
    display = f"{format_argv(['touch', str(root / NEVER_INDEX)])} {_WHY}"
    return Step(display, lambda: _create_marker(root))


def _mounted_again(host: Host, where: Path, uuid: Slot) -> None:
    """Mount the volume *uuid* at *where* again; confirm it is mounted, and that one."""
    run_checked(host, resolve(mount_argv(where, uuid)), COMMAND_TIMEOUT)
    if not is_mount_point(where, host.device_of):
        raise ToolError(f"{where} is not mounted after diskutil mount")
    found = sf_macos.volume_info(host, where).volume_uuid
    if found != uuid.get():
        raise ToolError(f"{where} is volume {found}, not {uuid.get()}")


def _no_check() -> None:
    """The default check before the mount: nothing to check."""


@dataclass
class _Remount:
    """One remount: where, which volume, and whether it is caught between the two.

    *between* is set once the unmount has taken and cleared once the volume is back,
    so the recovery names the mount only for the remount that stopped half way, never
    for one that completed (resize later unmounts the same place on purpose).
    *before_mount* runs between the two, and raises to stop before the mount.
    """

    host: Host
    where: Path
    uuid: Slot
    before_mount: Callable[[], None] = _no_check
    between: bool = False

    def run(self) -> None:
        """Unmount, waiting out a busy volume, check, then mount the volume again.

        A failure once the unmount has taken, the check's included, is a
        ``ConfirmFailed``, so the recovery block prints this step's own line, the
        mount that brings the volume back, when the step has one.
        """
        unmount_waiting(self.host, self.where)
        if is_mount_point(self.where, self.host.device_of):
            raise ToolError(f"{self.where} is still mounted after diskutil unmount")
        self.between = True
        confirmed(self.before_mount)
        confirmed(lambda: _mounted_again(self.host, self.where, self.uuid))
        self.between = False

    def recovery(self) -> Optional[str]:
        """The mount that brings the volume back, if this remount stopped unmounted."""
        if not self.between or is_mount_point(self.where, self.host.device_of):
            return None
        return render(mount_argv(self.where, self.uuid))


def remount_step(
    host: Host,
    where: Path,
    uuid: Slot,
    *,
    mount_back: bool = True,
    before_mount: Callable[[], None] = _no_check,
) -> Step:
    """Unmount the new volume from *where* and mount it there again, nobrowse.

    The marker is read only as a volume mounts, so this is what makes it take.
    *mount_back* is whether a remount stopped unmounted prints the mount that brings
    the volume back; a plan whose every undo ends in deleting the volume passes
    False, since mounting it again would only put a volume over a folder the
    recovery then removes. *before_mount* runs once the unmount has taken: a check
    that raises stops the step before the mount (setup's, that the folder is still
    empty, so the mount hides nothing).
    """
    remount = _Remount(host, where, uuid, before_mount)

    def shown() -> str:
        return f"{render(unmount_argv(where))} && {render(mount_argv(where, uuid))}"

    return Step(shown, remount.run, remount.recovery if mount_back else None)


def off_argv(path: Path) -> list[str]:
    """``sudo mdutil -i off <path>``: turn Spotlight indexing off for the volume."""
    return ["sudo", "mdutil", "-i", "off", str(path)]


def _off_failure(result: RunResult) -> str:
    """What ``mdutil -i off`` said as it exited non-zero: its exit, stderr and stdout."""
    message = failure_message(result)
    said = result.stdout_text().strip()
    return f"{message}\nits output: {said}" if said else message


def _require_off(host: Host, path: Path, off: RunResult) -> None:
    """Raise unless ``mdutil -s`` reads *path* off, or neither way after a clean *off*.

    The read is the verdict, and *off*'s exit code only breaks a tie.
    """
    status = run_checked(host, sf_macos.mdutil_status_argv(path), COMMAND_TIMEOUT)
    text = status.stdout_text().strip()
    verdict = sf_macos.spotlight_verdict(text)
    if verdict is True:
        return
    failed = off.returncode != 0
    if verdict is False:
        message = f"Spotlight still indexes {path} after mdutil -i off"
        raise ToolError(f"{message}\n{_off_failure(off)}" if failed else message)
    if failed:
        raise ToolError(
            f"{_off_failure(off)}\n"
            f"and mdutil -s does not say Spotlight is off for {path}: {text}"
        )


def off_step(host: Host, path: Path) -> Step:
    """Turn Spotlight off for the volume at *path*; the ``mdutil -s`` read is the verdict.

    No recovery of its own: every undo after a later failure ends in deleting the new
    volume. A read that says neither way after a clean exit is left to
    ``--mode status`` to report, rather than stopping a run whose data is safe.
    """
    argv = off_argv(path)

    def act() -> None:
        off = host.run(argv, COMMAND_TIMEOUT)
        confirmed(lambda: _require_off(host, path, off))

    return Step(tuple(argv), act)
