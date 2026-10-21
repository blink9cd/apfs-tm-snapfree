"""``--mode status``: eight read-only checks that a folder's own volume is set up right.

Each check prints one line, ``ok|WARN|FAIL <check>: <detail>``. FAIL means the state
is known to be wrong; WARN means it needs a look but is not known to be wrong (no quota,
use near the quota, a retired volume still kept, or a state that could not be read).
A host command that fails inside a check becomes that check's FAIL line rather than
ending the run, so one denied read still leaves the other checks reported.

When nothing is mounted at the folder, the volume checks are skipped: they would read
the parent volume and report on the wrong thing.

Status never changes anything: it runs only ``diskutil info``, ``diskutil apfs list``,
``tmutil isexcluded`` and ``mdutil -s``, and never ``sudo``.

Entry point assumptions: ``run_status`` is the ``MODES`` handler; ``opts.path`` is
already resolved through symlinks, and every host read goes through ``host``.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TextIO

import sf_commands
import sf_fstab
import sf_macos
from sf_errors import EXIT_FAIL, EXIT_OK, EXIT_USAGE, ToolError
from sf_host import Host
from sf_options import Options
from sf_size import format_size

#: The three levels a check line opens with.
OK = "ok"
WARN = "WARN"
FAIL = "FAIL"
#: The checks, in the order they print.
FSTAB_LINE = "fstab line"
MOUNTED = "mounted"
RIGHT_VOLUME = "right volume"
TM_EXCLUDED = "Time Machine excluded"
SPOTLIGHT_OFF = "Spotlight off"
OWNERSHIP = "ownership honored"
QUOTA_USE = "use against quota"
RETIRED_PENDING = "retired volume pending"
#: What the volume checks say when they cannot run.
_NOT_MOUNTED = "skipped (not mounted)"
_NO_APFS_INFO = "skipped (no APFS volume info)"
_PERCENT = 100


@dataclass(frozen=True)
class CheckLine:
    """One check's verdict: its level, its name and what it found."""

    level: str
    check: str
    detail: str

    def render(self) -> str:
        """The line as printed; a multi-line detail is folded onto one line."""
        detail = " ".join(self.detail.splitlines())
        return f"{self.level} {self.check}: {detail}"


def _guarded(check: str, read: Callable[[], CheckLine]) -> CheckLine:
    """*read*'s line, or a FAIL line for *check* carrying the host error it raised."""
    try:
        return read()
    except ToolError as exc:
        return CheckLine(FAIL, check, str(exc))


def _mounted_check(host: Host, path: Path) -> CheckLine:
    """Check 2: something is mounted at *path*, read from ``st_dev``, never ``df``."""
    try:
        mounted = sf_fstab.is_mount_point(path, host.device_of)
    except OSError as exc:
        return CheckLine(FAIL, MOUNTED, f"could not read the device of {path}: {exc}")
    if mounted:
        return CheckLine(OK, MOUNTED, f"a volume is mounted at {path}")
    return CheckLine(
        FAIL, MOUNTED, f"nothing is mounted at {path}: it is on its parent's volume"
    )


def _right_volume_check(
    host: Host, path: Path, entry: sf_fstab.FstabEntry
) -> tuple[CheckLine, Optional[sf_macos.ApfsInfo]]:
    """Check 3: the mounted volume is the fstab's; also its APFS info, if readable."""
    try:
        info = sf_macos.volume_info(host, path)
        apfs = sf_macos.require_apfs(info, path)
    except ToolError as exc:
        return CheckLine(FAIL, RIGHT_VOLUME, str(exc)), None
    expected = entry.volume_id
    if not sf_fstab.same_uuid(apfs.volume_uuid, expected):
        detail = (
            f"{path} holds volume {apfs.volume_uuid}, but the fstab names {expected}"
        )
        return CheckLine(FAIL, RIGHT_VOLUME, detail), apfs
    return CheckLine(OK, RIGHT_VOLUME, f"{apfs.volume_uuid} ({info.name})"), apfs


def _excluded_line(host: Host, path: Path) -> CheckLine:
    """Check 4: Time Machine excludes *path*.

    It proves the outcome, not its cause: Time Machine treats a volume mounted nobrowse
    as excluded with or without an exclusion of its own, and ``isexcluded`` says so.
    The explicit exclusion lives in ``ExcludedVolumeUUIDs``, which setup and resize
    read under sudo, needing Full Disk Access, to confirm it; status never runs sudo.
    """
    if sf_macos.is_excluded(host, path):
        return CheckLine(OK, TM_EXCLUDED, f"{path} is excluded")
    return CheckLine(FAIL, TM_EXCLUDED, f"{path} is not excluded from backups")


