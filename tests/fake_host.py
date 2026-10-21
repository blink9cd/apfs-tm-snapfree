"""A scripted ``Host`` for the suites: canned command replies, a call log, fake mounts.

Every mode's suite runs against this instead of the machine, so no test can reach
diskutil, tmutil, mdutil, sudo, ditto or a real fstab. What it scripts, and how:

* **Commands.** ``respond(argv, ...)`` registers a reply for an exact argv, or with
  ``prefix=True`` for any argv starting with it. The most specific match answers: an
  exact match beats a prefix, and a longer prefix beats a shorter one. Among equally
  specific replies a ``once=True`` reply answers before a standing one and is used up
  by that match, so a test can queue "the next read says X" over a reply set up
  earlier (a confirm step's re-read, say); otherwise the earliest registered answers. An argv nothing matches raises ``UnscriptedCommand``, an
  ``AssertionError``, so an unexpected command fails the test where it happens rather
  than reading as a command that succeeded.
* **Effects.** A reply may carry ``effect``, called with the argv tuple before the reply
  is returned. It can change the fake's state (``mount`` a folder), raise (a
  ``ToolError`` for a timeout), or return a whole ``RunResult`` to use instead.
* **The call log.** ``calls`` holds every argv run, as tuples in order, and ``timeouts``
  the timeout each was given.
* **Mounts.** ``device_of`` reports a folder's real ``st_dev`` unless ``mount(path)``
  gave it a fresh fake device; ``unmount(path)`` takes that away again. That is all
  ``sf_fstab.is_mount_point`` needs to see a mount come and go.
* **Volume identity.** As on macOS, ``diskutil info`` refuses a path that is not a
  mount point: an argument starting with ``/`` that no ``mount(path)`` made a mount
  point fails with "Could not find disk", whatever replies are registered, so code
  that hands diskutil a folder fails here as it does on the machine. A path's volume
  is found by ``stat -L -f %Sd``, which ``respond_device`` answers with the fake device
  name ``device_name(path)`` gives that path (each path its own, unless a test names
  one); ``respond_info`` registers both reads.
* **Privacy, environment, clock.** ``read_probe`` opens nothing: it records the path in
  ``probes`` and raises ``probe_error`` when a test set one (a ``PermissionError`` for a
  terminal without Full Disk Access, a ``FileNotFoundError`` for a Mac whose Time
  Machine was never set up), so the default is a granted machine. ``getenv`` reads
  ``env``, empty unless a test fills it, never the test runner's own environment.
  ``sleep`` returns at once and records each wait in ``sleeps``.
* **Files.** ``read_text`` reads the real file, which a test keeps in its own temporary
  folder; ``write_temp`` writes into that folder and records the path in ``temps``.
  ``emulate_file_commands()`` makes ``sudo cp``, ``sudo install`` and ``sudo mv -f``
  actually copy and rename there, refusing any path outside it, so
  ``sf_fstab.replace_fstab`` really rewrites a test's fstab.
* **Plists.** ``info_plist``, ``volume_entry``, ``container_entry``,
  ``apfs_list_plist`` and ``isexcluded_plist`` build replies with the real key names
  from obvious fakes, and the ``respond_*`` methods register them for the right argv.

A typical setup::

    fake = FakeHost(tmp)
    fake.respond_info(folder, mount_point=str(folder), volume_uuid=FAKE_UUID_1)
    fake.respond(["sudo", "diskutil", "mount"], prefix=True,
                 effect=fake.mount_effect(folder))
    fake.emulate_file_commands()
    ...  # run the code under test
    assert fake.calls[0] == ("sudo", "diskutil", "mount", ...)

Entry point assumptions: imported by the suites as ``tests.fake_host``; ``src/`` is on
``sys.path`` (the tests package puts it there).
"""

from __future__ import annotations

import itertools
import os
import plistlib
import shutil
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable, Optional, Sequence

import sf_macos
from sf_host import RunResult

