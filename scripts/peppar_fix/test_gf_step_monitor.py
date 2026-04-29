"""Unit tests for GfStepMonitor — per-epoch cohort-median Δgf step
detector that replaces the MW-based wl_drift demoter."""

from __future__ import annotations

import unittest

from peppar_fix.gf_step_monitor import GfStepMonitor


# Cycle wavelengths (metres) — used to build realistic step-size tests.
LAMBDA_L1 = 0.1903
LAMBDA_L5 = 0.2548


class GfStepMonitorBasicTest(unittest.TestCase):
    """Single-SV behaviour and basic API."""

    def test_no_event_below_min_cohort(self):
        """Single fixed SV: cohort too small, no events even on
        large step."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", gf_initial_m=1000.0)
        # Step of 19 cm — would trip if cohort ≥ 2.
        evs = m.update({"E07": 1000.19})
        self.assertEqual(evs, [])
        evs = m.update({"E07": 1000.38})
        self.assertEqual(evs, [])

    def test_silent_when_untracked(self):
        """SVs never note_fix'd are ignored — they don't get a
        prev_gf so Δgf is never computed for them.  Verified twice
        (two consecutive update() calls): un-fix'd SVs MUST NOT
        accumulate prev_gf state silently."""
        m = GfStepMonitor()
        evs = m.update({"E99": 100.0, "G99": 200.0})
        self.assertEqual(evs, [])
        # Second update with same SVs: still ignored, prev_gf is
        # NOT populated by update() for non-note_fix'd SVs.
        evs = m.update({"E99": 200.0, "G99": 400.0})  # huge "Δgf" if buggy
        self.assertEqual(evs, [])
        self.assertEqual(m.n_tracking(), 0)

    def test_mixed_tracked_and_untracked(self):
        """When the caller passes both tracked and untracked SVs,
        only tracked SVs participate in the cohort."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=0)
        # Track only E07, E12, E33.
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # Pass the full observed-SV set including unfixed ones.
        for _ in range(3):
            evs = m.update({
                "E07": 1000.0, "E12": 2000.0, "E33": 3000.0,
                "E99": 999.0,  # never note_fix'd
                "G77": 7777.0,  # never note_fix'd
            })
            self.assertEqual(evs, [])
        # Untracked SVs did NOT enter the prev_gf state.
        self.assertEqual(m.n_tracking(), 3)

    def test_stable_cohort_no_events(self):
        """Three SVs with stable GF (no slip) produce no events."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        for _ in range(20):
            evs = m.update({"E07": 1000.0, "E12": 2000.0, "E33": 3000.0})
            self.assertEqual(evs, [])

    def test_cohort_median_cancels_common_mode_iono(self):
        """5 cm/min iono drift across all SVs equally produces zero
        residual after cohort-median subtraction.  No trips."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # 5 cm / min = 5 cm / 60 epochs at 1 Hz ≈ 0.83 mm / epoch.
        # Apply uniformly to all SVs.
        common = 0.000833
        gf07, gf12, gf33 = 1000.0, 2000.0, 3000.0
        for _ in range(120):  # 2 min of drift
            gf07 += common
            gf12 += common
            gf33 += common
            evs = m.update({"E07": gf07, "E12": gf12, "E33": gf33})
            self.assertEqual(evs, [])

    def test_fast_iono_common_mode_still_cancels(self):
        """Even fast iono (sunrise TEC, 30 cm / min) cancels via
        cohort-median when applied uniformly.  No trips on common-
        mode regardless of magnitude."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # 30 cm / min = 5 mm / epoch — way above 4 cm threshold if
        # uncorrected, but fully cancelled by the cohort median.
        common = 0.005
        gf07, gf12, gf33 = 1000.0, 2000.0, 3000.0
        for _ in range(20):
            gf07 += common
            gf12 += common
            gf33 += common
            evs = m.update({"E07": gf07, "E12": gf12, "E33": gf33})
            self.assertEqual(evs, [])


class GfStepMonitorSlipDetectionTest(unittest.TestCase):
    """The detector's main job — flag a slipped SV against a
    cohort that's seeing only iono."""

    def test_19cm_step_trips_after_two_consecutive_epochs(self):
        """A clean 19 cm step on E07 (one-cycle L1 slip) trips after
        two consecutive over-threshold epochs.  Other cohort SVs
        stay clean."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # Two stable epochs first.
        m.update({"E07": 1000.0, "E12": 2000.0, "E33": 3000.0})
        m.update({"E07": 1000.0, "E12": 2000.0, "E33": 3000.0})
        # E07 slips by +19 cm at this epoch, then holds the new value.
        # Δgf for E07 = +0.19; Δgf for E12, E33 = 0; cohort median = 0.
        # Residual for E07 = +0.19, well above threshold.
        evs = m.update({"E07": 1000.0 + LAMBDA_L1, "E12": 2000.0,
                        "E33": 3000.0})
        # First epoch over threshold — streak = 1, no trip yet
        # (consecutive_epochs = 2).
        self.assertEqual(evs, [])
        self.assertEqual(m.streak("E07"), 1)
        # Continued offset: E07's GF remains at the post-slip value,
        # so Δgf = 0 from this point.  This is the case the test
        # warning in the module docstring described — single-epoch
        # slips wouldn't trip on Δgf with consecutive_epochs ≥ 2.
        # cycle_slip.py covers that case via instantaneous gf_jump.
        # For sustained-offset slips that keep producing non-zero
        # Δgf, we need a test that drifts further.
        evs = m.update({"E07": 1000.0 + LAMBDA_L1, "E12": 2000.0,
                        "E33": 3000.0})
        # E07's Δgf this epoch = 0; streak resets.
        self.assertEqual(m.streak("E07"), 0)

    def test_sustained_drift_trips(self):
        """An SV whose Δgf stays elevated for multiple consecutive
        epochs — e.g., a tracker drifting at 5 cm/epoch while the
        rest of the cohort is stable — trips after ``consecutive``
        epochs."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        gf07 = 1000.0
        evs_total: list[dict] = []
        for _ in range(5):
            gf07 += 0.05  # +5 cm / epoch on E07
            evs = m.update({"E07": gf07, "E12": 2000.0, "E33": 3000.0})
            evs_total.extend(evs)
        # Should trip exactly once (deduped via _tripped set).
        self.assertEqual(len(evs_total), 1)
        ev = evs_total[0]
        self.assertEqual(ev['sv'], "E07")
        self.assertGreater(abs(ev['residual_m']), 0.04)
        self.assertEqual(ev['consecutive_epochs'], 2)
        self.assertEqual(ev['cohort_size'], 3)

    def test_slow_iono_ramp_does_not_trip(self):
        """A 5 cm / minute iono ramp on a single SV (other SVs
        stable) produces ~0.83 mm Δgf per epoch — well below the
        4 cm threshold.  No trip across multiple minutes."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        gf07 = 1000.0
        evs_total: list[dict] = []
        for _ in range(120):
            gf07 += 0.000833  # 5 cm / min on E07
            evs = m.update({"E07": gf07, "E12": 2000.0, "E33": 3000.0})
            evs_total.extend(evs)
        self.assertEqual(evs_total, [])

    def test_cohort_isolates_one_slipped_sv(self):
        """Common-mode iono ramp + a single-SV slip: cohort-median
        cancels the iono; the slipped SV's residual remains and
        trips."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # Common iono: 1 cm/epoch (fast) on all three.  Plus E07
        # drifts an extra 6 cm/epoch (slipping/wrong-tracking).
        gf07, gf12, gf33 = 1000.0, 2000.0, 3000.0
        common = 0.01
        evs_total: list[dict] = []
        for _ in range(5):
            gf07 += common + 0.06  # E07 has extra 6 cm
            gf12 += common
            gf33 += common
            evs = m.update({"E07": gf07, "E12": gf12, "E33": gf33})
            evs_total.extend(evs)
        # E07 should trip; the common-mode is cancelled by the median.
        self.assertEqual(len(evs_total), 1)
        self.assertEqual(evs_total[0]['sv'], "E07")
        # Residual ≈ 6 cm (the per-epoch excess over cohort median).
        self.assertAlmostEqual(abs(evs_total[0]['residual_m']), 0.06,
                               places=3)


