"""Unit tests for WlDriftMonitor."""

from __future__ import annotations

import unittest

from peppar_fix.wl_drift_monitor import (
    CONS_HIGH,
    CONS_LOW,
    CONS_MEDIUM,
    CONS_UNKNOWN,
    WlDriftMonitor,
)


class WlDriftMonitorTest(unittest.TestCase):
    """Per-SV rolling-mean drift detector — correctness + edge cases."""

    def test_no_event_when_not_fixed(self):
        """ingest() on an SV that was never note_fix'd is silent."""
        m = WlDriftMonitor(window_epochs=5, threshold_cyc=0.15,
                           min_samples=3, warmup_epochs=0)
        for _ in range(10):
            self.assertIsNone(m.ingest("G01", 0.30))  # big drift, ignored

    def test_no_event_below_min_samples(self):
        """Needs ≥ min_samples before any event can fire."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5, warmup_epochs=0)
        m.note_fix("G01")
        for _ in range(4):
            # Persistent +0.3 cyc drift — well above threshold —
            # but fewer than 5 samples, so no event yet.
            self.assertIsNone(m.ingest("G01", 0.30))

    def test_fires_when_rolling_mean_exceeds_threshold(self):
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5, warmup_epochs=0)
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
                           min_samples=5, warmup_epochs=0)
        m.note_fix("G01")
        samples = [0.02, -0.03, 0.01, -0.02, 0.04, -0.01, 0.02]
        for s in samples:
            self.assertIsNone(m.ingest("G01", s))

    def test_transient_spike_does_not_fire(self):
        """Single large sample averaged with many near-zero samples
        stays within threshold — drift must be sustained."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5, warmup_epochs=0)
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
                            min_samples=5, warmup_epochs=0)
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
                           min_samples=15, warmup_epochs=0)
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
        and restarts the warmup count (fresh window after re-fix).
        Models the case where MW was reset externally and re-fixed
        without going through the monitor's note_unfix path."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5, warmup_epochs=0)
        m.note_fix("G01")
        for _ in range(5):
            m.ingest("G01", 0.30)
        # Now we're armed.  Re-fix clears history.
        m.note_fix("G01")
        ev = m.ingest("G01", 0.30)
        self.assertIsNone(ev)  # one sample, below min_samples

    def test_note_unfix_stops_tracking(self):
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.10,
                           min_samples=5, warmup_epochs=0)
        m.note_fix("G01")
        m.ingest("G01", 0.30)
        m.note_unfix("G01")
        # After unfix, ingest returns None (not tracking).
        self.assertIsNone(m.ingest("G01", 0.30))
        self.assertEqual(m.n_tracking(), 0)

    def test_multiple_svs_independent(self):
        m = WlDriftMonitor(window_epochs=5, threshold_cyc=0.10,
                           min_samples=3, warmup_epochs=0)
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
                           min_samples=15, warmup_epochs=0)
        self.assertIn("tracking 0", m.summary())
        m.note_fix("G01")
        m.note_fix("E05")
        self.assertIn("tracking 2", m.summary())


class WarmupBehaviorTest(unittest.TestCase):
    """Warmup period suppresses the first N ingest calls post-fix.

    The MW tracker's EMA (tau ≈ 60) takes ~30 epochs to settle to
    the post-fix mean even when the integer commitment is correct
    (fixes happen at |frac| < 0.15, not exactly 0).  Ingesting
    during settling produces drift signals from correct integers
    that the monitor cannot distinguish from wrong-integer drift.
    Warmup suppresses that ambiguity.  Day0423a showed ~270 drift
    events per host in 1h20m without warmup — 90% false positives.
    """

    def test_default_warmup_is_30(self):
        """Default value matches MW EMA settling time."""
        m = WlDriftMonitor()
        self.assertEqual(m._warmup, 30)

    def test_default_threshold_is_0_25(self):
        """Loosened from 0.15 after day0423a false-positive pattern.
        Still far below wrong-by-1 cyc (1.0 cyc) but tolerant of
        MW residual noise during settling."""
        m = WlDriftMonitor()
        self.assertAlmostEqual(m._threshold, 0.25)

    def test_warmup_suppresses_drift_during_settling(self):
        """During the warmup window, even a 1.0 cyc drift returns
        no event.  Monitor starts ingesting after warmup."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.20,
                           min_samples=5, warmup_epochs=10)
        m.note_fix("G01")
        # Ten large-drift samples during warmup: no event.
        for _ in range(10):
            self.assertIsNone(m.ingest("G01", 1.0))

    def test_fires_after_warmup_if_drift_persists(self):
        """A persistent wrong-integer drift past the warmup period
        fires as expected once min_samples accumulate post-warmup."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.20,
                           min_samples=5, warmup_epochs=10)
        m.note_fix("G01")
        # Warmup: first 10 samples ignored.
        for _ in range(10):
            self.assertIsNone(m.ingest("G01", 1.0))
        # Post-warmup: first 5 samples fill min_samples.
        ev = None
        for _ in range(5):
            ev = m.ingest("G01", 1.0)
        self.assertIsNotNone(ev)
        self.assertAlmostEqual(ev['drift_cyc'], 1.0)

    def test_settling_followed_by_stability_no_event(self):
        """Realistic correct-integer scenario: large residual during
        warmup (fix at frac=0.14, EMA settling), then near-zero once
        settled.  Monitor must NOT fire.  This is the false-positive
        case the warmup addresses."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.20,
                           min_samples=5, warmup_epochs=15)
        m.note_fix("G01")
        # Settling: residual decays from 0.14 toward 0 over 15 epochs.
        for i in range(15):
            s = 0.14 - 0.14 * (i / 14)  # 0.14 → 0
            self.assertIsNone(m.ingest("G01", s))
        # Post-warmup: residuals hover near 0, no event.
        for _ in range(10):
            self.assertIsNone(m.ingest("G01", 0.02))

    def test_warmup_resets_on_re_note_fix(self):
        """note_fix after an earlier fix restarts warmup.  Prior
        samples don't carry over."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.20,
                           min_samples=5, warmup_epochs=5)
        m.note_fix("G01")
        # Run through warmup + collect samples.
        for _ in range(5):
            m.ingest("G01", 0.0)
        for _ in range(5):
            m.ingest("G01", 0.05)
        # Re-fix — warmup restarts.
        m.note_fix("G01")
        # First 5 samples after re-fix are ignored again.
        for _ in range(5):
            self.assertIsNone(m.ingest("G01", 1.0))

    def test_note_unfix_clears_ingest_count(self):
        """note_unfix must clean up the ingest counter too, not just
        the history deque — otherwise a later note_fix followed by
        ingest could read a stale count."""
        m = WlDriftMonitor(window_epochs=10, threshold_cyc=0.20,
                           min_samples=5, warmup_epochs=5)
        m.note_fix("G01")
        for _ in range(3):
            m.ingest("G01", 0.0)
        m.note_unfix("G01")
        self.assertNotIn("G01", m._ingest_count)


