"""Unit tests for peppar_mon._util."""

from __future__ import annotations

import unittest
from datetime import datetime

from peppar_mon._util import (
    format_elapsed_short, format_uptime, parse_log_timestamp,
)


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


class ParseLogTimestampTest(unittest.TestCase):
    def test_typical_engine_line(self):
        line = "2026-04-19 21:09:12,007 INFO Host config: /home/bob/..."
        ts = parse_log_timestamp(line)
        self.assertIsNotNone(ts)
        self.assertEqual(ts, datetime(2026, 4, 19, 21, 9, 12, 7_000))

    def test_with_level_warning(self):
        line = "2026-04-19 23:14:05,123 WARNING Job A: G17 |PR|=3.2m > 2.0m"
        ts = parse_log_timestamp(line)
        self.assertEqual(ts.second, 5)
        self.assertEqual(ts.microsecond, 123_000)

    def test_rejects_blank(self):
        self.assertIsNone(parse_log_timestamp(""))
        self.assertIsNone(parse_log_timestamp("\n"))

    def test_rejects_traceback_continuation(self):
        # Middle lines of a traceback have no timestamp.
        self.assertIsNone(parse_log_timestamp("  File '/home/bob/.py', ..."))
        self.assertIsNone(parse_log_timestamp("Traceback (most recent ..."))

    def test_rejects_non_timestamp_prefix(self):
        # Rejects lines that start with numbers but aren't log timestamps.
        self.assertIsNone(parse_log_timestamp("123456 something"))

    def test_ignores_trailing_garbage(self):
        # Even if the rest of the line is malformed, the timestamp at
        # the start should still parse.
        line = "2026-04-19 21:09:12,007 \x00\x01 corrupted line"
        ts = parse_log_timestamp(line)
        self.assertEqual(ts.hour, 21)


class FormatElapsedShortTest(unittest.TestCase):
    """Compact Xh Ym Zs / Xm Ys / Xs format for the DOWN indicator."""

    def test_sub_minute(self):
        self.assertEqual(format_elapsed_short(0), "0s")
        self.assertEqual(format_elapsed_short(5), "5s")
        self.assertEqual(format_elapsed_short(59), "59s")

    def test_minute_scale_keeps_seconds(self):
        """Under 1 h, seconds matter — a minute-scale stall is
        still in the "engine briefly hung" regime.  35 s and 45 s
        are operationally different."""
        self.assertEqual(format_elapsed_short(60), "1m 0s")
        self.assertEqual(format_elapsed_short(75), "1m 15s")
        self.assertEqual(format_elapsed_short(599), "9m 59s")

    def test_hour_scale_drops_seconds(self):
        """Past 1 h we're well into "engine died hours ago"
        territory where second precision is noise — keep it
        coarse for readability."""
        self.assertEqual(format_elapsed_short(3600), "1h 0m")
        self.assertEqual(format_elapsed_short(3600 + 125), "1h 2m")
        self.assertEqual(format_elapsed_short(25 * 3600), "25h 0m")

    def test_accepts_float(self):
        self.assertEqual(format_elapsed_short(12.7), "12s")

    def test_negative_clamps_to_zero(self):
        """Tiny clock skew between host and log could produce a
        negative delta; don't render ``-5s``."""
        self.assertEqual(format_elapsed_short(-5), "0s")


if __name__ == "__main__":
    unittest.main()
