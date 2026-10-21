"""``--mode resize``: move a configured folder's data onto a new volume with a new quota.

diskutil has no verb that changes an APFS volume's quota, so a resize is a new volume.
It is added beside the old one in the same container, mounted at a hidden temporary
mount point beside the folder (visible this once, so that its Time Machine exclusion
takes: ruling D8), given the folder's owner and mode and the exclusion, its own
Spotlight marker (and mounted again there, nobrowse, so the marker takes), Spotlight
off, and filled with a copy of the data, the old marker left out. Then the old volume is
unmounted from the folder, the new one mounted there, and the fstab line switched to
the new UUID with a marker comment recording the old one.

The old volume is never deleted here: it stays intact, until the operator has
checked the new copy and runs ``--finish`` (``sf_resize_finish``). It has no fstab line
by then, so a restart may mount it under /Volumes; ``--finish`` refuses that. Each
step's recovery names the real UUIDs once they are known, so after a failure the
recovery block reads as the commands to type.

Entry point assumptions: ``run_resize`` is the ``resize`` entry of the CLI's mode table;
every host effect goes through the ``Host`` handed in, except the temporary mount
point's mkdir and rmdir and the copy's file counts, which the user does directly
beside the folder.
"""

from __future__ import annotations

import dataclasses
import os
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional, TextIO

import sf_macos
import sf_privacy
import sf_resize_confirm
import sf_resize_finish
import sf_spotlight
import sf_unmount
from sf_errors import ToolError
from sf_fstab import (
    add_line,
    entry_for,
    is_mount_point,
    marker_for,
    read_fstab,
    render_marker,
    same_uuid,
    swap_uuid,
)
from sf_commands import finish_command
from sf_fstab_steps import fstab_step
from sf_host import COMMAND_TIMEOUT, Host, run_checked
from sf_options import Options, volume_name_stem
from sf_plan import (
    Arg,
    ConfirmFailed,
    Recover,
    Slot,
    Step,
    command_step,
    render,
    run_plan,
)
from sf_size import SPACE_MARGIN, format_size, quota_arg
from sf_volume_copy import copy_step
from sf_walk import check_never_index, check_private_entries

#: The temporary mount point is ``.<folder name><infix><stamp>`` beside the folder.
_TEMP_INFIX = ".snapfree-resize-"
#: What a dry run shows where the new volume's UUID will be, once addVolume makes it.
NEW_UUID_PLACEHOLDER = "<new-volume-uuid>"
#: addVolume must have made exactly one volume of the new name.
_ONE_VOLUME = 1
#: Printed when the folder's volume root holds any of macOS's own folders.
_PRIVATE_STAY = (
    "macOS's own {names} at the top of {path} are not carried to the new volume: "
    "they and their contents, Versions history (.DocumentRevisions-V100) and the "
    "volume's Trash included, stay on the old volume, which --finish deletes."
)


@dataclass(frozen=True)
class ResizeJob:
    """Everything a validated resize needs, read before any step runs."""

    path: Path
    fstab: Path
    old_uuid: str
    container: str
    in_use: int
    quota: int
    new_name: str
    temp: Path
    uid: int
    gid: int
    mode: int
    filevault: bool
    #: The volume-private names at the folder's top, proven macOS's own.
    private: tuple[str, ...] = ()


def new_volume_name(current: str, stamp: str) -> str:
    """*current* with any trailing ``-<14 digit stamp>`` replaced by ``-<stamp>``.

    The previous stamp is dropped so names do not grow a stamp per resize.
    """
    return f"{volume_name_stem(current)}-{stamp}"


def temp_mount_point(path: Path, stamp: str) -> Path:
    """Where the new volume is mounted while it is filled: hidden, beside *path*."""
    return path.parent / f".{path.name}{_TEMP_INFIX}{stamp}"


# -- validate ------------------------------------------------------------------------


def _required_quota(opts: Options) -> int:
    """The new quota; the command line requires it, so its absence is a caller error."""
    if opts.quota is None:
        raise ToolError("--quota is required for --mode resize: the new volume's size")
    return opts.quota


def _configured_volume(host: Host, text: str, opts: Options) -> sf_macos.ApfsInfo:
    """The volume at the folder, once the fstab names it and it is the one mounted."""
    entry = entry_for(text, opts.path)
    if entry is None:
        raise ToolError(
            f"{opts.fstab} has no line mounting a volume at {opts.path}: "
            "use --mode setup first"
        )
    if not is_mount_point(opts.path, host.device_of):
        raise ToolError(
            f"{opts.path} is not mounted: the fstab names {entry.volume_id}; "
            "see --mode status"
        )
    info = sf_macos.require_apfs(sf_macos.volume_info(host, opts.path), opts.path)
    if not same_uuid(info.volume_uuid, entry.volume_id):
        raise ToolError(
            f"{opts.path} holds volume {info.volume_uuid}, but the fstab names "
            f"{entry.volume_id}"
        )
    return info


