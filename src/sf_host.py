"""The host seams: every command, stat, read and temporary file goes through ``Host``.

Every effect on the machine passes through one of these methods, so a suite can swap
the whole machine for a scripted fake (``tests/fake_host.py``) and nothing it runs can
reach a disk, a system preference or root. ``real_host()`` is the machine itself.

Commands run without a shell, with stdout and stderr captured and stdin left attached
to the terminal, so ``sudo`` can prompt for a password. Every run carries a timeout.

Entry point assumptions: imported by the entry point and the mode modules; standard
library only, and safe on Python 3.9.
"""

from __future__ import annotations

import os
import shlex
import subprocess
import sys
import tempfile
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Optional, Protocol, Sequence

from sf_errors import ToolError

#: diskutil, tmutil, mdutil and the sudo file commands finish in seconds; two minutes is
#: generous for a slow disk or a sudo password typed at the prompt, and still ends a hang.
COMMAND_TIMEOUT = 120.0
#: ditto copies the whole folder, so it is bounded by the data, not by the tool: six
#: hours covers hundreds of gigabytes on a slow disk while still ending a stuck copy.
COPY_TIMEOUT = 6 * 60 * 60.0
#: How long a timed-out child gets to exit after SIGTERM before SIGKILL: long enough
#: for diskutil to release a disk it was working on, short enough not to look hung.
_STOP_GRACE = 5.0
#: Child output is the host's bytes; one stray byte must not abort a run.
_DECODE_ERRORS = "replace"
_ENCODING = "utf-8"
#: How macOS words a privacy (TCC) refusal, in every tool this project runs.
_PRIVACY_DENIAL = "Operation not permitted"
_FDA_HINT = (
    "macOS refused access: the terminal app running this likely lacks Full Disk "
    "Access (System Settings > Privacy & Security > Full Disk Access). This tool "
    "never changes privacy settings."
)
#: The stamp format: UTC, sortable, and safe in a file name.
_STAMP_FORMAT = "%Y%m%d%H%M%S"
#: Said by the user and group lookups on the one platform that has neither. The tool
#: refuses to run off macOS before it asks, so this is a type-level guarantee only.
_NO_POSIX_IDS = "user and group IDs exist only on a POSIX system"


@dataclass(frozen=True)
class RunResult:
    """What one command did: its argv, exit code and captured bytes."""

    argv: tuple[str, ...]
    returncode: int
    stdout: bytes
    stderr: bytes

    def stdout_text(self) -> str:
        """Stdout decoded as UTF-8, a bad byte replaced rather than raised."""
        return self.stdout.decode(_ENCODING, _DECODE_ERRORS)

    def stderr_text(self) -> str:
        """Stderr decoded as UTF-8, a bad byte replaced rather than raised."""
        return self.stderr.decode(_ENCODING, _DECODE_ERRORS)


class Host(Protocol):
    """Everything the tool asks of the machine."""

    platform: str

    def run(self, argv: Sequence[str], timeout: float) -> RunResult:
        """Run *argv* without a shell, bounded by *timeout* seconds."""

    def device_of(self, path: Path) -> int:
        """The ``st_dev`` of *path*: which filesystem it lives on."""

    def read_text(self, path: Path) -> str:
        """The UTF-8 text of *path*; a missing file raises ``FileNotFoundError``."""

    def write_temp(self, text: str) -> Path:
        """Write *text* to a new file only this user can read, and return its path."""

    def getuid(self) -> int:
        """The user ID the tool runs as."""

    def getgid(self) -> int:
        """The group ID the tool runs as."""

    def now(self) -> str:
        """A UTC timestamp, ``YYYYmmddHHMMSS``, for naming aside folders and volumes."""

    def read_probe(self, path: Path) -> None:
        """Open *path* for reading and read at most one byte; any ``OSError`` raises."""

    def getenv(self, name: str) -> Optional[str]:
        """The environment variable *name*, or None when it is unset."""

    def sleep(self, seconds: float) -> None:
        """Wait *seconds*: the one pause, taken only between retries."""


def format_argv(argv: Sequence[str]) -> str:
    """*argv* as one shell-quoted line an operator can copy and paste."""
    return shlex.join(argv)


def failure_message(result: RunResult) -> str:
    """The ToolError text for a command that exited non-zero."""
    message = f"{format_argv(result.argv)} exited {result.returncode}"
    detail = result.stderr_text().strip()
    if detail:
        message = f"{message}: {detail}"
    if _PRIVACY_DENIAL in detail:
        message = f"{message}\n{_FDA_HINT}"
    return message


