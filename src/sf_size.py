"""Sizes in diskutil's syntax: an integer and k, m, g or t, in powers of 1000.

diskutil reads ``40g`` as 40 * 1000**3 bytes, not 40 * 1024**3, so every size this tool
reads, prints or passes back to diskutil uses the same decimal units.

Entry point assumptions: pure functions; no host access.
"""

from __future__ import annotations

import re

#: The suffixes diskutil accepts, largest first, each a power of 1000.
_UNITS = (("t", 10**12), ("g", 10**9), ("m", 10**6), ("k", 10**3))
_UNIT_BYTES = dict(_UNITS)
#: Display labels for format_size, matching _UNITS.
_LABELS = {"t": "TB", "g": "GB", "m": "MB", "k": "kB"}
#: ASCII digits and letters only, matched whole: \d also matches other scripts' digits,
#: and IGNORECASE would admit the Kelvin sign as k; diskutil rejects both.
_SIZE_RE = re.compile(r"([1-9][0-9]*)([kmgtKMGT])")
#: Headroom kept free beyond the data being copied, so a copy that fits exactly never
#: leaves the container full: one gigabyte.
SPACE_MARGIN = 10**9


def parse_size(text: str) -> int:
    """The bytes *text* names, such as ``40g``; ``ValueError`` if it is not that form."""
    match = _SIZE_RE.fullmatch(text)
    if match is None:
        raise ValueError(
            f"{text!r} is not a size: use a positive whole number and k, m, g or t, "
            "such as 40g (powers of 1000, as diskutil reads them)"
        )
    return int(match.group(1)) * _UNIT_BYTES[match.group(2).lower()]


def quota_arg(size: int) -> str:
    """*size* in diskutil's syntax, in the largest unit that divides it exactly."""
    if size <= 0:
        raise ValueError(f"a quota must be positive, got {size}")
    for suffix, unit in _UNITS:
        if size % unit == 0:
            return f"{size // unit}{suffix}"
    return str(size)


def format_size(size: int) -> str:
    """*size* for a person to read: ``40.0 GB``, or whole bytes below a kilobyte."""
    for suffix, unit in _UNITS:
        if size >= unit:
            return f"{size / unit:.1f} {_LABELS[suffix]}"
    return f"{size} B"