class WlIntConsistencyTest(unittest.TestCase):
    """Per-SV WL integer consistency classification + adaptive
    threshold lookup."""

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
        # New fix at same integer must be classifiable as HIGH.
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

    def test_threshold_lookup_by_consistency(self):
        """threshold_for() returns the level-appropriate threshold."""
        m = WlDriftMonitor(
            threshold_cyc=0.25,
            threshold_high_cyc=0.60,
            threshold_medium_cyc=0.35,
        )
        # UNKNOWN → defaults to LOW threshold.
        self.assertEqual(m.threshold_for("G01"), 0.25)
        # HIGH path
        m.note_fix("G01", n_wl=5)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=5)
        self.assertEqual(m.threshold_for("G01"), 0.60)
        # MEDIUM path
        m.note_fix("E01", n_wl=10)
        m.note_unfix("E01")
        m.note_fix("E01", n_wl=11)
        self.assertEqual(m.threshold_for("E01"), 0.35)
        # LOW path
        m.note_fix("E02", n_wl=0)
        m.note_unfix("E02")
        m.note_fix("E02", n_wl=99)
        self.assertEqual(m.threshold_for("E02"), 0.25)

    def test_high_consistency_uses_wide_threshold(self):
        """HIGH-consistency SV with rolling mean +0.40 cyc — under
        the 0.60 HIGH threshold — should NOT fire wl_drift."""
        m = WlDriftMonitor(
            window_epochs=15, threshold_cyc=0.25,
            threshold_high_cyc=0.60, min_samples=10, warmup_epochs=0,
        )
        # Establish HIGH consistency (two fixes at same integer).
        m.note_fix("G01", n_wl=5)
        m.note_unfix("G01")
        m.note_fix("G01", n_wl=5)
        # Steady +0.40 cyc residual — over LOW threshold, under HIGH.
        for _ in range(15):
            ev = m.ingest("G01", 0.40)
        self.assertIsNone(ev)

    def test_low_consistency_keeps_strict_threshold(self):
        """LOW-consistency SV (wandering integer) keeps the 0.25
        threshold — should still fire on +0.40 cyc."""
        m = WlDriftMonitor(
            window_epochs=15, threshold_cyc=0.25,
            min_samples=10, warmup_epochs=0,
        )
        m.note_fix("G20", n_wl=-41)
        m.note_unfix("G20")
        m.note_fix("G20", n_wl=18)
        # LOW consistency now.
        ev = None
        for _ in range(15):
            ev = m.ingest("G20", 0.40)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["consistency"], CONS_LOW)
        self.assertAlmostEqual(ev["threshold_cyc"], 0.25)

    def test_event_carries_consistency(self):
        """Drift event dict includes the consistency level used."""
        m = WlDriftMonitor(
            window_epochs=10, threshold_cyc=0.20,
            min_samples=5, warmup_epochs=0,
        )
        m.note_fix("G01", n_wl=5)
        ev = None
        for _ in range(10):
            ev = m.ingest("G01", 0.50)
        self.assertIsNotNone(ev)
        self.assertEqual(ev["consistency"], CONS_UNKNOWN)


if __name__ == "__main__":
    unittest.main()
