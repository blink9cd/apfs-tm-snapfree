"""Tests for reading and writing diskutil's size syntax."""

import unittest

import sf_size


class ParseSizeTest(unittest.TestCase):
    def test_each_suffix_is_a_power_of_1000(self) -> None:
        cases = {
            "5k": 5 * 10**3,
            "1500m": 1500 * 10**6,
            "40g": 40 * 10**9,
            "2t": 2 * 10**12,
        }
        for text, expected in cases.items():
            with self.subTest(text=text):
                self.assertEqual(sf_size.parse_size(text), expected)

    def test_suffix_case_is_ignored(self) -> None:
        self.assertEqual(sf_size.parse_size("40G"), 40 * 10**9)

    def test_malformed_sizes_are_refused(self) -> None:
        for text in (
            "",
            "40",
            "g",
            "40x",
            "4.5g",
            "-1g",
            " 40g",
            "40gb",
            "0g",
            "00g",
            "40g\n",
            "40\u212a",
        ):
            with self.subTest(text=text), self.assertRaises(ValueError):
                sf_size.parse_size(text)

    def test_non_ascii_digits_are_refused(self) -> None:
        # str.isdigit accepts these; diskutil would not.
        with self.assertRaises(ValueError):
            sf_size.parse_size("٤٠g")


class QuotaArgTest(unittest.TestCase):
    def test_largest_exact_unit_is_used(self) -> None:
        self.assertEqual(sf_size.quota_arg(40 * 10**9), "40g")
        self.assertEqual(sf_size.quota_arg(1500 * 10**6), "1500m")
        self.assertEqual(sf_size.quota_arg(3 * 10**12), "3t")
        self.assertEqual(sf_size.quota_arg(7000), "7k")

    def test_a_size_below_one_unit_is_plain_bytes(self) -> None:
        self.assertEqual(sf_size.quota_arg(1234), "1234")

    def test_round_trips_through_parse(self) -> None:
        for text in ("5k", "1500m", "40g", "2t"):
            with self.subTest(text=text):
                self.assertEqual(sf_size.quota_arg(sf_size.parse_size(text)), text)

    def test_non_positive_is_refused(self) -> None:
        for value in (0, -5):
            with self.subTest(value=value), self.assertRaises(ValueError):
                sf_size.quota_arg(value)


class FormatSizeTest(unittest.TestCase):
    def test_bytes_below_a_kilobyte_are_whole(self) -> None:
        self.assertEqual(sf_size.format_size(0), "0 B")
        self.assertEqual(sf_size.format_size(999), "999 B")

    def test_larger_sizes_take_one_decimal(self) -> None:
        self.assertEqual(sf_size.format_size(1500), "1.5 kB")
        self.assertEqual(sf_size.format_size(40 * 10**9), "40.0 GB")
        self.assertEqual(sf_size.format_size(2 * 10**12), "2.0 TB")

    def test_margin_is_one_gigabyte(self) -> None:
        self.assertEqual(sf_size.SPACE_MARGIN, 10**9)


if __name__ == "__main__":
    unittest.main()
