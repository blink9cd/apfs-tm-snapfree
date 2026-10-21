"""Grouped suite: the escaping boundary -- a message holding a lone surrogate, printed.

Python decodes argv and file names with ``surrogateescape``, so a byte that is not UTF-8
arrives as a lone surrogate, and a strict stream refuses it -- stdout always, once
``force_utf8_streams`` has run; stderr escapes it by itself. Every stream here is a real
``io.TextIOWrapper`` over bytes. Never ``io.StringIO``: it stores text without encoding
it, so it accepts what a real stream refuses, and a test written over one passes with
the defect present.
"""

from __future__ import annotations

import argparse
import contextlib
import io
import subprocess
import sys
import tempfile
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stdio_utf8  # noqa: E402 - import follows the sys.path bootstrap above

#: A generous ceiling for the black-box child: a hung CLI must fail this suite
#: rather than hang it.
_SUBPROCESS_TIMEOUT = 120

#: What ``surrogateescape`` makes of the byte 0xFF in argv or a file name.
_LONE = "\udcff"

#: The same character as ``printable`` spells it: a backslash, then ``udcff``.
_ESCAPED = "\\udcff"

#: Text holding at least one lone surrogate: alone, at either end, inside a word, beside a
#: second one, and among characters outside the BMP.
_TEXTS_WITH_A_SURROGATE = (
    _LONE,
    f"{_LONE}x",
    f"x{_LONE}",
    f"no-{_LONE}-dir",
    f"{_LONE}\udc80 caf\u00e9 \U0001f600",
)
#: Text holding no lone surrogate, ASCII and beyond, which ``printable`` must keep as is.
_ENCODABLE_TEXTS = (
    "",
    "plain",
    "caf\u00e9",
    "\u00e9\u20ac\u2192",
    "\U0001f600 emoji",
    "tab\tand\nnewline",
)


def _strict_stream() -> tuple[io.BytesIO, io.TextIOWrapper]:
    """A strict UTF-8 text stream over a byte buffer: what ``force_utf8_streams`` leaves."""
    buffer = io.BytesIO()
    return buffer, io.TextIOWrapper(buffer, encoding="utf-8", newline="")


def _written(buffer: io.BytesIO, stream: io.TextIOWrapper) -> str:
    """Everything *stream* wrote to *buffer*, flushed and decoded strictly."""
    stream.flush()
    return buffer.getvalue().decode("utf-8")


class TestStrictStreamHarness(unittest.TestCase):
    """Guards: the strict stream really reproduces the crash the escaping exists for."""

    def test_a_strict_stream_refuses_a_lone_surrogate(self) -> None:
        """The raw write raises, so a test passing over this stream proves the escape."""
        _, stream = _strict_stream()
        with self.assertRaises(UnicodeEncodeError):
            print(f"x{_LONE}", file=stream)
            stream.flush()

    def test_forcing_leaves_even_a_lenient_stream_strict(self) -> None:
        """Every stream but the process's stderr is forced strict, whatever it was.

        This one opened with ``backslashreplace``, as stderr does. Handed over as any
        other stream -- stdout, say -- it refuses a lone surrogate once forced, which is
        why a stdout message must still pass through ``printable``.
        """
        stream = io.TextIOWrapper(io.BytesIO(), "cp1252", errors="backslashreplace")
        stdio_utf8.force_utf8_streams(stream)
        self.assertEqual(stream.errors, "strict")


class TestPrintable(unittest.TestCase):
    """``printable`` spells each character UTF-8 cannot encode as a backslash escape."""

    def test_a_lone_surrogate_becomes_its_escape_in_place(self) -> None:
        """Only the surrogate changes; the text either side of it is kept exactly."""
        self.assertEqual(stdio_utf8.printable(f"no-{_LONE}-dir"), f"no-{_ESCAPED}-dir")

    def test_encodable_text_passes_through_unchanged(self) -> None:
        """Everything but a lone surrogate encodes, so nothing else is ever altered."""
        for text in _ENCODABLE_TEXTS:
            with self.subTest(text=ascii(text)):
                self.assertEqual(stdio_utf8.printable(text), text)

    def test_text_with_a_surrogate_escapes_to_a_fixed_point(self) -> None:
        """Text holding a surrogate: the result encodes strictly, and is a fixed point."""
        for text in _TEXTS_WITH_A_SURROGATE:
            with self.subTest(text=ascii(text)):
                result = stdio_utf8.printable(text)
                result.encode("utf-8")
                self.assertNotEqual(result, text)
                self.assertEqual(stdio_utf8.printable(result), result)