#: Obvious fakes: RFC 4122 version-4 shaped, all zeros but the last digits.
FAKE_UUID_1 = "00000000-0000-4000-8000-000000000001"
FAKE_UUID_2 = "00000000-0000-4000-8000-000000000002"
FAKE_UUID_3 = "00000000-0000-4000-8000-000000000003"
FAKE_CONTAINER = "disk9"
FAKE_DEVICE = "disk9s1"
FAKE_UID = 501
FAKE_GID = 20
FAKE_STAMP = "20261016120000"
#: Comfortably more than any test's data, so free space never decides a test by accident.
FAKE_FREE = 500 * 10**9
#: Fake device numbers start here, far above any real st_dev a test would see.
_FIRST_FAKE_DEVICE = 1_000_000
#: Fake device names are ``disk9s<n>`` from here up, clear of the slices tests name.
_FIRST_FAKE_SLICE = 100
#: What the real diskutil says, with exit 1, for an argument that names no disk.
_NO_SUCH_DISK = b"Could not find disk: "
_INFO_PREFIX = ("diskutil", "info", "-plist")

Effect = Callable[[tuple[str, ...]], Optional[RunResult]]


class UnscriptedCommand(AssertionError):
    """The code under test ran a command no reply was registered for."""


@dataclass
class _Reply:
    """One registered reply and how it matches."""

    argv: tuple[str, ...]
    prefix: bool
    once: bool
    returncode: int
    stdout: bytes
    stderr: bytes
    effect: Optional[Effect]

    def specificity(self, argv: tuple[str, ...]) -> Optional[int]:
        """How closely this reply matches *argv*: higher wins, None for no match."""
        if self.prefix:
            matched = argv[: len(self.argv)] == self.argv
            return len(self.argv) if matched else None
        # One above any prefix of the same argv, so an exact match always wins.
        return len(argv) + 1 if argv == self.argv else None


