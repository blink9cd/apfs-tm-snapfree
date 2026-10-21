"""Read, edit and safely replace the fstab line that mounts a folder's volume at boot.

The fstab is the machine's own declaration of which volume belongs at which path, so it
is both what makes the mount survive a reboot and the record that a volume is expected
there at all. fstab(5) writes whitespace inside a field as a backslash and three octal
digits; lines are matched by mount point with both sides resolved through symlinks,
and the first matching line wins, as at boot.

Every edit here is a pure function from text to text. ``replace_fstab`` is the one
writer: the new text goes to a user temporary file, ``sudo install`` stages it beside
the fstab as root-owned 0644, and ``sudo mv -f`` renames it into place, which is atomic
because both sit in one folder. The previous fstab is copied to a backup first.

Entry point assumptions: the edits are pure; ``replace_fstab`` and ``is_mount_point``
reach the machine only through the ``Host`` seams they are handed.
"""

from __future__ import annotations

import os
import re
import shlex
from dataclasses import dataclass
from pathlib import Path
from typing import Callable, Optional

from sf_errors import ToolError
from sf_host import COMMAND_TIMEOUT, Host, format_argv, run_checked

#: Where macOS reads the volumes it mounts at boot.
DEFAULT_FSTAB = Path("/etc/fstab")
#: The filesystem type and options every line this tool writes carries. nobrowse keeps
#: the volume out of the Finder sidebar and off the desktop.
FS_TYPE = "apfs"
MOUNT_OPTIONS = "rw,nobrowse"
#: The previous fstab, kept beside it, and the staged copy renamed onto it.
BACKUP_SUFFIX = ".apfs_tm_snapfree.bak"
STAGED_SUFFIX = ".apfs_tm_snapfree.new"
#: The placeholder a dry run shows for the temporary file a real run creates.
TEMP_PLACEHOLDER = "<temp>"
#: The comment resize leaves until --finish deletes the volume it retired.
MARKER_PREFIX = "# apfs_tm_snapfree: retired UUID="
_MARKER_RE = re.compile(re.escape(MARKER_PREFIX) + r"(\S+) for (\S+)\s*")
#: fstab writes a character inside a field as a backslash and three octal digits.
_OCTAL_ESCAPE = re.compile(r"\\([0-7]{3})")
#: The characters that would split or corrupt a field, so encode_field escapes them.
_MUST_ESCAPE = frozenset(" \t\n\\")
#: The spec form naming a volume by UUID; diskutil wants the bare UUID.
_UUID_PREFIX = "uuid="
_SPEC_PREFIX = "UUID="
#: A mount line has at least a spec and a mount point.
_MIN_FIELDS = 2
#: The first field of a line, kept apart from the whitespace before it.
_FIRST_FIELD = re.compile(r"^(\s*)\S+")


@dataclass(frozen=True)
class FstabEntry:
    """One fstab line's first two fields, decoded: what to mount, and where."""

    spec: str
    mount_point: str

    @property
    def volume_id(self) -> str:
        """The spec as diskutil takes it: a ``UUID=`` prefix dropped."""
        return volume_id(self.spec)


@dataclass(frozen=True)
class RetiredMarker:
    """A volume a resize retired and kept, pending ``--mode resize --finish``."""

    uuid: str
    path: str


def decode_field(field: str) -> str:
    r"""*field* with fstab's octal escapes (``\040`` for a space) decoded."""
    return _OCTAL_ESCAPE.sub(lambda match: chr(int(match.group(1), 8)), field)


def encode_field(text: str) -> str:
    r"""*text* as one fstab field: space, tab, newline and backslash as ``\ooo``."""
    return "".join(
        f"\\{ord(char):03o}" if char in _MUST_ESCAPE else char for char in text
    )


def volume_id(spec: str) -> str:
    """The identifier ``diskutil mount`` takes for *spec*: a ``UUID=`` prefix dropped."""
    if spec.lower().startswith(_UUID_PREFIX):
        return spec[len(_UUID_PREFIX) :]
    return spec


