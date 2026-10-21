"""Setup mode: move a folder onto a new APFS volume mounted at the same path.

``run_setup`` validates every precondition first (a refusal is a ``ToolError``, which
the entry point turns into exit 2), then builds the whole twelve-step plan and hands it to
``sf_plan.run_plan``, which prints it as a dry run or performs it with ``--apply``.

The steps run in safe-failure order: the data stays recoverable after each one, and the
fstab is written only once the volume has been proven to mount. The original folder is
renamed aside and never deleted; the last step prints how to delete it once checked.
The new volume's UUID exists only after ``addVolume`` has run, so it lives in a
``Slot`` that step's confirm fills, and the steps after it read it as they print.

Entry point assumptions: called with resolved options on macOS as a non-root user; every
command goes through the ``Host``, and the folder, its aside and the copy check are
direct filesystem work on the user's own folder.
"""

from __future__ import annotations

import os
import shlex
import stat
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Optional, TextIO

import sf_commands
import sf_fstab
import sf_macos
import sf_privacy
import sf_spotlight
import sf_unmount
from sf_errors import ToolError
from sf_host import COMMAND_TIMEOUT, COPY_TIMEOUT, Host, format_argv, run_checked
from sf_options import Options
from sf_plan import Arg, ConfirmFailed, Slot, Step, command_step, run_plan
from sf_size import SPACE_MARGIN, format_size, quota_arg
from sf_walk import (
    VOLUME_OWN,
    TreeStats,
    check_never_index,
    manifest_difference,
    tree_manifest,
    tree_stats,
)

#: What the dry run shows until addVolume has run and its confirm read the UUID back.
UUID_PLACEHOLDER = "<new-volume-uuid>"
#: The original folder is renamed to its own name plus this and a stamp.
ASIDE_INFIX = ".snapfree-aside-"
#: How far the fstab rewrite's commands are indented under their step's first line,
#: clear of the step number.
_REWRITE_INDENT = " " * 7


@dataclass(frozen=True)
class SetupFacts:
    """What validation established, and every value the plan is built from."""

    path: Path
    aside: Path
    fstab: Path
    fstab_exists: bool
    container: str
    name: str
    quota: int
    mode: int
    uid: int
    gid: int
    filevault: bool


# -- validate --------------------------------------------------------------------------


def _require_quota(opts: Options) -> int:
    """The quota; the entry point already requires it for setup, so this is a backstop."""
    if opts.quota is None:
        raise ToolError("setup needs --quota: the size ceiling of the new volume")
    return opts.quota


def _check_folder(path: Path, uid: int) -> os.stat_result:
    """*path*'s ``lstat``, once it is known to be a folder *uid* owns."""
    if not os.path.lexists(path):
        raise ToolError(f"{path} does not exist")
    info = os.lstat(path)
    if not stat.S_ISDIR(info.st_mode):
        raise ToolError(f"{path} is not a folder")
    if info.st_uid != uid:
        raise ToolError(
            f"{path} is not owned by you (user {uid}): setup restores ownership to "
            "the user running it"
        )
    return info


def _read_fstab(host: Host, fstab: Path) -> Optional[str]:
    """The fstab's text, or None when there is no fstab yet."""
    try:
        return host.read_text(fstab)
    except FileNotFoundError:
        return None


def _check_unconfigured(host: Host, path: Path, text: Optional[str]) -> None:
    """Refuse a folder something is mounted at, or one the fstab already names."""
    if sf_fstab.is_mount_point(path, host.device_of):
        raise ToolError(f"{path} is already a mount point")
    if text is not None and sf_fstab.entry_for(text, path) is not None:
        raise ToolError(f"the fstab already has a line mounting a volume at {path}")


def _check_name_free(host: Host, container: str, name: str) -> None:
    """Refuse a volume name the container already holds."""
    if sf_macos.container_volumes(host, container).volumes_named(name):
        raise ToolError(
            f"a volume named {name} already exists in {container}: choose another "
            "with --name"
        )


def _check_space(data: TreeStats, quota: int, free: int) -> None:
    """Refuse data that does not fit the quota, or the container with the margin."""
    if data.bytes >= quota:
        raise ToolError(
            f"the folder holds {format_size(data.bytes)}, which does not fit under "
            f"the quota of {format_size(quota)}"
        )
    if data.bytes + SPACE_MARGIN > free:
        raise ToolError(
            f"the folder holds {format_size(data.bytes)}, and copying it needs that "
            f"plus {format_size(SPACE_MARGIN)} free; the container has "
            f"{format_size(free)} free"
        )


def _aside_for(path: Path, stamp: str) -> Path:
    """Where the original folder is renamed to; a ``ToolError`` if that is taken."""
    aside = path.with_name(f"{path.name}{ASIDE_INFIX}{stamp}")
    if os.path.lexists(aside):
        raise ToolError(f"{aside} already exists: move it, then run setup again")
    return aside


