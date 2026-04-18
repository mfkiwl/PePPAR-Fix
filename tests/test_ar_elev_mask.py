#!/usr/bin/env python3
"""Unit tests for NarrowLaneResolver.attempt() AR elevation mask gate.

The gate keeps low-elevation satellites out of the NL candidate pool
(while they stay in the PPP float filter for observations).  Matches
RTKLIB's `arelmask` semantics.

Run: python3 -m unittest tests.test_ar_elev_mask
"""

import sys
import unittest
from pathlib import Path

import numpy as np

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / 'scripts'))

from ppp_ar import MelbourneWubbenaTracker, NarrowLaneResolver, C
from solve_ppp import N_BASE


# GPS L1 / L5
_F1 = 1_575.42e6
_F2 = 1_176.45e6


class _FakePPPFilter:
    """Minimal surface for NLResolver.attempt()."""

    def __init__(self, svs):
        self.sv_to_idx = {sv: i for i, sv in enumerate(svs)}
        n_amb = len(svs)
        n = N_BASE + n_amb
        self.x = np.zeros(n, dtype=float)
        # Large ambiguity values so fractional parts don't accidentally
        # line up with integers.
        for sv, i in self.sv_to_idx.items():
            self.x[N_BASE + i] = 1_234.5 + i
        self.P = np.eye(n) * 1e-6   # trivial, ambiguities look ultra-tight

    def sat_position(self, *_):
        return None, None


def _prime_mw(mw, sv):
    """Fill a tracker's state so get_wl returns a non-None value."""
    # Directly set state to a "fixed" entry — bypass the 60-epoch
    # warm-up that MelbourneWubbenaTracker.update requires.
    from collections import deque
    mw._state[sv] = {
        'mw_avg': 0.0,
        'n_epochs': 100,
        'n_wl': 5,
        'fixed': True,
        'f1': _F1,
        'f2': _F2,
        'resid_deque': deque(maxlen=60),
        'resid_std_cyc': 0.1,
    }


class TestArElevMask(unittest.TestCase):

    def _setup(self, elev_mask_deg, elevations):
        svs = list(elevations.keys())
        filt = _FakePPPFilter(svs)
        mw = MelbourneWubbenaTracker()
        for sv in svs:
            _prime_mw(mw, sv)
        nl = NarrowLaneResolver(ar_elev_mask_deg=elev_mask_deg)
        return filt, mw, nl

    def test_high_elev_svs_enter_candidate_pool(self):
        """All SVs above the mask survive the pre-candidate gate."""
        elevations = {'E01': 45.0, 'E02': 60.0, 'E03': 25.0, 'E04': 30.0}
        filt, mw, nl = self._setup(20.0, elevations)
        # attempt() will try LAMBDA/rounding and may or may not fix;
        # we only care that the elev gate didn't mask any of these out.
        nl.attempt(filt, mw, elevations=elevations)
        self.assertFalse(hasattr(nl, 'last_skipped_by_elev')
                         and nl.last_skipped_by_elev > 0)

    def test_low_elev_svs_skipped(self):
        """SVs below the mask are skipped (pre-candidate)."""
        elevations = {'E01': 45.0, 'E02': 5.0, 'E03': 15.0, 'E04': 30.0}
        filt, mw, nl = self._setup(20.0, elevations)
        nl.attempt(filt, mw, elevations=elevations)
        self.assertEqual(nl.last_skipped_by_elev, 2)   # E02 and E03

    def test_mask_at_zero_disables_gate(self):
        """ar_elev_mask_deg=0 lets every SV through."""
        elevations = {'E01': 3.0, 'E02': 5.0, 'E03': 8.0}
        filt, mw, nl = self._setup(0.0, elevations)
        nl.attempt(filt, mw, elevations=elevations)
        self.assertFalse(hasattr(nl, 'last_skipped_by_elev')
                         and nl.last_skipped_by_elev > 0)

    def test_missing_elevation_does_not_skip(self):
        """SVs with no elevation entry are NOT skipped — we refuse to
        silently exclude a satellite when the caller couldn't compute
        its elevation.  The slip-monitor path has the same property."""
        elevations = {'E01': 45.0}   # only E01 has elev
        filt = _FakePPPFilter(['E01', 'E02'])
        mw = MelbourneWubbenaTracker()
        _prime_mw(mw, 'E01')
        _prime_mw(mw, 'E02')
        nl = NarrowLaneResolver(ar_elev_mask_deg=20.0)
        nl.attempt(filt, mw, elevations=elevations)
        # E01 passes, E02 has no elev → not skipped by the gate.
        self.assertFalse(hasattr(nl, 'last_skipped_by_elev')
                         and nl.last_skipped_by_elev > 0)

    def test_no_elevations_dict_disables_gate(self):
        """Calling attempt() without elevations (backward-compat)."""
        elevations = {'E01': 45.0, 'E02': 5.0}
        filt, mw, nl = self._setup(20.0, elevations)
        nl.attempt(filt, mw)   # no elevations kwarg
        self.assertFalse(hasattr(nl, 'last_skipped_by_elev')
                         and nl.last_skipped_by_elev > 0)


if __name__ == '__main__':
    unittest.main()