def _line_entry(line: str) -> Optional[FstabEntry]:
    """The entry *line* declares, or None for a comment, a blank or a short line."""
    fields = line.split()
    if len(fields) < _MIN_FIELDS or fields[0].startswith("#"):
        return None
    return FstabEntry(decode_field(fields[0]), decode_field(fields[1]))


def parse_entries(text: str) -> list[FstabEntry]:
    """Every mount line in fstab *text*, in order."""
    entries = (_line_entry(line) for line in text.splitlines())
    return [entry for entry in entries if entry is not None]


def same_path(first: str, second: str | Path) -> bool:
    """Whether two paths name one place once symlinks and trailing slashes resolve."""
    return os.path.realpath(first) == os.path.realpath(second)


def entry_for(text: str, path: Path) -> Optional[FstabEntry]:
    """The first line of *text* mounting a volume at *path*, or None."""
    for entry in parse_entries(text):
        if same_path(entry.mount_point, path):
            return entry
    return None


def render_line(uuid: str, path: Path) -> str:
    """The line this tool writes: ``UUID=<uuid> <escaped path> apfs rw,nobrowse``."""
    return f"{_SPEC_PREFIX}{uuid} {encode_field(str(path))} {FS_TYPE} {MOUNT_OPTIONS}"


def add_line(text: str, line: str) -> str:
    """*text* with *line* appended, each ending in a newline."""
    if "\n" in line:
        raise ValueError("an fstab line cannot contain a newline")
    if text and not text.endswith("\n"):
        text += "\n"
    return f"{text}{line}\n"


def _entry_index(lines: list[str], path: Path) -> int:
    """The index of the first line mounting at *path*; a ``ToolError`` if none does."""
    for index, line in enumerate(lines):
        entry = _line_entry(line)
        if entry is not None and same_path(entry.mount_point, path):
            return index
    raise ToolError(f"the fstab has no line mounting a volume at {path}")


def remove_entry(text: str, path: Path) -> str:
    """*text* without the first line mounting at *path*; every other line kept as is."""
    lines = text.splitlines(keepends=True)
    del lines[_entry_index(lines, path)]
    return "".join(lines)


def swap_uuid(text: str, path: Path, new_uuid: str) -> str:
    """*text* with the first line for *path* naming *new_uuid*; its spacing kept."""
    lines = text.splitlines(keepends=True)
    index = _entry_index(lines, path)
    # A function, not a replacement template, so nothing in the UUID is read as one.
    lines[index] = _FIRST_FIELD.sub(
        lambda match: f"{match.group(1)}{_SPEC_PREFIX}{new_uuid}", lines[index], count=1
    )
    return "".join(lines)


def render_marker(uuid: str, path: Path) -> str:
    """The comment resize leaves naming the volume it retired for *path*."""
    return f"{MARKER_PREFIX}{uuid} for {encode_field(str(path))}"


def _line_marker(line: str) -> Optional[RetiredMarker]:
    """The marker *line* holds, or None."""
    match = _MARKER_RE.fullmatch(line.strip())
    if match is None:
        return None
    return RetiredMarker(match.group(1), decode_field(match.group(2)))


def parse_markers(text: str) -> list[RetiredMarker]:
    """Every retired-volume marker in *text*, in order."""
    markers = (_line_marker(line) for line in text.splitlines())
    return [marker for marker in markers if marker is not None]


def marker_for(text: str, path: Path) -> Optional[RetiredMarker]:
    """The first marker in *text* for *path*, or None."""
    for marker in parse_markers(text):
        if same_path(marker.path, path):
            return marker
    return None


def remove_marker(text: str, path: Path) -> str:
    """*text* without the first marker for *path*; a ``ToolError`` if there is none."""
    lines = text.splitlines(keepends=True)
    for index, line in enumerate(lines):
        marker = _line_marker(line)
        if marker is not None and same_path(marker.path, path):
            del lines[index]
            return "".join(lines)
    raise ToolError(f"the fstab has no retired-volume marker for {path}")