class TestPrintEscaped(unittest.TestCase):
    """``print_escaped`` prints one line that a strict stream always accepts."""

    def test_a_lone_surrogate_is_printed_escaped_rather_than_raised(self) -> None:
        """The report of a mistyped path survives, naming the byte it could not print."""
        buffer, stream = _strict_stream()
        stdio_utf8.print_escaped(f"tool: no-{_LONE}-dir is not a folder", stream)
        self.assertEqual(
            _written(buffer, stream), f"tool: no-{_ESCAPED}-dir is not a folder\n"
        )

    def test_encodable_text_is_printed_exactly(self) -> None:
        """Non-ASCII that UTF-8 can encode is printed as itself, not escaped."""
        buffer, stream = _strict_stream()
        stdio_utf8.print_escaped("arrow → name", stream)
        self.assertEqual(_written(buffer, stream), "arrow → name\n")


class TestEscapingArgumentParser(unittest.TestCase):
    """Every message argparse prints is escaped, whichever stream it goes to."""

    def _parse(
        self, parser: argparse.ArgumentParser, argv: list[str]
    ) -> tuple[object, str, str]:
        """Parse *argv* with both streams strict; return the exit status, stdout, stderr."""
        out_buffer, out = _strict_stream()
        err_buffer, err = _strict_stream()
        with (
            contextlib.redirect_stdout(out),
            contextlib.redirect_stderr(err),
            self.assertRaises(SystemExit) as caught,
        ):
            parser.parse_args(argv)
        return (
            caught.exception.code,
            _written(out_buffer, out),
            _written(err_buffer, err),
        )

    def test_a_plain_parser_crashes_on_a_mistyped_argument(self) -> None:
        """Guard: the harness reproduces the defect, a traceback in place of exit 2."""
        _, err = _strict_stream()
        with contextlib.redirect_stderr(err), self.assertRaises(UnicodeEncodeError):
            argparse.ArgumentParser(prog="probe").parse_args(["--bogus", f"x{_LONE}"])

    def test_a_mistyped_argument_is_refused_escaped(self) -> None:
        """Usage and the error go to stderr escaped, and the exit status stays 2."""
        parser = stdio_utf8.EscapingArgumentParser(prog="probe")
        code, out, err = self._parse(parser, ["--bogus", f"x{_LONE}"])
        self.assertEqual((code, out), (2, ""))
        self.assertIn(f"probe: error: unrecognized arguments: --bogus x{_ESCAPED}", err)

    def test_help_under_an_undecodable_prog_is_escaped(self) -> None:
        """A program name taken from argv can hold a surrogate too; help goes to stdout."""
        parser = stdio_utf8.EscapingArgumentParser(prog=f"tool{_LONE}")
        code, out, err = self._parse(parser, ["--help"])
        self.assertEqual((code, err), (0, ""))
        self.assertTrue(out.startswith(f"usage: tool{_ESCAPED} "), out)

    def test_version_is_escaped(self) -> None:
        """The version action prints through the same method, to stdout."""
        parser = stdio_utf8.EscapingArgumentParser(prog="probe")
        parser.add_argument("--version", action="version", version=f"v{_LONE}")
        self.assertEqual(self._parse(parser, ["--version"]), (0, f"v{_ESCAPED}\n", ""))

    def test_a_subparser_inherits_the_escaping(self) -> None:
        """``add_subparsers`` builds each sub-parser from the parent's own class.

        Asked for its OWN help, so the sub-parser's method does the printing. An
        unrecognized argument would not prove it: argparse hands one back up to the
        parent, which reports it.
        """
        parser = stdio_utf8.EscapingArgumentParser(prog="probe")
        commands = parser.add_subparsers(dest="command")
        sub = commands.add_parser("run", description=f"d{_LONE}")
        self.assertIsInstance(sub, stdio_utf8.EscapingArgumentParser)
        code, out, _ = self._parse(parser, ["run", "--help"])
        self.assertEqual(code, 0)
        self.assertIn(f"d{_ESCAPED}", out)

    def test_encodable_help_is_what_argparse_itself_prints(self) -> None:
        """Adopting the class changes nothing a strict stream could already carry."""
        outputs = []
        for parser_class in (
            argparse.ArgumentParser,
            stdio_utf8.EscapingArgumentParser,
        ):
            parser = parser_class(prog="probe", description="arrow → name")
            parser.add_argument("--path", help="a folder, e.g. café")
            outputs.append(self._parse(parser, ["--help"]))
        self.assertEqual(outputs[0], outputs[1])