def _refuse_pending(text: str, path: Path, fstab: Path) -> None:
    """Refuse while a previous resize's retired volume is still waiting for --finish."""
    marker = marker_for(text, path)
    if marker is not None:
        raise ToolError(
            f"a resize of {path} already retired volume {marker.uuid}: run "
            f"{finish_command(path, fstab)} first"
        )


def _old_volume(container: sf_macos.ApfsContainer, uuid: str) -> sf_macos.ApfsVolume:
    """The volume being replaced, as its container lists it."""
    volume = container.volume_with_uuid(uuid)
    if volume is None:
        raise ToolError(f"container {container.reference} does not list volume {uuid}")
    return volume


def _require_room(in_use: int, quota: int, container: sf_macos.ApfsContainer) -> None:
    """Refuse unless the data and the margin fit both the new quota and the container."""
    needed = in_use + SPACE_MARGIN
    if needed > quota:
        raise ToolError(
            f"the volume holds {format_size(in_use)}, which with the "
            f"{format_size(SPACE_MARGIN)} margin does not fit a {format_size(quota)} quota"
        )
    if container.free < needed:
        raise ToolError(
            f"container {container.reference} has {format_size(container.free)} free, "
            f"short of the {format_size(needed)} the copy needs"
        )


def _require_free_names(
    container: sf_macos.ApfsContainer, name: str, temp: Path
) -> None:
    """Refuse a new volume name or temporary mount point that is already taken."""
    if container.volumes_named(name):
        raise ToolError(f"container {container.reference} already has a volume {name}")
    if os.path.lexists(temp):
        raise ToolError(f"{temp} already exists: move it out of the way first")


def _parent_filevault(host: Host, path: Path) -> bool:
    """Whether FileVault encrypts the volume the folder's parent is on."""
    return sf_macos.volume_info(host, path.parent).filevault is True


def validate_resize(opts: Options, host: Host) -> ResizeJob:
    """The resize to perform, or a ``ToolError`` naming the first reason it may not."""
    quota = _required_quota(opts)
    text = read_fstab(host, opts.fstab)
    info = _configured_volume(host, text, opts)
    _refuse_pending(text, opts.path, opts.fstab)
    private = check_private_entries(opts.path)
    # The copy leaves the marker out: one that is not the tool's empty file would be lost.
    check_never_index(opts.path)
    container = sf_macos.container_volumes(host, info.container)
    old = _old_volume(container, info.volume_uuid)
    _require_room(old.in_use, quota, container)
    stamp = host.now()
    name = new_volume_name(old.name, stamp)
    temp = temp_mount_point(opts.path, stamp)
    _require_free_names(container, name, temp)
    root = os.stat(opts.path)
    return ResizeJob(
        path=opts.path,
        fstab=opts.fstab,
        old_uuid=info.volume_uuid,
        container=container.reference,
        in_use=old.in_use,
        quota=quota,
        new_name=name,
        temp=temp,
        uid=root.st_uid,
        gid=root.st_gid,
        mode=root.st_mode & sf_resize_confirm.MODE_BITS,
        filevault=_parent_filevault(host, opts.path),
        private=private,
    )


# -- plan ----------------------------------------------------------------------------


def _mount_step(
    host: Host, where: Path, uuid: Slot, recover: Recover, *, visible: bool = False
) -> Step:
    """Mount the new volume at *where*, confirmed by the UUID diskutil then reports.

    *visible* is for the first mount at the temporary mount point only (ruling D8).
    """

    def mounted() -> None:
        found = sf_macos.volume_info(host, where).volume_uuid
        if found != uuid.get():
            raise ToolError(f"{where} shows volume {found}, not {uuid.get()}")

    argv = sf_unmount.mount_argv(where, uuid, visible=visible)
    return command_step(host, argv, COMMAND_TIMEOUT, recover=recover, confirm=mounted)


