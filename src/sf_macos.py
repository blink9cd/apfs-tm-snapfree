"""Typed readers over what diskutil, tmutil, mdutil and stat report about a volume.

Each reader asks for the structured form (``-plist``) wherever one exists and checks
every key it uses for presence and type, so a reply that drifts across macOS versions
fails loudly, naming the command and the key, instead of reading as a wrong answer.
``mdutil -s`` has no structured form, so ``spotlight_verdict`` is the one prose read,
and it says "unknown" (None) rather than guess.

Time Machine treats a volume mounted ``nobrowse`` as excluded whether or not anything
excluded it, and ``tmutil isexcluded`` reports that, so it cannot tell whether
``tmutil addexclusion -v`` or ``removeexclusion -v`` did anything to a volume this tool
mounted. Time Machine's own list of explicitly excluded volumes, ``ExcludedVolumeUUIDs``
in its preferences file, can: ``excluded_volume_uuids`` reads it with ``plutil``, under
sudo, and never writes it. That list also showed, live on macOS 27, that
``addexclusion -v`` on a nobrowse mount exits 0 and adds nothing, which is why setup and
resize exclude a new volume while its first mount is still visible (ruling D8).

``diskutil info`` takes a device, a volume UUID or a mount point, and exits 1 ("Could
not find disk") for a folder inside a volume, so a path is never handed to it: the
path's volume is named first by ``stat -L -f %Sd``, the BSD stat's device name for the
filesystem holding the path, and diskutil is asked about that device. Walking up the
path until ``st_dev`` changes would not do: firmlinks splice ``/Users`` in from the Data
volume, ``st_dev`` reports that volume for ``/`` as well, and so the walk climbs to ``/``,
which diskutil describes as the sealed system volume, the wrong one.

Entry point assumptions: every command goes through the ``Host`` it is handed, and a
non-zero exit is a ``ToolError`` from ``sf_host.run_checked``.
"""

from __future__ import annotations

import plistlib
import re
import xml.parsers.expat
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping, Optional, Sequence, Type, TypeVar

from sf_errors import ToolError
from sf_host import COMMAND_TIMEOUT, Host, failure_message, format_argv, run_checked
from sf_privacy import TIME_MACHINE_PREFERENCES

#: The FilesystemType diskutil reports for an APFS volume.
APFS = "apfs"
#: Every APFS container and its volumes, with quotas and use.
APFS_LIST_ARGV = ("diskutil", "apfs", "list", "-plist")
#: What setup and resize print first when FileVault encrypts the volume *path* is on.
FILEVAULT_NOTE = (
    "Note: FileVault is on for the volume holding {path}, but the new volume will "
    "not be encrypted: a volume mounted at boot cannot ask for a password."
)
#: The words mdutil uses; "disabled" is checked first because it contains "enabled".
_SPOTLIGHT_OFF = "disabled"
_SPOTLIGHT_ON = "enabled"
#: The errors plistlib raises for bytes that are not a plist: its own (a ValueError)
#: for binary or malformed input, and expat's for broken XML.
_PLIST_ERRORS = (ValueError, xml.parsers.expat.ExpatError)
#: The system's BSD stat, by absolute path: a GNU stat earlier on PATH reads ``-f`` as
#: "describe the filesystem" and ``%Sd`` as something else entirely.
#: With ``-L`` it follows a symlink, so a link reports the volume it points into.
STAT_DEVICE_ARGV = ("/usr/bin/stat", "-L", "-f", "%Sd")
#: What ``stat -f %Sd`` prints for a volume diskutil can describe: a disk slice such
#: as ``disk3s5``. devfs prints ``???`` and a network share names no disk.
_DEVICE_NAME = re.compile(r"disk[0-9]+(s[0-9]+)*")

#: Time Machine's list of the volumes explicitly excluded from backups, by UUID.
EXCLUDED_UUIDS_KEY = "ExcludedVolumeUUIDs"
#: Its read: plutil prints the one key's value as an XML plist on stdout. The system
#: plutil by absolute path, under sudo as tmutil's own steps are.
EXCLUDED_UUIDS_ARGV = (
    "sudo",
    "/usr/bin/plutil",
    "-extract",
    EXCLUDED_UUIDS_KEY,
    "xml1",
    "-o",
    "-",
    str(TIME_MACHINE_PREFERENCES),
)
#: How plutil says, exiting 1, that the key is absent (nothing was ever excluded) or
#: that the file is (Time Machine never set up): both mean an empty list. Anything else,
#: a privacy refusal above all, is a failure, never read as "nothing excluded".
NO_EXCLUSIONS_MARKERS = ("No value at that key path", "there is no such file")

