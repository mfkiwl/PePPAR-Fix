"""Unit tests for peppar_mon._util."""

from __future__ import annotations

import unittest

from peppar_mon._util import format_uptime


class FormatUptimeTest(unittest.TestCase):
    def test_zero(self):
        self.assertEqual(format_uptime(0), "0d 0h 0m")

    def test_sub_minute_rounds_down(self):
        # Seconds intentionally dropped — 59 s still reads "0m".
        self.assertEqual(format_uptime(59), "0d 0h 0m")

    def test_one_minute(self):
        self.assertEqual(format_uptime(60), "0d 0h 1m")

    def test_one_hour(self):
        self.assertEqual(format_uptime(3600), "0d 1h 0m")

    def test_compound(self):
        # 1d 1h 2m
        self.assertEqual(format_uptime(86400 + 3600 + 120), "1d 1h 2m")

    def test_long_uptime(self):
        # 99d 23h 59m — approaching but not overflowing the "Dd Hh Mm" frame
        seconds = 99 * 86400 + 23 * 3600 + 59 * 60
        self.assertEqual(format_uptime(seconds), "99d 23h 59m")

    def test_accepts_float(self):
        # Fractional seconds truncate, don't round up.
        self.assertEqual(format_uptime(59.9), "0d 0h 0m")
        self.assertEqual(format_uptime(60.5), "0d 0h 1m")


if __name__ == "__main__":
    unittest.main()
