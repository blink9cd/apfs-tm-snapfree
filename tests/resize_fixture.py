"""The scripted machine the resize suites share: one folder on its own volume, ready to resize.

``ResizeCase`` builds, in a test's own temporary folder, a folder ``home/work`` with a
few files in it, an fstab naming the old volume for it, and a ``FakeHost`` whose
diskutil, tmutil, mdutil and ditto replies follow the state the commands change: adding
a volume lists it, mounting one makes ``diskutil info`` describe it, ``ditto`` really
copies the folder, unmounting takes the copy out of view. So a test changes one fact
(a volume missing, a command refused) and the rest of the run behaves as macOS would.

Entry point assumptions: imported by the resize suites as ``tests.resize_fixture``;
``src/`` is on ``sys.path`` (the tests package puts it there).
"""

from __future__ import annotations

import functools
import os
import shutil
import tempfile
import unittest
from pathlib import Path
from typing import Any, Optional
from unittest import mock

import sf_macos
import sf_resize
import sf_walk
from sf_host import RunResult
from sf_options import Options
from tests import fake_host, tm_prefs
from tests.fake_host import (
    FAKE_CONTAINER,
    FAKE_FREE,
    FAKE_STAMP,
    FAKE_UUID_1,
    FAKE_UUID_2,
    FAKE_UUID_3,
)

#: The volume mounted at the folder now, the one a resize adds, and an unrelated one.
OLD = FAKE_UUID_1
NEW = FAKE_UUID_2
OTHER = FAKE_UUID_3
OLD_NAME = "snapfree-work"
NEW_NAME = f"snapfree-work-{FAKE_STAMP}"
GB = 10**9
#: The folder's current quota and use, and the quota the tests resize it to.
OLD_QUOTA = 10 * GB
IN_USE = 5 * GB
NEW_QUOTA = 20 * GB
NEW_QUOTA_ARG = "20g"
#: The folder's root mode, set on purpose to differ from mkdir's default.
FOLDER_MODE = 0o750
FOLDER_MODE_ARG = "750"
#: Where the parent folder's volume is mounted, as diskutil would report it.
PARENT_MOUNT = "/System/Volumes/Data"
#: What diskutil says, on stderr, for an identifier it cannot find.
_NOT_FOUND = b"Could not find disk"
_REFUSED = 1


def write_file(path: Path, text: str) -> None:
    """Write *text* as UTF-8 with its newlines as given, the same on every host."""
    path.write_bytes(text.encode("utf-8"))


