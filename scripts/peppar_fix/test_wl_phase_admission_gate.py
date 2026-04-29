"""Unit tests for WlPhaseAdmissionGate — pre-WL-fix phase residual
consistency check that prevents PR-driven false admissions."""

from __future__ import annotations

import unittest

from peppar_fix.wl_phase_admission_gate import WlPhaseAdmissionGate


class WlPhaseAdmissionGateBasicTest(unittest.TestCase):
    """Untracked SVs and insufficient-data behaviour."""

    def test_unknown_sv_returns_consistent(self):
        """Never-ingested SV: gate has no opinion, returns True."""
        gate = WlPhaseAdmissionGate()
        self.assertTrue(gate.is_phase_consistent("E07"))

    def test_below_min_samples_returns_consistent(self):
        """Fewer than min_samples: gate defers to MW, returns True."""
        gate = WlPhaseAdmissionGate(min_samples=10)
        for _ in range(5):
            gate.ingest("E07", 0.30)  # huge residual but only 5 samples
        self.assertTrue(gate.is_phase_consistent("E07"))

    def test_diagnostic_detail_none_when_insufficient(self):
        gate = WlPhaseAdmissionGate(min_samples=10)
        for _ in range(5):
            gate.ingest("E07", 0.30)
        self.assertIsNone(gate.evaluation_detail("E07"))


class WlPhaseAdmissionGateCleanArcTest(unittest.TestCase):
    """A clean float ambiguity has mm-cm post-fit phase residuals."""

    def test_mm_residuals_pass_gate(self):
        """30 epochs of mm-scale residuals: phase consistent → admit."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10,
                                    min_cohort_size=99)  # disable cohort
        # Simulate 30 epochs of ±5mm noise.
        seq = [0.001, -0.002, 0.003, -0.001, 0.002, -0.003,
               0.001, 0.000, -0.002, 0.001] * 3
        for r in seq:
            gate.ingest("E07", r)
        self.assertTrue(gate.is_phase_consistent("E07"))

    def test_clean_cohort_no_blocks(self):
        """3-SV cohort with mm-scale residuals: all admit."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10)
        for _ in range(15):
            gate.ingest("E07", 0.001)
            gate.ingest("E12", -0.002)
            gate.ingest("E33", 0.003)
        for sv in ("E07", "E12", "E33"):
            self.assertTrue(gate.is_phase_consistent(sv))


class WlPhaseAdmissionGateBlocksWrongFixTest(unittest.TestCase):
    """The detector's main job — block PR-driven false admissions."""

    def test_high_mean_residual_blocks(self):
        """Sustained 10cm mean residual > 5cm threshold → block."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10,
                                    min_cohort_size=99)
        for _ in range(15):
            gate.ingest("E07", 0.10)  # 10cm mean
        self.assertFalse(gate.is_phase_consistent("E07"))
        detail = gate.evaluation_detail("E07")
        self.assertIsNotNone(detail)
        self.assertGreater(abs(detail['mean_m']), 0.05)

    def test_high_std_residual_blocks(self):
        """Mean ~0 but std 10cm → block (volatile signal)."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10,
                                    min_cohort_size=99)
        seq = [0.10, -0.10] * 10  # zero-mean ±10cm oscillation
        for r in seq:
            gate.ingest("E07", r)
        self.assertFalse(gate.is_phase_consistent("E07"))

    def test_one_outlier_in_clean_cohort_blocks_outlier(self):
        """3-SV cohort, one with sustained high residual: outlier
        blocked, others admit."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10)
        for _ in range(15):
            gate.ingest("E07", 0.001)  # clean
            gate.ingest("E12", 0.002)  # clean
            gate.ingest("E33", 0.10)   # 10cm — wrong
        self.assertTrue(gate.is_phase_consistent("E07"))
        self.assertTrue(gate.is_phase_consistent("E12"))
        self.assertFalse(gate.is_phase_consistent("E33"))


class WlPhaseAdmissionGateCohortMedianTest(unittest.TestCase):
    """Cohort-median subtraction handles common-mode residuals."""

    def test_common_mode_offset_doesnt_block(self):
        """Receiver clock residual gives all SVs the same 8cm
        offset.  Cohort-median absorbs it; per-SV residuals after
        subtraction are at noise floor → admit."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10,
                                    min_cohort_size=2)
        for _ in range(15):
            # All three SVs see the same 8cm common-mode + tiny noise.
            gate.ingest("E07", 0.080 + 0.001)
            gate.ingest("E12", 0.080 - 0.002)
            gate.ingest("E33", 0.080 + 0.000)
        # Cohort median ≈ 8cm; residuals after subtraction are mm.
        for sv in ("E07", "E12", "E33"):
            self.assertTrue(gate.is_phase_consistent(sv),
                            f"{sv} should pass via cohort subtraction")

    def test_cohort_isolates_outlier_under_common_mode(self):
        """Common-mode offset on all SVs + one extra outlier:
        outlier still blocked after median subtraction."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, min_samples=10,
                                    min_cohort_size=2)
        for _ in range(15):
            gate.ingest("E07", 0.080 + 0.001)
            gate.ingest("E12", 0.080 + 0.002)
            gate.ingest("E33", 0.080 + 0.10)  # +10cm extra
        self.assertTrue(gate.is_phase_consistent("E07"))
        self.assertTrue(gate.is_phase_consistent("E12"))
        self.assertFalse(gate.is_phase_consistent("E33"))


class WlPhaseAdmissionGateLifecycleTest(unittest.TestCase):
    """Window rolloff and drop handling."""

    def test_evict_unobserved_drops_history(self):
        gate = WlPhaseAdmissionGate(min_samples=3)
        for _ in range(5):
            gate.ingest("E07", 0.001)
            gate.ingest("E12", 0.001)
        self.assertEqual(gate.n_tracking(), 2)
        gate.evict_unobserved({"E07"})  # E12 dropped from view
        self.assertEqual(gate.n_tracking(), 1)
        # E12 needs at least min_samples ingests after re-emerging
        # before the gate has an opinion.
        self.assertTrue(gate.is_phase_consistent("E12"))  # untracked

    def test_window_rolls_off_old_data(self):
        """Recovery from a bad arc: after threshold breach, new clean
        samples should clear the window and allow admission."""
        gate = WlPhaseAdmissionGate(threshold_m=0.05, window_epochs=10,
                                    min_samples=10, min_cohort_size=99)
        for _ in range(10):
            gate.ingest("E07", 0.10)  # block-trigger
        self.assertFalse(gate.is_phase_consistent("E07"))
        for _ in range(10):
            gate.ingest("E07", 0.001)  # window now full of clean data
        self.assertTrue(gate.is_phase_consistent("E07"))


class WlPhaseAdmissionGatePrPriorReservedTest(unittest.TestCase):
    """The pr_prior_sigma_m parameter is reserved for the Kalman+LAMBDA
    upgrade.  In v1 it's stored but unused — no behaviour change."""

    def test_pr_prior_sigma_does_not_affect_v1(self):
        gate_no_prior = WlPhaseAdmissionGate(threshold_m=0.05,
                                              min_samples=5,
                                              min_cohort_size=99)
        gate_with_prior = WlPhaseAdmissionGate(threshold_m=0.05,
                                                min_samples=5,
                                                min_cohort_size=99,
                                                pr_prior_sigma_m=2.0)
        for _ in range(5):
            gate_no_prior.ingest("E07", 0.10)
            gate_with_prior.ingest("E07", 0.10)
        self.assertEqual(
            gate_no_prior.is_phase_consistent("E07"),
            gate_with_prior.is_phase_consistent("E07"),
        )