class FakeHost:
    """A ``Host`` whose machine is a script; see the module docstring."""

    def __init__(
        self,
        temp_dir: Path,
        *,
        platform: str = "darwin",
        uid: int = FAKE_UID,
        gid: int = FAKE_GID,
        stamp: str = FAKE_STAMP,
    ) -> None:
        """Script a host whose files live in *temp_dir*, a test's temporary folder."""
        self.temp_dir = Path(os.path.realpath(temp_dir))
        self.platform = platform
        self._uid = uid
        self._gid = gid
        self._stamp = stamp
        self.calls: list[tuple[str, ...]] = []
        self.timeouts: list[float] = []
        self.temps: list[Path] = []
        self._replies: list[_Reply] = []
        self._devices: dict[str, int] = {}
        self._next_device = itertools.count(_FIRST_FAKE_DEVICE)
        self._device_names: dict[str, str] = {}
        self._next_slice = itertools.count(_FIRST_FAKE_SLICE)
        self._temp_names = itertools.count(1)
        self.probes: list[Path] = []
        self.probe_error: Optional[OSError] = None
        self.env: dict[str, str] = {}
        self.sleeps: list[float] = []

    # -- commands ---------------------------------------------------------------------

    def respond(
        self,
        argv: Sequence[str],
        *,
        returncode: int = 0,
        stdout: bytes = b"",
        stderr: bytes = b"",
        effect: Optional[Effect] = None,
        prefix: bool = False,
        once: bool = False,
    ) -> None:
        """Register a reply for *argv* (or, with *prefix*, any argv starting with it)."""
        self._replies.append(
            _Reply(tuple(argv), prefix, once, returncode, stdout, stderr, effect)
        )

    def _match(self, argv: tuple[str, ...]) -> _Reply:
        """The reply that answers *argv*, using up a once-only one."""
        best: Optional[_Reply] = None
        best_score = (-1, False)
        for reply in self._replies:
            specificity = reply.specificity(argv)
            # A once-only reply outranks a standing one of equal specificity, so a
            # test can queue "the next read says X" over a reply set up earlier.
            score = (-1 if specificity is None else specificity, reply.once)
            if specificity is not None and score > best_score:
                best, best_score = reply, score
        if best is None:
            raise UnscriptedCommand(f"no scripted reply for: {' '.join(argv)}")
        if best.once:
            self._replies.remove(best)
        return best

    def run(self, argv: Sequence[str], timeout: float) -> RunResult:
        """Log the call, apply the matching reply's effect, and return its result."""
        command = tuple(argv)
        self.calls.append(command)
        self.timeouts.append(timeout)
        refused = self._refuse_folder_info(command)
        if refused is not None:
            return refused
        reply = self._match(command)
        if reply.effect is not None:
            result = reply.effect(command)
            if result is not None:
                return result
        return RunResult(command, reply.returncode, reply.stdout, reply.stderr)

    def _refuse_folder_info(self, argv: tuple[str, ...]) -> Optional[RunResult]:
        """The real refusal of ``diskutil info`` for a path that is no mount point.

        diskutil takes a device, a volume UUID or a mount point; a folder inside a
        volume exits 1. The fake refuses it before any registered reply can answer,
        so a reply registered for a folder cannot hide the defect.
        """
        if (
            argv[: len(_INFO_PREFIX)] != _INFO_PREFIX
            or len(argv) != len(_INFO_PREFIX) + 1
        ):
            return None
        target = argv[-1]
        if not target.startswith("/") or os.path.realpath(target) in self._devices:
            return None
        return RunResult(argv, 1, b"", _NO_SUCH_DISK + target.encode("utf-8"))

    # -- mounts -----------------------------------------------------------------------

    def device_of(self, path: Path) -> int:
        """A mounted path's fake device, or the path's real ``st_dev``."""
        key = os.path.realpath(path)
        if key in self._devices:
            return self._devices[key]
        return os.stat(key).st_dev

    def mount(self, path: Path) -> int:
        """Give *path* a fresh fake device, as a mount would; return it."""
        device = next(self._next_device)
        self._devices[os.path.realpath(path)] = device
        return device

    def unmount(self, path: Path) -> None:
        """Take *path*'s fake device away; a path never mounted fails the test."""
        key = os.path.realpath(path)
        if key not in self._devices:
            raise AssertionError(f"{path} was never mounted on this fake host")
        del self._devices[key]

    def mount_effect(self, path: Path) -> Effect:
        """A reply effect that mounts *path*, for a scripted ``diskutil mount``."""

        def effect(_argv: tuple[str, ...]) -> None:
            self.mount(path)

        return effect

    def unmount_effect(self, path: Path) -> Effect:
        """A reply effect that unmounts *path*, for a scripted ``diskutil unmount``."""

        def effect(_argv: tuple[str, ...]) -> None:
            self.unmount(path)

        return effect

    # -- files, identity, clock -------------------------------------------------------

    def read_text(self, path: Path) -> str:
        """The real file's text; a test keeps it in its own temporary folder."""
        return path.read_text(encoding="utf-8")

    def write_temp(self, text: str) -> Path:
        """Write *text* to a new file in the temporary folder and record it."""
        path = self.temp_dir / f"fake-temp-{next(self._temp_names)}.txt"
        with path.open("w", encoding="utf-8", newline="\n") as stream:
            stream.write(text)
        self.temps.append(path)
        return path

    def getuid(self) -> int:
        """The configured user ID."""
        return self._uid

    def getgid(self) -> int:
        """The configured group ID."""
        return self._gid

    def now(self) -> str:
        """The configured stamp."""
        return self._stamp

    def read_probe(self, path: Path) -> None:
        """Record the probe of *path*, and raise ``probe_error`` if a test set one."""
        self.probes.append(path)
        if self.probe_error is not None:
            raise self.probe_error

    def getenv(self, name: str) -> Optional[str]:
        """*name* from ``env``, the test's own environment, or None."""
        return self.env.get(name)

    def sleep(self, seconds: float) -> None:
        """Record the wait and return at once."""
        self.sleeps.append(seconds)

    # -- the sudo file commands, performed on the temporary folder --------------------

    def _inside(self, raw: str) -> Path:
        """*raw* as a path, which must lie in the temporary folder."""
        path = Path(os.path.realpath(raw))
        if self.temp_dir not in (path, *path.parents):
            raise AssertionError(f"{raw} is outside this test's temporary folder")
        return path

    def _copy(self, argv: tuple[str, ...]) -> None:
        """``sudo cp SRC DST`` and ``sudo install ... SRC DST``: copy the last two."""
        shutil.copyfile(self._inside(argv[-2]), self._inside(argv[-1]))

    def _move(self, argv: tuple[str, ...]) -> None:
        """``sudo mv -f SRC DST``: rename."""
        os.replace(self._inside(argv[-2]), self._inside(argv[-1]))

    def emulate_file_commands(self) -> None:
        """Make the fstab rewrite's sudo commands act on the temporary folder."""
        self.respond(["sudo", "cp"], prefix=True, effect=self._copy)
        self.respond(["sudo", "install"], prefix=True, effect=self._copy)
        self.respond(["sudo", "mv", "-f"], prefix=True, effect=self._move)

    # -- plist replies ----------------------------------------------------------------

    def device_name(self, path: Path) -> str:
        """The fake device name *path*'s volume has: its own, the same every call."""
        key = os.path.realpath(path)
        if key not in self._device_names:
            self._device_names[key] = f"{FAKE_CONTAINER}s{next(self._next_slice)}"
        return self._device_names[key]

    def path_for_device(self, device: str) -> Optional[str]:
        """The real path ``device_name`` gave *device*, or None for any other name."""
        for path, name in self._device_names.items():
            if name == device:
                return path
        return None

    def respond_device(
        self, path: Path, device: Optional[str] = None, *, once: bool = False
    ) -> None:
        """Answer ``stat -L -f %Sd <path>`` with *device*, or ``device_name(path)``."""
        name = self.device_name(path) if device is None else device
        self.respond(
            sf_macos.device_argv(path), stdout=f"{name}\n".encode("utf-8"), once=once
        )

    def volume_reads(self, path: Path) -> list[tuple[str, ...]]:
        """The two calls ``sf_macos.volume_info`` makes for *path*: stat, then diskutil."""
        return [
            tuple(sf_macos.device_argv(path)),
            tuple(sf_macos.info_argv(self.device_name(path))),
        ]

    def device_effect(self, argv: tuple[str, ...]) -> RunResult:
        """A reply effect answering ``stat -L -f %Sd`` for any path it is given."""
        name = self.device_name(Path(argv[-1]))
        return RunResult(argv, 0, f"{name}\n".encode("utf-8"), b"")

    def respond_info(self, path: Path, *, once: bool = False, **fields: Any) -> None:
        """Answer the volume holding *path* with ``info_plist(**fields)``.

        Both reads are registered: the stat naming the volume's device, and
        ``diskutil info -plist`` for that device. The device is *fields*' ``device``
        when given, or ``device_name(path)``, and the plist reports the same one.
        """
        named: Optional[str] = fields.pop("device", None)
        if named is not None:
            # A device the test names becomes the path's own, so volume_reads agrees.
            self._device_names[os.path.realpath(path)] = named
        device = self.device_name(path)
        self.respond_device(path, device, once=once)
        self.respond_info_for(device, once=once, device=device, **fields)

    def respond_info_for(
        self, identifier: str, *, once: bool = False, **fields: Any
    ) -> None:
        """Answer ``diskutil info -plist <identifier>`` (a device or a volume UUID)."""
        self.respond(
            sf_macos.info_argv(identifier), stdout=info_plist(**fields), once=once
        )

    def respond_apfs_list(
        self, containers: Sequence[dict[str, object]], *, once: bool = False
    ) -> None:
        """Answer ``diskutil apfs list -plist`` with these containers."""
        self.respond(
            list(sf_macos.APFS_LIST_ARGV), stdout=apfs_list_plist(containers), once=once
        )

    def respond_isexcluded(
        self, path: Path, *, excluded: bool, once: bool = False
    ) -> None:
        """Answer ``tmutil isexcluded -X <path>``."""
        self.respond(
            sf_macos.isexcluded_argv(path),
            stdout=isexcluded_plist(path, excluded=excluded),
            once=once,
        )

    def respond_mdutil(self, path: Path, text: str, *, once: bool = False) -> None:
        """Answer ``mdutil -s <path>`` with *text*."""
        self.respond(
            sf_macos.mdutil_status_argv(path), stdout=text.encode("utf-8"), once=once
        )


