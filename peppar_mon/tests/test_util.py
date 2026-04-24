"""Unit tests for peppar_mon._util."""

from __future__ import annotations

import unittest
from datetime import datetime

from peppar_mon._util import (
    format_elapsed_short, format_uncertainty, format_uptime,
    parse_log_timestamp, uncertain_decimals_deg, uncertain_decimals_m,
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


class FormatUncertaintyTest(unittest.TestCase):
    """Adaptive ± display: cm at sub-meter, m beyond."""

    def test_sub_10cm_one_decimal_cm(self):
        self.assertEqual(format_uncertainty(0.023), "± 2.3 cm")
        self.assertEqual(format_uncertainty(0.099), "± 9.9 cm")

    def test_sub_meter_integer_cm(self):
        self.assertEqual(format_uncertainty(0.23), "± 23 cm")
        self.assertEqual(format_uncertainty(0.99), "± 99 cm")

    def test_sub_10m_one_decimal_m(self):
        self.assertEqual(format_uncertainty(1.0), "± 1.0 m")
        self.assertEqual(format_uncertainty(9.9), "± 9.9 m")

    def test_beyond_10m_integer_m(self):
        self.assertEqual(format_uncertainty(12.0), "± 12 m")
        self.assertEqual(format_uncertainty(99.5), "± 100 m")

    def test_none_renders_question(self):
        self.assertEqual(format_uncertainty(None), "± ?")

    def test_negative_renders_question(self):
        """Guard against arithmetic that could produce a negative."""
        self.assertEqual(format_uncertainty(-1.0), "± ?")


class UncertainDecimalsDegTest(unittest.TestCase):
    """How many trailing decimals of a degree are below the σ
    quantum (latitude — 1° ≈ 111 km)."""

    def test_three_cm_sigma_yields_seven_confident_decimals(self):
        # σ = 0.03 m → σ_deg = 2.7e-7 → floor(-log10(2.7e-7)) = 6.
        # So 6 confident decimals; the 7th and beyond are dim.
        self.assertEqual(uncertain_decimals_deg(0.03), 6)

    def test_meter_sigma_reduces_confident_decimals(self):
        # σ = 1 m → σ_deg ≈ 9e-6 → ~5 confident decimals.
        self.assertEqual(uncertain_decimals_deg(1.0), 5)

    def test_tiny_sigma_yields_many_decimals(self):
        # σ = 1 mm → σ_deg = 9e-9 → 8 confident decimals.
        self.assertEqual(uncertain_decimals_deg(0.001), 8)

    def test_none_returns_zero(self):
        self.assertEqual(uncertain_decimals_deg(None), 0)

    def test_zero_sigma_returns_zero(self):
        self.assertEqual(uncertain_decimals_deg(0.0), 0)


class UncertainDecimalsMTest(unittest.TestCase):
    """Same idea for altitude (metres directly)."""

    def test_three_cm_sigma_one_confident_decimal(self):
        # σ = 0.023 → floor(-log10(0.023)) = floor(1.64) = 1.
        self.assertEqual(uncertain_decimals_m(0.023), 1)

    def test_one_meter_sigma_zero_decimals(self):
        self.assertEqual(uncertain_decimals_m(1.0), 0)

    def test_one_mm_sigma_three_decimals(self):
        self.assertEqual(uncertain_decimals_m(0.001), 3)

    def test_none_returns_zero(self):
        self.assertEqual(uncertain_decimals_m(None), 0)


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
