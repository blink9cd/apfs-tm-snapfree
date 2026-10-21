"""The parsed command line every mode is handed.

It lives in ``src/`` rather than in the entry point so the mode modules can name the
type without importing the entry point that imports them.

Entry point assumptions: pure definitions; the entry point builds the one instance.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from pathlib import Path
from typing import Optional

#: The four modes, in the order --help lists them.
MODE_NAMES = ("setup", "status", "resize", "teardown")
#: Use of quota at or above this percentage is "needs attention" in status.
DEFAULT_WARN_PERCENT = 90
#: setup names the new volume this, then the folder's own name, unless --name is given.
VOLUME_NAME_PREFIX = "snapfree-"
#: The stamp a resize appends to the volume's name: a dash and exactly fourteen
#: digits, at the very end.
_STAMP_SUFFIX = re.compile(r"-[0-9]{14}\Z")


def volume_name_stem(name: str) -> str:
    """*name* without the trailing ``-<14 digit stamp>`` a resize gives its volume.

    Every volume resize makes for a folder shares one stem, which is how --finish
    tells the volume a resize retired from an unrelated one.
    """
    return _STAMP_SUFFIX.sub("", name)


@dataclass(frozen=True)
class Options:
    """One run's options; ``path`` is already resolved through symlinks."""

    mode: str
    path: Path
    quota: Optional[int]
    name: Optional[str]
    fstab: Path
    warn_percent: int
    finish: bool
    apply: bool

    @property
    def volume_name(self) -> str:
        """The name setup gives the new volume: --name, or one from the folder's name."""
        if self.name is not None:
            return self.name
        return f"{VOLUME_NAME_PREFIX}{self.path.name}"
