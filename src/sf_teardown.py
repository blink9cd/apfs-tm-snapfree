"""``--mode teardown``: move a folder's data off its own volume, then delete the volume.

Teardown is setup in reverse, ordered so that every failure leaves the data whole in
one known place. The data is copied beside the folder onto the parent volume first and
counted; only then is the volume unmounted, the copy renamed into the folder's place
and the fstab line removed. Only then is the volume deleted, the one step with no undo,
once the restored folder is proven to be a plain folder holding the data.

Time Machine is checked twice. Step 2's ``removeexclusion`` is confirmed by Time
Machine's own list of excluded volumes, not by ``tmutil isexcluded``, which reports a
volume mounted nobrowse excluded whatever its exclusion. And the last step, once the
volume is gone, asks ``isexcluded`` about the plain folder, where its answer is the one
that matters: whether the restored data is backed up again.

macOS's own folders at the volume root (``sf_walk.VOLUME_PRIVATE``) and the tool's
Spotlight marker (together ``sf_walk.VOLUME_OWN``) are not copied and go with the
volume, so validation first proves each one present is macOS's own folder or the
tool's empty marker, and the run says which are there. Just before the unmount, the copy is compared with the
folder once more, so nothing written after the copy is hidden by the unmount and then
deleted with the volume.

Unmounting uncovers the folder the volume was mounted over. It is normally empty; if
something wrote there while the volume was not mounted, it is moved aside, never
deleted, and the run says where.

Entry point assumptions: ``run_teardown`` is the ``MODES`` handler, called with
resolved options; every command goes through the ``Host``, and the folder's copies are
made and renamed in the folder's own parent.
"""

from __future__ import annotations

import dataclasses
import os
import stat
from dataclasses import dataclass
from pathlib import Path
from typing import TextIO

import sf_commands
import sf_fstab
import sf_macos
import sf_privacy
import sf_unmount
from sf_errors import EXIT_OK, ToolError
from sf_host import COMMAND_TIMEOUT, Host
from sf_options import Options
from sf_plan import (
    ConfirmFailed,
    Slot,
    Step,
    command_step,
    confirmed,
    render,
    run_plan,
)
from sf_size import SPACE_MARGIN, format_size
from sf_volume_copy import copy_step, require_same_data
from sf_walk import (
    NEVER_INDEX,
    TreeStats,
    check_never_index,
    check_private_entries,
    manifest_difference,
    volume_manifest,
    volume_stats,
)

#: The copy made beside the folder: hidden, and named for the folder and the run.
RESTORE_INFIX = ".snapfree-restore-"
#: Where data found in the uncovered folder is moved, beside the folder.
HIDDEN_INFIX = ".snapfree-hidden-"
#: Printed once every step has completed.
DONE_NOTE = (
    "{path} is a plain folder again, and Spotlight and Time Machine now follow the "
    "parent volume's settings."
)
#: The fstab rewrite's commands, shown as one line that stops at the first failure.
_COMMAND_JOIN = " && "
#: Printed before the steps, dry run or not.
_QUIT_WRITERS = (
    "Quit anything that writes to {path} first: whatever it writes once the copy "
    "starts stays on the volume, which teardown deletes, as it does anything "
    "left in the volume's own Trash."
)
#: Printed after that when the volume root holds any of macOS's own folders.
_PRIVATE_GO = (
    "macOS's own {names} at the top of {path} are not copied: they and their "
    "contents, Versions history (.DocumentRevisions-V100) and the volume's Trash "
    "included, go with the volume when teardown deletes it."
)
#: Printed after those when the volume root holds the tool's Spotlight marker.
_MARKER_GO = (
    "This tool's empty {marker} at the top of {path}, which keeps Spotlight off the "
    "volume, is not copied: it goes with the volume, and Spotlight then treats {path} "
    "as the parent volume says."
)
#: Follows a refused unmount: the usual cause, and how to find it.
_BUSY_HINT = (
    "Something has {path} open: quit it (lsof +D {path} lists what), then run the "
    "recovery commands below and teardown again."
)
#: The last step's failure: the folder is restored, yet Time Machine skips it.
_STILL_EXCLUDED = (
    "{path} is a plain folder again and its data is safe there, but Time Machine still "
    "excludes it.\nSee why with {check}; {fix} is the likely fix. Nothing else is "
    "left to do or undo."
)
#: The placeholder the uncovered folder's recovery shows until step 4 has run.
_UNCOVERED_PLACEHOLDER = "<recreate the uncovered folder>"


