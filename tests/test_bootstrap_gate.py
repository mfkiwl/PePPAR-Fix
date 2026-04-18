#!/usr/bin/env python3
"""Unit tests for the Phase-1 convergence gate (W1, W2, W3).

Matches the behavior documented in
docs/position-bootstrap-reliability-plan.md.

Run: python3 -m unittest tests.test_bootstrap_gate
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from solve_ppp import SIGMA_P_IF, N_BASE
from peppar_fix.bootstrap_gate import (
    nav2_agrees, residuals_consistent, scrub_for_retry,
)


# ── fakes ─────────────────────────────────────────────────────────── #

class _FakeFilter:
    """Minimal PPPFilter surface for the bootstrap-gate helpers."""

    def __init__(self, n_base=N_BASE, n_amb=4):
        self.x = np.zeros(n_base + n_amb, dtype=float)
        self.x[0:3] = np.array([100.0, 200.0, 300.0], dtype=float)
        self.x[n_base:] = np.array([10.0, 20.0, 30.0, 40.0])
        self.P = np.eye(n_base + n_amb, dtype=float) * 0.001
        self.prev_obs = {'G01': {'phi_if_m': 1.0}}
        self.last_residual_labels = []

    def _pos(self, ecef):
        self.x[0:3] = np.asarray(ecef, dtype=float)


# ── W1 ────────────────────────────────────────────────────────────── #

class TestResidualsConsistent(unittest.TestCase):
    """PR-residual RMS and max checks."""

    def _run(self, pr_vals, phi_vals=None, **kwargs):
        filt = _FakeFilter()
        phi_vals = phi_vals or []
        labels = [(f"sv{i}", 'pr', 45.0) for i in range(len(pr_vals))]
        labels += [(f"sv{i}", 'phi', 45.0) for i in range(len(phi_vals))]
        filt.last_residual_labels = labels
        resid = np.array(list(pr_vals) + list(phi_vals), dtype=float)
        return residuals_consistent(filt, resid, SIGMA_P_IF, **kwargs)

    def test_clean_residuals_pass(self):
        # RMS ~ 1 m, max 1.5 m — well under 2σ=6m.
        ok, d = self._run([0.5, -1.0, 1.2, -0.8, 1.5])
        self.assertTrue(ok)
        self.assertLess(d['rms_pr'], 1.5)
        self.assertLess(d['max_pr'], 2.0)

    def test_high_rms_fails(self):
        # All residuals at 5 m → rms=5m > 2×SIGMA_P_IF/sqrt(n) path
        # but specifically > 2×3=6m? No, rms=5 < 6 so passes rms gate.
        # Need higher to exercise the RMS threshold.
        ok, d = self._run([7.0, -7.0, 7.0, -7.0])   # rms=7 > 6 threshold
        self.assertFalse(ok)
        self.assertIn('rms_pr', d['reason'])

    def test_single_outlier_fails_max(self):
        # Enough inliers to keep RMS under the 6 m floor so the max
        # gate is the one that rejects.
        vals = [0.3, -0.5, 0.4, 0.8, -0.6, 0.2, 0.7, -0.4,
                0.5, -0.3, 0.6, 0.4, 0.2, -0.7, 0.3, 16.0]
        ok, d = self._run(vals)
        self.assertFalse(ok)
        self.assertLess(d['rms_pr'], 6.0)
        self.assertGreaterEqual(d['max_pr'], 15.0)
        self.assertIn('max', d['reason'])

    def test_no_labels_pass_silently(self):
        """If the filter didn't populate last_residual_labels, don't
        block convergence — this is a pre-existing edge case."""
        filt = _FakeFilter()
        filt.last_residual_labels = None
        resid = np.array([100.0, 100.0], dtype=float)   # would fail if checked
        ok, _ = residuals_consistent(filt, resid, SIGMA_P_IF)
        self.assertTrue(ok)

    def test_phi_residuals_ignored(self):
        """phi residuals are small (~1 cm) and dominated by ambiguity
        bias during cold-start; only PR is checked.  A huge phi value
        must NOT block convergence when PR is clean."""
        ok, _ = self._run(pr_vals=[0.5, -0.7, 1.0],
                          phi_vals=[100.0, -100.0])   # nonsense phi
        self.assertTrue(ok)


# ── W2 ────────────────────────────────────────────────────────────── #

class TestNav2Agrees(unittest.TestCase):
    """Horizontal-only disagreement check."""

    # Somewhere at ~ Earth's surface, 41.84°N 88.10°W (lab coords).
    _pos_base = np.array([157468.4, -4756190.5, 4232770.7])

    def _opinion(self, offset_ecef):
        # opinion['ecef'] is the full ECEF vector.
        return {'ecef': self._pos_base + offset_ecef, 'h_acc_m': 0.8}

    def test_no_opinion_passes(self):
        ok, d = nav2_agrees(self._pos_base, None)
        self.assertTrue(ok)
        self.assertFalse(d['available'])

    def test_zero_offset_passes(self):
        ok, d = nav2_agrees(self._pos_base, self._opinion(np.zeros(3)))
        self.assertTrue(ok)
        self.assertLess(d['disp_h_m'], 0.01)

    def test_vertical_offset_ignored(self):
        # A 30 m pure vertical offset must not fail the horizontal gate.
        up = self._pos_base / np.linalg.norm(self._pos_base)
        ok, d = nav2_agrees(self._pos_base, self._opinion(up * 30.0))
        self.assertTrue(ok)
        self.assertGreater(d['disp_v_m'], 25.0)
        self.assertLess(d['disp_h_m'], 1.0)

    def test_horizontal_fail(self):
        # Offset perpendicular to up → all horizontal.  Pick any
        # orthogonal direction.
        up = self._pos_base / np.linalg.norm(self._pos_base)
        # Find a vector orthogonal to up: take (1,0,0) minus its up component.
        e = np.array([1.0, 0.0, 0.0])
        e_h = e - np.dot(e, up) * up
        e_h /= np.linalg.norm(e_h)
        ok, d = nav2_agrees(self._pos_base, self._opinion(e_h * 10.0),
                            horiz_m=5.0)
        self.assertFalse(ok)
        self.assertAlmostEqual(d['disp_h_m'], 10.0, places=1)


# ── W3 ────────────────────────────────────────────────────────────── #

class TestScrubForRetry(unittest.TestCase):

    def test_position_covariance_inflated(self):
        filt = _FakeFilter()
        scrub_for_retry(filt, N_BASE)
        self.assertAlmostEqual(filt.P[0, 0], 100.0 ** 2)
        self.assertAlmostEqual(filt.P[1, 1], 100.0 ** 2)
        self.assertAlmostEqual(filt.P[2, 2], 100.0 ** 2)
        # Off-diagonals zeroed.
        self.assertEqual(filt.P[0, 1], 0.0)
        self.assertEqual(filt.P[0, 4], 0.0)

    def test_ambiguity_covariance_inflated(self):
        filt = _FakeFilter()
        scrub_for_retry(filt, N_BASE)
        # Every ambiguity diagonal is now (50 m)^2; off-diagonals zero.
        for i in range(N_BASE, filt.P.shape[0]):
            self.assertAlmostEqual(filt.P[i, i], 50.0 ** 2)
            for j in range(filt.P.shape[0]):
                if i != j:
                    self.assertEqual(filt.P[i, j], 0.0)

    def test_clock_isb_ztd_preserved(self):
        """States between position (0:3) and ambiguities (N_BASE:) must
        keep their values and covariance after scrub — these don't
        carry position bias and re-learning them costs minutes."""
        filt = _FakeFilter()
        mid = N_BASE - 3
        if mid > 0:
            filt.x[3:N_BASE] = np.arange(mid, dtype=float) + 0.123
            filt.P[3, 3] = 0.042
        scrub_for_retry(filt, N_BASE)
        if mid > 0:
            self.assertAlmostEqual(filt.x[3], 0.123)
            self.assertAlmostEqual(filt.P[3, 3], 0.042)

    def test_reseed_applied(self):
        filt = _FakeFilter()
        new_pos = np.array([999.0, 888.0, 777.0])
        scrub_for_retry(filt, N_BASE, reseed_ecef=new_pos)
        np.testing.assert_array_almost_equal(filt.x[0:3], new_pos)

    def test_prev_obs_cleared(self):
        filt = _FakeFilter()
        self.assertTrue(filt.prev_obs)
        scrub_for_retry(filt, N_BASE)
        self.assertFalse(filt.prev_obs)


if __name__ == '__main__':
    unittest.main()
