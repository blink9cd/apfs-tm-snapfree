"""``--mode resize --finish``: delete the volume a previous resize retired, then its marker.

A resize keeps the volume it replaced, intact, and leaves a marker comment in the
fstab naming it, so the old copy of the data survives until the operator has checked
the new one. That volume has no fstab line, so a restart may mount it under /Volumes;
it must be unmounted again before it is deleted. Finishing deletes that volume and then removes the marker, in that order:
a failure between the two leaves a marker naming a volume that is gone, which the
recovery line says how to clear by hand, rather than a volume nothing records.

A resize leaves both volumes in Time Machine's ``ExcludedVolumeUUIDs``, and once the
retired one is deleted no ``tmutil`` command can unlist it: ``removeexclusion -v`` needs
the volume mounted. So the last step only reads that list, after the delete and the
marker, and if it still names the deleted volume says the entry is harmless and where
the operator can review exclusions. The tool never edits Time Machine's preferences
behind tmutil's back, so the entry is left for the operator.

Deleting a volume has no undo, so validation first proves the data lives elsewhere:
the folder's fstab line names a volume, that volume is the one mounted there, and it
is not the retired one, and no fstab line, for any mount point, names the retired
one (the next boot would mount it there). The retired volume must then look like what
a resize leaves behind: in the same container, named from the same stem, with no APFS role, and
mounted nowhere.

Entry point assumptions: ``run_finish`` is reached through ``sf_resize.run_resize``
when ``--finish`` is given; every host effect goes through the ``Host`` handed in.
"""

from __future__ import annotations

from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import sf_macos
import sf_privacy
from sf_errors import ToolError
from sf_fstab import (
    RetiredMarker,
    entry_for,
    is_mount_point,
    marker_for,
    parse_entries,
    read_fstab,
    remove_marker,
    same_uuid,
)
from sf_fstab_steps import fstab_step
from sf_host import COMMAND_TIMEOUT, Host, format_argv, run_checked
from sf_options import Options, volume_name_stem
from sf_plan import Step, render, run_plan

#: Printed by the last step when Time Machine still lists the deleted volume.
_STALE_EXCLUSION = (
    "     Note: Time Machine's ExcludedVolumeUUIDs still lists the deleted volume "
    "{uuid}, which tmutil cannot remove once the volume is gone. The entry is "
    "harmless, since no volume has that UUID now, and this tool never edits Time "
    "Machine's preferences: review its exclusions in System Settings > General > "
    "Time Machine > Options."
)


@dataclass(frozen=True)
class FinishJob:
    """What a validated --finish deletes: the retired volume recorded for a folder."""

    path: Path
    fstab: Path
    retired: str


def _pending_marker(text: str, path: Path) -> RetiredMarker:
    """The retired-volume marker for *path*, or a ``ToolError`` if there is none."""
    marker = marker_for(text, path)
    if marker is None:
        raise ToolError(f"no retired volume is pending for {path}: nothing to finish")
    return marker


@dataclass(frozen=True)
class _Current:
    """The volume mounted at the folder now, as validation proved it."""

    uuid: str
    container: str
    name: str


def _mounted_volume(host: Host, text: str, opts: Options) -> sf_macos.ApfsInfo:
    """The volume the fstab names for the folder, proven to be the one mounted there."""
    entry = entry_for(text, opts.path)
    if entry is None:
        raise ToolError(
            f"{opts.fstab} has no line mounting a volume at {opts.path}: nothing proves "
            "the data is on another volume, so the retired one is kept"
        )
    if not is_mount_point(opts.path, host.device_of):
        raise ToolError(
            f"{opts.path} is not mounted: mount the volume the fstab names "
            f"({entry.volume_id}) and check the data before deleting the retired one"
        )
    info = sf_macos.require_apfs(sf_macos.volume_info(host, opts.path), opts.path)
    if not same_uuid(info.volume_uuid, entry.volume_id):
        raise ToolError(
            f"{opts.path} holds volume {info.volume_uuid}, but the fstab names "
            f"{entry.volume_id}: check which volume holds the data first"
        )
    return info


def _current_volume(
    containers: tuple[sf_macos.ApfsContainer, ...], info: sf_macos.ApfsInfo
) -> _Current:
    """The mounted volume as ``diskutil apfs list`` reports it, for its name."""
    found = sf_macos.find_volume(containers, info.volume_uuid)
    if found is None:
        raise ToolError(f"diskutil apfs list reports no volume {info.volume_uuid}")
    return _Current(info.volume_uuid, info.container, found[1].name)