def is_mount_point(path: Path, device_of: Callable[[Path], int]) -> bool:
    """Whether something is mounted at *path*: its device differs from its parent's.

    The path is resolved first, so the parent compared is the real one, not a
    symlink's. This reads the authoritative ``st_dev`` and never parses ``df``.
    """
    resolved = Path(os.path.realpath(path))
    return device_of(resolved) != device_of(resolved.parent)


def backup_path(fstab: Path) -> Path:
    """Where the previous fstab is copied before a rewrite."""
    return fstab.with_name(fstab.name + BACKUP_SUFFIX)


def restore_command(fstab: Path) -> str:
    """The command putting back the fstab a rewrite backed up."""
    return format_argv(["sudo", "cp", str(backup_path(fstab)), str(fstab)])


def read_fstab(host: Host, fstab: Path) -> str:
    """The fstab's text; an absent fstab reads as empty, an unreadable one is an error.

    Empty is what an absent fstab means at boot: no line for anyone.
    """
    try:
        return host.read_text(fstab)
    except FileNotFoundError:
        return ""
    except (OSError, UnicodeDecodeError) as exc:
        raise ToolError(f"could not read {fstab}: {exc}") from exc


def same_uuid(first: str, second: str) -> bool:
    """Whether two volume UUIDs are the same; diskutil prints upper case, people may not."""
    return first.upper() == second.upper()


def staged_path(fstab: Path) -> Path:
    """Where the new fstab is staged, beside it, so the final rename is atomic."""
    return fstab.with_name(fstab.name + STAGED_SUFFIX)


def rewrite_argvs(fstab: Path, temp: str, *, backup: bool) -> list[list[str]]:
    """The commands that put *temp*'s text in place as *fstab*, backup first if asked."""
    staged = str(staged_path(fstab))
    commands = [
        ["sudo", "install", "-m", "0644", "-o", "root", "-g", "wheel", temp, staged],
        ["sudo", "mv", "-f", staged, str(fstab)],
    ]
    if backup:
        commands.insert(0, ["sudo", "cp", str(fstab), str(backup_path(fstab))])
    return commands


def _shown_item(item: str) -> str:
    """One argv item as a person reads it: quoted, except the bare placeholder."""
    return item if item == TEMP_PLACEHOLDER else shlex.quote(item)


def describe_rewrite(fstab: Path, *, backup: bool) -> list[str]:
    """The rewrite's commands as a dry run shows them, the temp file a placeholder.

    The placeholder stays bare, as every other placeholder prints: quoted, it would
    read as a literal file name.
    """
    return [
        " ".join(_shown_item(item) for item in argv)
        for argv in rewrite_argvs(fstab, TEMP_PLACEHOLDER, backup=backup)
    ]


def _exists(host: Host, path: Path) -> bool:
    """Whether *path* can be read on *host*."""
    try:
        host.read_text(path)
    except FileNotFoundError:
        return False
    return True


def replace_fstab(host: Host, path: Path, text: str) -> None:
    """Make *text* the fstab at *path*: back up, stage beside it, rename into place.

    The temporary file is removed whether or not the commands succeed. A failure raises
    ``ToolError`` and leaves the fstab as it was, or as the backup shows it was.
    """
    backup = _exists(host, path)
    temp = host.write_temp(text)
    try:
        for argv in rewrite_argvs(path, str(temp), backup=backup):
            run_checked(host, argv, COMMAND_TIMEOUT)
    finally:
        _remove_temp(temp)


def _remove_temp(temp: Path) -> None:
    """Delete the user temporary file, never raising over the rewrite's own outcome.

    A failure here would replace the error that says whether the fstab changed, which
    is the one thing the operator needs; the file is mode 0600 in the user's own
    temporary folder, so leaving it behind exposes nothing.
    """
    try:
        temp.unlink(missing_ok=True)
    except OSError:
        return