def run_checked(host: Host, argv: Sequence[str], timeout: float) -> RunResult:
    """Run *argv* on *host*; a non-zero exit raises ``ToolError`` naming the command."""
    result = host.run(argv, timeout)
    if result.returncode != 0:
        raise ToolError(failure_message(result))
    return result


class RealHost:
    """The machine itself."""

    def __init__(self, temp_dir: Optional[Path] = None) -> None:
        """Bind the folder temporary files go in; None means the user's default."""
        self.platform = sys.platform
        self.temp_dir = temp_dir

    def run(self, argv: Sequence[str], timeout: float) -> RunResult:
        """Run *argv*; a timeout or a program that cannot start is a ``ToolError``."""
        command = tuple(argv)
        try:
            # stdin is deliberately not redirected, and the child stays in this
            # session and process group: sudo reads its password from the terminal.
            process = subprocess.Popen(
                list(command), stdout=subprocess.PIPE, stderr=subprocess.PIPE
            )
        except OSError as exc:
            raise ToolError(f"{format_argv(command)} could not start: {exc}") from exc
        try:
            stdout, stderr = process.communicate(timeout=timeout)
        except subprocess.TimeoutExpired as exc:
            note = _stop(process)
            raise ToolError(
                f"{format_argv(command)} did not finish within {timeout:g} s{note}"
            ) from exc
        return RunResult(command, process.returncode, stdout, stderr)

    def device_of(self, path: Path) -> int:
        """``os.stat(path).st_dev``."""
        return os.stat(path).st_dev

    def read_text(self, path: Path) -> str:
        """Read *path* as UTF-8."""
        return path.read_text(encoding=_ENCODING)

    def write_temp(self, text: str) -> Path:
        """Write *text* to a fresh file in ``temp_dir`` that only this user can read.

        ``mkstemp`` creates it mode 0600, so the fstab text is never readable by other
        users on its way to the root-owned copy sudo installs.
        """
        handle, name = tempfile.mkstemp(
            prefix="apfs_tm_snapfree-", suffix=".txt", dir=self.temp_dir
        )
        with os.fdopen(handle, "w", encoding=_ENCODING, newline="\n") as stream:
            stream.write(text)
        return Path(name)

    def getuid(self) -> int:
        """``os.getuid()``."""
        if sys.platform == "win32":  # pragma: only-on win32
            raise ToolError(_NO_POSIX_IDS)
        return os.getuid()

    def getgid(self) -> int:
        """``os.getgid()``."""
        if sys.platform == "win32":  # pragma: only-on win32
            raise ToolError(_NO_POSIX_IDS)
        return os.getgid()

    def now(self) -> str:
        """The current UTC time as ``YYYYmmddHHMMSS``."""
        return datetime.now(timezone.utc).strftime(_STAMP_FORMAT)

    def read_probe(self, path: Path) -> None:
        """Read one byte of *path*, so that a refusal to open or read it raises.

        Binary and unbuffered: only whether macOS lets this process read the file
        matters, never what it holds.
        """
        with open(path, "rb", buffering=0) as stream:
            stream.read(1)

    def getenv(self, name: str) -> Optional[str]:
        """``os.environ.get(name)``."""
        return os.environ.get(name)

    def sleep(self, seconds: float) -> None:
        """``time.sleep(seconds)``."""
        time.sleep(seconds)


def _stop(process: subprocess.Popen[bytes]) -> str:
    """End a child that ran past its timeout, without ever blocking on its pipes.

    ``subprocess.run`` would SIGKILL the direct child, which under sudo is sudo itself:
    a signal it cannot relay, so the command it started (diskutil, ditto) would carry on
    unsupervised. SIGTERM comes first instead, which sudo does relay to the command;
    SIGKILL only if the child outlives the grace period, then a second bounded wait to
    reap it. This end of the pipes is closed rather than read, so a grandchild still
    holding them cannot hold this call. Returns a note for the timeout error: empty when
    the child was reaped, else a sentence saying it may still be running.
    """
    process.terminate()
    try:
        process.wait(timeout=_STOP_GRACE)
        return ""
    except subprocess.TimeoutExpired:
        process.kill()
        try:
            process.wait(timeout=_STOP_GRACE)
            return ""
        except subprocess.TimeoutExpired:
            return f"; it did not exit even after SIGKILL (pid {process.pid})"
    finally:
        for stream in filter(None, (process.stdout, process.stderr)):
            stream.close()


def real_host() -> RealHost:
    """The machine itself, writing temporary files to the user's default folder."""
    return RealHost()
