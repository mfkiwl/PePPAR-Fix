"""Unit tests for IfStepMonitor — NL-layer cohort-median post-fit
IF residual demoter that replaces FalseFixMonitor's PR-residual
eviction action."""

from __future__ import annotations

import unittest

from peppar_fix.if_step_monitor import IfStepMonitor


# Approximate NL wavelengths (metres).  A wrong NL fix by 1 cycle
# leaks into the post-fit phase residual at full λ_NL or fractions
# thereof depending on filter absorption.
LAMBDA_NL_GAL_L1L5 = 0.1066
LAMBDA_NL_GPS_L1L2 = 0.1071


class IfStepMonitorBasicTest(unittest.TestCase):
    """Untracked SVs, cohort-size guards, stable cohorts."""

    def test_silent_when_untracked(self):
        """SVs never note_fix'd are ignored — they don't enter the
        tracked set, residuals on them don't update streak state."""
        m = IfStepMonitor()
        evs = m.update({"E07": 0.30, "G01": -0.40})
        self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 0)
        # Second update with same SVs: still no trips, still no
        # tracking buildup.
        evs = m.update({"E07": 0.30, "G01": -0.40})
        self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 0)

    def test_cohort_too_small_no_events(self):
        """Single tracked SV: cohort-median is itself, residual
        identically zero, no trip even on huge raw residual."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        m.note_fix("E07")
        for _ in range(5):
            evs = m.update({"E07": 0.50})  # 50 cm residual
            self.assertEqual(evs, [])

    def test_clean_cohort_no_events(self):
        """Three NL-fixed SVs with mm-scale post-fit residuals
        produce no events (the correct-fix scenario)."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        m.note_fix("E07")
        m.note_fix("E12")
        m.note_fix("E33")
        for _ in range(20):
            evs = m.update({"E07": 0.001, "E12": -0.002, "E33": 0.003})
            self.assertEqual(evs, [])


class IfStepMonitorWrongFixDetectionTest(unittest.TestCase):
    """The detector's main job — flag a wrong-NL-fixed SV against
    a cohort whose post-fit residuals are clean."""

    def test_one_wrong_sv_in_clean_cohort_trips(self):
        """One wrong NL fix in a 4-SV cohort: cohort median is
        small (clean SVs dominate); the wrong SV's residual stands
        out and trips after consecutive_epochs."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        m.note_fix("E07")
        m.note_fix("E12")
        m.note_fix("E33")
        m.note_fix("G01")
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": LAMBDA_NL_GAL_L1L5,  # 10.7 cm wrong
                 "E12": 0.002, "E33": -0.001, "G01": 0.000}
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E07")
        self.assertEqual(events[0]['consecutive_epochs'], 2)
        self.assertGreater(abs(events[0]['cohort_residual_m']), 0.05)

    def test_clean_residuals_dont_trip(self):
        """20 epochs of mm-scale residuals across a 4-SV cohort
        produce zero events."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        events_total: list[dict] = []
        for i in range(20):
            r = {"E07": 0.001 * (i % 3 - 1),  # tiny oscillation
                 "E12": 0.002, "E33": -0.001, "G01": 0.000}
            evs = m.update(r)
            events_total.extend(evs)
        self.assertEqual(events_total, [])

    def test_below_consecutive_epochs_no_event(self):
        """A single epoch over threshold isn't enough to trip; the
        streak has to reach ``consecutive_epochs``."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        # First epoch over threshold.
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])
        self.assertEqual(m.streak("E07"), 1)
        # Second.
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])
        self.assertEqual(m.streak("E07"), 2)
        # Third — trip.
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]['consecutive_epochs'], 3)

    def test_recovery_resets_streak(self):
        """Below-threshold residual after a partial streak resets
        the counter; subsequent over-threshold has to rebuild."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        # Recovery.
        m.update({"E07": 0.001, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 0)


class IfStepMonitorCohortTest(unittest.TestCase):
    """Cohort-median behaviour."""

    def test_common_mode_residual_cancels(self):
        """If the filter has absorbed common-mode error imperfectly
        and ALL NL-fixed SVs show the same post-fit offset, the
        cohort median absorbs it and nothing trips."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        common = 0.30  # 30 cm common bias on all SVs (huge)
        for _ in range(10):
            evs = m.update({"E07": common, "E12": common,
                            "E33": common, "G01": common})
            self.assertEqual(evs, [])

    def test_cohort_isolates_one_outlier_under_common_bias(self):
        """Common-mode bias on all SVs + one extra outlier on one
        SV: cohort cancels the common part; the outlier residual
        relative to the cohort trips."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        common = 0.10
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": common + 0.10,  # extra 10 cm outlier
                 "E12": common, "E33": common, "G01": common}
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E07")
        self.assertAlmostEqual(events[0]['cohort_residual_m'], 0.10,
                               places=3)