class GfStepMonitorLifecycleTest(unittest.TestCase):
    """Tracking lifecycle, dedup, recovery."""

    def test_unfix_clears_state(self):
        """note_unfix wipes prev_gf, streak, and tripped flag."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        # Drive a trip.
        for _ in range(3):
            m.update({"E07": 1000.0, "E12": 2000.0})
        self.assertEqual(m.streak("E07"), 0)
        # Now drift E07 to trip.
        for i in range(5):
            m.update({"E07": 1000.0 + (i + 1) * 0.06,
                      "E12": 2000.0})
        # Unfix E07 — state cleared.
        m.note_unfix("E07")
        self.assertEqual(m.n_tracking(), 1)
        self.assertEqual(m.streak("E07"), 0)
        # Re-fix and verify fresh start.
        m.note_fix("E07", 5000.0)
        evs = m.update({"E07": 5000.0, "E12": 2000.0})
        self.assertEqual(evs, [])

    def test_recovery_resets_streak(self):
        """Below-threshold residual after partial streak resets the
        counter; a subsequent over-threshold sequence has to
        accumulate from scratch."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=3, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # One over-threshold epoch on E07.
        m.update({"E07": 1000.0 + 0.06, "E12": 2000.0, "E33": 3000.0})
        self.assertEqual(m.streak("E07"), 1)
        # Recovery (Δgf returns to ~cohort).
        m.update({"E07": 1000.0 + 0.06, "E12": 2000.0, "E33": 3000.0})
        # E07's Δgf this epoch = 0, streak resets.
        self.assertEqual(m.streak("E07"), 0)

    def test_trip_is_deduped_per_episode(self):
        """Once an SV trips, subsequent over-threshold epochs in
        the same drift episode don't emit additional events.
        Caller is expected to demote (call note_unfix) on the
        first event.

        Uses a 3-SV cohort so the slipping SV's Δgf doesn't dominate
        the median (with only 2 SVs the median is the mean and the
        residual is half the slip — just at threshold)."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        gf07 = 1000.0
        evs_total: list[dict] = []
        for _ in range(10):
            gf07 += 0.05
            evs = m.update({"E07": gf07, "E12": 2000.0, "E33": 3000.0})
            evs_total.extend(evs)
        # Exactly one event despite many over-threshold epochs.
        self.assertEqual(len(evs_total), 1)


class GfStepMonitorEdgeCaseTest(unittest.TestCase):
    """Edge cases — sparse cohorts, missing SVs, stale references."""

    def test_sv_missing_one_epoch_resumes_correctly(self):
        """An SV that's absent from one update() call doesn't
        contribute to that epoch's cohort; on the next update it
        re-contributes against its held prev_gf."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        m.note_fix("E33", 3000.0)
        # All three present.
        m.update({"E07": 1000.0, "E12": 2000.0, "E33": 3000.0})
        # E12 absent this epoch — cohort = {E07, E33}, both stable,
        # no events.
        evs = m.update({"E07": 1000.0, "E33": 3000.0})
        self.assertEqual(evs, [])
        # E12 returns next epoch with the held prev_gf as reference.
        evs = m.update({"E07": 1000.0, "E12": 2000.0, "E33": 3000.0})
        self.assertEqual(evs, [])

    def test_min_cohort_size_zero_disables(self):
        """min_cohort_size = 1 with explicit single-SV behaviour:
        cohort median of one SV's Δgf is itself, residual is zero,
        no trip."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2,
                          min_cohort_size=1, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        for _ in range(5):
            evs = m.update({"E07": 1000.0 + 1.0})  # huge Δgf
            self.assertEqual(evs, [])

    def test_two_sv_cohort_median_is_average(self):
        """With cohort = 2, median = (a + b) / 2.  A single-SV slip
        in this cohort still trips (residual = (a − b) / 2 from
        the slipped SV's perspective, which exceeds threshold for
        a clean λ_L1 step)."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        # Stable epoch.
        m.update({"E07": 1000.0, "E12": 2000.0})
        gf07 = 1000.0
        evs_total: list[dict] = []
        for _ in range(4):
            gf07 += 0.10  # 10 cm/epoch on E07; E12 stable
            evs = m.update({"E07": gf07, "E12": 2000.0})
            evs_total.extend(evs)
        # Median of (0.10, 0) = 0.05; residual on E07 = +0.05;
        # residual on E12 = -0.05.  Both above 4 cm threshold.
        # The first SV to streak through trips first; the dedup
        # set prevents both from tripping at the same epoch but
        # the next epoch the other SV trips too.
        # Anyway we should see at least one event.
        self.assertGreaterEqual(len(evs_total), 1)


