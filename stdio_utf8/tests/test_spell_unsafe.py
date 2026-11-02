"""Grouped suite: ``spell_unsafe``, a name a terminal would act on, made visible.

A name or message the tool did not write can hold an escape sequence (a clipboard write,
a hyperlink, a cursor move) that drives the terminal it is printed to, or a direction
override that makes it read as another name. ``spell_unsafe`` prints each such character
as a visible escape and leaves every other character as it is. Unlike ``printable``, it
is about what a terminal *does* with a character, not whether UTF-8 can encode it.
"""

from __future__ import annotations

import sys
import unittest
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import stdio_utf8  # noqa: E402 - import follows the sys.path bootstrap above

#: Every code point the function must escape, written out here rather than imported,
#: so the test is a second statement of the set and not the subject read back.
_CONTROLS = (*range(0x00, 0x20), 0x7F, *range(0x80, 0xA0))
_LAYOUT = (
    0x061C,
    0x200E,
    0x200F,
    *range(0x202A, 0x202F),
    *range(0x2066, 0x206A),
    0x2028,
    0x2029,
)
_UNSAFE = frozenset(map(chr, (*_CONTROLS, *_LAYOUT)))

#: A clipboard-writing OSC 52 sequence, then a newline that would forge a second line.
_HOSTILE = "01 \x1b]52;c;aGk=\x07\nBell.mp3"
_HOSTILE_SHOWN = "01 \\x1b]52;c;aGk=\\x07\\x0aBell.mp3"

#: Text either side of an unsafe character: none, ASCII, and characters beyond it.
_SURROUNDINGS = (("", ""), ("01 ", ".mp3"), ("caf\u00e9 ", " \U0001f600"))

#: Text with no unsafe character, ASCII and beyond, which must come back as it went in.
_SAFE_TEXTS = (
    "",
    "plain",
    "caf\u00e9 \u00a0 \u706f\u53f0",
    "\U0001f600 a backslash \\x1b stays",
    "x\udcff",
)


class TestSpellUnsafe(unittest.TestCase):
    """Each control and layout character prints as an escape; nothing else changes."""

    def test_each_control_character_prints_as_two_hex_digits(self) -> None:
        """C0 (tab and newline among them), DEL and C1: ``\\xNN``, never ``\\t``."""
        cases = {
            "\x00": "\\x00",
            "\t": "\\x09",
            "\n": "\\x0a",
            "\r": "\\x0d",
            "\x1b": "\\x1b",
            "\x1f": "\\x1f",
            "\x7f": "\\x7f",
            "\x80": "\\x80",
            "\x85": "\\x85",
            "\x9b": "\\x9b",
            "\x9f": "\\x9f",
        }
        for raw, shown in cases.items():
            with self.subTest(code=ord(raw)):
                self.assertEqual(stdio_utf8.spell_unsafe(f"a{raw}b"), f"a{shown}b")

    def test_each_direction_or_separator_mark_prints_as_four_hex_digits(self) -> None:
        """Four lowercase hex digits, never an uppercase or a short form."""
        for code in _LAYOUT:
            with self.subTest(code=hex(code)):
                shown = stdio_utf8.spell_unsafe(f"a{chr(code)}b")
                self.assertEqual(shown, f"a\\u{code:04x}b")

    def test_the_whole_set_and_nothing_beside_it(self) -> None:
        """Every code point up to U+206F is escaped exactly when it is in the set."""
        for code in range(0x2070):
            char = chr(code)
            with self.subTest(code=hex(code)):
                changed = stdio_utf8.spell_unsafe(char) != char
                self.assertEqual(changed, char in _UNSAFE)

    def test_a_hostile_name_prints_escaped(self) -> None:
        """A clipboard write and a forged line break, both made visible."""
        self.assertEqual(stdio_utf8.spell_unsafe(_HOSTILE), _HOSTILE_SHOWN)

    def test_a_right_to_left_override_prints_escaped(self) -> None:
        """``01 <U+202E>gpj.mp3`` displays as an image's name; the escape shows it."""
        self.assertEqual(stdio_utf8.spell_unsafe("01 ‮gpj.mp3"), "01 \\u202egpj.mp3")

    def test_every_other_character_prints_as_it_is(self) -> None:
        """Accents, CJK, emoji, a no-break space and a backslash are left alone."""
        text = " ~\xa0é灯台🌊 café \\x1b"
        self.assertEqual(stdio_utf8.spell_unsafe(text), text)

    def test_a_lone_surrogate_is_not_this_function_s_concern(self) -> None:
        """That is ``printable``'s job; the two compose rather than overlap."""
        self.assertEqual(stdio_utf8.spell_unsafe("x\udcff"), "x\udcff")

    def test_escaping_twice_changes_nothing_more(self) -> None:
        """A backslash is not escaped, so the result is a fixed point."""
        once = stdio_utf8.spell_unsafe(_HOSTILE)
        self.assertEqual(stdio_utf8.spell_unsafe(once), once)

    def test_safe_text_passes_through_unchanged(self) -> None:
        """Text with no character in the set comes back exactly as it went in."""
        for text in _SAFE_TEXTS:
            with self.subTest(text=ascii(text)):
                self.assertEqual(stdio_utf8.spell_unsafe(text), text)

    def test_text_with_an_unsafe_character_escapes_to_a_fixed_point(self) -> None:
        """Each one, between other text: none survives, and the result is a fixed point."""
        for char in sorted(_UNSAFE):
            for before, after in _SURROUNDINGS:
                text = f"{before}{char}{after}"
                with self.subTest(text=ascii(text)):
                    result = stdio_utf8.spell_unsafe(text)
                    self.assertNotEqual(result, text)
                    self.assertFalse(_UNSAFE.intersection(result))
                    self.assertEqual(stdio_utf8.spell_unsafe(result), result)


if __name__ == "__main__":
    unittest.main()
