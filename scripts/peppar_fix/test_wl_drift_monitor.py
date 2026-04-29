"""Unit tests for WlDriftMonitor (now an integer-consistency tracker).

The MW-residual ingest path was retired 2026-04-29 (I-202241).
What remains: per-SV n_wl history + HIGH / MEDIUM / LOW / UNKNOWN
classification fed into [WL_FIX_LIFE].  Tests for the retired
ingest / rolling-mean / adaptive-threshold paths were removed
alongside the engine emits.
"""

from __future__ import annotations

import unittest

from peppar_fix.wl_drift_monitor import (
    CONS_HIGH,
    CONS_LOW,
    CONS_MEDIUM,
    CONS_UNKNOWN,
    WlDriftMonitor,
)


class WlIntConsistencyTest(unittest.TestCase):
    """Per-SV WL integer consistency classification."""

    def test_unknown_until_two_cycles(self):
        """A fresh SV with < 2 fix cycles in history is UNKNOWN."""
        m = WlDriftMonitor(k_short=4)
        self.assertEqual(m.consistency_level("G01"), CONS_UNKNOWN)
        m.note_fix("G01", n_wl=10)
        self.assertEqual(m.consistency_level("G01"), CONS_UNKNOWN)

    def test_high_when_integer_repeats(self):
        """Two cycles at the same integer → HIGH consistency."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=10)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=10)
        self.assertEqual(m.consistency_level("G01"), CONS_HIGH)

    def test_medium_when_integer_adjacent(self):
        """Two cycles at adjacent integers (range = 1) → MEDIUM."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=10)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=11)
        self.assertEqual(m.consistency_level("G01"), CONS_MEDIUM)

    def test_low_when_integer_wanders(self):
        """Cycles spanning >1 integer (range >= 2) → LOW."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=10)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=15)
        self.assertEqual(m.consistency_level("G01"), CONS_LOW)

    def test_low_with_three_wandering_integers(self):
        """G20-style: -41, -4, 18 → range 59 → LOW."""
        m = WlDriftMonitor(k_short=4)
        for n in [-41, -4, 18]:
            m.note_fix("G20", n_wl=n)
            m.note_unfix("G20")
        self.assertEqual(m.consistency_level("G20"), CONS_LOW)

    def test_history_persists_across_unfix(self):
        """Integer history is preserved across note_unfix → next
        note_fix sees the previous integer."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=10)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=10)
        self.assertEqual(m.consistency_level("G01"), CONS_HIGH)
        self.assertEqual(m.integer_history("G01"), [10, 10])

    def test_history_window_is_k_short(self):
        """Only the last K_short integers are remembered."""
        m = WlDriftMonitor(k_short=3)
        for n in [5, 5, 5, 99]:  # last 3 → [5, 5, 99] → range 94 → LOW
            m.note_fix("G01", n_wl=n)
            m.note_unfix("G01")
        self.assertEqual(m.integer_history("G01"), [5, 5, 99])
        self.assertEqual(m.consistency_level("G01"), CONS_LOW)

    def test_forget_history_clears(self):
        """forget_history wipes integer memory (use after real slip)."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=10)
        m.note_fix("G01", n_wl=10)
        self.assertEqual(m.consistency_level("G01"), CONS_HIGH)
        m.forget_history("G01")
        self.assertEqual(m.consistency_level("G01"), CONS_UNKNOWN)

    def test_note_fix_without_n_wl_is_noop(self):
        """note_fix(sv) without an integer is silent — no history change."""
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01")
        self.assertEqual(m.integer_history("G01"), [])
        self.assertEqual(m.consistency_level("G01"), CONS_UNKNOWN)

    def test_summary_reports_tracked_count(self):
        m = WlDriftMonitor(k_short=4)
        m.note_fix("G01", n_wl=5)
        m.note_fix("E11", n_wl=10)
        s = m.summary()
        self.assertIn("tracking 2 SVs", s)
        self.assertIn("k_short=4", s)


if __name__ == "__main__":
    unittest.main()
