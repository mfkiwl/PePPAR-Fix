"""Unit tests for AntPosEstThread._null_mode_sigma_max.

Diagnostic-only helper that extracts the largest σ from P's
base-state block.  Used as a null-mode excitation proxy in the
[AntPosEst] status line.
"""

from __future__ import annotations

import math
import os
import sys
import unittest

import numpy as np

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)


class _StubFilter:
    """Minimal stand-in with .x and .P attributes.  AntPosEstThread's
    helper only reads P; x is included so shape checks work."""

    def __init__(self, n_base=7, n_amb=5):
        n = n_base + n_amb
        self.x = np.zeros(n)
        self.P = np.eye(n) * 0.01   # well-conditioned, σ=0.1 m per state

    def inflate_state(self, idx, variance):
        self.P[idx, idx] = variance


class NullModeSigmaMaxTest(unittest.TestCase):

    def _max_sigma(self, filt):
        # Lazy import of the engine module — it has heavy dependencies
        # and the import cost isn't worth paying at module load time.
        import peppar_fix_engine
        return peppar_fix_engine.AntPosEstThread._null_mode_sigma_max(filt)

    def test_none_filter_returns_none(self):
        self.assertIsNone(self._max_sigma(None))

    def test_well_conditioned_returns_small(self):
        """Uniform P diagonal = 0.01 → all eigenvalues 0.01 →
        max σ = 0.1 m."""
        f = _StubFilter()
        sigma = self._max_sigma(f)
        self.assertIsNotNone(sigma)
        self.assertAlmostEqual(sigma, 0.1, places=5)

    def test_inflated_clock_state_detected(self):
        """Artificially inflate the clock state's variance to 100 m²
        (σ=10 m).  Max σ should detect this."""
        f = _StubFilter()
        f.inflate_state(3, 100.0)   # clock idx = 3
        sigma = self._max_sigma(f)
        self.assertAlmostEqual(sigma, 10.0, places=3)

    def test_inflated_ztd_detected(self):
        """Same for ZTD state."""
        f = _StubFilter()
        f.inflate_state(6, 25.0)    # ZTD idx = 6
        sigma = self._max_sigma(f)
        self.assertAlmostEqual(sigma, 5.0, places=3)

    def test_inflated_ambiguity_ignored(self):
        """Ambiguity states (index ≥ N_BASE=7) are NOT in the
        monitored block — their covariance doesn't inflate the
        reported σ_max."""
        f = _StubFilter(n_amb=5)
        f.inflate_state(7, 1e6)     # amb[0], outside the block
        sigma = self._max_sigma(f)
        # Should still see the uniform 0.1 m of the base block.
        self.assertAlmostEqual(sigma, 0.1, places=5)

    def test_near_singular_gracefully_handled(self):
        """If P has tiny negative eigenvalues from numerical error,
        the clip-and-sqrt returns a non-NaN result."""
        f = _StubFilter()
        # Force asymmetric noise that could produce near-zero eigs.
        f.P[3, 3] = 0.0    # clock variance exactly zero
        sigma = self._max_sigma(f)
        # Either 0.1 (from the rest of the diagonal) or 0.0 — must
        # not be NaN / None.
        self.assertIsNotNone(sigma)
        self.assertFalse(math.isnan(sigma))

    def test_correlation_captured(self):
        """A large off-diagonal cross-correlation produces a larger
        eigenvalue than any individual diagonal entry.  This is the
        null-mode signature — the filter is uncertain about a
        DIRECTION spanning multiple states, not any one state."""
        f = _StubFilter()
        # Fully-correlated clock-ZTD direction at σ_each = 1 m:
        f.P[3, 3] = 1.0
        f.P[6, 6] = 1.0
        f.P[3, 6] = 1.0    # correlation ρ = 1.0
        f.P[6, 3] = 1.0
        sigma = self._max_sigma(f)
        # The matrix has eigenvalues {2.0, 0.0} in this 2-state
        # subblock; combined with uniform 0.01 elsewhere, max
        # eig = 2.0 → σ = √2 ≈ 1.414 m.
        self.assertGreater(sigma, 1.3)
        self.assertLess(sigma, 1.5)

    def test_dim_too_small(self):
        """< 4 base states → None (insufficient for null-mode check)."""
        f = _StubFilter(n_base=3, n_amb=0)
        # Manually resize to fewer than 4 total states.
        f.x = f.x[:3]
        f.P = f.P[:3, :3]
        self.assertIsNone(self._max_sigma(f))


if __name__ == "__main__":
    unittest.main()
