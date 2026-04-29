"""Unit tests for SecondOpinionPosMonitor."""
from __future__ import annotations

import unittest

from peppar_fix.second_opinion_pos_monitor import SecondOpinionPosMonitor


class SecondOpinionPosMonitorTest(unittest.TestCase):

    def test_below_threshold_silent(self):
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        for ep in range(10):
            self.assertIsNone(m.evaluate(ep, 2.0))
        self.assertEqual(m.streak(), 0)

    def test_none_does_not_reset_streak(self):
        """A missing NAV2 opinion shouldn't break the streak — the
        engine sometimes gets sparse opinions and we don't want a
        single missing sample to abort a real divergence."""
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        m.evaluate(0, 7.0)
        m.evaluate(1, 7.0)
        m.evaluate(2, None)  # no opinion — preserve streak
        self.assertEqual(m.streak(), 2)
        ev = m.evaluate(3, 7.0)
        self.assertIsNotNone(ev)

    def test_below_threshold_clears_streak(self):
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        m.evaluate(0, 7.0)
        m.evaluate(1, 7.0)
        m.evaluate(2, 1.0)  # cleared
        self.assertEqual(m.streak(), 0)
        # And re-fires after another sustained disagreement
        for ep in range(3, 6):
            m.evaluate(ep, 7.0)
        self.assertEqual(m.streak(), 3)

    def test_fires_at_sustained(self):
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        self.assertIsNone(m.evaluate(0, 7.0))
        self.assertIsNone(m.evaluate(1, 7.0))
        ev = m.evaluate(2, 7.0)
        self.assertIsNotNone(ev)
        self.assertEqual(ev['reason'], 'second_opinion_3d')
        self.assertAlmostEqual(ev['nav2_delta_3d_m'], 7.0)
        self.assertAlmostEqual(ev['threshold_m'], 5.0)
        self.assertEqual(ev['sustained_epochs'], 3)

    def test_only_fires_once_per_period(self):
        """After firing, don't re-fire until the gate drops below
        threshold (caller acts, then nav2Δ presumably resolves)."""
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        m.evaluate(0, 7.0)
        m.evaluate(1, 7.0)
        ev = m.evaluate(2, 7.0)
        self.assertIsNotNone(ev)
        # Subsequent above-threshold samples don't re-fire
        for ep in range(3, 20):
            self.assertIsNone(m.evaluate(ep, 7.0))

    def test_recovery_re_arms(self):
        """After note_recovery and one above-threshold sample, the
        streak restarts so a future divergence can fire again."""
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        m.evaluate(0, 7.0)
        m.evaluate(1, 7.0)
        m.evaluate(2, 7.0)
        m.note_recovery()
        self.assertFalse(m.is_armed())
        # New divergence — fires again
        m.evaluate(10, 7.0)
        m.evaluate(11, 7.0)
        ev = m.evaluate(12, 7.0)
        self.assertIsNotNone(ev)

    def test_negative_displacement_treated_as_magnitude(self):
        m = SecondOpinionPosMonitor(threshold_m=5.0, sustained_epochs=3)
        m.evaluate(0, -7.0)
        m.evaluate(1, -7.0)
        ev = m.evaluate(2, -7.0)
        self.assertIsNotNone(ev)


if __name__ == "__main__":
    unittest.main()