def _add_volume_step(job: ResizeJob, host: Host, uuid: Slot) -> Step:
    """Add the new volume, unmounted; its confirm records the UUID it was given."""

    def found() -> None:
        container = sf_macos.container_volumes(host, job.container)
        named = container.volumes_named(job.new_name)
        if len(named) != _ONE_VOLUME:
            raise ToolError(
                f"after addVolume, found {len(named)} volumes named {job.new_name} "
                f"in {job.container}, not exactly one"
            )
        uuid.set(named[0].uuid)

    argv = ["sudo", "diskutil", "apfs", "addVolume", job.container, "APFS"]
    argv += [job.new_name, "-quota", quota_arg(job.quota), "-nomount"]
    recover: list[Arg] = ["sudo", "diskutil", "apfs", "deleteVolume", uuid]
    return command_step(host, argv, COMMAND_TIMEOUT, recover=recover, confirm=found)


def _remove_temp_line(temp: Path) -> Optional[str]:
    """``rmdir <temp>`` while the temporary mount point is still there."""
    return render(["rmdir", str(temp)]) if temp.is_dir() else None


def _unmount_temp_line(host: Host, temp: Path) -> Optional[str]:
    """``sudo diskutil unmount <temp>`` while a volume is still mounted there."""
    if temp.is_dir() and is_mount_point(temp, host.device_of):
        return render(["sudo", "diskutil", "unmount", str(temp)])
    return None


def _new_volume_steps(job: ResizeJob, host: Host, uuid: Slot) -> list[Step]:
    """Add the volume, make the temporary mount point, and mount the volume on it.

    That first mount is visible, so the exclusion after it takes (ruling D8).
    """
    temp = job.temp
    return [
        _add_volume_step(job, host, uuid),
        Step(
            ["mkdir", str(temp)], lambda: temp.mkdir(), lambda: _remove_temp_line(temp)
        ),
        _mount_step(
            host, temp, uuid, lambda: _unmount_temp_line(host, temp), visible=True
        ),
    ]


def _ownership_on(host: Host, temp: Path) -> bool:
    """Whether the volume at *temp* honors file ownership."""
    info = sf_macos.volume_info(host, temp)
    return sf_macos.require_apfs(info, temp).ownership_enabled


def _ownership_step(host: Host, temp: Path) -> Step:
    """Enable ownership on the new volume, only if it ignores it, and confirm."""
    argv = ["sudo", "diskutil", "enableOwnership", str(temp)]

    def act() -> None:
        if _ownership_on(host, temp):
            return
        run_checked(host, argv, COMMAND_TIMEOUT)
        if not _ownership_on(host, temp):
            raise ConfirmFailed(f"{temp} still ignores ownership after enableOwnership")

    return Step(f"if the new volume ignores ownership: {render(argv)}", act)


def _settings_steps(job: ResizeJob, host: Host, uuid: Slot) -> list[Step]:
    """Give the new volume the folder's owner and mode, and keep it out of indexes.

    The Time Machine exclusion runs while the first mount is still visible, since on
    a nobrowse mount ``addexclusion -v`` lists nothing (ruling D8). The Spotlight
    marker goes on once the user owns the root, and the remount after it, nobrowse
    from then on, is what makes it take, before ``mdutil -i off``.
    """
    temp, where = job.temp, str(job.temp)
    owner = f"{job.uid}:{job.gid}"
    return [
        _ownership_step(host, temp),
        command_step(
            host,
            ["sudo", "chown", owner, where],
            COMMAND_TIMEOUT,
            confirm=sf_resize_confirm.owned(temp, job.uid, job.gid),
        ),
        command_step(
            host,
            ["sudo", "chmod", f"{job.mode:o}", where],
            COMMAND_TIMEOUT,
            confirm=sf_resize_confirm.moded(temp, job.mode),
        ),
        command_step(
            host,
            ["sudo", "tmutil", "addexclusion", "-v", where],
            COMMAND_TIMEOUT,
            confirm=sf_resize_confirm.excluded(host, uuid),
        ),
        sf_spotlight.marker_step(temp),
        # No mount back: every undo ends in deleteVolume, after rmdir of the folder.
        sf_spotlight.remount_step(host, temp, uuid, mount_back=False),
        sf_spotlight.off_step(host, temp),
    ]


def _copy_steps(
    job: ResizeJob, host: Host, data: sf_resize_confirm.CopiedData
) -> list[Step]:
    """Copy the data across as the user, then take the temporary mount away.

    The copy goes entry by entry, so neither volume's own entries (``sf_walk``'s
    ``VOLUME_OWN``: macOS's root-only folders and the tool's Spotlight marker) are
    read, copied or counted; the new volume has its own marker. Its confirm records the
    folder's data in *data*, for the folder's unmount to check against.
    """
    temp = job.temp
    return [
        copy_step(
            host,
            job.path,
            temp,
            confirm=sf_resize_confirm.copied(job.path, temp, data),
        ),
        sf_unmount.unmount_step(
            host,
            temp,
            recover=None,
            confirm=sf_resize_confirm.unmounted(host, temp),
        ),
        Step(["rmdir", str(temp)], lambda: temp.rmdir()),
    ]


