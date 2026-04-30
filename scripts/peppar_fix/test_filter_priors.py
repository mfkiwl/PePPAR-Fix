"""Tests for PPPFilter physics-tight priors (I-024532-charlie).

Per I-133648-main consensus (Q1-Q4 + Bravo's design):
  - Initial position σ from caller (NAV2.hAcc), default 10 m
    (was 100 m — wide enough to absorb systematic δ_PB into position).
  - Initial residual ZTD σ = 0.2 m (was 0.5 m — wide enough to absorb
    SSR phase-bias residuals into ZTD, producing the doom-loop
    cascade observed on TimeHat 2026-04-29 overnight, 36/73 trips).
  - Q[IDX_ZTD] random-walk PSD = 1 cm² / min so the filter can
    follow real weather without resisting legitimate ZTD movement.

These tests pin the physics-tight values to lock down the scope of
the I-024532-charlie change.  Loosening any of them in the future
should require revisiting the integrity-trip cascade evidence.
"""

from __future__ import annotations

import os
import sys
import unittest

import numpy as np

_SCRIPTS_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from solve_ppp import PPPFilter, IDX_ZTD


class InitialPriorTest(unittest.TestCase):
    """PPPFilter.initialize prior values."""

    def test_default_position_sigma_is_10m(self):
        """The default seeds at 10 m σ — appropriate for an SPP-grade
        seed.  Not as wide as the old 100 m default."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        for axis in range(3):
            self.assertAlmostEqual(f.P[axis, axis], 10.0**2)

    def test_default_ztd_sigma_is_200mm(self):
        """The default residual-ZTD σ is 200 mm — physical envelope of
        residual wet delay at sea level."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        self.assertAlmostEqual(f.P[IDX_ZTD, IDX_ZTD], 0.2**2)

    def test_pos_sigma_m_override(self):
        """Caller overrides pos_sigma_m (engine passes NAV2.hAcc)."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0, pos_sigma_m=1.5)
        for axis in range(3):
            self.assertAlmostEqual(f.P[axis, axis], 1.5**2)

    def test_ztd_sigma_m_override(self):
        """Caller can supply a tighter or looser ztd_sigma_m."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0, ztd_sigma_m=0.1)
        self.assertAlmostEqual(f.P[IDX_ZTD, IDX_ZTD], 0.1**2)
        self.assertAlmostEqual(f._ztd_sigma_m, 0.1)


class ZtdProcessNoiseTest(unittest.TestCase):
    """Q[IDX_ZTD] in the default RW (non-PWC) regime."""

    def test_random_walk_psd_is_1cm2_per_minute(self):
        """The random-walk PSD admits ~1 cm² per minute → ~7.7 cm/hour
        σ growth.  Physical bound for real-weather ZTD movement
        without standing-baseline-loose."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0)
        # Snapshot P[ZTD] before predict; predict for 60 s; check
        # that ΔP[ZTD] = (1.29e-3)² · 60 = 1e-4 m².
        p_before = float(f.P[IDX_ZTD, IDX_ZTD])
        # Pin position so the predict's adaptive q_pos branch picks
        # the converged regime — keeps ΔP[ZTD] isolated from cross
        # terms.
        f.P[0, 0] = 1e-4
        f.P[1, 1] = 1e-4
        f.P[2, 2] = 1e-4
        f.predict(dt=60.0)
        p_after = float(f.P[IDX_ZTD, IDX_ZTD])
        delta = p_after - p_before
        # Expect ΔP ≈ (1.29e-3)² · 60 = 1.0e-4 m² (within 5%).
        self.assertAlmostEqual(delta, 1.0e-4, delta=5e-6)

    def test_pwc_segment_boundary_uses_instance_ztd_sigma(self):
        """PWC-N segment boundary inflates P[IDX_ZTD] to
        self._ztd_sigma_m² (not the old hardcoded 0.5²).  Verifies
        the per-instance value is propagated."""
        f = PPPFilter()
        f.initialize([1e6, 0.0, 0.0], clock_m=0.0, ztd_sigma_m=0.15)
        f.ZTD_PWC_WINDOW_S = 60.0
        # Drive past one segment boundary.
        f.P[0, 0] = 1e-4
        f.P[1, 1] = 1e-4
        f.P[2, 2] = 1e-4
        # Walk the variance up first so we can see the inflate clamp it.
        f.P[IDX_ZTD, IDX_ZTD] = 9.0  # 3-m σ — much larger than 0.15²
        f.predict(dt=60.0)
        # Boundary fired: P should be reset to ztd_sigma_m².
        self.assertAlmostEqual(f.P[IDX_ZTD, IDX_ZTD], 0.15**2)


if __name__ == "__main__":
    unittest.main()