@dataclass(frozen=True)
class Teardown:
    """What validation established: the folder, its volume and where the copies go."""

    path: Path
    fstab: Path
    uuid: str
    data: TreeStats
    restore: Path
    hidden: Path
    mode: int
    #: The volume-private names at the folder's top, proven macOS's own.
    private: tuple[str, ...]
    #: Whether the folder's top holds the tool's empty Spotlight marker.
    marker: bool = False


def _require_entry(text: str, path: Path, fstab: Path) -> sf_fstab.FstabEntry:
    """The fstab line mounting a volume at *path*, or a refusal."""
    entry = sf_fstab.entry_for(text, path)
    if entry is None:
        raise ToolError(
            f"{fstab} has no line mounting a volume at {path}: nothing to tear down"
        )
    return entry


def _require_mounted(host: Host, path: Path) -> None:
    """Refuse unless *path* is a folder with a volume mounted on it."""
    if not path.is_dir():
        raise ToolError(f"{path} is not a folder")
    if not sf_fstab.is_mount_point(path, host.device_of):
        raise ToolError(
            f"{path} is not mounted: mount its volume first, so its data can be copied "
            "back (or restart, and the fstab line mounts it)"
        )


def _require_same_volume(host: Host, path: Path, entry: sf_fstab.FstabEntry) -> str:
    """The mounted volume's UUID, which must be the one the fstab line names.

    Compared ignoring case: a UUID is hexadecimal, and an fstab written by hand may
    spell it in either case.
    """
    mounted = sf_macos.require_apfs(sf_macos.volume_info(host, path), path).volume_uuid
    if not sf_fstab.same_uuid(mounted, entry.volume_id):
        raise ToolError(
            f"the volume mounted at {path} is {mounted}, but the fstab names "
            f"{entry.volume_id}: check which volume this is before tearing it down"
        )
    return mounted


def _require_no_retired(text: str, path: Path, fstab: Path) -> None:
    """Refuse while a previous resize's retired volume is still pending deletion."""
    if sf_fstab.marker_for(text, path) is not None:
        raise ToolError(
            f"a resize of {path} left a retired volume to delete: run "
            f"{sf_commands.finish_command(path, fstab)} first"
        )


def _require_room(host: Host, path: Path, data: TreeStats) -> None:
    """Refuse unless the parent volume's container holds the data plus the margin."""
    parent = path.parent
    free = sf_macos.require_apfs(
        sf_macos.volume_info(host, parent), parent
    ).container_free
    if data.bytes + SPACE_MARGIN > free:
        raise ToolError(
            f"{path} holds {format_size(data.bytes)}, and the container of {parent} has "
            f"{format_size(free)} free; teardown needs the data plus a "
            f"{format_size(SPACE_MARGIN)} margin there"
        )


def _require_free(path: Path) -> None:
    """Refuse if *path*, where a copy is to go, already exists."""
    if os.path.lexists(path):
        raise ToolError(f"{path} already exists: move it away, then run teardown again")


def validate(opts: Options, host: Host) -> Teardown:
    """Every check teardown needs before its first step, in the order they depend."""
    path = opts.path
    text = sf_fstab.read_fstab(host, opts.fstab)
    entry = _require_entry(text, path, opts.fstab)
    _require_mounted(host, path)
    uuid = _require_same_volume(host, path, entry)
    _require_no_retired(text, path, opts.fstab)
    private = check_private_entries(path)
    marker = check_never_index(path)
    data = volume_stats(path)
    _require_room(host, path, data)
    stamp = host.now()
    restore = path.parent / f".{path.name}{RESTORE_INFIX}{stamp}"
    _require_free(restore)
    hidden = path.with_name(f"{path.name}{HIDDEN_INFIX}{stamp}")
    mode = stat.S_IMODE(os.stat(path).st_mode)
    return Teardown(
        path, opts.fstab, uuid, data, restore, hidden, mode, private, marker
    )


def confirm_plain_folder(host: Host, path: Path) -> None:
    """Raise unless *path* is a folder with nothing mounted on it."""
    if not path.is_dir():
        raise ToolError(f"{path} is not a folder after the rename")
    if sf_fstab.is_mount_point(path, host.device_of):
        raise ToolError(f"{path} is still a mount point")