#: The waits between reads of that list while a step's change has not shown: tmutil may
#: write it through cfprefsd, which can reach the file late. Escalating, about 15 s in
#: all, so a fast Mac is not held up and a slow one is not failed by one early read.
EXCLUSION_POLL_WAITS = (0.5, 1.0, 2.0, 4.0, 8.0)

_T = TypeVar("_T")


@dataclass(frozen=True)
class VolumeInfo:
    """``diskutil info -plist <device or UUID>``: one volume's record.

    The fields diskutil reports for APFS volumes are None on another filesystem that
    leaves them out, so status can still describe such a volume; ``require_apfs``
    turns them into the non-optional ``ApfsInfo`` where APFS is required.
    """

    mount_point: str
    volume_uuid: Optional[str]
    container: Optional[str]
    container_free: Optional[int]
    filesystem: str
    ownership_enabled: Optional[bool]
    filevault: Optional[bool]
    name: str
    device: str


@dataclass(frozen=True)
class ApfsInfo:
    """The APFS fields of a ``VolumeInfo``, each known to be present."""

    volume_uuid: str
    container: str
    container_free: int
    ownership_enabled: bool
    filevault: bool


@dataclass(frozen=True)
class ApfsVolume:
    """One volume from ``diskutil apfs list -plist``; sizes in bytes, 0 for none.

    *roles* are its APFS roles (``System``, ``Data``, ``Preboot``...), empty for a
    plain volume such as this tool makes, or None when diskutil does not report them.
    """

    uuid: str
    name: str
    device: str
    quota: int
    in_use: int
    reserve: int
    roles: Optional[tuple[str, ...]] = None


@dataclass(frozen=True)
class ApfsContainer:
    """One APFS container: its reference, its free space and its volumes."""

    reference: str
    free: int
    volumes: tuple[ApfsVolume, ...]

    def volume_with_uuid(self, uuid: str) -> Optional[ApfsVolume]:
        """The volume with *uuid*, or None; compared ignoring case.

        diskutil prints UUIDs in upper case, but one copied into an fstab or a marker
        by hand may be in lower case, and it names the same volume.
        """
        wanted = uuid.upper()
        for volume in self.volumes:
            if volume.uuid.upper() == wanted:
                return volume
        return None

    def volumes_named(self, name: str) -> tuple[ApfsVolume, ...]:
        """Every volume called *name*; APFS does not require names to be unique."""
        return tuple(volume for volume in self.volumes if volume.name == name)


def device_argv(path: Path) -> list[str]:
    """The command naming the device of the volume that holds *path*."""
    return [*STAT_DEVICE_ARGV, str(path)]


def info_argv(identifier: str) -> list[str]:
    """The command describing the volume *identifier* (a device or a volume UUID)."""
    return ["diskutil", "info", "-plist", identifier]


def isexcluded_argv(path: Path) -> list[str]:
    """The command asking Time Machine whether *path* is excluded."""
    return ["tmutil", "isexcluded", "-X", str(path)]


def mdutil_status_argv(path: Path) -> list[str]:
    """The command reporting whether Spotlight indexes *path*'s volume."""
    return ["mdutil", "-s", str(path)]


def _load_plist(host: Host, argv: Sequence[str]) -> object:
    """Run *argv* and parse its stdout as a plist, or raise naming the command."""
    result = run_checked(host, argv, COMMAND_TIMEOUT)
    try:
        return plistlib.loads(result.stdout)
    except _PLIST_ERRORS as exc:
        raise ToolError(f"{format_argv(argv)} gave an unreadable plist: {exc}") from exc


def _require(table: Mapping[str, object], key: str, kind: Type[_T], command: str) -> _T:
    """``table[key]`` if present and of *kind*; a ``ToolError`` naming both if not.

    A bool is refused where an int is wanted: plistlib's booleans are ints to Python,
    and a ``<true/>`` where a byte count belongs is a reply this tool cannot trust.
    """
    value = table.get(key)
    wrong_bool = kind is int and isinstance(value, bool)
    if not isinstance(value, kind) or wrong_bool:
        raise ToolError(
            f"{command} gave no usable {key} (expected {kind.__name__}, got {value!r})"
        )
    return value


def _require_dict(value: object, what: str, command: str) -> Mapping[str, object]:
    """*value* as a plist dict, or a ``ToolError`` naming *what* it should be."""
    if not isinstance(value, dict):
        raise ToolError(f"{command} gave {what} that is not a dictionary: {value!r}")
    return value


def _require_list(value: object, what: str, command: str) -> list[object]:
    """*value* as a plist array, or a ``ToolError`` naming *what* it should be."""
    if not isinstance(value, list):
        raise ToolError(f"{command} gave {what} that is not a list: {value!r}")
    return value


def _optional(
    table: Mapping[str, object], key: str, kind: Type[_T], command: str
) -> Optional[_T]:
    """``table[key]`` if present (checked like ``_require``), or None if absent."""
    if key not in table:
        return None
    return _require(table, key, kind, command)