def info_plist(
    *,
    mount_point: str,
    volume_uuid: str = FAKE_UUID_1,
    container: str = FAKE_CONTAINER,
    container_free: int = FAKE_FREE,
    filesystem: str = "apfs",
    ownership: bool = True,
    filevault: bool = False,
    name: str = "Data",
    device: str = FAKE_DEVICE,
    omit: Sequence[str] = (),
) -> bytes:
    """``diskutil info -plist`` output, with the real key names; *omit* drops keys."""
    reply: dict[str, object] = {
        "MountPoint": mount_point,
        "VolumeUUID": volume_uuid,
        "APFSContainerReference": container,
        "APFSContainerFree": container_free,
        "FilesystemType": filesystem,
        "GlobalPermissionsEnabled": ownership,
        "FileVault": filevault,
        "VolumeName": name,
        "DeviceIdentifier": device,
    }
    return plistlib.dumps(
        {key: value for key, value in reply.items() if key not in omit}
    )


def volume_entry(
    *,
    uuid: str = FAKE_UUID_1,
    name: str = "Data",
    device: str = FAKE_DEVICE,
    quota: int = 0,
    in_use: int = 0,
    reserve: int = 0,
    roles: Optional[Sequence[str]] = None,
) -> dict[str, object]:
    """One entry of a container's ``Volumes`` array; *roles* None leaves Roles out."""
    entry: dict[str, object] = {
        "APFSVolumeUUID": uuid,
        "Name": name,
        "DeviceIdentifier": device,
        "CapacityQuota": quota,
        "CapacityInUse": in_use,
        "CapacityReserve": reserve,
    }
    if roles is not None:
        entry["Roles"] = list(roles)
    return entry