class IfStepMonitorLifecycleTest(unittest.TestCase):
    """Tracking lifecycle, dedup, re-fix."""

    def test_unfix_clears_state(self):
        """note_unfix wipes tracked / streak / tripped state."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        m.note_unfix("E07")
        self.assertEqual(m.streak("E07"), 0)
        self.assertEqual(m.n_tracking(), 2)
        # ingest after unfix is silent for E07.
        evs = m.update({"E07": 0.50, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])

    def test_trip_is_deduped_per_episode(self):
        """Once an SV trips, subsequent over-threshold epochs in
        the same episode don't emit additional events."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        events_total: list[dict] = []
        for _ in range(10):
            r = {"E07": 0.20, "E12": 0.001, "E33": -0.001, "G01": 0.001}
            evs = m.update(r)
            events_total.extend(evs)
        self.assertEqual(len(events_total), 1)

    def test_re_note_fix_clears_streak(self):
        """Re-notifying an already-tracked SV clears its streak —
        e.g., after the caller acted on a trip and is restarting."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 2)
        # Re-fix — clears streak and trip.
        m.note_fix("E07")
        self.assertEqual(m.streak("E07"), 0)


class IfStepMonitorEdgeCaseTest(unittest.TestCase):
    """Edge cases — sparse cohorts, mixed tracked/untracked."""

    def test_mixed_tracked_and_untracked_residuals(self):
        """Caller can pass the full filter residual dict; only
        tracked SVs participate."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2)
        m.note_fix("E07")
        m.note_fix("E12")
        m.note_fix("E33")
        for _ in range(3):
            evs = m.update({
                "E07": 0.001, "E12": 0.002, "E33": 0.003,
                "G77": 999.0, "G88": -888.0,  # untracked, ignored
            })
            self.assertEqual(evs, [])
        # Untracked never enter the tracked set.
        self.assertEqual(m.n_tracking(), 3)

    def test_sv_missing_one_epoch_resumes_correctly(self):
        """An SV that's absent from one update() call keeps its
        streak through the absence; its prior streak doesn't grow
        but doesn't reset either."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        # E07 absent this epoch — cohort = E12 + E33; E07's streak
        # is unchanged (no input for E07 → no streak increment).
        m.update({"E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        # E07 returns next epoch with same residual; streak resumes.
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 2)

    def test_min_cohort_size_two_is_default(self):
        """Verify the default ``min_cohort_size`` matches the
        documented value."""
        m = IfStepMonitor()
        self.assertEqual(m._min_cohort, 2)


class IfStepMonitorSummaryTest(unittest.TestCase):
    def test_summary_text(self):
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2)
        m.note_fix("E07")
        m.note_fix("E12")
        s = m.summary()
        self.assertIn("2 NL-fixed SVs", s)
        self.assertIn("5.0cm", s)
        self.assertIn("2ep", s)


if __name__ == "__main__":
    unittest.main()