def validate(opts: Options, host: Host) -> SetupFacts:
    """Every precondition of setup, in the spec's order, or a ``ToolError``."""
    quota = _require_quota(opts)
    uid = host.getuid()
    folder = _check_folder(opts.path, uid)
    text = _read_fstab(host, opts.fstab)
    _check_unconfigured(host, opts.path, text)
    apfs = sf_macos.require_apfs(sf_macos.volume_info(host, opts.path), opts.path)
    _check_name_free(host, apfs.container, opts.volume_name)
    _check_space(tree_stats(opts.path), quota, apfs.container_free)
    # An empty marker of the tool's name is copied as it is; anything else is refused.
    check_never_index(opts.path)
    return SetupFacts(
        path=opts.path,
        aside=_aside_for(opts.path, host.now()),
        fstab=opts.fstab,
        fstab_exists=text is not None,
        container=apfs.container,
        name=opts.volume_name,
        quota=quota,
        mode=stat.S_IMODE(folder.st_mode),
        uid=uid,
        gid=host.getgid(),
        filevault=apfs.filevault,
    )


# -- plan: steps 1 to 3, the volume and the mount --------------------------------------


def _add_volume_step(host: Host, facts: SetupFacts, uuid: Slot) -> Step:
    """Step 1: create the volume unmounted; confirm exactly one has the name."""

    def found() -> None:
        named = sf_macos.container_volumes(host, facts.container).volumes_named(
            facts.name
        )
        if len(named) != 1:
            raise ToolError(
                f"after addVolume, found {len(named)} volumes named {facts.name} in "
                f"{facts.container}, not one"
            )
        uuid.set(named[0].uuid)

    argv = ["sudo", "diskutil", "apfs", "addVolume", facts.container, "APFS"]
    argv += [facts.name, "-quota", quota_arg(facts.quota), "-nomount"]
    recover: list[Arg] = ["sudo", "diskutil", "apfs", "deleteVolume", uuid]
    return command_step(host, argv, COMMAND_TIMEOUT, recover=recover, confirm=found)


def _mode_text(mode: int) -> str:
    """A permission mode as chmod and mkdir take it: ``0750``."""
    return f"{mode:04o}"


def _put_back(facts: SetupFacts) -> str:
    """The command that undoes the rename aside, whether or not the folder came back.

    A bare ``mv`` onto a recreated folder would move the aside INTO it, hiding the
    original one level down; ``rmdir`` first removes the empty recreation, and on a
    missing folder it fails before ``mv`` runs, so one form serves both cases.
    """
    rmdir = format_argv(["rmdir", str(facts.path)])
    return f"{rmdir} && {format_argv(['mv', str(facts.aside), str(facts.path)])}"


def _move_aside(facts: SetupFacts) -> None:
    """Rename the folder aside, then recreate it empty with its original mode.

    rename, mkdir and chmod are synchronous calls that raise on failure, so their
    return is the confirmation. A failure after the rename says where the data is,
    because this step then counts as not done and prints no recovery of its own.
    """
    os.rename(facts.path, facts.aside)
    try:
        os.mkdir(facts.path)
        os.chmod(facts.path, facts.mode)
    except OSError as exc:
        raise ToolError(
            f"{facts.path} was moved to {facts.aside} but could not be recreated "
            f"({exc}); to put it back: {_put_back(facts)}"
        ) from exc


def _aside_step(facts: SetupFacts) -> Step:
    """Step 2: rename the folder aside (instant, same volume) and recreate it."""
    path, aside = shlex.quote(str(facts.path)), shlex.quote(str(facts.aside))
    display = f"mv {path} {aside} && mkdir -m {_mode_text(facts.mode)} {path}"
    return Step(display, lambda: _move_aside(facts), _put_back(facts))


def _require_empty(facts: SetupFacts) -> None:
    """Refuse to mount over a folder that gained entries since it was recreated.

    The mount would hide them, and they are not in the aside the copy reads from.
    """
    count = len(os.listdir(facts.path))
    if count:
        raise ToolError(
            f"{facts.path} holds {count} entries written after it was recreated, "
            f"which the mount would hide: move them back into {facts.aside}, then "
            f"{_put_back(facts)}"
        )


def _require_still_empty(facts: SetupFacts) -> None:
    """Refuse to mount the volume again over entries written while it was unmounted.

    Step 8's remount uncovers the bare folder for a moment; anything written there
    then would be hidden by the mount, and is not in the aside the copy reads from.
    """
    names = sorted(os.listdir(facts.path))
    if names:
        them = "it" if len(names) == 1 else "them"
        raise ToolError(
            f"{facts.path} holds {', '.join(names)}, written while the volume was "
            f"unmounted, which the mount would hide: move {them} into {facts.aside}, "
            "then run the recovery commands below"
        )


