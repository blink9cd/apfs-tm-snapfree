"""The mount and unmount every mode shares: nobrowse, and wait out a busy volume.

Every ``diskutil mount`` the tool runs or prints comes from ``mount_argv``, which passes
``-mountOptions nobrowse`` unless asked for a visible mount, so a volume it mounts is
kept from Finder and Spotlight. diskutil applies a matching fstab line's options by UUID
even without them, but setup's volume has no fstab line until late in its run, and
resize's temporary mount never does.

The one visible mount is a new volume's first, because ``tmutil addexclusion -v``
exits 0 on a nobrowse mount and adds nothing to Time Machine's list, so setup and
resize exclude the volume while it is visible, then remount it nobrowse for good.

Spotlight's ``mds`` can open a volume the moment it is mounted, even one whose indexing
``mdutil -i off`` has just turned off, and a plain ``diskutil unmount`` is then refused,
"dissented by" mds, for a while before it lets go. A fixed sleep before unmounting would
be a bet on how long that is, so instead a refusal that says the volume is busy is
retried on an escalating schedule, the mount state read again between attempts (the
folder's ``st_dev`` against its parent's, the same read the step's confirm makes), and
the retries stop the moment the folder is seen unmounted. Any other failure fails at
once, unless that same read finds the folder unmounted anyway. A forced unmount is never tried: once a copy is involved, whatever
holds the volume may be writing to it, and the operator should decide.

Entry point assumptions: called by setup, resize and teardown while they build a plan;
every command and every wait goes through the ``Host`` handed in.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Callable, Optional

from sf_errors import ToolError
from sf_fstab import is_mount_point
from sf_host import COMMAND_TIMEOUT, Host, RunResult, failure_message
from sf_plan import Arg, Recover, Step, confirmed

#: The options every mount passes: hidden from Finder's sidebar and the desktop, the
#: state in which Spotlight's mds was never seen to hold a volume.
MOUNT_OPTIONS = ("-mountOptions", "nobrowse")

#: The wait before each retry of a busy unmount: escalating, about half a minute in all,
#: which outlasts the few seconds mds was seen to hold a fresh volume.
UNMOUNT_RETRY_WAITS = (1.0, 2.0, 4.0, 8.0, 16.0)
#: How diskutil says something holds the volume open: by name, or as the errno text.
UNMOUNT_BUSY_MARKERS = ("dissented by", "Resource busy")
#: The process diskutil names, such as ``PID 123 (/path/to/mds)``, to the end of its line.
_DISSENTER = re.compile(r"dissented by (.+)")
_GAVE_UP = (
    "{where} was still busy after {tries} tries over {total:g} s: {holder}. Wait for "
    "it to let go, or quit it, then run the recovery commands below."
)
_HELD_BY = "diskutil says {who} holds it"
_HELD_UNNAMED = "something holds it open (lsof +D {where} lists what)"


def mount_argv(where: Path, volume: Arg, *, visible: bool = False) -> list[Arg]:
    """``sudo diskutil mount -mountOptions nobrowse -mountPoint <where> <volume>``.

    *volume* is a UUID, or the ``Slot`` that will hold one once addVolume has run.
    *visible* leaves nobrowse out, for a new volume's first mount only, so that its
    Time Machine exclusion takes (see the module docstring).
    """
    options = () if visible else MOUNT_OPTIONS
    return [
        "sudo",
        "diskutil",
        "mount",
        *options,
        "-mountPoint",
        str(where),
        volume,
    ]


def unmount_argv(where: Path) -> list[str]:
    """``sudo diskutil unmount <where>``."""
    return ["sudo", "diskutil", "unmount", str(where)]


def _said(result: RunResult) -> str:
    """Everything diskutil printed, both streams: which one it uses is not a contract."""
    return f"{result.stdout_text()}\n{result.stderr_text()}"


def is_busy(result: RunResult) -> bool:
    """Whether a refused unmount says something holds the volume open."""
    text = _said(result)
    return any(marker in text for marker in UNMOUNT_BUSY_MARKERS)


def _holder(result: RunResult, where: Path) -> str:
    """Who holds the volume, as diskutil named it, or how to find out."""
    found = _DISSENTER.search(_said(result))
    if found is None:
        return _HELD_UNNAMED.format(where=where)
    return _HELD_BY.format(who=found.group(1).strip())


def _gave_up(result: RunResult, where: Path) -> ToolError:
    """The error once every retry was refused: the last refusal, and who held on."""
    detail = _GAVE_UP.format(
        where=where,
        tries=len(UNMOUNT_RETRY_WAITS) + 1,
        total=sum(UNMOUNT_RETRY_WAITS),
        holder=_holder(result, where),
    )
    return ToolError(f"{failure_message(result)}\n{detail}")


def unmount_waiting(host: Host, where: Path) -> None:
    """Unmount *where*, retrying while it is busy; a ``ToolError`` once it fails.

    Each call carries the usual per-command timeout. Every failure is followed by a
    read of the mount state, and a folder already unmounted counts as unmounted,
    whatever diskutil's exit said; the step's confirm reads it again.
    """
    argv = unmount_argv(where)
    waits = iter(UNMOUNT_RETRY_WAITS)
    while True:
        result = host.run(argv, COMMAND_TIMEOUT)
        if result.returncode == 0:
            return
        # Read after every failure, busy or not: a folder seen unmounted is an
        # unmount that took, and a caller then goes on as after one that succeeded.
        if not is_mount_point(where, host.device_of):
            return
        if not is_busy(result):
            raise ToolError(failure_message(result))
        wait = next(waits, None)
        if wait is None:
            raise _gave_up(result, where)
        host.sleep(wait)


def unmount_step(
    host: Host,
    where: Path,
    *,
    recover: Recover,
    confirm: Callable[[], None],
    hint: Optional[str] = None,
) -> Step:
    """The step unmounting *where*, waiting out a busy volume, then *confirm*.

    It behaves as ``sf_plan.command_step`` does: *hint* follows the error when the
    unmount itself fails, and a failing *confirm* is a ``ConfirmFailed``.
    """

    def act() -> None:
        try:
            unmount_waiting(host, where)
        except ToolError as exc:
            if hint is None:
                raise
            raise ToolError(f"{exc}\n{hint}") from exc
        confirmed(confirm)

    return Step(unmount_argv(where), act, recover)