def container_entry(
    *,
    reference: str = FAKE_CONTAINER,
    free: int = FAKE_FREE,
    volumes: Sequence[dict[str, object]] = (),
) -> dict[str, object]:
    """One entry of the ``Containers`` array."""
    return {
        "ContainerReference": reference,
        "CapacityFree": free,
        "Volumes": list(volumes),
    }


def apfs_list_plist(containers: Sequence[dict[str, object]]) -> bytes:
    """``diskutil apfs list -plist`` output holding *containers*."""
    return plistlib.dumps({"Containers": list(containers)})


def isexcluded_plist(path: Path, *, excluded: bool) -> bytes:
    """``tmutil isexcluded -X`` output: an array of one dict, IsExcluded 0 or 1."""
    return plistlib.dumps([{"Path": str(path), "IsExcluded": int(excluded)}])


#: Where ``st_uid`` sits in an ``os.stat_result`` read as a sequence.
_UID_INDEX = 4
#: The user ID macOS gives the folders it keeps at a volume's root.
ROOT_UID = 0


def root_owned_lstat(path: str) -> os.stat_result:
    """``os.lstat(path)``, except that the entry reads as owned by root.

    The suites never run as root, so they cannot make a root-owned folder; handed to
    ``sf_walk.check_private_entries`` this makes a folder the test created read as the
    one macOS would have made.
    """
    fields = list(os.lstat(path))
    fields[_UID_INDEX] = ROOT_UID
    return os.stat_result(fields)
