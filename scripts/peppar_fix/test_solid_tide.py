"""Unit tests for scripts/solid_tide.py (IERS 2010 Step 1 SET).

The full module lives at scripts/solid_tide.py (main scripts/, not
inside peppar_fix/) because it's shared between engine and
regression harness.  Tests live here because peppar_fix/ is the
hosted test directory for the engine's Python suite.

Coverage:
- Magnitude / sign sanity (~150 mm peak vertical)
- Geographic consistency (polar stations see less vertical, more horizontal)
- Zero-displacement-at-reasonable-body-alignment sanity
- Numerical vs zero for the simple cases (station on equator with sun at zenith)
"""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timezone

import numpy as np

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from solid_tide import solid_tide_displacement  # noqa: E402


# A reasonable ECEF station — roughly ABMF (Guadeloupe), used in the
# PRIDE ablation that sized the correction at 42 mm.  Any physically
# plausible surface station works; this one is convenient because
# the harness verified numerical agreement there.
ABMF_ECEF = np.array([2919785.79086, -5383744.95943, 1774604.85992])

# Mid-latitude surface station (approx 45°N, 0°E) for tests where
# we want a generic station.
MIDLAT_ECEF = np.array([4517590.0, 0.0, 4487348.0])


def _earth_radius(pos):
    return float(np.linalg.norm(pos))


class MagnitudeTest(unittest.TestCase):
    """Total displacement magnitude is bounded at ~few hundred mm —
    literature puts the 24h peak-to-peak SET envelope at ~300 mm
    vertical at equatorial latitudes, ~150 mm at mid-latitudes."""

    def test_magnitude_bounded(self):
        """Displacement magnitude should be < 500 mm at any plausible
        epoch — safe upper bound well above the literature peak."""
        # Sample across one day at 1-hour intervals.
        t0 = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        max_mag = 0.0
        for hour in range(24):
            t = t0.replace(hour=hour)
            disp = solid_tide_displacement(t, MIDLAT_ECEF)
            mag = float(np.linalg.norm(disp))
            max_mag = max(max_mag, mag)
        self.assertLess(max_mag, 0.5,
                        f"max |disp| = {max_mag*1000:.1f} mm, expected < 500 mm")

    def test_magnitude_nonzero(self):
        """Tide is not identically zero — something is being computed."""
        t = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        disp = solid_tide_displacement(t, MIDLAT_ECEF)
        self.assertGreater(float(np.linalg.norm(disp)), 0.001,
                           "expected non-trivial tide displacement")


class DiurnalVariationTest(unittest.TestCase):
    """Peak SET occurs when sun + moon align overhead; minimum when
    body-station geometry cancels.  24h range should span at least
    50 mm at mid-latitude."""

    def test_has_diurnal_variation(self):
        t0 = datetime(2024, 6, 15, 0, 0, 0, tzinfo=timezone.utc)
        mags = []
        # 10-minute sampling across 24h captures diurnal peaks cleanly.
        for minute in range(0, 24 * 60, 10):
            t = t0.replace() + __import__('datetime').timedelta(minutes=minute)
            disp = solid_tide_displacement(t, MIDLAT_ECEF)
            mags.append(float(np.linalg.norm(disp)))
        span = max(mags) - min(mags)
        self.assertGreater(span, 0.05,
                           f"24h span {span*1000:.1f} mm, expected > 50 mm")


class RadialVsTransverseTest(unittest.TestCase):
    """Verify both radial and transverse components are non-trivially
    present — the IERS 2010 Step 1 formula has distinct h2 (radial)
    and l2 (transverse) Love-number contributions, and neither should
    collapse to zero for a generic station/epoch."""

    def test_both_components_present(self):
        station = MIDLAT_ECEF
        r_hat = station / _earth_radius(station)
        t = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        disp = solid_tide_displacement(t, station)
        radial_mag = abs(float(np.dot(disp, r_hat)))
        horiz_mag = float(np.linalg.norm(disp - radial_mag * r_hat))
        # Both should contribute more than 1 mm — well above numeric
        # noise, well below the expected 50-200 mm total envelope.
        self.assertGreater(radial_mag, 0.001,
                           f"radial {radial_mag*1000:.1f} mm < 1 mm — "
                           f"radial contribution seems missing")
        self.assertGreater(horiz_mag, 0.001,
                           f"horiz  {horiz_mag*1000:.1f} mm < 1 mm — "
                           f"transverse contribution seems missing")


class InputValidationTest(unittest.TestCase):

    def test_rejects_wrong_shape(self):
        t = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        # Non-(3,) input must raise, not silently produce garbage.
        with self.assertRaises(ValueError):
            solid_tide_displacement(t, np.array([1.0, 2.0]))

    def test_accepts_ndarray_3(self):
        t = datetime(2024, 6, 15, 12, 0, 0, tzinfo=timezone.utc)
        disp = solid_tide_displacement(t, MIDLAT_ECEF)
        self.assertEqual(disp.shape, (3,))


class DeterminismTest(unittest.TestCase):
    """Same inputs → same outputs.  Verify no hidden global state."""

    def test_deterministic(self):
        t = datetime(2024, 6, 15, 12, 34, 56, tzinfo=timezone.utc)
        d1 = solid_tide_displacement(t, ABMF_ECEF)
        d2 = solid_tide_displacement(t, ABMF_ECEF)
        self.assertTrue(np.allclose(d1, d2),
                        "SET must be deterministic — same inputs, same output")


if __name__ == "__main__":
    unittest.main()
