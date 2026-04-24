"""Tests for PPPFilter.clock_model scaffold.

Covers:
- Default mode preserves the legacy random-walk behavior bit-exactly.
- 'calibrated_white' mode computes Q_clk from the ADEV formula.
- Invalid clock_model raises ValueError.
- No state-layout change between modes (N_BASE invariant).

See docs/clock-state-modeling.md option (A).
"""

from __future__ import annotations

import math
import os
import sys
import unittest

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from solve_ppp import (                       # noqa: E402
    PPPFilter, CLOCK_MODELS, CLOCK_RW_Q_DEFAULT,
    F9T_TCXO_ADEV_1S_DEFAULT, IDX_CLK, N_BASE,
)
from solve_pseudorange import C               # noqa: E402


class ClockModelDefaultsTest(unittest.TestCase):

    def test_default_is_random_walk(self):
        f = PPPFilter()
        self.assertEqual(f.clock_model, 'random_walk')
        self.assertEqual(f.rx_tcxo_adev_1s, F9T_TCXO_ADEV_1S_DEFAULT)

    def test_random_walk_q_matches_legacy(self):
        f = PPPFilter()
        self.assertEqual(f._q_clk(), CLOCK_RW_Q_DEFAULT)

    def test_invalid_model_rejected(self):
        with self.assertRaises(ValueError):
            PPPFilter(clock_model='two_state')
        with self.assertRaises(ValueError):
            PPPFilter(clock_model='')


class CalibratedWhiteTest(unittest.TestCase):

    def test_q_clk_from_adev(self):
        adev = 1e-10
        f = PPPFilter(clock_model='calibrated_white',
                      rx_tcxo_adev_1s=adev)
        expected = (C * adev) ** 2
        self.assertAlmostEqual(f._q_clk(), expected, delta=expected * 1e-12)

    def test_default_adev_is_pessimistic(self):
        # 1e-8 is ~100x looser than our measured TCXO (σ_y(1s) ≈ 2e-9).
        # Intentional safety margin until lab-local F9T characterization.
        f = PPPFilter(clock_model='calibrated_white')
        self.assertAlmostEqual(f.rx_tcxo_adev_1s, 1e-8)

    def test_calibrated_white_tightens_q_vs_random_walk(self):
        # Calibrated mode MUST produce a smaller Q than random_walk for
        # any realistic TCXO; otherwise it has no effect on the null
        # mode.  Guards against an accidentally-loose default.
        f_rw = PPPFilter()
        f_cw = PPPFilter(clock_model='calibrated_white',
                          rx_tcxo_adev_1s=1e-8)  # pessimistic default
        self.assertLess(f_cw._q_clk(), f_rw._q_clk())

    def test_q_clk_scales_with_adev_squared(self):
        f1 = PPPFilter(clock_model='calibrated_white', rx_tcxo_adev_1s=1e-10)
        f2 = PPPFilter(clock_model='calibrated_white', rx_tcxo_adev_1s=2e-10)
        self.assertAlmostEqual(f2._q_clk() / f1._q_clk(), 4.0, places=6)


class StateLayoutInvarianceTest(unittest.TestCase):

    def test_n_base_unchanged_across_modes(self):
        # The scaffold must NOT change state layout; N_BASE is module-
        # level and consumed by ppp_ar.py, cycle_slip.py, and every
        # join-test / bootstrap-gate test.  Adding a CLK_RATE state
        # must happen in a separate refactor.
        f_rw = PPPFilter()
        f_cw = PPPFilter(clock_model='calibrated_white')
        f_rw.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        f_cw.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        self.assertEqual(f_rw.x.shape[0], N_BASE)
        self.assertEqual(f_cw.x.shape[0], N_BASE)
        self.assertEqual(f_rw.P.shape, (N_BASE, N_BASE))
        self.assertEqual(f_cw.P.shape, (N_BASE, N_BASE))


class PredictIntegrationTest(unittest.TestCase):
    """Drive a few predict() steps in each mode and verify the P[CLK,CLK]
    growth matches _q_clk() · dt."""

    def _run(self, clock_model, adev=None, dt=1.0, steps=5):
        kwargs = {'clock_model': clock_model}
        if adev is not None:
            kwargs['rx_tcxo_adev_1s'] = adev
        f = PPPFilter(**kwargs)
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        # Pin position variance so the predict()'s adaptive pos_sigma
        # branch picks the converged regime, keeping Q_pos small and
        # isolating IDX_CLK growth.
        f.P[0, 0] = 1e-4
        f.P[1, 1] = 1e-4
        f.P[2, 2] = 1e-4
        p0 = f.P[IDX_CLK, IDX_CLK]
        for _ in range(steps):
            f.predict(dt)
        return f.P[IDX_CLK, IDX_CLK] - p0

    def test_random_walk_growth(self):
        grew = self._run('random_walk', dt=1.0, steps=10)
        self.assertAlmostEqual(grew, CLOCK_RW_Q_DEFAULT * 10 * 1.0,
                                delta=1e-6)

    def test_calibrated_white_growth(self):
        adev = 1e-10
        grew = self._run('calibrated_white', adev=adev, dt=1.0, steps=10)
        expected = (C * adev) ** 2 * 10 * 1.0
        self.assertAlmostEqual(grew, expected, delta=expected * 1e-4)


if __name__ == '__main__':
    unittest.main()
