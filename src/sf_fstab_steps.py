"""The plan step that rewrites the fstab and reads it back, shared by the modes.

It lives apart from ``sf_fstab`` so that library keeps to text, the fstab file and
the mount check, with no dependency on the plan machinery: another tool can import
``sf_fstab`` alone.

Entry point assumptions: called by the mode modules while they build a plan; the step
runs through the ``Host`` it is handed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Callable

from sf_fstab import describe_rewrite, read_fstab, replace_fstab, restore_command
from sf_host import Host
from sf_plan import ConfirmFailed, Step


def fstab_step(
    host: Host,
    fstab: Path,
    what: str,
    edit: Callable[[str], str],
    took: Callable[[str], bool],
) -> Step:
    """A step rewriting *fstab* with *edit*, then re-reading it until *took* holds.

    The text is read again when the step runs, not when the plan is built, so an
    edit made in between is kept. A rewrite that cannot be read back is a
    ``ConfirmFailed``: the fstab may already have been replaced, so the recovery
    block prints this step's restore command too.
    """

    def act() -> None:
        replace_fstab(host, fstab, edit(read_fstab(host, fstab)))
        if not took(read_fstab(host, fstab)):
            raise ConfirmFailed(
                f"{fstab} does not show the change ({what}) after the rewrite"
            )

    commands = "; ".join(describe_rewrite(fstab, backup=True))
    return Step(f"in {fstab}, {what}: {commands}", act, restore_command(fstab))