def _mount_step(host: Host, facts: SetupFacts, uuid: Slot) -> Step:
    """Step 3: mount the new volume at the folder, visible; confirm it is that volume.

    Visible so that step 6's exclusion takes (ruling D8); step 8 remounts it nobrowse.
    """

    def mounted() -> None:
        if not sf_fstab.is_mount_point(facts.path, host.device_of):
            raise ToolError(f"{facts.path} is not mounted after diskutil mount")
        found = sf_macos.volume_info(host, facts.path).volume_uuid
        if found != uuid.get():
            raise ToolError(f"{facts.path} is volume {found}, not {uuid.get()}")

    argv = sf_unmount.mount_argv(facts.path, uuid, visible=True)
    recover = ["sudo", "diskutil", "unmount", str(facts.path)]
    step = command_step(host, argv, COMMAND_TIMEOUT, recover=recover, confirm=mounted)

    def act() -> None:
        _require_empty(facts)
        step.action()

    return replace(step, action=act)


# -- plan: steps 4 to 9, the volume's settings -----------------------------------------


def _ownership_honored(host: Host, path: Path) -> bool:
    """Whether the volume mounted at *path* honors file ownership."""
    return sf_macos.require_apfs(
        sf_macos.volume_info(host, path), path
    ).ownership_enabled


def _ownership_step(host: Host, facts: SetupFacts) -> Step:
    """Step 4: enable ownership, only if the mounted volume ignores it."""
    argv = ["sudo", "diskutil", "enableOwnership", str(facts.path)]

    def enable() -> None:
        if _ownership_honored(host, facts.path):
            return
        run_checked(host, argv, COMMAND_TIMEOUT)
        if not _ownership_honored(host, facts.path):
            raise ConfirmFailed(
                f"{facts.path} still ignores ownership after enableOwnership"
            )

    return Step(f"{format_argv(argv)} (if ownership is ignored)", enable)


def _owner_step(host: Host, facts: SetupFacts) -> Step:
    """Step 5: give the volume's root the folder's owner and mode; confirm by stat."""
    path, mode = str(facts.path), _mode_text(facts.mode)
    chown = ["sudo", "chown", f"{facts.uid}:{facts.gid}", path]
    chmod = ["sudo", "chmod", mode, path]

    def own() -> None:
        run_checked(host, chown, COMMAND_TIMEOUT)
        run_checked(host, chmod, COMMAND_TIMEOUT)
        info = os.stat(facts.path)
        seen = (info.st_uid, info.st_gid, stat.S_IMODE(info.st_mode))
        if seen != (facts.uid, facts.gid, facts.mode):
            raise ConfirmFailed(
                f"{path} has owner {seen[0]}:{seen[1]} and mode {_mode_text(seen[2])} "
                f"after chown and chmod, not {facts.uid}:{facts.gid} and {mode}"
            )

    return Step(f"{format_argv(chown)} && {format_argv(chmod)}", own)


def _exclusion_step(host: Host, facts: SetupFacts, uuid: Slot) -> Step:
    """Step 6: exclude the whole volume from Time Machine; confirm the exclusion.

    It runs while step 3's mount is still visible: on a nobrowse mount
    ``addexclusion -v`` exits 0 and lists nothing (ruling D8). Confirmed by Time
    Machine's own list of excluded volumes, never ``isexcluded``, which reports any
    nobrowse volume excluded, so it would pass once the volume is hidden whatever
    this did. No recovery of its own: every undo ends in deleting the volume, and a
    deleted volume's UUID left in the list is harmless.
    """
    argv = ["sudo", "tmutil", "addexclusion", "-v", str(facts.path)]
    return command_step(
        host,
        argv,
        COMMAND_TIMEOUT,
        confirm=lambda: sf_macos.require_explicitly_excluded(host, uuid.get()),
    )


# -- plan: steps 10 to 12, the fstab, the copy and the report --------------------------


@dataclass
class _FstabSeen:
    """Whether the fstab existed when step 10 read it, so its recovery matches.

    Starts as validation saw it and is overwritten as the step reads the fstab just
    before rewriting it, because ``replace_fstab`` backs up only a file it finds then.
    """

    existed: bool


def _fstab_recovery(fstab: Path, seen: _FstabSeen) -> str:
    """How to put the fstab back: restore the backup, or remove a file setup created."""
    if seen.existed:
        backup = sf_fstab.backup_path(fstab)
        return format_argv(["sudo", "cp", str(backup), str(fstab)])
    return format_argv(["sudo", "rm", str(fstab)])


