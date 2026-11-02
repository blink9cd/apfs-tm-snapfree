#!/usr/bin/env python3
"""Force UTF-8 on a tool's streams, name the capture policy, and escape its messages.

Why this exists
---------------
Python derives a text stream's encoding from the locale whenever that stream is not a
terminal. On Windows that is the legacy ANSI code page (cp1252 on a Western install),
which cannot encode most non-ASCII characters. A tool that prints a path it discovered
on disk therefore works when run by hand in a UTF-8 terminal and raises
``UnicodeEncodeError`` the moment stdout is a pipe, which is every scheduled run, every
CI run, and every run whose parent captures it.

The same locale default governs the other direction. ``subprocess.run(..., text=True)``
with no explicit ``encoding`` decodes the child using the locale code page, so a child
emitting UTF-8 decodes to mojibake. Worse, a byte undefined in cp1252 (0x81, 0x8D,
0x8F, 0x90, 0x9D) raises ``UnicodeDecodeError`` on subprocess's reader thread, where it
is swallowed: ``run()`` returns ``stdout=None`` and the caller fails later with a
confusing ``TypeError``.

The two halves must be fixed together in any tool that captures a child and reprints it.
A cp1252 decode followed by a cp1252 encode restores the original bytes by accident, so
forcing only the output stream to UTF-8 turns a working accident into double-encoded
corruption.

A strict stream brings a third hazard with it. Python decodes argv and file names with
``surrogateescape``, so a byte that is not UTF-8 arrives as a lone surrogate, which a
strict stream refuses: a refusal quoting a mistyped argument becomes a traceback. So the
two streams are forced differently. stderr keeps the ``backslashreplace`` Python opened
it with, and every diagnostic on it (argparse's errors included) prints the escape.
stdout stays strict, so a tool's data still fails loudly, and a message that reaches
stdout is escaped at one boundary instead: ``print_escaped`` for a tool's own lines, and
``EscapingArgumentParser`` for argparse's help and ``--version``.

Entry point assumptions
-----------------------
``force_utf8_streams`` is called once, early in a tool's ``main()``, before anything is
printed. It deliberately does nothing for a stream a test has replaced with
``io.StringIO`` (no ``reconfigure`` method) or for a closed stream, so adopting it can
never destabilize an existing test harness.

This module is a library: it has no CLI, and each tool imports it after putting this
folder on ``sys.path`` from the project root, as ``stdio_utf8.md`` documents.
"""

from __future__ import annotations

import argparse
import io
import sys
from typing import IO, Protocol

#: Text encoding for both directions: our own streams and child captures. UTF-8 is what
#: command-line tools overwhelmingly emit; the locale default is what corrupts them.
ENCODING = "utf-8"

#: Error policy for decoding a child's output. ``replace`` rather than ``strict`` because a
#: child's bytes are not ours to guarantee: one stray byte from a third-party tool must not
#: abort the caller's whole run. Our own stdout stays strict (see ``force_utf8_streams``): a
#: report that silently mangles a path is worse than one that fails loudly.
CAPTURE_ERRORS = "replace"

#: Error policy for a message a tool prints, never for its data: stderr's own, which
#: ``force_utf8_streams`` keeps, and ``printable``'s. ``backslashreplace`` spells a lone
#: surrogate as its escape (a backslash, ``u`` and four hex digits), which is visible,
#: ASCII, and names the byte the shell or filesystem could not decode.
ESCAPE_ERRORS = "backslashreplace"

#: Error policy for every forced stream but stderr, stdout above all, which carries a
#: tool's data. A report that printed a path which is not the real path would be silent
#: corruption; strict makes it a loud failure instead.
_DATA_ERRORS = "strict"


class _Writable(Protocol):
    """Where argparse writes a message: anything with a text ``write``."""

    def write(self, text: str, /) -> object:
        """Write *text*; argparse ignores the result."""


def _errors_for(stream: io.TextIOWrapper) -> str:
    """The error handler *stream* keeps once forced: stderr's escape, strict otherwise.

    Naming only an encoding in ``reconfigure`` resets the handler to ``strict``, which
    silently drops Python's own ``backslashreplace`` on stderr. So it is named here. The
    process's stderr carries diagnostics, and one quoting a mistyped argument must print
    its escape rather than raise. Everything else, stdout above all, carries data and
    stays strict. The role is read off ``sys`` at the call, which is what a tool's
    ``main`` hands over; a stream serving as both stdout and stderr is strict, because
    data is on it.
    """
    if stream is sys.stderr and stream is not sys.stdout:
        return ESCAPE_ERRORS
    return _DATA_ERRORS


