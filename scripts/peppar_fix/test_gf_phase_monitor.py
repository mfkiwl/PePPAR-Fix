"""Unit tests for GfPhaseRollingMeanMonitor."""

from __future__ import annotations

import unittest

from peppar_fix.gf_phase_monitor import (
    GfPhaseRollingMeanMonitor,
    gf_phase_m,
)


class GfPhaseRollingMeanMonitorTest(unittest.TestCase):
    """Per-SV rolling-mean GF residual detector."""

    def test_no_event_when_not_fixed(self):
        """ingest() on an SV that was never note_fix'd is silent."""
        m = GfPhaseRollingMeanMonitor(window_epochs=5, threshold_m=0.05,
                                      min_samples=3, warmup_epochs=0)
        for _ in range(10):
            self.assertIsNone(m.ingest("E07", 1000.30))

    def test_no_event_below_min_samples(self):
        """Needs ≥ min_samples before any event can fire."""
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=5, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        for _ in range(4):
            # Persistent +30 cm residual — well above threshold —
            # but fewer than 5 samples, so no event yet.
            self.assertIsNone(m.ingest("E07", 1000.30))

    def test_fires_when_rolling_mean_exceeds_threshold(self):
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=5, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        ev = None
        for _ in range(5):
            ev = m.ingest("E07", 1000.20)  # +20 cm residual
        self.assertIsNotNone(ev)
        self.assertEqual(ev['sv'], "E07")
        self.assertAlmostEqual(ev['drift_m'], 0.20, places=3)
        self.assertEqual(ev['n_samples'], 5)
        self.assertEqual(ev['gf_ref_m'], 1000.0)

    def test_does_not_fire_at_zero_residual(self):
        """Correct integer + zero iono drift: GF stays at gf_ref."""
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=5, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        for _ in range(20):
            self.assertIsNone(m.ingest("E07", 1000.0))

    def test_does_not_fire_under_slow_iono_drift(self):
        """Slow iono drift (1 mm/epoch over 30 epochs = 3 cm) stays
        below 5 cm threshold — exactly the use case the rolling-mean
        approach handles via time-scale separation."""
        m = GfPhaseRollingMeanMonitor(window_epochs=30, threshold_m=0.05,
                                      min_samples=15, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        events = []
        for i in range(30):
            ev = m.ingest("E07", 1000.0 + i * 0.001)  # +1 mm/epoch ramp
            if ev is not None:
                events.append(ev)
        # Final residual ≈ 0.029 m; rolling mean ≈ 0.0145 m; below 0.05.
        self.assertEqual(events, [])

    def test_fires_on_sustained_step(self):
        """A wrong-L1-integer commit produces a +λ_L1 ≈ 19 cm step in GF.
        A sustained step of that magnitude trips the rolling-mean
        threshold once enough samples accumulate.  We use 25 cm here
        to ensure the rolling mean exceeds threshold even with a
        partially-filled window from earlier zero-residual samples."""
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=5, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        events = []
        # 5 zero-residual samples first.
        for _ in range(5):
            ev = m.ingest("E07", 1000.0)
            if ev:
                events.append(ev)
        # Then a step jump of +25 cm (wrong-L5-integer signature).
        for _ in range(5):
            ev = m.ingest("E07", 1000.25)
            if ev:
                events.append(ev)
        # After 5 +25 cm samples in the 10-window, mean = +12.5 cm
        # which exceeds the 5 cm threshold.
        self.assertGreaterEqual(len(events), 1)
        last = events[-1]
        self.assertGreater(abs(last['drift_m']), 0.05)

    def test_warmup_skips_initial_samples(self):
        """The first ``warmup_epochs`` ingest calls are ignored."""
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=3, warmup_epochs=5)
        m.note_fix("E07", gf_ref_m=1000.0)
        events = []
        # First 5 ingests are warmup — even at 0.5 m residual, no event.
        for _ in range(5):
            ev = m.ingest("E07", 1000.50)
            if ev:
                events.append(ev)
        self.assertEqual(events, [])
        # Sixth ingest enters the window; need 3 to satisfy min_samples.
        for _ in range(3):
            ev = m.ingest("E07", 1000.50)
            if ev:
                events.append(ev)
        self.assertEqual(len(events), 1)

    def test_unfix_clears_state(self):
        """note_unfix wipes the SV's reference and history."""
        m = GfPhaseRollingMeanMonitor(window_epochs=5, threshold_m=0.05,
                                      min_samples=3, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        for _ in range(3):
            m.ingest("E07", 1000.20)
        m.note_unfix("E07")
        # ingest after unfix is silent.
        self.assertIsNone(m.ingest("E07", 1000.30))
        # Re-fix at a new reference starts fresh.
        m.note_fix("E07", gf_ref_m=2000.0)
        for _ in range(2):
            self.assertIsNone(m.ingest("E07", 2000.20))  # below min_samples
        ev = m.ingest("E07", 2000.20)
        self.assertIsNotNone(ev)
        self.assertEqual(ev['gf_ref_m'], 2000.0)

    def test_re_note_fix_clears_history(self):
        """Re-notifying an already-fixed SV captures a new reference
        and restarts warmup — for example, after a real cycle slip
        that was caught by the slip detector and the SV got re-fixed."""
        m = GfPhaseRollingMeanMonitor(window_epochs=10, threshold_m=0.05,
                                      min_samples=3, warmup_epochs=2)
        m.note_fix("E07", gf_ref_m=1000.0)
        # Burn through warmup.
        for _ in range(2):
            m.ingest("E07", 1000.0)
        m.ingest("E07", 1000.0)
        m.ingest("E07", 1000.0)
        # Re-fix at a different reference, restart warmup.
        m.note_fix("E07", gf_ref_m=1005.0)
        events = []
        # First 2 ingests after re-fix: warmup, no events even at
        # large residual.
        for _ in range(2):
            ev = m.ingest("E07", 1005.50)
            if ev:
                events.append(ev)
        self.assertEqual(events, [])

    def test_rolling_mean_diagnostic(self):
        """rolling_mean() returns the current mean once the window
        has min_samples; None before."""
        m = GfPhaseRollingMeanMonitor(window_epochs=5, threshold_m=0.10,
                                      min_samples=3, warmup_epochs=0)
        m.note_fix("E07", gf_ref_m=1000.0)
        self.assertIsNone(m.rolling_mean("E07"))
        m.ingest("E07", 1000.05)
        self.assertIsNone(m.rolling_mean("E07"))  # still below min_samples
        m.ingest("E07", 1000.05)
        self.assertIsNone(m.rolling_mean("E07"))
        m.ingest("E07", 1000.05)  # now at min_samples=3
        self.assertAlmostEqual(m.rolling_mean("E07"), 0.05, places=4)

    def test_summary_text(self):
        m = GfPhaseRollingMeanMonitor(window_epochs=30, threshold_m=0.05)
        m.note_fix("E07", gf_ref_m=1000.0)
        m.note_fix("E12", gf_ref_m=2000.0)
        s = m.summary()
        self.assertIn("2 SVs", s)
        self.assertIn("30ep", s)
        self.assertIn("5.0cm", s)


class GfPhaseMHelperTest(unittest.TestCase):
    """``gf_phase_m`` helper — sanity check the combination math."""

    def test_basic_combination(self):
        # GF = phi1 * lambda_L1 - phi2 * lambda_L5
        # phi1 = 100 cyc * 0.190 m = 19.0 m
        # phi2 = 100 cyc * 0.255 m = 25.5 m
        # GF = -6.5 m
        result = gf_phase_m(100.0, 100.0, 0.190, 0.255)
        self.assertAlmostEqual(result, -6.5, places=4)

    def test_one_cycle_l1_step(self):
        """A 1-cycle slip on L1 produces a λ_L1 step in GF."""
        before = gf_phase_m(100.0, 100.0, 0.190, 0.255)
        after = gf_phase_m(101.0, 100.0, 0.190, 0.255)
        self.assertAlmostEqual(after - before, 0.190, places=4)


if __name__ == "__main__":
    unittest.main()