class ResizeCase(unittest.TestCase):
    """A folder ``home/work`` on volume OLD, with every resize command scripted."""

    def setUp(self) -> None:
        """Build the folder, its fstab and the fake machine around them."""
        holder = tempfile.TemporaryDirectory()
        self.addCleanup(holder.cleanup)
        self.tmp = Path(os.path.realpath(holder.name))
        self.parent = self.tmp / "home"
        self.path = self.parent / "work"
        self.temp = self.parent / f".work.snapfree-resize-{FAKE_STAMP}"
        #: Where the new volume's root entries go while it is unmounted.
        self.new_store = self.tmp / "new-volume-store"
        self.fstab = self.tmp / "fstab"
        self._make_folder()
        write_file(self.fstab, f"# local\nUUID={OLD} {self.path} apfs rw,nobrowse\n")
        self.fake = fake_host.FakeHost(self.tmp)
        self.fake.mount(self.path)
        stat = os.stat(self.path)
        self.owner = f"{stat.st_uid}:{stat.st_gid}"
        self.volumes = [
            fake_host.volume_entry(
                uuid=OLD, name=OLD_NAME, quota=OLD_QUOTA, in_use=IN_USE
            )
        ]
        self.free = FAKE_FREE
        # Time Machine's explicit exclusions: the old volume's, as setup left it. An
        # exclusion lists the new volume only while it is mounted visible, as live.
        self.exclusions = tm_prefs.ExcludedList(self.fake, [OLD])
        self.excluded_uuids = self.exclusions.uuids
        self.new_ownership = True
        self.infos: dict[str, dict[str, Any]] = {
            str(self.path): {"mount_point": str(self.path), "volume_uuid": OLD},
            str(self.parent): {"mount_point": PARENT_MOUNT, "volume_uuid": OTHER},
            OLD: {"mount_point": "", "volume_uuid": OLD},
        }
        self._script_reads()
        self._script_steps()
        self.fake.emulate_file_commands()

    def _make_folder(self) -> None:
        """The folder being resized, holding a file, a subfolder and a symlink."""
        (self.path / "sub").mkdir(parents=True)
        write_file(self.path / "a.txt", "alpha")
        write_file(self.path / "sub" / "b.txt", "beta")
        (self.path / "link").symlink_to("a.txt")
        self.path.chmod(FOLDER_MODE)

    # -- options ----------------------------------------------------------------------

    def options(
        self, *, apply: bool = False, finish: bool = False, quota: Optional[int] = None
    ) -> Options:
        """Resize options for the folder; the quota is NEW_QUOTA unless finishing."""
        if quota is None and not finish:
            quota = NEW_QUOTA
        return Options(
            mode="resize",
            path=self.path,
            quota=quota,
            name=None,
            fstab=self.fstab,
            warn_percent=90,
            finish=finish,
            apply=apply,
        )

    # -- the reads, answered from the current state ----------------------------------

    def _script_reads(self) -> None:
        """Answer stat, diskutil info and apfs list from the state.

        ``tmutil isexcluded`` reads excluded for any nobrowse volume, as on macOS,
        whatever ``addexclusion`` did; only ``ExcludedVolumeUUIDs`` follows it, and
        ``exclusions`` answers that.
        """
        self.fake.respond(
            sf_macos.STAT_DEVICE_ARGV, prefix=True, effect=self.fake.device_effect
        )
        self.fake.respond(
            ["diskutil", "info", "-plist"], prefix=True, effect=self._info
        )
        self.fake.respond(list(sf_macos.APFS_LIST_ARGV), effect=self._apfs_list)
        self.fake.respond_isexcluded(self.temp, excluded=True)
        self.fake.respond_mdutil(self.temp, "/:\n\tIndexing disabled.\n")

    def _info(self, argv: tuple[str, ...]) -> RunResult:
        """``diskutil info -plist <device or uuid>`` for what is there now.

        ``infos`` is keyed by path or UUID; a device is the one the fake's stat
        named for a path, so it is read back as that path.
        """
        key = self.fake.path_for_device(argv[-1]) or argv[-1]
        fields = self.infos.get(key)
        if fields is None:
            return RunResult(argv, _REFUSED, b"", _NOT_FOUND)
        return RunResult(argv, 0, fake_host.info_plist(**fields), b"")

    def _apfs_list(self, argv: tuple[str, ...]) -> RunResult:
        """``diskutil apfs list -plist`` holding the fixture's volumes."""
        container = fake_host.container_entry(free=self.free, volumes=self.volumes)
        return RunResult(argv, 0, fake_host.apfs_list_plist([container]), b"")

    # -- the steps' commands, each changing the state as macOS would -----------------

    def add_volume_argv(self) -> list[str]:
        """The addVolume command a resize to NEW_QUOTA runs."""
        return [
            "sudo",
            "diskutil",
            "apfs",
            "addVolume",
            FAKE_CONTAINER,
            "APFS",
            NEW_NAME,
            "-quota",
            NEW_QUOTA_ARG,
            "-nomount",
        ]

    def visible_mount_argv(self, where: Path, uuid: str) -> list[str]:
        """``sudo diskutil mount -mountPoint <where> <uuid>``: the first mount (D8)."""
        return ["sudo", "diskutil", "mount", "-mountPoint", str(where), uuid]

    def mount_argv(self, where: Path, uuid: str) -> list[str]:
        """``sudo diskutil mount -mountOptions nobrowse -mountPoint <where> <uuid>``."""
        mount = ["sudo", "diskutil", "mount", "-mountOptions", "nobrowse"]
        return [*mount, "-mountPoint", str(where), uuid]

    def step_argvs(self) -> dict[str, list[str]]:
        """Every mutating command of a resize, by a short name."""
        temp, path = str(self.temp), str(self.path)
        return {
            "addVolume": self.add_volume_argv(),
            "mount temp": self.visible_mount_argv(self.temp, NEW),
            "remount temp": self.mount_argv(self.temp, NEW),
            "chown": ["sudo", "chown", self.owner, temp],
            "chmod": ["sudo", "chmod", FOLDER_MODE_ARG, temp],
            "mdutil": ["sudo", "mdutil", "-i", "off", temp],
            "exclude": ["sudo", "tmutil", "addexclusion", "-v", temp],
            "ditto": ["ditto", f"{path}/a.txt", f"{temp}/a.txt"],
            "ditto sub": ["ditto", f"{path}/sub", f"{temp}/sub"],
            "unmount temp": ["sudo", "diskutil", "unmount", temp],
            "unmount path": ["sudo", "diskutil", "unmount", path],
            "mount path": self.mount_argv(self.path, NEW),
            "enableOwnership": ["sudo", "diskutil", "enableOwnership", temp],
            "deleteVolume": ["sudo", "diskutil", "apfs", "deleteVolume", OLD],
        }

    def _script_steps(self) -> None:
        """Register each mutating command with the effect it has on the state."""
        argvs = self.step_argvs()
        effects = {
            "addVolume": self._add_volume,
            "mount temp": self._mount_temp,
            "remount temp": self._mount_temp,
            "chmod": self._chmod,
            "exclude": self.exclusions.add,
            "unmount temp": self._unmount_temp,
            "unmount path": self._unmount_path,
            "mount path": self._mount_path,
            "enableOwnership": self._enable_ownership,
            "deleteVolume": self._delete_old,
        }
        for name, argv in argvs.items():
            if not name.startswith("ditto"):
                self.fake.respond(argv, effect=effects.get(name))
        # Every copy, whichever entry it names; a test refuses or ignores one by its
        # exact argv, which outranks this.
        self.fake.respond(["ditto"], prefix=True, effect=self._ditto)

    def _add_volume(self, _argv: tuple[str, ...]) -> None:
        self.volumes.append(
            fake_host.volume_entry(uuid=NEW, name=NEW_NAME, quota=NEW_QUOTA)
        )

    def _mount_temp(self, _argv: tuple[str, ...]) -> None:
        """Mount the new volume at the temporary mount point, its entries back in view."""
        self.fake.mount(self.temp)
        if self.new_store.is_dir():
            for child in self.new_store.iterdir():
                os.replace(child, self.temp / child.name)
        self.infos[str(self.temp)] = {
            "mount_point": str(self.temp),
            "volume_uuid": NEW,
            "ownership": self.new_ownership,
        }

    def _chmod(self, _argv: tuple[str, ...]) -> None:
        self.temp.chmod(FOLDER_MODE)

    def _ditto(self, argv: tuple[str, ...]) -> None:
        """``ditto SRC DST``: a folder copied whole into DST, a file as a file."""
        source, target = (Path(item.rstrip("/")) for item in argv[1:])
        if source.is_dir():
            shutil.copytree(source, target, symlinks=True, dirs_exist_ok=True)
        else:
            shutil.copy2(source, target)

    def as_macos_made(self) -> None:
        """Make validation read the volume-private entries as root-owned, as macOS's.

        The suites never run as root, so the check is handed an ``lstat`` that reports
        root as the owner; everything else about each entry is read for real.
        """
        checker = functools.partial(
            sf_walk.check_private_entries, lstat=fake_host.root_owned_lstat
        )
        patcher = mock.patch.object(sf_resize, "check_private_entries", checker)
        patcher.start()
        self.addCleanup(patcher.stop)

    def add_volume_private(self, root: Path) -> None:
        """Give *root* a ``.fseventsd`` the user cannot list, as macOS does.

        Mode 000 stands in for its real drwx------ root ownership, and validation is
        made to read it as root's.
        """
        self.as_macos_made()
        locked = root / ".fseventsd"
        locked.mkdir()
        (locked / "0000000000000001").write_bytes(b"journal")
        locked.chmod(0)

        def unlock() -> None:
            if locked.is_dir():  # an unmount may have taken it away already
                locked.chmod(0o700)

        self.addCleanup(unlock)

    def _enable_ownership(self, _argv: tuple[str, ...]) -> None:
        # A volume setting: it holds across a remount.
        self.new_ownership = True
        self.infos[str(self.temp)]["ownership"] = True

    def _unmount_temp(self, _argv: tuple[str, ...]) -> None:
        """The volume's entries go with it, leaving the empty mount point behind.

        They are kept in ``new_store``, so a remount brings them back and a test can
        read what the new volume holds once the run is over.
        """
        self.fake.unmount(self.temp)
        del self.infos[str(self.temp)]
        self.new_store.mkdir(exist_ok=True)
        for child in self.temp.iterdir():
            if child.is_dir() and not child.is_symlink():
                child.chmod(0o700)  # a locked .fseventsd goes with the volume too
            os.replace(child, self.new_store / child.name)

    def _unmount_path(self, _argv: tuple[str, ...]) -> None:
        self.fake.unmount(self.path)
        self.infos[str(self.path)] = dict(self.infos[str(self.parent)])

    def _mount_path(self, _argv: tuple[str, ...]) -> None:
        self.fake.mount(self.path)
        self.infos[str(self.path)] = {"mount_point": str(self.path), "volume_uuid": NEW}

    def _delete_old(self, _argv: tuple[str, ...]) -> None:
        self.volumes[:] = [vol for vol in self.volumes if vol["APFSVolumeUUID"] != OLD]
        self.infos.pop(OLD, None)

    # -- helpers for the assertions ---------------------------------------------------

    def then(self, argv: list[str], effect: fake_host.Effect) -> None:
        """Let the next run of *argv* behave as usual, and give the one after *effect*.

        The new volume is unmounted from the temporary mount point twice (the remount,
        then once filled), so a test of the later run first lets the earlier one
        through. The two mounts there differ (visible, then nobrowse), so each is
        scripted by its own argv instead.
        """
        self.fake.respond(argv, effect=self._unmount_temp, once=True)
        self.fake.respond(argv, effect=effect, once=True)

    def refuse(self, argv: list[str]) -> None:
        """Make the next run of *argv* exit non-zero."""
        self.fake.respond(argv, returncode=_REFUSED, stderr=b"boom", once=True)

    def ignore(self, argv: list[str], *, prefix: bool = False) -> None:
        """Make the next run of *argv* exit zero having changed nothing."""
        self.fake.respond(argv, once=True, prefix=prefix)

    def retire_old(self) -> None:
        """The state a completed resize leaves: NEW at the folder, OLD retired."""
        write_file(
            self.fstab,
            f"# local\nUUID={NEW} {self.path} apfs rw,nobrowse\n"
            f"# apfs_tm_snapfree: retired UUID={OLD} for {self.path}\n",
        )
        self.volumes.append(
            fake_host.volume_entry(uuid=NEW, name=NEW_NAME, quota=NEW_QUOTA)
        )
        self.infos[str(self.path)]["volume_uuid"] = NEW
        # Both exclusions stay listed: tmutil cannot unlist a volume it cannot mount.
        self.excluded_uuids[:] = [OLD, NEW]