def _write_fstab(host: Host, facts: SetupFacts, uuid: Slot, seen: _FstabSeen) -> None:
    """Append the volume's line to the fstab as it is now; confirm by re-reading."""
    text = _read_fstab(host, facts.fstab)
    seen.existed = text is not None
    line = sf_fstab.render_line(uuid.get(), facts.path)
    sf_fstab.replace_fstab(host, facts.fstab, sf_fstab.add_line(text or "", line))
    entry = sf_fstab.entry_for(_read_fstab(host, facts.fstab) or "", facts.path)
    if entry is None or entry.volume_id != uuid.get():
        raise ConfirmFailed(
            f"the fstab at {facts.fstab} does not mount {uuid.get()} at {facts.path}"
        )


def _fstab_step(host: Host, facts: SetupFacts, uuid: Slot) -> Step:
    """Step 10: add the boot-time mount line, now the volume is proven to mount."""

    def describe() -> str:
        line = sf_fstab.render_line(uuid.text(), facts.path)
        commands = sf_fstab.describe_rewrite(facts.fstab, backup=facts.fstab_exists)
        rows = [f"add `{line}` to {facts.fstab}:"]
        rows += [f"{_REWRITE_INDENT}{command}" for command in commands]
        return "\n".join(rows)

    # The display is a function, worked out as the step prints, because it names
    # the slot inside a larger multi-line text, which an argv display cannot express.
    seen = _FstabSeen(facts.fstab_exists)
    return Step(
        describe,
        lambda: _write_fstab(host, facts, uuid, seen),
        lambda: _fstab_recovery(facts.fstab, seen),
    )


def _copy_step(host: Host, facts: SetupFacts) -> Step:
    """Step 11: copy the data back in as the user; confirm every entry matches."""

    def matches() -> None:
        # The new volume's root gains macOS's own entries once mounted (.fseventsd)
        # and the tool's Spotlight marker, which are not part of the copy; skip only
        # those the aside does not hold (an empty marker of its own is copied as is).
        own = VOLUME_OWN - set(os.listdir(facts.aside))
        want = tree_manifest(facts.aside)
        difference = manifest_difference(want, tree_manifest(facts.path, skip_top=own))
        if difference is not None:
            raise ToolError(
                f"the copy in {facts.path} does not match {facts.aside}: {difference}"
            )

    # No sudo: the user's own copy keeps the user's ownership on every file.
    argv = ["ditto", f"{facts.aside}/", f"{facts.path}/"]
    return command_step(host, argv, COPY_TIMEOUT, confirm=matches)


def status_command(opts: Options) -> str:
    """The status command to run after the next reboot, naming a non-default fstab."""
    return sf_commands.status_command(opts.path, opts.fstab)


def _report_step(facts: SetupFacts, status: str, out: TextIO) -> Step:
    """Step 12: say where the original is and how to delete it; never delete it."""

    def report() -> None:
        print(f"The original folder is kept at {facts.aside}.", file=out)
        print("Check the copy, then delete the original yourself:", file=out)
        print(f"  {format_argv(['rm', '-rf', str(facts.aside)])}", file=out)
        print("After the next reboot, check the volume mounted:", file=out)
        print(f"  {status}", file=out)

    return Step(
        f"keep {facts.aside}, and print how to delete it after checking", report
    )


def plan(opts: Options, host: Host, facts: SetupFacts, out: TextIO) -> list[Step]:
    """The twelve steps, in safe-failure order; the new UUID shared through one slot."""
    uuid = Slot(UUID_PLACEHOLDER)
    return [
        _add_volume_step(host, facts, uuid),
        _aside_step(facts),
        _mount_step(host, facts, uuid),
        _ownership_step(host, facts),
        _owner_step(host, facts),
        # While the first mount is visible, so the exclusion takes (ruling D8).
        _exclusion_step(host, facts, uuid),
        sf_spotlight.marker_step(facts.path),
        sf_spotlight.remount_step(
            host, facts.path, uuid, before_mount=lambda: _require_still_empty(facts)
        ),
        sf_spotlight.off_step(host, facts.path),
        _fstab_step(host, facts, uuid),
        _copy_step(host, facts),
        _report_step(facts, status_command(opts), out),
    ]


def run_setup(opts: Options, host: Host, out: TextIO) -> int:
    """Validate, print the FileVault note if it applies, then print or run the plan."""
    facts = validate(opts, host)
    sf_privacy.check_full_disk_access(
        host, sf_privacy.EXCLUDE_COMMAND, apply=opts.apply, out=out
    )
    if facts.filevault:
        print(sf_macos.FILEVAULT_NOTE.format(path=facts.path), file=out)
    return run_plan(plan(opts, host, facts, out), opts.apply, out)
