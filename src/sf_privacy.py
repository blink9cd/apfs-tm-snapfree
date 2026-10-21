"""The Full Disk Access preflight: refuse an applied run that macOS would stop midway.

``sudo tmutil addexclusion -v`` (setup's and resize's exclusion step) exits 80 with
"requires Full Disk Access privileges" when the terminal app running it lacks the
grant, by which point setup or resize has already made, mounted and owned a volume.
The run should refuse before its first step instead. ``tmutil`` offers no probe that
needs the grant and runs without root: a plain ``addexclusion`` of one of the user's
own files succeeds without it, and ``isexcluded`` never needs it. Reading Time
Machine's preferences file does: it is mode 0644, readable by anyone as far as POSIX
goes, yet macOS's privacy controls refuse the read with EPERM unless the process holds
Full Disk Access. So that read is the probe, made as the invoking user, through the
``Host`` so the suites fake it.

The same grant covers the steps' read of that file's ``ExcludedVolumeUUIDs`` under
sudo (``sf_macos.excluded_volume_uuids``), which confirms each exclusion step and is
resize --finish's last step: root is refused the read without it too.

The probe only ever reads, and the tool never changes the grant: it names the app that
needs it, from ``TERM_PROGRAM``. A file that is not there (Time Machine never set up)
or any other read error says nothing about the grant, so the run goes on and the
step's own failure, with its recovery, still holds.

Entry point assumptions: called by setup, resize (and its --finish) and teardown after validation and
before any step, through the ``Host`` they were handed.
"""

from __future__ import annotations

import errno
from pathlib import Path
from typing import Optional, TextIO

from sf_errors import ToolError
from sf_host import Host

#: Readable only with Full Disk Access, though its mode lets anyone read it.
TIME_MACHINE_PREFERENCES = Path("/Library/Preferences/com.apple.TimeMachine.plist")
#: What macOS's privacy controls raise; a POSIX refusal is EACCES, and means nothing here.
_PRIVACY_ERRNO = errno.EPERM
#: The variable a macOS terminal names itself in, and the names worth translating.
_TERM_PROGRAM = "TERM_PROGRAM"
_APP_NAMES = {"Apple_Terminal": "Terminal"}
_UNNAMED_APP = "the app running this terminal"
_SETTINGS = "System Settings > Privacy & Security > Full Disk Access"
_MISSING_GRANT = (
    "{app} does not have Full Disk Access, which {command} needs: macOS refused to "
    "let this run read {probe}. Grant it in {settings}, then quit and reopen {app} "
    "and run this again. This tool never changes privacy settings."
)
#: The steps that need the grant: setup's and resize's exclusion, teardown's removal,
#: and resize --finish's read of the exclusions list.
EXCLUDE_COMMAND = "sudo tmutil addexclusion -v"
INCLUDE_COMMAND = "sudo tmutil removeexclusion -v"
EXCLUSIONS_READ_COMMAND = "sudo /usr/bin/plutil -extract ExcludedVolumeUUIDs"
_DRY_RUN_WARNING = "Warning: {message} Until then --apply refuses to start."


def full_disk_access(host: Host) -> Optional[bool]:
    """Whether this process holds Full Disk Access: True, False, or None if unknown."""
    try:
        host.read_probe(TIME_MACHINE_PREFERENCES)
    except OSError as exc:
        return False if exc.errno == _PRIVACY_ERRNO else None
    return True


def terminal_app(host: Host) -> str:
    """The app the grant belongs to, as its terminal names itself, or a description."""
    name = host.getenv(_TERM_PROGRAM)
    if not name:
        return _UNNAMED_APP
    return _APP_NAMES.get(name, name)


def check_full_disk_access(
    host: Host, command: str, *, apply: bool, out: TextIO
) -> None:
    """Refuse an applied run without the grant; warn a dry run; else say nothing.

    *command* is the step that needs the grant, named in the message. The refusal is
    a ``ToolError``, which the entry point turns into exit 2 before any step runs.
    """
    if full_disk_access(host) is not False:
        return
    message = _MISSING_GRANT.format(
        app=terminal_app(host),
        command=command,
        probe=TIME_MACHINE_PREFERENCES,
        settings=_SETTINGS,
    )
    if apply:
        raise ToolError(message)
    print(_DRY_RUN_WARNING.format(message=message), file=out)
