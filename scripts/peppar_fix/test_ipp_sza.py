"""Tests for IPP solar zenith angle computation."""

from __future__ import annotations

import math
import os
import sys
import unittest
from datetime import datetime, timezone

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from peppar_fix.ipp_sza import ipp_solar_zenith_deg  # noqa: E402


# DuPage County, IL (UFO1 site): 41.84° N, -88.10° W.  In April at this
# latitude, solar declination is ~+13° (sun north of equator).  Local
# solar noon is ~17:52 UTC (longitude corrections apply) on 2026-04-24.
DUP_LAT = 41.843
DUP_LON = -88.104


class IppSzaSanityTest(unittest.TestCase):

    def test_below_horizon_returns_none(self):
        # elev=0° is the horizon; below it is undefined.
        self.assertIsNone(ipp_solar_zenith_deg(
            DUP_LAT, DUP_LON, 90.0, 0.0,
            datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc),
        ))
        self.assertIsNone(ipp_solar_zenith_deg(
            DUP_LAT, DUP_LON, 90.0, -5.0,
            datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc),
        ))

    def test_zenith_at_local_solar_noon(self):
        # Near local solar noon for DuPage (~17:52 UTC on 2026-04-24)
        # with an SV near receiver zenith, IPP-SZA should be close to
        # (90° - solar_altitude) = (90° - (lat - declination)).  At
        # 41.84°N with sun declination ~+13° → sun altitude ~61° →
        # SZA at zenith IPP ~29°.  Accept a 5° window for our
        # low-accuracy solar ephemeris.
        sza = ipp_solar_zenith_deg(
            DUP_LAT, DUP_LON, 180.0, 85.0,  # south-near-zenith SV
            datetime(2026, 4, 24, 18, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(sza)
        # Allow a generous window — exact value depends on sun ephem
        # accuracy and IPP projection.  What we want to assert is
        # "daytime": SZA < 60° comfortably.
        self.assertLess(sza, 45.0, f"zenith-SZA at noon should be low, got {sza}")

    def test_midnight_is_nighttime(self):
        # Local midnight (~06:00 UTC April 24): SZA at any overhead
        # IPP should be > 90° (sun is below horizon).
        sza = ipp_solar_zenith_deg(
            DUP_LAT, DUP_LON, 180.0, 85.0,
            datetime(2026, 4, 24, 6, 0, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(sza)
        self.assertGreater(sza, 95.0,
                            f"midnight SZA should be >> 90°, got {sza}")

    def test_terminator_at_sunrise(self):
        # At DuPage in April, receiver sees sunrise ~11:30 UTC (ground
        # level).  At the same epoch, an EASTERN low-elev SV has its
        # IPP ~1000 km east at 350 km altitude — that IPP passes its
        # own "sunrise" 30-60 min earlier.  Expect SZA somewhere near
        # 90° ±30° depending on how far east the IPP is.  Sanity
        # bound: 60° < SZA < 120° — the terminator-ish band, not full
        # day or full night.
        sza = ipp_solar_zenith_deg(
            DUP_LAT, DUP_LON, 90.0, 10.0,   # low-elev due-east SV
            datetime(2026, 4, 24, 11, 30, 0, tzinfo=timezone.utc),
        )
        self.assertIsNotNone(sza)
        self.assertGreater(sza, 60.0,
                            f"sunrise IPP SZA should be near terminator, got {sza}")
        self.assertLess(sza, 120.0,
                         f"sunrise IPP SZA should be near terminator, got {sza}")

    def test_east_vs_west_at_sunrise(self):
        # Same epoch, same receiver: eastern IPP sees terminator
        # earlier (lower SZA — closer to dawn side) than western IPP
        # (which is still deeper in night).  So SZA_east < SZA_west
        # during sunrise window.
        t = datetime(2026, 4, 24, 11, 30, 0, tzinfo=timezone.utc)
        sza_east = ipp_solar_zenith_deg(DUP_LAT, DUP_LON, 90.0, 15.0, t)
        sza_west = ipp_solar_zenith_deg(DUP_LAT, DUP_LON, 270.0, 15.0, t)
        self.assertIsNotNone(sza_east)
        self.assertIsNotNone(sza_west)
        self.assertLess(sza_east, sza_west,
                         f"east IPP ({sza_east:.1f}) should be sunlit before "
                         f"west ({sza_west:.1f}) at sunrise")

    def test_ipp_not_at_pole_handled(self):
        # Cos(phi_p) near zero would blow up the longitude computation.
        # DuPage latitude is mid, SV elev high — IPP stays mid-lat.
        # Test the guard path with a near-pole receiver + near-zenith SV.
        sza = ipp_solar_zenith_deg(
            89.99, 0.0, 180.0, 89.0,
            datetime(2026, 4, 24, 12, 0, 0, tzinfo=timezone.utc),
        )
        # Shouldn't raise; may or may not be None depending on sun.
        self.assertTrue(sza is None or (0.0 <= sza <= 180.0))


if __name__ == '__main__':
    unittest.main()
