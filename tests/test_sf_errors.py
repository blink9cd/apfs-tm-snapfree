"""Tests for the exit codes and the one error type every module raises."""

import unittest

import sf_errors


class ExitCodeTest(unittest.TestCase):
    def test_codes_are_the_documented_three(self) -> None:
        self.assertEqual(
            (sf_errors.EXIT_OK, sf_errors.EXIT_FAIL, sf_errors.EXIT_USAGE), (0, 1, 2)
        )


class ToolErrorTest(unittest.TestCase):
    def test_carries_its_message(self) -> None:
        error = sf_errors.ToolError("diskutil info exited 1")
        self.assertIsInstance(error, Exception)
        self.assertEqual(str(error), "diskutil info exited 1")


if __name__ == "__main__":
    unittest.main()