def _retired_volume(
    containers: tuple[sf_macos.ApfsContainer, ...], uuid: str, current: _Current
) -> sf_macos.ApfsVolume:
    """The retired volume, once it looks like the one a resize of *current* left."""
    found = sf_macos.find_volume(containers, uuid)
    if found is None:
        raise ToolError(
            f"the retired volume {uuid} no longer exists: remove its marker from "
            "the fstab by hand (sudo vifs)"
        )
    container, volume = found
    if container.reference != current.container:
        raise ToolError(
            f"the retired volume {uuid} is in {container.reference}, not in "
            f"{current.container} beside {current.name}: refusing to delete it"
        )
    if volume_name_stem(volume.name) != volume_name_stem(current.name):
        raise ToolError(
            f"the retired volume {uuid} is named {volume.name}, which is not a name "
            f"a resize of {current.name} gives: refusing to delete it"
        )
    if volume.roles:
        raise ToolError(
            f"the retired volume {uuid} has the APFS role {', '.join(volume.roles)}, "
            "which no volume this tool makes has: refusing to delete it"
        )
    return volume


def _require_unmounted(host: Host, uuid: str) -> None:
    """Refuse unless the volume *uuid* is mounted nowhere."""
    # diskutil info takes a volume UUID as it is, so this reads the volume's own
    # record, whose MountPoint is empty exactly when it is mounted nowhere, without
    # guessing where it might be. It is an identifier, never a path to stat.
    info = sf_macos.volume_info_for(host, uuid)
    if info.mount_point:
        raise ToolError(
            f"the retired volume {uuid} is mounted at {info.mount_point}: unmount it "
            f"first ({format_argv(['sudo', 'diskutil', 'unmount', info.mount_point])})"
        )


def _require_unlisted(text: str, fstab: Path, uuid: str) -> None:
    """Refuse if any fstab line, for whatever mount point, mounts the volume *uuid*."""
    for entry in parse_entries(text):
        if same_uuid(entry.volume_id, uuid):
            raise ToolError(
                f"{fstab} still mounts the retired volume {uuid} at "
                f"{entry.mount_point}: refusing to delete it; remove or change that "
                "line first (sudo vifs)"
            )


def validate_finish(opts: Options, host: Host) -> FinishJob:
    """The retired volume to delete, or a ``ToolError`` saying why it may not be."""
    text = read_fstab(host, opts.fstab)
    marker = _pending_marker(text, opts.path)
    mounted = _mounted_volume(host, text, opts)
    if same_uuid(mounted.volume_uuid, marker.uuid):
        raise ToolError(
            f"the retired-volume marker names {marker.uuid}, the volume mounted at "
            f"{opts.path}: refusing to delete it"
        )
    _require_unlisted(text, opts.fstab, marker.uuid)
    # One listing serves both volumes, so they are compared as of the same moment.
    containers = sf_macos.apfs_containers(host)
    _retired_volume(containers, marker.uuid, _current_volume(containers, mounted))
    _require_unmounted(host, marker.uuid)
    return FinishJob(opts.path, opts.fstab, marker.uuid)


def _exclusions_step(job: FinishJob, host: Host, out: TextIO) -> Step:
    """Read Time Machine's exclusions, and note a deleted volume still listed there.

    A final cleanup: the volume is deleted and the marker gone by then, so a failed
    read prints only itself, as the retry.
    """
    argv = list(sf_macos.EXCLUDED_UUIDS_ARGV)

    def note() -> None:
        if job.retired.upper() in sf_macos.excluded_volume_uuids(host):
            print(_STALE_EXCLUSION.format(uuid=job.retired), file=out)

    return Step(
        f"check Time Machine's exclusions for the deleted volume {job.retired}: "
        f"{render(argv)}",
        note,
        recover=argv,
        final_cleanup=True,
    )


def plan_finish(job: FinishJob, host: Host, out: TextIO) -> list[Step]:
    """Delete the retired volume, then its marker, then note a stale exclusion of it."""

    def gone() -> None:
        if (
            sf_macos.find_volume(sf_macos.apfs_containers(host), job.retired)
            is not None
        ):
            raise ToolError(
                f"diskutil apfs list still shows the retired volume {job.retired}"
            )

    what = f"remove the marker retiring {job.retired}"
    hand_edit = (
        f"{what} from {job.fstab} by hand (sudo vifs): the volume itself is deleted"
    )
    argv = ["sudo", "diskutil", "apfs", "deleteVolume", job.retired]

    def delete() -> None:
        # Not a command_step: its confirm would raise ConfirmFailed and print the
        # hand-edit line, which is right only once the volume is known to be gone.
        run_checked(host, argv, COMMAND_TIMEOUT)
        gone()

    return [
        Step(argv, delete, recover=hand_edit),
        fstab_step(
            host,
            job.fstab,
            what,
            edit=lambda text: remove_marker(text, job.path),
            took=lambda text: marker_for(text, job.path) is None,
        ),
        _exclusions_step(job, host, out),
    ]


def run_finish(opts: Options, host: Host, out: TextIO) -> int:
    """Validate, then print or perform the deletion of the retired volume."""
    job = validate_finish(opts, host)
    sf_privacy.check_full_disk_access(
        host, sf_privacy.EXCLUSIONS_READ_COMMAND, apply=opts.apply, out=out
    )
    steps = plan_finish(job, host, out)
    print(
        f"Finish the resize of {job.path}: delete the retired volume {job.retired}.",
        file=out,
    )
    return run_plan(steps, opts.apply, out)
