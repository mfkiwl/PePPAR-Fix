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


class PastAnchoredShortcutTest(unittest.TestCase):
    """ANCHORED-as-trust-shortcut (I-004810-main).

    Past-anchored SVs that would otherwise classify as NEW (empty,
    single, or wide-range history) get bumped one step up to
    PROVISIONAL.  Slip-driven forget_history clears the flag.
    """

    def test_note_anchored_lifts_single_admission_to_provisional(self):
        """SV admitted once, reached ANCHORED, evicted (no slip),
        re-evaluated.  Without the boost it would be NEW; with the
        boost it's PROVISIONAL."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_NEW)
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)

    def test_note_anchored_does_not_promote_above_provisional(self):
        """A past-anchored SV that already has range≤1 history stays
        PROVISIONAL — the shortcut never grants TRUSTED.  TRUSTED
        requires k_long matching admits."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("G01", n_nl=11)  # PROVISIONAL via range=1
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)

    def test_note_anchored_does_not_demote_trusted(self):
        """A TRUSTED SV stays TRUSTED — the shortcut only fires when
        base would be NEW."""
        m = NlAdmissionTier(k_long=4)
        for _ in range(4):
            m.note_admit("G01", n_nl=10)
        self.assertEqual(m.tier_for("G01"), TIER_TRUSTED)
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_TRUSTED)

    def test_forget_history_clears_past_anchored(self):
        """Real cycle slip wipes both integer history and the
        past-anchored flag — the SV climbs from NEW again."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)
        m.forget_history("G01")
        # No history → tier_for would be NEW.  Past-anchored flag is
        # cleared, so the boost no longer fires.
        self.assertEqual(m.tier_for("G01"), TIER_NEW)
        self.assertEqual(m.integer_history("G01"), [])

    def test_note_anchored_idempotent(self):
        """Calling note_anchored repeatedly is safe and doesn't
        compound."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_anchored("G01")
        m.note_anchored("G01")
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)
        # Single forget_history still clears it cleanly.
        m.forget_history("G01")
        self.assertEqual(m.tier_for("G01"), TIER_NEW)

    def test_proposed_range_check_defeats_shortcut(self):
        """A past-anchored SV whose tier_for says PROVISIONAL still
        faces NEW gate at tier_for_proposed when the proposed integer
        would push the (history ∪ {proposed}) range past 1.  This is
        the wandering-SV protection."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_anchored("G01")
        # Same integer: range=0 ≤ 1 → PROVISIONAL gate
        self.assertEqual(m.tier_for_proposed("G01", 10), TIER_PROVISIONAL)
        # Adjacent: range=1 ≤ 1 → PROVISIONAL gate
        self.assertEqual(m.tier_for_proposed("G01", 11), TIER_PROVISIONAL)
        # Wild: range=89 → NEW gate (boost defeated)
        self.assertEqual(m.tier_for_proposed("G01", 99), TIER_NEW)

    def test_note_anchored_independent_per_sv(self):
        """Past-anchored is per-SV.  Anchoring G01 doesn't affect E11."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_admit("E11", n_nl=20)
        m.note_anchored("G01")
        self.assertEqual(m.tier_for("G01"), TIER_PROVISIONAL)
        self.assertEqual(m.tier_for("E11"), TIER_NEW)

    def test_admits_at_uses_boosted_gate_after_anchored(self):
        """admits_at, the composite gate used by the resolver, picks
        up the boosted PROVISIONAL bar (5.0/0.99) instead of the
        strict NEW bar (10.0/0.999) on a past-anchored re-admit."""
        m = NlAdmissionTier(k_long=4)
        m.note_admit("G01", n_nl=10)
        m.note_anchored("G01")
        # Stats (5.5, 0.992) clear PROVISIONAL but fail NEW
        ok, tier = m.admits_at("G01", 10, ratio=5.5, p_bootstrap=0.992)
        self.assertTrue(ok)
        self.assertEqual(tier, TIER_PROVISIONAL)
        # Without the boost (no note_anchored): same stats fail NEW
        m2 = NlAdmissionTier(k_long=4)
        m2.note_admit("G01", n_nl=10)
        ok, tier = m2.admits_at("G01", 10, ratio=5.5, p_bootstrap=0.992)
        self.assertFalse(ok)
        self.assertEqual(tier, TIER_NEW)


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
