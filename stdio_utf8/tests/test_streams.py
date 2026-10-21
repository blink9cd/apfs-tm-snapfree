"""Grouped suite: the stream policy itself -- forcing a tool's own streams to UTF-8, and
the constants that decode a child's capture.
"""

from __future__ import annotations

import contextlib
import io
import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stdio_utf8  # noqa: E402 - import follows the sys.path bootstrap above

#: What ``surrogateescape`` makes of the byte 0xFF in argv or a file name.
_LONE = "\udcff"


def _strict_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A strict UTF-8 text stream over a byte buffer.

    Never ``io.StringIO``: it stores text without encoding it, so it accepts the lone
    surrogate a real stream refuses, and a test over it passes whatever the handler is.
    """
    buffer = io.BytesIO()
    return buffer, io.TextIOWrapper(buffer, encoding="utf-8", newline="")


def _cp1252_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A real locale-style text stream: cp1252 over a byte buffer.

    The encoding is pinned rather than inherited so these tests prove the behavior on
    any host locale, including a UTF-8 one where the defect does not reproduce at all.
    """
    buffer = io.BytesIO()
    return buffer, io.TextIOWrapper(buffer, encoding="cp1252", newline="")


class TestForceUtf8Streams(unittest.TestCase):
    """force_utf8_streams must convert real text streams and skip the rest."""

    def test_cp1252_stream_cannot_encode_non_ascii_before(self) -> None:
        """Guard: the harness really does reproduce the defect it claims to."""
        _, stream = _cp1252_stream()
        with self.assertRaises(UnicodeEncodeError):
            stream.write("arrow → name")
            stream.flush()

    def test_reconfigures_text_stream_to_utf8(self) -> None:
        """A TextIOWrapper is switched to UTF-8 and then encodes non-ASCII correctly."""
        buffer, stream = _cp1252_stream()
        changed = stdio_utf8.force_utf8_streams(stream)
        self.assertEqual(len(changed), 1)
        self.assertEqual(stream.encoding, "utf-8")
        stream.write("arrow → name")
        stream.flush()
        self.assertEqual(buffer.getvalue().decode("utf-8"), "arrow → name")

    def test_skips_stream_without_reconfigure(self) -> None:
        """io.StringIO -- what tests substitute for stdout -- is skipped, not an error."""
        self.assertEqual(stdio_utf8.force_utf8_streams(io.StringIO()), ())

    def test_skips_closed_stream(self) -> None:
        """A closed/detached stream raises ValueError on reconfigure; that is skipped."""
        _, stream = _cp1252_stream()
        stream.close()
        self.assertEqual(stdio_utf8.force_utf8_streams(stream), ())

    def test_refuses_a_call_passing_no_stream(self) -> None:
        """The empty call would force nothing, so it is an error rather than a no-op.

        Five CLIs once called it that way, read as compliant, and kept the code page on a
        piped stdout. Requiring a first stream makes the same mistake a type-check error
        at write time and a TypeError before anything prints.
        """
        with self.assertRaises(TypeError):
            # The call the checker rejects is exactly the one under test.
            stdio_utf8.force_utf8_streams()  # type: ignore[call-arg]

    def test_takes_its_streams_positionally_only(self) -> None:
        """A keyword stream is refused, as it always was when every stream was variadic.

        One calling convention keeps the stdio_policy gate honest: it counts a call's
        positional arguments, so a keyword spelling it cannot see must not work either.
        """
        _, stream = _cp1252_stream()
        with self.assertRaises(TypeError):
            # A positional-only parameter passed by keyword; the checker rejects it too.
            stdio_utf8.force_utf8_streams(stream=stream)  # type: ignore[call-arg]
        self.assertEqual(stream.encoding, "cp1252")

    def test_handles_mixed_streams_in_one_call(self) -> None:
        """The real call site passes stdout and stderr together; one bad one is skipped."""
        _, good = _cp1252_stream()
        changed = stdio_utf8.force_utf8_streams(good, io.StringIO())
        self.assertEqual(len(changed), 1)
        self.assertEqual(good.encoding, "utf-8")

    def test_reports_the_stream_name_when_there_is_one(self) -> None:
        """A named stream reports its name; the repr fallback covers the unnamed case."""
        # Any named file will do. The subject's own survives a move of this test file.
        with open(stdio_utf8.__file__, encoding="cp1252") as named:
            changed = stdio_utf8.force_utf8_streams(named)
        self.assertEqual(changed, (stdio_utf8.__file__,))

    def test_falls_back_to_repr_for_an_unnamed_stream(self) -> None:
        """A wrapper over BytesIO has no .name, so the repr fallback is used."""
        _, stream = _cp1252_stream()
        changed = stdio_utf8.force_utf8_streams(stream)
        self.assertIn("TextIOWrapper", changed[0])


class TestForcedErrorHandlers(unittest.TestCase):
    """stderr keeps Python's own escape; stdout, which carries data, stays strict.

    The role comes from the process: each stream is redirected into ``sys.stdout`` or
    ``sys.stderr`` and handed over exactly as a tool's ``main`` hands its own.
    """

    def test_the_process_stderr_escapes_a_lone_surrogate(self) -> None:
        """A diagnostic quoting a mistyped argument prints its escape, not a traceback."""
        _, out = _strict_stream()
        buffer, err = _strict_stream()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
        print(f"tool: no-{_LONE}-dir → missing", file=err)
        err.flush()
        self.assertEqual(
            buffer.getvalue().decode("utf-8"), "tool: no-\\udcff-dir → missing\n"
        )

    def test_the_process_stdout_still_refuses_a_lone_surrogate(self) -> None:
        """Forced strict even from a lenient start: a report never prints a false path.

        It starts as ``surrogateescape``, which is Python's own stdout default under the
        C locale a scheduled job gets, so keeping each stream's handler fails here too.
        """
        buffer = io.BytesIO()
        out = io.TextIOWrapper(buffer, "utf-8", errors="surrogateescape", newline="")
        _, err = _strict_stream()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
        with self.assertRaises(UnicodeEncodeError):
            print(f"no-{_LONE}-dir", file=out)
            out.flush()

    def test_a_stderr_that_is_also_stdout_stays_strict(self) -> None:
        """One stream in both roles carries data, so strict wins over the escape."""
        _, shared = _strict_stream()
        with contextlib.redirect_stdout(shared), contextlib.redirect_stderr(shared):
            stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)
        with self.assertRaises(UnicodeEncodeError):
            print(f"no-{_LONE}-dir", file=shared)
            shared.flush()


class TestCapturePolicy(unittest.TestCase):
    """The capture constants are the single source of truth for the read side."""

    def test_encoding_is_utf8(self) -> None:
        """Both directions use UTF-8; the locale default is what corrupts output."""
        self.assertEqual(stdio_utf8.ENCODING, "utf-8")

    def test_capture_errors_is_replace(self) -> None:
        """Child bytes are not ours to guarantee, so a capture must not raise."""
        self.assertEqual(stdio_utf8.CAPTURE_ERRORS, "replace")

    def test_capture_policy_replaces_undecodable_bytes(self) -> None:
        """A byte undefined in cp1252 must decode to a replacement, not raise."""
        raw = b"path \x81 name"
        decoded = raw.decode(stdio_utf8.ENCODING, errors=stdio_utf8.CAPTURE_ERRORS)
        self.assertIn("�", decoded)