class WlPhaseAdmissionGateSummaryTest(unittest.TestCase):
    def test_summary_text(self):
        gate = WlPhaseAdmissionGate(threshold_m=0.05, window_epochs=30,
                                    std_threshold_m=0.05)
        gate.ingest("E07", 0.001)
        gate.ingest("E12", 0.001)
        s = gate.summary()
        self.assertIn("2 SVs", s)
        self.assertIn("5.0cm", s)
        self.assertIn("30ep", s)


class WlPhaseAdmissionGateThresholdSplitTest(unittest.TestCase):
    """I-131954-main: mean and std thresholds are decoupled.  Mean
    is loosened to 15 cm (natural per-SV residual mean is 7-17 cm
    in normal operation); std stays tight at 5 cm so volatile
    arcs are still caught."""

    def test_default_thresholds_match_loosened_values(self):
        """Default constructor should use the post-I-131954 values:
        15 cm mean, 5 cm std."""
        gate = WlPhaseAdmissionGate()
        self.assertAlmostEqual(gate._threshold, 0.15)
        self.assertAlmostEqual(gate._std_threshold, 0.05)

    def test_natural_residual_mean_passes_default(self):
        """A 10 cm sustained mean (within the 7-17 cm normal range
        observed in the lab) passes the default gate."""
        gate = WlPhaseAdmissionGate(min_cohort_size=99)  # disable cohort
        for _ in range(15):
            gate.ingest("E07", 0.10)  # 10cm mean, near-zero std
        self.assertTrue(gate.is_phase_consistent("E07"))

    def test_high_mean_exceeds_default(self):
        """20 cm sustained mean exceeds the 15 cm default."""
        gate = WlPhaseAdmissionGate(min_cohort_size=99)
        for _ in range(15):
            gate.ingest("E07", 0.20)
        self.assertFalse(gate.is_phase_consistent("E07"))

    def test_high_std_blocks_at_default(self):
        """8 cm std (volatile arc) exceeds the 5 cm std default
        even though mean is well below the 15 cm mean default."""
        gate = WlPhaseAdmissionGate(min_cohort_size=99)
        seq = [0.08, -0.08] * 8  # zero-mean ±8cm volatility
        for r in seq:
            gate.ingest("E07", r)
        self.assertFalse(gate.is_phase_consistent("E07"))

    def test_explicit_std_threshold_overrides(self):
        """Caller can pass std_threshold_m to override the default."""
        gate = WlPhaseAdmissionGate(threshold_m=0.20,
                                    std_threshold_m=0.10,
                                    min_cohort_size=99)
        seq = [0.08, -0.08] * 8  # std ≈ 8cm; below 10 cm threshold
        for r in seq:
            gate.ingest("E07", r)
        self.assertTrue(gate.is_phase_consistent("E07"))


if __name__ == "__main__":
    unittest.main()
