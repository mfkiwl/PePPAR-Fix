"""Unit tests for IfStepMonitor — NL-layer cohort-median post-fit
IF residual demoter that replaces FalseFixMonitor's PR-residual
eviction action.

Existing tests pass ``min_cohort_size=2, warmup_epochs=0`` at
construction so they preserve their original semantics under the
post-I-140938 default changes (min_cohort 2 → 4, warmup 0 → 30).
New behavior coverage is in the dedicated test classes at the
bottom of the file."""

from __future__ import annotations

import unittest

from peppar_fix.if_step_monitor import IfStepMonitor


# Approximate NL wavelengths (metres).  A wrong NL fix by 1 cycle
# leaks into the post-fit phase residual at full λ_NL or fractions
# thereof depending on filter absorption.
LAMBDA_NL_GAL_L1L5 = 0.1066
LAMBDA_NL_GPS_L1L2 = 0.1071


def _mk(threshold_m=0.05, consecutive_epochs=2, min_cohort_size=2,
        warmup_epochs=0):
    """Build a monitor with the test-friendly defaults that preserve
    pre-I-140938 behavior."""
    return IfStepMonitor(
        threshold_m=threshold_m,
        consecutive_epochs=consecutive_epochs,
        min_cohort_size=min_cohort_size,
        warmup_epochs=warmup_epochs,
    )