def _spotlight_line(host: Host, path: Path) -> CheckLine:
    """Check 5: Spotlight does not index the volume; unclear mdutil text is unknown."""
    disabled = sf_macos.spotlight_disabled(host, path)
    if disabled is None:
        return CheckLine(WARN, SPOTLIGHT_OFF, "unknown: mdutil -s said neither")
    if disabled:
        return CheckLine(OK, SPOTLIGHT_OFF, "indexing is disabled")
    return CheckLine(FAIL, SPOTLIGHT_OFF, "indexing is enabled")


def _ownership_line(apfs: sf_macos.ApfsInfo) -> CheckLine:
    """Check 6: the volume honors file ownership."""
    if apfs.ownership_enabled:
        return CheckLine(OK, OWNERSHIP, "ownership is enabled")
    return CheckLine(FAIL, OWNERSHIP, "ownership is ignored on this volume")


def _quota_detail(in_use: int, quota: int) -> str:
    """Use against quota for a person, the percentage in whole numbers."""
    percent = in_use * _PERCENT // quota
    return f"{format_size(in_use)} of {format_size(quota)} ({percent}%)"


def _quota_line(host: Host, uuid: str, warn_percent: int) -> CheckLine:
    """Check 7: the volume's use against its quota, from ``diskutil apfs list``."""
    found = sf_macos.find_volume(sf_macos.apfs_containers(host), uuid)
    if found is None:
        return CheckLine(
            FAIL, QUOTA_USE, f"diskutil apfs list reports no volume {uuid}"
        )
    volume = found[1]
    if volume.quota == 0:
        return CheckLine(
            WARN, QUOTA_USE, f"no quota ({format_size(volume.in_use)} used)"
        )
    detail = _quota_detail(volume.in_use, volume.quota)
    # Integer math, so the boundary is exact: no float rounds 89.99% up to 90%.
    if volume.in_use * _PERCENT >= warn_percent * volume.quota:
        return CheckLine(WARN, QUOTA_USE, f"{detail}, at or over {warn_percent}%")
    return CheckLine(OK, QUOTA_USE, detail)


def _apfs_checks(
    host: Host, apfs: Optional[sf_macos.ApfsInfo], warn_percent: int
) -> list[CheckLine]:
    """Checks 6 and 7, which need the volume's APFS info, or their skip lines."""
    if apfs is None:
        return [
            CheckLine(WARN, check, _NO_APFS_INFO) for check in (OWNERSHIP, QUOTA_USE)
        ]
    uuid = apfs.volume_uuid
    return [
        _ownership_line(apfs),
        _guarded(QUOTA_USE, lambda: _quota_line(host, uuid, warn_percent)),
    ]


def _volume_checks(
    opts: Options, host: Host, entry: sf_fstab.FstabEntry
) -> list[CheckLine]:
    """Checks 2 to 7: the mount, then what the mounted volume reports."""
    path = opts.path
    mounted = _mounted_check(host, path)
    if mounted.level != OK:
        skipped = (RIGHT_VOLUME, TM_EXCLUDED, SPOTLIGHT_OFF, OWNERSHIP, QUOTA_USE)
        return [mounted, *(CheckLine(WARN, check, _NOT_MOUNTED) for check in skipped)]
    volume, apfs = _right_volume_check(host, path, entry)
    return [
        mounted,
        volume,
        _guarded(TM_EXCLUDED, lambda: _excluded_line(host, path)),
        _guarded(SPOTLIGHT_OFF, lambda: _spotlight_line(host, path)),
        *_apfs_checks(host, apfs, opts.warn_percent),
    ]


def _retired_check(text: str, path: Path, fstab: Path) -> CheckLine:
    """Check 8: no volume a resize retired is still waiting for ``--finish``."""
    marker = sf_fstab.marker_for(text, path)
    if marker is None:
        return CheckLine(OK, RETIRED_PENDING, "none")
    finish = sf_commands.finish_command(path, fstab)
    detail = (
        f"volume {marker.uuid}, retired by a resize, is kept; delete it with {finish}"
    )
    return CheckLine(WARN, RETIRED_PENDING, detail)


def _not_configured(opts: Options) -> CheckLine:
    """Check 1's line when the fstab has no line for the folder."""
    detail = f"not configured: no {opts.fstab} line mounts a volume at {opts.path}"
    return CheckLine(FAIL, FSTAB_LINE, detail)


def run_status(opts: Options, host: Host, out: TextIO) -> int:
    """Print the eight checks; exit 0 all ok, 1 any WARN or FAIL, 2 not configured."""
    text = sf_fstab.read_fstab(host, opts.fstab)
    entry = sf_fstab.entry_for(text, opts.path)
    if entry is None:
        print(_not_configured(opts).render(), file=out)
        return EXIT_USAGE
    lines = [
        CheckLine(OK, FSTAB_LINE, f"{entry.spec} at {opts.path}"),
        *_volume_checks(opts, host, entry),
        _retired_check(text, opts.path, opts.fstab),
    ]
    for line in lines:
        print(line.render(), file=out)
    return EXIT_OK if all(line.level == OK for line in lines) else EXIT_FAIL