def _retires_old(job: ResizeJob, uuid: Slot) -> Callable[[str], bool]:
    """Whether fstab text names the new volume for the folder and retires the old."""

    def took(text: str) -> bool:
        entry = entry_for(text, job.path)
        marker = marker_for(text, job.path)
        new_named = entry is not None and entry.volume_id == uuid.get()
        return new_named and marker is not None and marker.uuid == job.old_uuid

    return took


def _unmount_folder_step(
    job: ResizeJob, host: Host, data: sf_resize_confirm.CopiedData
) -> Step:
    """Unmount the old volume from the folder, once the folder still matches the copy.

    The check runs just before the command: whatever reached the folder after the copy
    would otherwise stay on the old volume, out of sight, until --finish deleted it.
    """
    step = sf_unmount.unmount_step(
        host,
        job.path,
        recover=sf_unmount.mount_argv(job.path, job.old_uuid),
        confirm=sf_resize_confirm.unmounted(host, job.path),
    )

    def checked_unmount() -> None:
        sf_resize_confirm.require_unchanged(job.path, data)
        step.action()

    return dataclasses.replace(step, action=checked_unmount)


def _swap_steps(
    job: ResizeJob, host: Host, uuid: Slot, data: sf_resize_confirm.CopiedData
) -> list[Step]:
    """Swap the volumes at the folder, record the swap in the fstab, and report."""
    path = str(job.path)
    old = job.old_uuid

    def edit(text: str) -> str:
        swapped = swap_uuid(text, job.path, uuid.get())
        return add_line(swapped, render_marker(old, job.path))

    what = f"name the new volume on the line for {path} and add a marker retiring {old}"
    # The old volume has no fstab line once the swap is done, so a restart before
    # --finish may automount it under /Volumes; --finish refuses that and says how
    # to unmount it, so the line says so rather than promising it stays unmounted.
    report = (
        f"keep the old volume {old}, its data intact, until the new one is checked; "
        "it has no fstab line, so a restart may mount it under /Volumes, and "
        "--finish then prints the command to unmount it; delete it with: "
        f"{finish_command(job.path, job.fstab)}"
    )
    return [
        _unmount_folder_step(job, host, data),
        _mount_step(host, job.path, uuid, ["sudo", "diskutil", "unmount", path]),
        fstab_step(host, job.fstab, what, edit, _retires_old(job, uuid)),
        Step(report, lambda: None),
    ]


def plan_resize(job: ResizeJob, host: Host) -> list[Step]:
    """Every step of the resize, in safe-failure order: the old volume is kept."""
    uuid = Slot(NEW_UUID_PLACEHOLDER)
    data = sf_resize_confirm.CopiedData()
    return [
        *_new_volume_steps(job, host, uuid),
        *_settings_steps(job, host, uuid),
        *_copy_steps(job, host, data),
        *_swap_steps(job, host, uuid, data),
    ]


# -- run -----------------------------------------------------------------------------


def _print_notes(job: ResizeJob, out: TextIO) -> None:
    """What the resize does, what to quit first, and the FileVault caveat if it applies."""
    print(
        f"Resize {job.path}: copy volume {job.old_uuid} ({format_size(job.in_use)} in "
        f"use) onto a new volume {job.new_name} with a {format_size(job.quota)} quota "
        f"in {job.container}.",
        file=out,
    )
    print(
        f"Quit anything that writes to {job.path} first: whatever it writes once the "
        "copy starts stays on the old volume.",
        file=out,
    )
    if job.private:
        print(
            _PRIVATE_STAY.format(names=", ".join(job.private), path=job.path), file=out
        )
    if job.filevault:
        print(sf_macos.FILEVAULT_NOTE.format(path=job.path.parent), file=out)


def run_resize(opts: Options, host: Host, out: TextIO) -> int:
    """The ``resize`` mode: ``--finish`` deletes a retired volume, else resize."""
    if opts.finish:
        return sf_resize_finish.run_finish(opts, host, out)
    job = validate_resize(opts, host)
    sf_privacy.check_full_disk_access(
        host, sf_privacy.EXCLUDE_COMMAND, apply=opts.apply, out=out
    )
    steps = plan_resize(job, host)
    _print_notes(job, out)
    return run_plan(steps, opts.apply, out)