def volume_device(host: Host, path: Path) -> str:
    """The device (``disk3s5``) of the volume holding *path*, read by ``stat``.

    Anything but one disk name is refused: diskutil could not describe it.
    """
    argv = device_argv(path)
    device = run_checked(host, argv, COMMAND_TIMEOUT).stdout_text().strip()
    if _DEVICE_NAME.fullmatch(device) is None:
        raise ToolError(
            f"{path} is not on a disk volume diskutil can describe: "
            f"{format_argv(argv)} reported {device!r}"
        )
    return device


def volume_info(host: Host, path: Path) -> VolumeInfo:
    """What diskutil says about the volume holding *path*, any path on it.

    The path goes to ``stat`` and its device to diskutil, never the path itself:
    diskutil refuses a path that is not a mount point (see the module docstring).
    """
    return volume_info_for(host, volume_device(host, path))


def volume_info_for(host: Host, identifier: str) -> VolumeInfo:
    """What ``diskutil info -plist`` says about the volume *identifier*.

    *identifier* is a device (``disk3s5``) or a volume UUID, which diskutil takes as
    it is, mounted or not. The filesystem is read first: on APFS every field is
    required, and on any other filesystem the APFS fields may be absent and read as
    None.
    """
    argv = info_argv(identifier)
    command = format_argv(argv)
    table = _require_dict(_load_plist(host, argv), "a reply", command)
    filesystem = _require(table, "FilesystemType", str, command)
    read = _require if filesystem == APFS else _optional
    return VolumeInfo(
        mount_point=_require(table, "MountPoint", str, command),
        volume_uuid=read(table, "VolumeUUID", str, command),
        container=read(table, "APFSContainerReference", str, command),
        container_free=read(table, "APFSContainerFree", int, command),
        filesystem=filesystem,
        ownership_enabled=read(table, "GlobalPermissionsEnabled", bool, command),
        filevault=read(table, "FileVault", bool, command),
        name=_require(table, "VolumeName", str, command),
        device=_require(table, "DeviceIdentifier", str, command),
    )


def require_apfs(info: VolumeInfo, path: Path) -> ApfsInfo:
    """*info*'s APFS fields, or a ``ToolError`` saying *path* is not on APFS."""
    if info.filesystem != APFS:
        raise ToolError(f"{path} is on a {info.filesystem} filesystem, not APFS")
    return ApfsInfo(
        volume_uuid=_present(info.volume_uuid, "VolumeUUID", path),
        container=_present(info.container, "APFSContainerReference", path),
        container_free=_present(info.container_free, "APFSContainerFree", path),
        ownership_enabled=_present(
            info.ownership_enabled, "GlobalPermissionsEnabled", path
        ),
        filevault=_present(info.filevault, "FileVault", path),
    )


def _present(value: Optional[_T], key: str, path: Path) -> _T:
    """*value*, or a ``ToolError`` for an APFS volume whose info lacks *key*.

    ``volume_info`` already requires every APFS field, so only a hand-built
    ``VolumeInfo`` reaches the refusal.
    """
    if value is None:
        raise ToolError(f"the volume info for {path} has no {key}")
    return value


def _roles(table: Mapping[str, object], command: str) -> Optional[tuple[str, ...]]:
    """A volume's ``Roles``: a list of names, or None when the key is absent."""
    if "Roles" not in table:
        return None
    roles = _require_list(table["Roles"], "a Roles entry", command)
    if not all(isinstance(role, str) for role in roles):
        raise ToolError(
            f"{command} gave a Roles entry that is not all names: {roles!r}"
        )
    return tuple(str(role) for role in roles)


def _volume(raw: object, command: str) -> ApfsVolume:
    """One entry of a container's Volumes array."""
    table = _require_dict(raw, "a volume", command)
    return ApfsVolume(
        uuid=_require(table, "APFSVolumeUUID", str, command),
        name=_require(table, "Name", str, command),
        device=_require(table, "DeviceIdentifier", str, command),
        quota=_require(table, "CapacityQuota", int, command),
        in_use=_require(table, "CapacityInUse", int, command),
        reserve=_require(table, "CapacityReserve", int, command),
        roles=_roles(table, command),
    )


def _container(raw: object, command: str) -> ApfsContainer:
    """One entry of the Containers array."""
    table = _require_dict(raw, "a container", command)
    volumes = _require_list(table.get("Volumes"), "a Volumes entry", command)
    return ApfsContainer(
        reference=_require(table, "ContainerReference", str, command),
        free=_require(table, "CapacityFree", int, command),
        volumes=tuple(_volume(volume, command) for volume in volumes),
    )


