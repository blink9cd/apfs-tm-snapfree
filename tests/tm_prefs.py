"""Scripted replies for the read of Time Machine's ``ExcludedVolumeUUIDs`` list.

The suites answer ``sudo /usr/bin/plutil -extract ExcludedVolumeUUIDs xml1 -o - <prefs>``
through a ``FakeHost``, so no test reads the machine's own preferences. The two failure
texts are plutil's own wording, captured from the real tool on a scratch plist, so the
matchers in ``sf_macos`` are tested against what plutil prints rather than against
themselves. Kept beside ``fake_host`` rather than in it, which is at its length bar.

``ExcludedList`` models how ``tmutil addexclusion -v`` changes that list, as it was seen
to live on macOS 27: on a volume mounted visible it adds the volume's UUID, and on one
mounted ``nobrowse`` it exits 0 and adds nothing. A plan that excluded a volume only
after mounting it nobrowse therefore fails its confirm here as it did on the machine.

Entry point assumptions: imported by the suites as ``tests.tm_prefs``.
"""

from __future__ import annotations

import plistlib
from pathlib import Path
from typing import Optional, Sequence

import sf_macos
from sf_fstab import is_mount_point
from sf_host import RunResult
from tests.fake_host import FakeHost

#: How every mount the tool runs begins, and the flag that names where it mounts.
_MOUNT_PREFIX = ("sudo", "diskutil", "mount")
_MOUNT_POINT_FLAG = "-mountPoint"
#: The mount option that hides a volume, and makes addexclusion -v a no-op on it.
_NOBROWSE = "nobrowse"

#: What plutil prints, with exit 1, for a plist that lacks the key.
NO_KEY_STDERR = (
    b"/Library/Preferences/com.apple.TimeMachine.plist: Could not extract value, "
    b"error: No value at that key path or invalid key path: ExcludedVolumeUUIDs\n"
)
#: What plutil prints, with exit 1, for a plist that is not there.
NO_FILE_STDERR = (
    "/Library/Preferences/com.apple.TimeMachine.plist: (The file "
    "“com.apple.TimeMachine.plist” couldn’t be opened because there "
    "is no such file.)\n"
).encode("utf-8")


def excluded_uuids_plist(uuids: Sequence[object]) -> bytes:
    """What ``plutil -extract ExcludedVolumeUUIDs xml1 -o -`` prints: the bare array."""
    return plistlib.dumps(list(uuids))


#: Enough once-only replies to answer every read of one whole poll.
EVERY_POLL_READ = len(sf_macos.EXCLUSION_POLL_WAITS) + 1


def respond_excluded_uuids(
    fake: FakeHost, uuids: Sequence[str], *, once: bool = False, reads: int = 0
) -> None:
    """Answer the ``ExcludedVolumeUUIDs`` read with *uuids*.

    With *reads*, as that many once-only replies: the next *reads* reads see *uuids*
    over any standing reply, so a test can make a whole poll, or its first few reads,
    see a state the run's own reply would not.
    """
    for _ in range(max(reads, 1)):
        fake.respond(
            list(sf_macos.EXCLUDED_UUIDS_ARGV),
            stdout=excluded_uuids_plist(uuids),
            once=once or reads > 0,
        )


def _mounted_at(argv: tuple[str, ...], where: str) -> bool:
    """Whether *argv* is a ``diskutil mount`` of a volume at *where*."""
    if argv[: len(_MOUNT_PREFIX)] != _MOUNT_PREFIX or _MOUNT_POINT_FLAG not in argv:
        return False
    return argv[argv.index(_MOUNT_POINT_FLAG) + 1] == where


def visible_volume_at(fake: FakeHost, where: str) -> Optional[str]:
    """The UUID of the volume mounted visible at *where*, or None.

    Read from the fake's call log: the last ``diskutil mount`` at *where* names the
    volume and says whether it passed ``nobrowse``. None also when nothing is mounted
    there now, or the last mount was nobrowse.
    """
    if not is_mount_point(Path(where), fake.device_of):
        return None
    for argv in reversed(fake.calls):
        if _mounted_at(argv, where):
            return None if _NOBROWSE in argv else argv[-1]
    return None


class ExcludedList:
    """Time Machine's ``ExcludedVolumeUUIDs``, which ``addexclusion -v`` changes as live.

    Answers the list's read from ``uuids`` as a standing reply, so a test's once-only
    ``respond_excluded_uuids`` still answers first. ``add`` is the effect of a scripted
    ``tmutil addexclusion -v <where>``: it lists the volume only when it is mounted
    visible at *where*, the live finding this models.
    """

    def __init__(self, fake: FakeHost, uuids: Sequence[str]) -> None:
        """Start the list as *uuids*, and answer every read of it on *fake*."""
        self.fake = fake
        self.uuids = list(uuids)
        fake.respond(list(sf_macos.EXCLUDED_UUIDS_ARGV), effect=self._read)

    def _read(self, argv: tuple[str, ...]) -> RunResult:
        """The list as it stands, as plutil prints it."""
        return RunResult(argv, 0, excluded_uuids_plist(self.uuids), b"")

    def add(self, argv: tuple[str, ...]) -> None:
        """``tmutil addexclusion -v <where>``: list a visible volume; else nothing."""
        uuid = visible_volume_at(self.fake, argv[-1])
        if uuid is not None:
            self.uuids.append(uuid)
