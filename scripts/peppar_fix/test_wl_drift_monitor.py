"""Unit tests for WlDriftMonitor."""

from __future__ import annotations

import unittest

from peppar_fix.wl_drift_monitor import WlDriftMonitor


class WlDriftMonitorTest(unittest.TestCase):
    """Per-SV rolling-mean drift detector — correctness + edge cases."""

    def test_no_event_when_not_fixed(self):
        """ingest() on an SV that was never note_fix'd is silent."""
        m = WlDriftMonitor(window_epochs=5, threshold_cyc=0.15,
                           min_samples=3)
        for _ in range(10):
            self.assertIsNone(m.ingest("G01", 0.30))  # big drift, ignored

    def test_no_event_below_min_samples(self):
        """Needs ≥ min_samples before any event can fire."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        for _ in range(4):
            # Persistent +0.3 cyc drift — well above threshold —
            # but fewer than 5 samples, so no event yet.
            self.assertIsNone(m.ingest("G01", 0.30))

    def test_fires_when_rolling_mean_exceeds_threshold(self):
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        ev = None
        # Five samples at 0.30 cycles → rolling mean = 0.30 > 0.10.
        for _ in range(5):
            ev = m.ingest("G01", 0.30)
        self.assertIsNotNone(ev)
        self.assertEqual(ev['sv'], "G01")
        self.assertAlmostEqual(ev['drift_cyc'], 0.30)
        self.assertEqual(ev['n_samples'], 5)

    def test_does_not_fire_when_samples_near_zero(self):
        """Correct WL integer: MW residual hovers near 0, no event."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        samples = [0.02, -0.03, 0.01, -0.02, 0.04, -0.01, 0.02]
        for s in samples:
            self.assertIsNone(m.ingest("G01", s))

    def test_transient_spike_does_not_fire(self):
        """Single large sample averaged with many near-zero samples
        stays within threshold — drift must be sustained."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        # One +1.0 spike + several zero samples: rolling mean ≈ 0.10.
        # With window 10 and five zeros + one spike, mean=1/6 ≈ 0.17.
        # The threshold is absolute, so this would fire.  Use more
        # zeros to dilute:
        for s in [0.0, 0.0, 0.0, 0.0, 0.0, 1.0, 0.0, 0.0, 0.0, 0.0]:
            ev = m.ingest("G01", s)
        # After the full window: mean = 1/10 = 0.10 (right at threshold).
        # Just below threshold is safe; just above fires.  Construct
        # a case where mean is well below:
        m2 = WlDriftMonitor(window_epochs=20, threshold_cyc=0.10,
                            min_samples=5)
        m2.note_fix("G02")
        for _ in range(19):
            ev = m2.ingest("G02", 0.0)
            self.assertIsNone(ev)
        ev = m2.ingest("G02", 1.0)  # one spike at the end, mean = 0.05
        self.assertIsNone(ev)

    def test_wrong_integer_drift_pattern(self):
        """Realistic scenario: WL is wrong by 1 cycle.  Post-fix
        residual drifts toward ±1.0 cycles as the tracker catches up
        with incoming observations.  Monitor must fire well before
        the drift saturates.
        """
        m = WlDriftMonitor(window_epochs=30, threshold_cyc=0.15,
                           min_samples=15)
        m.note_fix("E01")
        # Simulate gradual drift from 0 → 0.5 cycles over 20 epochs,
        # then hold.  Monitor should catch it partway through.
        fired_at = None
        for i in range(30):
            sample = min(0.02 * i, 0.5)
            ev = m.ingest("E01", sample)
            if ev is not None and fired_at is None:
                fired_at = i
                break
        self.assertIsNotNone(fired_at)
        self.assertLess(fired_at, 25)  # caught before saturation

    def test_note_fix_is_idempotent(self):
        """Re-notifying an already-tracked SV clears its history
        (fresh window after re-fix).  This models the case where
        MW was reset externally and re-fixed without going through
        the monitor's note_unfix path."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        for _ in range(5):
            m.ingest("G01", 0.30)
        # Now we're armed.  Re-fix clears history.
        m.note_fix("G01")
        ev = m.ingest("G01", 0.30)
        self.assertIsNone(ev)  # one sample, below min_samples

    def test_note_unfix_stops_tracking(self):
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5)
        m.note_fix("G01")
        m.ingest("G01", 0.30)
        m.note_unfix("G01")
        # After unfix, ingest returns None (not tracking).
        self.assertIsNone(m.ingest("G01", 0.30))
        self.assertEqual(m.n_tracking(), 0)

    def test_multiple_svs_independent(self):
        m = WlDriftMonitor(window_epochs=5, threshold_cyc=0.10,
                           min_samples=3)
        m.note_fix("G01")
        m.note_fix("G02")
        # G01 drifts, G02 stays clean.
        for _ in range(3):
            ev_good = m.ingest("G02", 0.0)
            ev_bad = m.ingest("G01", 0.30)
        self.assertIsNone(ev_good)
        self.assertIsNotNone(ev_bad)
        self.assertEqual(ev_bad['sv'], "G01")

    def test_summary_reports_tracked_count(self):
        m = WlDriftMonitor(window_epochs=30, threshold_cyc=0.15,
                           min_samples=15)
        self.assertIn("tracking 0", m.summary())
        m.note_fix("G01")
        m.note_fix("E05")
        self.assertIn("tracking 2", m.summary())


if __name__ == "__main__":
    unittest.main()