class TestEscapingEndToEnd(unittest.TestCase):
    """The real route, in a real child: argv bytes the interpreter decodes, forced streams.

    The in-process tests hand argparse a surrogate directly. Only a child proves the
    interpreter itself turns a non-UTF-8 argv byte into one, and what a real
    ``force_utf8_streams`` then leaves: a stderr that escapes it and a stdout that
    refuses it.
    """

    #: A tool as small as the defect allows. Argument 1 is this library's folder,
    #: argument 2 names the parser, argument 3 is its ``prog``, and every later one is
    #: the tool's own argv. The conditional reaches only the class it names, so each
    #: plain run is a true guard.
    PROGRAM = (
        "import argparse, sys\n"
        "sys.path.insert(0, sys.argv.pop(1))\n"
        "import stdio_utf8\n"
        "stdio_utf8.force_utf8_streams(sys.stdout, sys.stderr)\n"
        "plain = sys.argv.pop(1) == 'plain'\n"
        "kind = argparse.ArgumentParser if plain else stdio_utf8.EscapingArgumentParser\n"
        "kind(prog=sys.argv.pop(1)).parse_args()\n"
    )

    def _run(
        self, kind: str, prog: str, *argv: str
    ) -> subprocess.CompletedProcess[bytes]:
        """Run the tool as *prog*, with *argv*; a surrogate in either is the byte 0xFF."""
        folder = str(Path(stdio_utf8.__file__).resolve().parent)
        command = [sys.executable, "-c", self.PROGRAM, folder, kind, prog, *argv]
        with tempfile.TemporaryFile() as out, tempfile.TemporaryFile() as err:
            # Files, not pipes: nothing for a grandchild to hold open, so the timeout
            # binds.
            done = subprocess.run(  # reliability: ignore[RL005] file sinks, no pipe
                command,
                stdin=subprocess.DEVNULL,
                stdout=out,
                stderr=err,
                timeout=_SUBPROCESS_TIMEOUT,
                check=False,
            )
            out.seek(0)
            err.seek(0)
            return subprocess.CompletedProcess(
                command, done.returncode, out.read(), err.read()
            )

    def test_a_plain_parser_refuses_a_mistyped_argument_escaped(self) -> None:
        """argparse's error goes to stderr, which escapes it: exit 2, not a traceback."""
        done = self._run("plain", "probe", "--bogus", f"x{_LONE}")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b"unrecognized arguments: --bogus x\\udcff", done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)

    def test_the_escaping_parser_refuses_and_exits_two(self) -> None:
        """The tool's own refusal survives, with the byte named rather than raised."""
        done = self._run("escaping", "probe", "--bogus", f"x{_LONE}")
        self.assertEqual(done.returncode, 2, done.stderr)
        self.assertIn(b"unrecognized arguments: --bogus x\\udcff", done.stderr)
        self.assertNotIn(b"Traceback", done.stderr)

    def test_a_plain_parser_help_under_an_undecodable_prog_tracebacks(self) -> None:
        """Guard: help goes to stdout, which stays strict -- a traceback and exit 1."""
        done = self._run("plain", f"tool{_LONE}", "--help")
        self.assertEqual(done.returncode, 1, done.stderr)
        self.assertIn(b"UnicodeEncodeError", done.stderr)

    def test_the_escaping_parser_help_under_an_undecodable_prog_is_escaped(
        self,
    ) -> None:
        """What the escaping parser is still for once stderr escapes: stdout's messages."""
        done = self._run("escaping", f"tool{_LONE}", "--help")
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertTrue(done.stdout.startswith(b"usage: tool\\udcff "), done.stdout)