def apfs_containers(host: Host) -> tuple[ApfsContainer, ...]:
    """Every APFS container ``diskutil apfs list -plist`` reports, in its order."""
    command = format_argv(APFS_LIST_ARGV)
    table = _require_dict(_load_plist(host, APFS_LIST_ARGV), "a reply", command)
    containers = _require_list(table.get("Containers"), "a Containers entry", command)
    return tuple(_container(container, command) for container in containers)


def container_volumes(host: Host, reference: str) -> ApfsContainer:
    """The container *reference* (such as ``disk3``), or a ``ToolError`` if absent."""
    for container in apfs_containers(host):
        if container.reference == reference:
            return container
    raise ToolError(f"diskutil apfs list reports no container {reference}")


def find_volume(
    containers: Sequence[ApfsContainer], uuid: str
) -> Optional[tuple[ApfsContainer, ApfsVolume]]:
    """The container and volume with *uuid*, searching every container, or None."""
    for container in containers:
        volume = container.volume_with_uuid(uuid)
        if volume is not None:
            return container, volume
    return None


def is_excluded(host: Host, path: Path) -> bool:
    """Whether Time Machine excludes *path*: ``tmutil isexcluded -X``'s IsExcluded.

    The outcome, not its cause: a volume mounted nobrowse reads excluded even with no
    exclusion of its own (see the module docstring).
    """
    argv = isexcluded_argv(path)
    command = format_argv(argv)
    entries = _require_list(_load_plist(host, argv), "a reply", command)
    if not entries:
        raise ToolError(f"{command} gave an empty reply")
    first = _require_dict(entries[0], "an entry", command)
    return _require(first, "IsExcluded", int, command) == 1


def excluded_volume_uuids(host: Host) -> list[str]:
    """The UUIDs Time Machine excludes explicitly, upper case; empty if it lists none."""
    command = format_argv(EXCLUDED_UUIDS_ARGV)
    result = host.run(EXCLUDED_UUIDS_ARGV, COMMAND_TIMEOUT)
    if result.returncode != 0:
        detail = result.stderr_text()
        if any(marker in detail for marker in NO_EXCLUSIONS_MARKERS):
            return []
        raise ToolError(failure_message(result))
    try:
        uuids = _require_list(plistlib.loads(result.stdout), "a reply", command)
    except _PLIST_ERRORS as exc:
        raise ToolError(f"{command} gave an unreadable plist: {exc}") from exc
    if not all(isinstance(uuid, str) for uuid in uuids):
        raise ToolError(f"{command} gave a list that is not all UUIDs: {uuids!r}")
    # diskutil prints UUIDs in upper case; Time Machine's own spelling is not promised.
    return [str(uuid).upper() for uuid in uuids]


def _poll_listed(host: Host, uuid: str, *, listed: bool) -> bool:
    """Whether the list comes to name *uuid* (or, unless *listed*, to omit it).

    Re-read after each of ``EXCLUSION_POLL_WAITS``, returning at the first read that
    shows it; False once the last read still does not. A failed read raises at once.
    """
    wanted = uuid.upper()
    for wait in EXCLUSION_POLL_WAITS:
        if (wanted in excluded_volume_uuids(host)) is listed:
            return True
        host.sleep(wait)
    return (wanted in excluded_volume_uuids(host)) is listed


def require_explicitly_excluded(host: Host, uuid: str) -> None:
    """Raise unless Time Machine's ``ExcludedVolumeUUIDs`` comes to list *uuid*."""
    if not _poll_listed(host, uuid, listed=True):
        raise ToolError(
            f"Time Machine's {EXCLUDED_UUIDS_KEY} does not list {uuid}: the volume is "
            "not excluded explicitly"
        )


def require_not_explicitly_excluded(host: Host, uuid: str) -> None:
    """Raise if Time Machine's ``ExcludedVolumeUUIDs`` still lists *uuid* after polling."""
    if not _poll_listed(host, uuid, listed=False):
        raise ToolError(f"Time Machine's {EXCLUDED_UUIDS_KEY} still lists {uuid}")


def spotlight_verdict(text: str) -> Optional[bool]:
    """Whether ``mdutil -s``'s *text* says indexing is off; None if it says neither."""
    # Only the indented verdict lines count: the unindented first line is the volume's
    # path, which may itself contain either word.
    verdicts = [line.lower() for line in text.splitlines() if line[:1].isspace()]
    if any(_SPOTLIGHT_OFF in line for line in verdicts):
        return True
    if any(_SPOTLIGHT_ON in line for line in verdicts):
        return False
    return None


def spotlight_disabled(host: Host, path: Path) -> Optional[bool]:
    """Whether Spotlight indexing is off for *path*'s volume; None if mdutil is unclear."""
    text = run_checked(host, mdutil_status_argv(path), COMMAND_TIMEOUT).stdout_text()
    return spotlight_verdict(text)