def _rename_into(source: Path, target: Path) -> None:
    """Rename *source* to *target*, refusing to land on anything already there.

    A folder renamed onto an existing empty folder replaces it without a word, so the
    target is checked first.
    """
    _require_free(target)
    os.rename(source, target)


def _copy_step(host: Host, plan: Teardown) -> Step:
    """Step 1: copy the data beside the folder, onto the parent volume, and count it.

    The restore folder takes the volume root's mode, and the volume's own entries
    (``sf_walk.VOLUME_OWN``, the Spotlight marker included) are neither copied nor
    compared.
    """
    advice = (
        f"quit whatever writes to {plan.path}, check the copy, and run teardown again"
    )

    def matches() -> None:
        require_same_data(plan.path, plan.restore, advice)

    return copy_step(
        host,
        plan.path,
        plan.restore,
        confirm=matches,
        create_mode=plan.mode,
        recover=(
            f"# the restore copy at {plan.restore} can be removed once {plan.path} "
            "is checked"
        ),
    )


def _include_step(host: Host, plan: Teardown) -> Step:
    """Step 2: let Time Machine back the folder up again, and read that back.

    Read back from Time Machine's own list of excluded volumes: ``isexcluded`` reports
    the volume, mounted nobrowse, excluded until it is unmounted, whatever this did.
    """
    path = str(plan.path)
    return command_step(
        host,
        ["sudo", "tmutil", "removeexclusion", "-v", path],
        COMMAND_TIMEOUT,
        recover=["sudo", "tmutil", "addexclusion", "-v", path],
        confirm=lambda: sf_macos.require_not_explicitly_excluded(host, plan.uuid),
    )


def _require_unchanged(plan: Teardown) -> None:
    """Refuse if the folder no longer holds exactly what was copied from it.

    Run just before the unmount: anything written after the copy would otherwise be
    hidden by the unmount, never restored, and deleted with the volume in step 7.
    """
    difference = manifest_difference(
        volume_manifest(plan.path), volume_manifest(plan.restore)
    )
    if difference is not None:
        raise ToolError(
            f"{plan.path} no longer matches the copy at {plan.restore}: something "
            f"wrote to {plan.path} after the copy ({difference}); quit it, then run "
            "the recovery commands below and teardown again"
        )


def _unmount_step(host: Host, plan: Teardown) -> Step:
    """Step 3: unmount the volume, and see the folder back on the parent's device.

    The folder is compared with the copy first, and the command runs only if they
    still match.
    """
    path = str(plan.path)

    def unmounted() -> None:
        if sf_fstab.is_mount_point(plan.path, host.device_of):
            raise ToolError(f"{plan.path} is still a mount point after the unmount")

    step = sf_unmount.unmount_step(
        host,
        plan.path,
        recover=sf_unmount.mount_argv(plan.path, plan.uuid),
        confirm=unmounted,
        hint=_BUSY_HINT.format(path=render([path])),
    )

    def checked_unmount() -> None:
        _require_unchanged(plan)
        step.action()

    return dataclasses.replace(step, action=checked_unmount)


def _uncovered_step(plan: Teardown, out: TextIO) -> Step:
    """Step 4: remove the folder the unmount uncovered, or move it aside if not empty.

    Which of the two happens is decided when the step runs, since only then is the
    folder visible; the recovery line is whichever undoes what was done.
    """
    undo = Slot(_UNCOVERED_PLACEHOLDER)

    def clear() -> None:
        if not os.listdir(plan.path):
            os.rmdir(plan.path)
            undo.set(render(["mkdir", str(plan.path)]))
            return
        _rename_into(plan.path, plan.hidden)
        undo.set(render(["mv", str(plan.hidden), str(plan.path)]))
        print(
            f"     {plan.path} held data written while the volume was not mounted: "
            f"moved it to {plan.hidden}",
            file=out,
        )

    return Step(
        f"rmdir {plan.path} (or move it to {plan.hidden} if not empty)",
        clear,
        recover=undo.text,
    )


