"""The follow-up commands the modes print for the operator to paste.

setup ends by naming the status command to run after the next reboot, and resize,
status and teardown name the ``--finish`` command that deletes a retired volume. Each
is built here, the one way: ``python3``, then this program's own file name, so the
line pastes as the documented ``python3 apfs_tm_snapfree.py ...`` from the tool's
folder even though the file is not executable; every word shell-quoted; and a
non-default ``--fstab`` carried along, because the command would otherwise read the
real fstab rather than the one this run was pointed at.

Entry point assumptions: pure functions of their arguments and ``sys.argv[0]``.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path

from sf_fstab import DEFAULT_FSTAB
from sf_host import format_argv

#: The interpreter each printed command names: the entry point is run, not executed.
PYTHON = "python3"


def tool_command(mode: str, path: Path, fstab: Path, *flags: str) -> str:
    """``python3 <prog> --mode <mode> --path <path> [flags] [--fstab <fstab>]``.

    *fstab* is named only when it is not the default, so the usual case stays short.
    """
    argv = [PYTHON, os.path.basename(sys.argv[0]), "--mode", mode, "--path", str(path)]
    argv += flags
    if fstab != DEFAULT_FSTAB:
        argv += ["--fstab", str(fstab)]
    return format_argv(argv)


def status_command(path: Path, fstab: Path) -> str:
    """The status check of *path* under *fstab*."""
    return tool_command("status", path, fstab)


def finish_command(path: Path, fstab: Path) -> str:
    """The run that deletes the volume a resize of *path* retired: --apply included."""
    return tool_command("resize", path, fstab, "--finish", "--apply")