def force_utf8_streams(first: IO[str], /, *more: IO[str]) -> tuple[str, ...]:
    """Reconfigure each text stream to UTF-8; return the names actually changed.

    The process's stderr keeps ``ESCAPE_ERRORS``, its own default; every other stream,
    stdout included, is strict (``_errors_for`` says why). At least one stream is
    required. Only the streams handed over are touched, so a call passing none would
    force nothing while reading, at the call site, as though it forced the process's own:
    the mistake this signature turns into a type-check error and a ``TypeError``. Streams are
    positional-only, as they were when all were variadic.

    Streams that cannot be reconfigured are skipped rather than treated as an error:
    ``io.StringIO`` (what tests substitute for stdout) has no ``reconfigure``, and a
    closed or detached stream raises ``ValueError``. The changed names are returned so a
    test can observe the effect without inspecting private state.
    """
    changed: list[str] = []
    for stream in (first, *more):
        # isinstance narrowing (rather than getattr) keeps this mypy --strict clean with
        # no Any: sys.stdout is typed TextIO, which does not declare reconfigure.
        if not isinstance(stream, io.TextIOWrapper):
            continue
        try:
            stream.reconfigure(encoding=ENCODING, errors=_errors_for(stream))
        except ValueError:
            continue
        changed.append(getattr(stream, "name", repr(stream)))
    return tuple(changed)


# fuzz-target
def printable(text: str) -> str:
    """Return *text* with each character UTF-8 cannot encode spelled as its escape.

    Only a lone surrogate qualifies, so any other text comes back unchanged, and the
    result is a fixed point: escaping it again changes nothing. The escape alone, for a
    message built here and printed elsewhere; ``print_escaped`` is the usual boundary.
    """
    return text.encode(ENCODING, ESCAPE_ERRORS).decode(ENCODING)


#: The first code point past Latin-1: below it an escape takes two hex digits.
_LATIN1_END = 0x100

#: Every control character a terminal can act on: C0 (tab and newline among them), DEL
#: and C1. Then the direction marks (the Arabic letter mark among them), embeddings,
#: overrides and isolates, and the line and paragraph separators, each of which can make
#: a printed name read as another.
_UNSAFE_CODES = (
    *range(0x00, 0x20),
    0x7F,
    *range(0x80, 0xA0),
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
    0x2028,
    0x2029,
)

#: ``str.translate`` table: each unsafe code point to its escape, lowercase hex, never
#: Python's short forms: a tab is ``\x09``, not ``\t``.
_UNSAFE_ESCAPES = {
    code: f"\\x{code:02x}" if code < _LATIN1_END else f"\\u{code:04x}"
    for code in _UNSAFE_CODES
}


# fuzz-target
def spell_unsafe(text: str) -> str:
    r"""Return *text* with each character a terminal would act on spelled as its escape.

    C0, DEL and C1 print as ``\xNN``; the direction marks, embeddings, overrides,
    isolates and the line and paragraph separators as ``\uXXXX``. Every other
    character (accents, CJK, emoji, a lone surrogate) comes back unchanged. A
    backslash is not escaped, so the result is a fixed point. Unlike ``printable``,
    which escapes only what UTF-8 cannot encode, this guards a terminal from a name
    the tool did not write; a message can need both.
    """
    return text.translate(_UNSAFE_ESCAPES)


def print_escaped(line: str, stream: IO[str]) -> None:
    """Print one line to *stream*, escaped so that a lone surrogate cannot make it raise.

    The writer for a tool's human-facing lines (a refusal, a warning, progress), which
    quote argv and file names as the tool received them. A tool's data does not
    come through here: a path in a report that is not the real path is the silent
    corruption the strict stream exists to refuse.
    """
    print(printable(line), file=stream)


class EscapingArgumentParser(argparse.ArgumentParser):
    """An ``argparse.ArgumentParser`` whose every message goes through ``printable``.

    argparse writes usage, help, errors and ``--version`` through the one method
    overridden here, so none of them raises on a strict stream. Once
    ``force_utf8_streams`` has run, errors go to a stderr that escapes by itself; help and
    ``--version`` go to stdout, which stays strict, and that is where this class earns its
    place. ``add_subparsers`` builds each sub-parser from the parent's own class, so
    sub-parsers inherit it.
    """

    def _print_message(self, message: str, file: _Writable | None = None) -> None:
        """Escape *message*, then write it exactly as argparse would have."""
        super()._print_message(printable(message), file)