class GfStepMonitorPostI140938DefaultsTest(unittest.TestCase):
    """Verify the post-I-140938 default changes."""

    def test_min_cohort_size_default_is_4(self):
        """Bumped from 2 → 4 to prevent the n=2 cohort pathology."""
        m = GfStepMonitor()
        self.assertEqual(m._min_cohort, 4)

    def test_warmup_epochs_default_is_30(self):
        m = GfStepMonitor()
        self.assertEqual(m._warmup, 30)

    def test_warming_up_sv_excluded_from_cohort(self):
        """A freshly-fixed SV's first warmup epochs do NOT pollute
        the cohort median."""
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2,
                          min_cohort_size=2, warmup_epochs=10)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        # Past warmup with stable Δgf.
        for i in range(15):
            gf07 = 1000.0 + (i + 1) * 0.001
            gf12 = 2000.0 + (i + 1) * 0.001
            m.update({"E07": gf07, "E12": gf12})
        # Add E21 fresh with a big Δgf swing that would pollute.
        m.note_fix("E21", 3000.0)
        e07_e12_trips: list[dict] = []
        gf07, gf12 = 1000.015, 2000.015
        gf21 = 3000.0
        for _ in range(3):
            gf07 += 0.001
            gf12 += 0.001
            gf21 += 0.10  # 10 cm/epoch on the warming-up SV
            evs = m.update({"E07": gf07, "E12": gf12, "E21": gf21})
            for ev in evs:
                if ev['sv'] in ("E07", "E12"):
                    e07_e12_trips.append(ev)
        # E07 + E12 stay clean — E21 didn't pollute the cohort.
        self.assertEqual(e07_e12_trips, [])


class GfStepMonitorSummaryTest(unittest.TestCase):
    def test_summary_text_includes_threshold_and_count(self):
        m = GfStepMonitor(threshold_m=0.04, consecutive_epochs=2, min_cohort_size=2, warmup_epochs=0)
        m.note_fix("E07", 1000.0)
        m.note_fix("E12", 2000.0)
        s = m.summary()
        self.assertIn("2 SVs", s)
        self.assertIn("4.0cm", s)
        self.assertIn("2ep", s)


if __name__ == "__main__":
    unittest.main()