def _restore_step(host: Host, plan: Teardown) -> Step:
    """Step 5: rename the copy into the folder's place, and see it is a plain folder."""

    def restore() -> None:
        _rename_into(plan.restore, plan.path)
        confirmed(lambda: confirm_plain_folder(host, plan.path))

    return Step(
        ["mv", str(plan.restore), str(plan.path)],
        restore,
        recover=["mv", str(plan.path), str(plan.restore)],
    )


def _fstab_step(host: Host, plan: Teardown) -> Step:
    """Step 6: remove the folder's fstab line, and read the fstab back without it."""

    def remove() -> None:
        text = host.read_text(plan.fstab)
        sf_fstab.replace_fstab(host, plan.fstab, sf_fstab.remove_entry(text, plan.path))
        if sf_fstab.entry_for(host.read_text(plan.fstab), plan.path) is not None:
            raise ConfirmFailed(f"{plan.fstab} still has a line for {plan.path}")

    commands = sf_fstab.describe_rewrite(plan.fstab, backup=True)
    return Step(
        f"remove the fstab line for {plan.path}: {_COMMAND_JOIN.join(commands)}",
        remove,
        recover=["sudo", "cp", str(sf_fstab.backup_path(plan.fstab)), str(plan.fstab)],
    )


def _delete_step(host: Host, plan: Teardown) -> Step:
    """Step 7: delete the volume, and see it gone from every container.

    A final cleanup: by now the data is restored and proven, so a failure here is
    retried on its own, never undone by taking steps 1 to 6 apart.
    """

    def gone() -> None:
        if sf_macos.find_volume(sf_macos.apfs_containers(host), plan.uuid) is not None:
            raise ToolError(f"diskutil apfs list still reports volume {plan.uuid}")

    argv = ["sudo", "diskutil", "apfs", "deleteVolume", plan.uuid]
    return command_step(
        host, argv, COMMAND_TIMEOUT, recover=argv, confirm=gone, final_cleanup=True
    )


def _backed_up_step(host: Host, plan: Teardown) -> Step:
    """Step 8: see that Time Machine no longer excludes the plain folder.

    A check only, once the volume is gone: if it fails, the data is already restored,
    so nothing is retried or undone, and the message names the read and the likely fix
    rather than guessing further.
    """
    argv = sf_macos.isexcluded_argv(plan.path)

    def check() -> None:
        if sf_macos.is_excluded(host, plan.path):
            fix = ["sudo", "tmutil", "removeexclusion", "-p", str(plan.path)]
            raise ToolError(
                _STILL_EXCLUDED.format(
                    path=plan.path, check=render(argv), fix=render(fix)
                )
            )

    return Step(argv, check, final_cleanup=True)


def build_steps(host: Host, plan: Teardown, out: TextIO) -> list[Step]:
    """Teardown's steps, in safe-failure order: the volume deleted once all else is."""
    return [
        _copy_step(host, plan),
        _include_step(host, plan),
        _unmount_step(host, plan),
        _uncovered_step(plan, out),
        _restore_step(host, plan),
        _fstab_step(host, plan),
        _delete_step(host, plan),
        _backed_up_step(host, plan),
    ]


def _describe(plan: Teardown) -> str:
    """The line printed before the steps: which volume, which folder, how much data."""
    return (
        f"Teardown: move {format_size(plan.data.bytes)} in {plan.data.files} files on "
        f"volume {plan.uuid} back into {plan.path} as a plain folder, then delete the "
        "volume."
    )


def run_teardown(opts: Options, host: Host, out: TextIO) -> int:
    """The ``teardown`` handler: validate, print or perform the steps, then say so.

    A refusal raises ``ToolError`` before anything changes; the entry point turns it
    into exit 2. A failed step returns ``EXIT_FAIL`` once its recovery is printed.
    """
    plan = validate(opts, host)
    sf_privacy.check_full_disk_access(
        host, sf_privacy.INCLUDE_COMMAND, apply=opts.apply, out=out
    )
    print(_describe(plan), file=out)
    print(_QUIT_WRITERS.format(path=plan.path), file=out)
    if plan.private:
        print(
            _PRIVATE_GO.format(names=", ".join(plan.private), path=plan.path), file=out
        )
    if plan.marker:
        print(_MARKER_GO.format(marker=NEVER_INDEX, path=plan.path), file=out)
    code = run_plan(build_steps(host, plan, out), opts.apply, out)
    if opts.apply and code == EXIT_OK:
        print(DONE_NOTE.format(path=plan.path), file=out)
    return code
