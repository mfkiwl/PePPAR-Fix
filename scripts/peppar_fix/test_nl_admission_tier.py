"""Unit tests for NlAdmissionTier (I-172719)."""

from __future__ import annotations

import unittest

from peppar_fix.nl_admission_tier import (
    NlAdmissionTier,
    TIER_NEW,
    TIER_PROVISIONAL,
    TIER_TRUSTED,
)


class TierClassificationTest(unittest.TestCase):
    """tier_for: pure history-based classification, no proposed
    integer in scope."""

    def test_new_when_no_history(self):
        m = NlAdmissionTier(k_long=4)
        self.assertEqual(m.tier_for("G01"), TIER_NEW)

    def test_new_when_only_one_admission(self):
        """Single admission isn't enough for any track record."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_NEW)

    def test_provisional_at_two_same_admissions(self):
        """Two same-integer admissions clear the PROVISIONAL bar but
        not yet TRUSTED (count < k_long)."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)

    def test_provisional_at_two_adjacent(self):
        """Two adjacent integers (range = 1) → PROVISIONAL."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)

    def test_new_when_range_above_one(self):
        """Range > 1 over the deque → NEW (active wrong-int cycler)."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=15)
        self.assertEqual(m.tier_for("G01"), TIER_NEW)

    def test_trusted_at_k_long_same_integer(self):
        """k_long admissions all at the same integer → TRUSTED."""
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_TRUSTED)

    def test_provisional_at_k_long_with_range_one(self):
        """k_long admissions with range = 1 → still PROVISIONAL.
        TRUSTED demands range = 0."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)

    def test_history_window_is_k_long(self):
        """Only the last k_long integers are remembered."""
        m = NlAdmissionTier(k_long=3)
        for n in [5, 5, 5, 5, 99]:  # last 3 → [5, 5, 99] → range 94 → NEW
            m.note_admit("G01", n_nl=n)
        self.assertEqual(m.integer_history("G01"), [5, 5, 99])
        self.assertEqual(m.tier_for("G01"), TIER_NEW)


class TierForProposedTest(unittest.TestCase):
    """tier_for_proposed: governing tier given the proposed integer.
    Encodes the TRUSTED-with-different-integer demotion."""

    def test_trusted_with_matching_integer_stays_trusted(self):
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for_proposed("G01", 10), TIER_TRUSTED)

    def test_trusted_with_different_integer_demotes_to_new(self):
        """The load-bearing circuit-breaker: drift-induced
        wrong-integer attempts on TRUSTED SVs face the strict gate."""
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for_proposed("G01", 11), TIER_NEW)
        self.assertEqual(m.tier_for_proposed("G01", -5), TIER_NEW)

    def test_provisional_with_in_range_stays_provisional(self):
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        # Proposed 10: range stays {10,11} → PROVISIONAL
        self.assertEqual(m.tier_for_proposed("G01", 10), TIER_PROVISIONAL)
        # Proposed 11: range stays {10,11} → PROVISIONAL
        self.assertEqual(m.tier_for_proposed("G01", 11), TIER_PROVISIONAL)

    def test_provisional_with_out_of_range_demotes_to_new(self):
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        # Proposed 13: range [10,13] → 3 → would push to NEW
        self.assertEqual(m.tier_for_proposed("G01", 13), TIER_NEW)

    def test_new_stays_new_regardless_of_proposed(self):
        m = NlAdmissionTier(k_long=4)
        # No history → NEW
        self.assertEqual(m.tier_for_proposed("G01", 10), TIER_NEW)
        # One admission → still NEW
        m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for_proposed("G01", 10), TIER_NEW)
        self.assertEqual(m.tier_for_proposed("G01", 99), TIER_NEW)


class ThresholdLookupTest(unittest.TestCase):
    """Tier → admission threshold mapping."""

    def test_ratio_thresholds(self):
        self.assertAlmostEqual(NlAdmissionTier.ratio_threshold(TIER_TRUSTED),
                               3.0)
        self.assertAlmostEqual(
            NlAdmissionTier.ratio_threshold(TIER_PROVISIONAL), 5.0)
        self.assertAlmostEqual(NlAdmissionTier.ratio_threshold(TIER_NEW),
                               10.0)

    def test_pbootstrap_thresholds(self):
        self.assertAlmostEqual(
            NlAdmissionTier.pbootstrap_threshold(TIER_TRUSTED), 0.95)
        self.assertAlmostEqual(
            NlAdmissionTier.pbootstrap_threshold(TIER_PROVISIONAL), 0.99)
        self.assertAlmostEqual(
            NlAdmissionTier.pbootstrap_threshold(TIER_NEW), 0.999)

    def test_strictest_to_loosest_ordering(self):
        """Sanity: TRUSTED is loosest, NEW is strictest, on both axes."""
        self.assertLess(
            NlAdmissionTier.ratio_threshold(TIER_TRUSTED),
            NlAdmissionTier.ratio_threshold(TIER_PROVISIONAL))
        self.assertLess(
            NlAdmissionTier.ratio_threshold(TIER_PROVISIONAL),
            NlAdmissionTier.ratio_threshold(TIER_NEW))
        self.assertLess(
            NlAdmissionTier.pbootstrap_threshold(TIER_TRUSTED),
            NlAdmissionTier.pbootstrap_threshold(TIER_PROVISIONAL))
        self.assertLess(
            NlAdmissionTier.pbootstrap_threshold(TIER_PROVISIONAL),
            NlAdmissionTier.pbootstrap_threshold(TIER_NEW))


class AdmitsAtTest(unittest.TestCase):
    """Composite admission decision used at the resolver hook."""

    def test_trusted_with_relaxed_lambda_stats_admits(self):
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        # ratio=3.5 ≥ 3.0; P=0.96 ≥ 0.95 → admit at TRUSTED
        ok, tier = m.admits_at("G01", 10, ratio=3.5, p_bootstrap=0.96)
        self.assertTrue(ok)
        self.assertEqual(tier, TIER_TRUSTED)

    def test_trusted_with_different_integer_blocked(self):
        """Same LAMBDA stats that admit a TRUSTED-matching integer
        get blocked when the proposed integer drifts (gate becomes
        the NEW tier — 10.0/0.999)."""
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        # ratio=3.5, P=0.96 — passes TRUSTED but fails NEW
        ok, tier = m.admits_at("G01", 11, ratio=3.5, p_bootstrap=0.96)
        self.assertFalse(ok)
        self.assertEqual(tier, TIER_NEW)

    def test_new_sv_needs_strict_stats(self):
        m = NlAdmissionTier(k_long=4)
        # No history — NEW.  ratio=8.0 fails NEW's 10.0 bar.
        ok, tier = m.admits_at("G01", 10, ratio=8.0, p_bootstrap=0.999)
        self.assertFalse(ok)
        self.assertEqual(tier, TIER_NEW)
        # ratio=10.5 passes
        ok, tier = m.admits_at("G01", 10, ratio=10.5, p_bootstrap=0.999)
        self.assertTrue(ok)
        self.assertEqual(tier, TIER_NEW)

    def test_provisional_with_borderline_pbar(self):
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)
        # ratio=5.0 OK, P=0.985 fails 0.99 bar
        ok, tier = m.admits_at("G01", 10, ratio=5.0, p_bootstrap=0.985)
        self.assertFalse(ok)
        self.assertEqual(tier, TIER_PROVISIONAL)
        # P=0.992 OK
        ok, tier = m.admits_at("G01", 10, ratio=5.0, p_bootstrap=0.992)
        self.assertTrue(ok)


class ForgetHistoryTest(unittest.TestCase):
    """Trust decay on real cycle slip."""

    def test_forget_history_resets_tier_to_new(self):
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_TRUSTED)
        m.forget_history("G01")
        self.assertEqual(m.tier_for("G01"), TIER_NEW)
        self.assertEqual(m.integer_history("G01"), [])

    def test_forget_history_idempotent_on_unknown_sv(self):
        m = NlAdmissionTier(k_long=4)
        m.forget_history("G99")  # no error
        self.assertEqual(m.tier_for("G99"), TIER_NEW)


class IsolationAndDiagnosticsTest(unittest.TestCase):
    """SV independence + summary."""

    def test_multiple_svs_independent(self):
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        m.note_admit("G02", n_nl=20)
        self.assertEqual(m.tier_for("G01"), TIER_TRUSTED)
        self.assertEqual(m.tier_for("G02"), TIER_NEW)
        # forget G01 doesn't disturb G02
        m.forget_history("G01")
        m.note_admit("G02", n_nl=20)
        self.assertEqual(m.tier_for("G02"), TIER_PROVISIONAL)

    def test_n_tracking_and_summary(self):
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("E11", n_nl=20)
        self.assertEqual(m.n_tracking(), 2)
        s = m.summary()
        self.assertIn("tracking 2 SVs", s)
        self.assertIn("k_long=4", s)


if __name__ == "__main__":
    unittest.main()