class IfStepMonitorBasicTest(unittest.TestCase):
    """Untracked SVs, cohort-size guards, stable cohorts."""

    def test_silent_when_untracked(self):
        """SVs never note_fix'd are ignored — they don't enter the
        tracked set, residuals on them don't update streak state."""
        m = _mk()
        evs = m.update({"E07": 0.30, "G01": -0.40})
        self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 0)
        evs = m.update({"E07": 0.30, "G01": -0.40})
        self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 0)

    def test_cohort_too_small_no_events(self):
        """Single tracked SV: cohort-median is itself, residual
        identically zero, no trip even on huge raw residual."""
        m = _mk()
        m.note_fix("E07")
        for _ in range(5):
            evs = m.update({"E07": 0.50})
            self.assertEqual(evs, [])

    def test_clean_cohort_no_events(self):
        """Three NL-fixed SVs with mm-scale post-fit residuals
        produce no events (the correct-fix scenario)."""
        m = _mk()
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
        m = _mk()
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": LAMBDA_NL_GAL_L1L5,
                 "E12": 0.002, "E33": -0.001, "G01": 0.000}
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E07")
        self.assertEqual(events[0]['consecutive_epochs'], 2)
        self.assertGreater(abs(events[0]['cohort_residual_m']), 0.05)

    def test_clean_residuals_dont_trip(self):
        m = _mk()
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        events_total: list[dict] = []
        for i in range(20):
            r = {"E07": 0.001 * (i % 3 - 1),
                 "E12": 0.002, "E33": -0.001, "G01": 0.000}
            evs = m.update(r)
            events_total.extend(evs)
        self.assertEqual(events_total, [])

    def test_below_consecutive_epochs_no_event(self):
        m = _mk(consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])
        self.assertEqual(m.streak("E07"), 1)
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])
        self.assertEqual(m.streak("E07"), 2)
        evs = m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(len(evs), 1)
        self.assertEqual(evs[0]['consecutive_epochs'], 3)

    def test_recovery_resets_streak(self):
        m = _mk(consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        m.update({"E07": 0.001, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 0)


class IfStepMonitorCohortTest(unittest.TestCase):
    """Cohort-median behaviour."""

    def test_common_mode_residual_cancels(self):
        m = _mk()
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        common = 0.30
        for _ in range(10):
            evs = m.update({"E07": common, "E12": common,
                            "E33": common, "G01": common})
            self.assertEqual(evs, [])

    def test_cohort_isolates_one_outlier_under_common_bias(self):
        m = _mk()
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        common = 0.10
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": common + 0.10,
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
        m = _mk()
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        m.note_unfix("E07")
        self.assertEqual(m.streak("E07"), 0)
        self.assertEqual(m.n_tracking(), 2)
        evs = m.update({"E07": 0.50, "E12": 0.001, "E33": -0.001})
        self.assertEqual(evs, [])

    def test_trip_is_deduped_per_episode(self):
        m = _mk()
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        events_total: list[dict] = []
        for _ in range(10):
            r = {"E07": 0.20, "E12": 0.001, "E33": -0.001, "G01": 0.001}
            evs = m.update(r)
            events_total.extend(evs)
        self.assertEqual(len(events_total), 1)

    def test_re_note_fix_clears_streak(self):
        m = _mk(consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 2)
        m.note_fix("E07")
        self.assertEqual(m.streak("E07"), 0)


class IfStepMonitorEdgeCaseTest(unittest.TestCase):
    """Edge cases — sparse cohorts, mixed tracked/untracked."""

    def test_mixed_tracked_and_untracked_residuals(self):
        m = _mk()
        m.note_fix("E07")
        m.note_fix("E12")
        m.note_fix("E33")
        for _ in range(3):
            evs = m.update({
                "E07": 0.001, "E12": 0.002, "E33": 0.003,
                "G77": 999.0, "G88": -888.0,
            })
            self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 3)

    def test_sv_missing_one_epoch_resumes_correctly(self):
        m = _mk(consecutive_epochs=3)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        m.update({"E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 1)
        m.update({"E07": 0.20, "E12": 0.001, "E33": -0.001})
        self.assertEqual(m.streak("E07"), 2)


class IfStepMonitorDefaultsTest(unittest.TestCase):
    """Verify the post-I-140938 defaults."""

    def test_min_cohort_size_default_is_4(self):
        """Default bumped from 2 → 4 to prevent the n=2 pathology
        that lost ANCHORED E23 + E33 on clkPoC3."""
        m = IfStepMonitor()
        self.assertEqual(m._min_cohort, 4)

    def test_warmup_epochs_default_is_30(self):
        """Default warmup matches WlDriftMonitor convention."""
        m = IfStepMonitor()
        self.assertEqual(m._warmup, 30)

    def test_anchored_threshold_mult_default_is_2(self):
        """Default 2× multiplier on threshold for ANCHORED SVs."""
        m = IfStepMonitor()
        self.assertEqual(m._anchored_mult, 2.0)


class IfStepMonitorWarmupTest(unittest.TestCase):
    """Warmup gating — freshly-fixed SVs don't pollute the cohort
    median during their first ``warmup_epochs`` epochs.

    Reproduces the MadHat 08:56:12 case: E23 anchored 422s with
    residual=-0.025m (within threshold) was evicted because freshly
    admitted E21 with residual=+0.164m polluted the median to
    +0.0425m, making E23's cohort_residual=-0.068m (above threshold)."""

    def test_warming_up_sv_excluded_from_cohort(self):
        """Reproduces MadHat 08:56:12 case.  3-SV cohort with one
        well-warmed clean SV (E23 at -0.025m), one well-warmed
        borderline SV (E07 at +0.0425m), and one freshly-admitted
        SV with big residual (E21 at +0.164m).  Without warmup
        gating, cohort_median = +0.0425m and E23 looks like an
        outlier (cohort_residual = -0.068m).  WITH warmup gating,
        E21 is excluded from cohort → median = average of E23 + E07
        = +0.00875m; E23's cohort_residual = -0.034m, well within
        threshold → ANCHORED E23 survives."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=10)
        m.note_fix("E23")
        m.note_fix("E07")
        for _ in range(15):
            m.update({"E23": -0.025, "E07": +0.0425})  # past warmup
        m.note_fix("E21")  # fresh
        e23_e07_trips: list[dict] = []
        for _ in range(3):
            r = {"E23": -0.025, "E07": +0.0425, "E21": +0.164}
            evs = m.update(r)
            for ev in evs:
                if ev['sv'] in ("E23", "E07"):
                    e23_e07_trips.append(ev)
        # The well-warmed SVs survive — E21 doesn't pollute the
        # median during its warmup.
        self.assertEqual(e23_e07_trips, [])

    def test_warming_up_sv_can_still_trip_on_own_signal(self):
        """A warming-up SV is still EVALUATED — it doesn't contribute
        to the cohort but its own residual can trip the gate."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=10)
        for sv in ("E07", "E12"):
            m.note_fix(sv)
        for _ in range(15):
            m.update({"E07": 0.001, "E12": 0.001})  # past warmup
        # Bring in E21 with sustained huge residual.
        m.note_fix("E21")
        events: list[dict] = []
        for _ in range(3):
            r = {"E07": 0.001, "E12": 0.001, "E21": 0.500}
            evs = m.update(r)
            events.extend(evs)
        # E21 trips on its own absurd residual (cohort median ≈ 0;
        # E21's cohort_residual ≈ 0.5m >> 0.05m threshold).
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E21")

    def test_post_warmup_sv_contributes_to_cohort(self):
        """Once an SV passes warmup_epochs, it joins the cohort
        median.  Verify by counting cohort_size on a trip emitted
        before vs after warmup."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=5)
        for sv in ("E07", "E12", "E33"):
            m.note_fix(sv)
        # Warm them all up.
        for _ in range(10):
            m.update({"E07": 0.001, "E12": 0.001, "E33": 0.001})
        # Add a fresh SV E21 with a small clean residual; force a
        # different SV (E33) to trip via a sudden bad value.
        m.note_fix("E21")
        events: list[dict] = []
        # During E21 warmup, cohort = 3 (E07/E12/E33).
        for _ in range(2):
            r = {"E07": 0.001, "E12": 0.001, "E33": +0.20,
                 "E21": 0.002}
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E33")
        # cohort_size=3 (E21 excluded during warmup);
        # cohort_size_total=4 includes E21.
        self.assertEqual(events[0]['cohort_size'], 3)
        self.assertEqual(events[0]['cohort_size_total'], 4)

    def test_cohort_size_field_excludes_warming_up(self):
        """Trip event's cohort_size reflects post-warmup count;
        cohort_size_total includes warming-up."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=5)
        for sv in ("E07", "E12"):
            m.note_fix(sv)
        for _ in range(10):
            m.update({"E07": 0.001, "E12": 0.001})
        m.note_fix("E21")
        events: list[dict] = []
        for _ in range(3):
            r = {"E07": 0.001, "E12": 0.001, "E21": 0.500}
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(events[0]['cohort_size'], 2)
        self.assertEqual(events[0]['cohort_size_total'], 3)


class IfStepMonitorAnchoredProtectionTest(unittest.TestCase):
    """ANCHORED SVs get a 2× threshold per Bob's earned-trust note.

    Reproduces the MadHat 08:56:12 outcome: E23 in ANCHORED state
    with cohort_residual=-0.068m would have survived (below 2× ×
    0.05m = 0.10m) instead of being evicted."""

    def test_anchored_sv_uses_2x_threshold(self):
        """An ANCHORED SV with cohort_residual just over base
        threshold passes the 2× check."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=0,
                          anchored_threshold_mult=2.0)
        for sv in ("E07", "E12", "E23"):
            m.note_fix(sv)
        # E23 has cohort_residual ≈ -0.068m: above 0.05m base, below
        # 0.10m anchored threshold.
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": +0.0425, "E12": +0.0425, "E23": -0.025}
            # cohort_median = +0.0425; E23 residual = -0.025 - 0.0425
            # = -0.0675.  Above base 0.05; below anchored 0.10.
            evs = m.update(r, anchored_svs={"E23"})
            events.extend(evs)
        # E23 is anchored — survives.  E07 + E12 cohort_residual = 0.
        self.assertEqual(events, [])

    def test_non_anchored_sv_uses_base_threshold(self):
        """Without ANCHORED tag, the same residual trips at base
        threshold."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=0)
        for sv in ("E07", "E12", "E23"):
            m.note_fix(sv)
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": +0.0425, "E12": +0.0425, "E23": -0.025}
            evs = m.update(r, anchored_svs=None)
            events.extend(evs)
        # E23 trips at base threshold.
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E23")
        self.assertFalse(events[0]['anchored'])

    def test_anchored_sv_still_trips_on_truly_huge_residual(self):
        """ANCHORED protection is 2×, not infinite — a really bad
        residual still trips."""
        m = IfStepMonitor(threshold_m=0.05, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=0,
                          anchored_threshold_mult=2.0)
        for sv in ("E07", "E12", "E23"):
            m.note_fix(sv)
        events: list[dict] = []
        for _ in range(5):
            r = {"E07": 0.001, "E12": 0.001, "E23": +0.30}
            # E23 cohort_residual ≈ +0.30 — way above 2× × 0.05 = 0.10.
            evs = m.update(r, anchored_svs={"E23"})
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "E23")
        self.assertTrue(events[0]['anchored'])
        # Effective threshold reflects the ANCHORED multiplier.
        self.assertAlmostEqual(events[0]['threshold_m'], 0.10)
        self.assertAlmostEqual(events[0]['threshold_base_m'], 0.05)


class IfStepMonitorMinCohortBumpTest(unittest.TestCase):
    """Reproduces the clkPoC3 09:00:09 case: with n=2 the median
    is the average; any disagreement makes BOTH SVs look like
    outliers.  Default min_cohort_size=4 prevents this."""

    def test_n_equals_2_no_evaluation_at_default(self):
        """Default min_cohort_size=4 means n=2 cohort doesn't fire."""
        m = IfStepMonitor()  # all defaults including warmup
        for sv in ("E23", "E33"):
            m.note_fix(sv)
        # Run past warmup with disagreement — would have tripped at
        # min_cohort=2 (each sees the other as outlier).
        events: list[dict] = []
        for _ in range(40):
            evs = m.update({"E23": +0.05, "E33": -0.05})
            events.extend(evs)
        # No events — cohort below min size.
        self.assertEqual(events, [])

    def test_n_equals_4_does_evaluate(self):
        """Cohort of 4: evaluation proceeds."""
        m = IfStepMonitor(consecutive_epochs=2)  # default min_cohort=4
        for sv in ("E07", "E12", "E33", "G01"):
            m.note_fix(sv)
        # Run past warmup, then an outlier in a clean cohort of 4.
        for _ in range(35):
            m.update({"E07": 0.001, "E12": -0.002, "E33": 0.003,
                      "G01": 0.000})
        events: list[dict] = []
        for _ in range(3):
            r = {"E07": 0.001, "E12": -0.002, "E33": 0.003,
                 "G01": +0.20}  # G01 outlier
            evs = m.update(r)
            events.extend(evs)
        self.assertEqual(len(events), 1)
        self.assertEqual(events[0]['sv'], "G01")


class IfStepMonitorSummaryTest(unittest.TestCase):
    def test_summary_text(self):
        m = _mk()
        m.note_fix("E07")
        m.note_fix("E12")
        s = m.summary()
        self.assertIn("2 NL-fixed SVs", s)
        self.assertIn("5.0cm", s)
        self.assertIn("2ep", s)


if __name__ == "__main__":
    unittest.main()
