"""Unit tests for scripts/antex.py — ANTEX parser + PCV correction.

Uses a synthetic mini-ANTEX file generated in setUp so tests don't
depend on a full IGS14.atx being present on the test host.
"""

from __future__ import annotations

import math
import os
import sys
import tempfile
import unittest
from datetime import datetime, timezone

import numpy as np

_SCRIPTS_DIR = os.path.abspath(os.path.join(os.path.dirname(__file__), '..'))
if _SCRIPTS_DIR not in sys.path:
    sys.path.insert(0, _SCRIPTS_DIR)

from antex import (                                            # noqa: E402
    ANTEXParser, AntennaPattern, compute_pcv_correction,
    sat_body_frame, nadir_angle_deg, ecef_to_enu_matrix,
)


def _line(data: str, tag: str) -> str:
    """Build one ANTEX record line with tag at cols 60-79.

    ANTEX 1.4 puts tag text exactly at columns 60-79 (inclusive);
    the parser matches the tag there after rstrip.
    """
    assert len(data) <= 60, f"data too long ({len(data)} chars): {data!r}"
    return data.ljust(60) + tag + "\n"


def _make_mini_antex() -> str:
    """Synthetic ANTEX 1.4 covering G01 (sat, L1+L2) and TEST_ANT
    (receiver, L1+L2).  PCV tables are linear for easy test math."""
    lines = [
        _line("     1.4            M", "ANTEX VERSION / SYST"),
        _line("A", "PCV TYPE / REFANT"),
        _line("", "END OF HEADER"),
        # Satellite G01
        _line("", "START OF ANTENNA"),
        _line("BLOCK IIA           G01", "TYPE / SERIAL NO"),
        _line("IGS14_TEST", "METH / BY / # / DATE"),
        _line("     0.0", "DAZI"),
        _line("     0.0  14.0   1.0", "ZEN1 / ZEN2 / DZEN"),
        _line("     2", "# OF FREQUENCIES"),
        _line("  1992     1     1     0     0    0.0000000",
              "VALID FROM"),
        # G01 frequency
        _line("   G01", "START OF FREQUENCY"),
        _line("    100.00      5.00   2000.00", "NORTH / EAST / UP"),
        ("   NOAZI    0.00   -0.50   -1.00   -1.50   -2.00   -2.50"
         "   -3.00   -3.50   -4.00   -4.50   -5.00   -5.50   -6.00"
         "   -6.50   -7.00\n"),
        _line("   G01", "END OF FREQUENCY"),
        # G02 frequency
        _line("   G02", "START OF FREQUENCY"),
        _line("    120.00     10.00   2100.00", "NORTH / EAST / UP"),
        ("   NOAZI    0.00   -0.50   -1.00   -1.50   -2.00   -2.50"
         "   -3.00   -3.50   -4.00   -4.50   -5.00   -5.50   -6.00"
         "   -6.50   -7.00\n"),
        _line("   G02", "END OF FREQUENCY"),
        _line("", "END OF ANTENNA"),
        # Receiver TEST_ANT
        _line("", "START OF ANTENNA"),
        _line("TEST_A.00      NONE", "TYPE / SERIAL NO"),
        _line("IGS14_TEST", "METH / BY / # / DATE"),
        _line("     0.0", "DAZI"),
        _line("     0.0  90.0   5.0", "ZEN1 / ZEN2 / DZEN"),
        _line("     2", "# OF FREQUENCIES"),
        # G01
        _line("   G01", "START OF FREQUENCY"),
        _line("      1.00      0.50     60.00", "NORTH / EAST / UP"),
        ("   NOAZI    0.00   -1.00   -2.00   -3.00   -4.00   -5.00"
         "   -6.00   -7.00   -8.00   -9.00  -10.00  -11.00  -12.00"
         "  -13.00  -14.00  -15.00  -16.00  -17.00  -18.00\n"),
        _line("   G01", "END OF FREQUENCY"),
        # G02
        _line("   G02", "START OF FREQUENCY"),
        _line("      1.50      0.75     65.00", "NORTH / EAST / UP"),
        ("   NOAZI    0.00   -0.80   -1.60   -2.40   -3.20   -4.00"
         "   -4.80   -5.60   -6.40   -7.20   -8.00   -8.80   -9.60"
         "  -10.40  -11.20  -12.00  -12.80  -13.60  -14.40\n"),
        _line("   G02", "END OF FREQUENCY"),
        _line("", "END OF ANTENNA"),
        _line("", "END OF FILE"),
    ]
    return "".join(lines)


_MINI_ANTEX = _make_mini_antex()


class MiniAntexFixture:
    """Context manager that writes _MINI_ANTEX to a temp file."""

    def __enter__(self):
        self._f = tempfile.NamedTemporaryFile(
            'w', suffix='.atx', delete=False, encoding='latin-1')
        self._f.write(_MINI_ANTEX)
        self._f.close()
        return self._f.name

    def __exit__(self, *a):
        os.unlink(self._f.name)


class ANTEXParserTest(unittest.TestCase):

    def test_parses_satellite_entry(self):
        with MiniAntexFixture() as path:
            p = ANTEXParser(path)
        # G01 should be loaded for both frequencies.
        t = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        pat_l1 = p.get_sat_pattern('G01', 'G01', t)
        pat_l2 = p.get_sat_pattern('G01', 'G02', t)
        self.assertIsNotNone(pat_l1)
        self.assertIsNotNone(pat_l2)
        # PCO values (stored internally in meters — mm → m at load).
        # G01: N=100, E=5, U=2000 → (0.100, 0.005, 2.000) m
        self.assertTrue(np.allclose(pat_l1.pco_m,
                                    [0.100, 0.005, 2.000], atol=1e-9))
        self.assertTrue(np.allclose(pat_l2.pco_m,
                                    [0.120, 0.010, 2.100], atol=1e-9))

    def test_parses_receiver_entry(self):
        with MiniAntexFixture() as path:
            p = ANTEXParser(path)
        pat_l1 = p.get_recv_pattern('TEST_A.00      NONE', 'G01')
        pat_l2 = p.get_recv_pattern('TEST_A.00      NONE', 'G02')
        self.assertIsNotNone(pat_l1)
        self.assertIsNotNone(pat_l2)
        # Receiver PCO: (N, E, U) in mm → m
        self.assertTrue(np.allclose(pat_l1.pco_m,
                                    [0.001, 0.0005, 0.060], atol=1e-9))
        self.assertTrue(np.allclose(pat_l2.pco_m,
                                    [0.0015, 0.00075, 0.065], atol=1e-9))

    def test_missing_antenna_returns_none(self):
        with MiniAntexFixture() as path:
            p = ANTEXParser(path)
        t = datetime(2024, 1, 1, 0, 0, 0, tzinfo=timezone.utc)
        self.assertIsNone(p.get_sat_pattern('G99', 'G01', t))
        self.assertIsNone(p.get_recv_pattern('NONEXISTENT', 'G01'))


class AntennaPatternInterpTest(unittest.TestCase):
    """Pattern interpolation (NOAZI).  The synthetic table is linear
    so we can compute the expected value directly."""

    def _get_sat_l1(self):
        with MiniAntexFixture() as path:
            return ANTEXParser(path).get_sat_pattern(
                'G01', 'G01',
                datetime(2024, 1, 1, tzinfo=timezone.utc))

    def test_pcv_at_table_node(self):
        pat = self._get_sat_l1()
        # Table step 1°, values 0, -0.5, -1.0, -1.5 mm at zen=0,1,2,3.
        # At zen=2° expect -1.0 mm = -0.001 m.
        self.assertAlmostEqual(pat.pcv(2.0), -0.001, places=6)

    def test_pcv_linear_interp(self):
        pat = self._get_sat_l1()
        # At zen=1.5° expect halfway between -0.5 and -1.0 mm = -0.75 mm.
        self.assertAlmostEqual(pat.pcv(1.5), -0.00075, places=6)

    def test_pcv_clamps_below_range(self):
        pat = self._get_sat_l1()
        # Below zen_start (0°) should clamp to the first value (0).
        self.assertAlmostEqual(pat.pcv(-5.0), 0.0, places=9)

    def test_pcv_clamps_above_range(self):
        pat = self._get_sat_l1()
        # Above zen_end (14°) clamps to last value (-7 mm).
        self.assertAlmostEqual(pat.pcv(50.0), -0.007, places=6)


class SatBodyFrameTest(unittest.TestCase):
    """Verify orthonormality and conventional directions."""

    def test_orthonormal(self):
        sat = np.array([26_560_000.0, 0.0, 0.0])  # GPS altitude along X
        sun = np.array([1.5e11, 0.0, 0.0])        # same direction
        # Fails because sat and sun are collinear → cross product = 0.
        # Shift sun slightly to make it work.
        sun = np.array([1.5e11, 1e6, 0.0])
        e_x, e_y, e_z = sat_body_frame(sat, sun)
        self.assertAlmostEqual(float(np.linalg.norm(e_x)), 1.0, places=6)
        self.assertAlmostEqual(float(np.linalg.norm(e_y)), 1.0, places=6)
        self.assertAlmostEqual(float(np.linalg.norm(e_z)), 1.0, places=6)
        self.assertAlmostEqual(float(np.dot(e_x, e_y)), 0.0, places=6)
        self.assertAlmostEqual(float(np.dot(e_y, e_z)), 0.0, places=6)
        self.assertAlmostEqual(float(np.dot(e_x, e_z)), 0.0, places=6)

    def test_ez_points_to_earth(self):
        """e_z (ANTEX body +Z) points from satellite TOWARD Earth."""
        sat = np.array([26_560_000.0, 0.0, 0.0])
        sun = np.array([1.5e11, 1e6, 0.0])
        _, _, e_z = sat_body_frame(sat, sun)
        # e_z should point in -X direction (toward Earth center from
        # satellite on +X axis).
        self.assertAlmostEqual(e_z[0], -1.0, places=4)

    def test_right_handed(self):
        """e_x × e_y = e_z (right-handed frame)."""
        sat = np.array([20_000_000.0, 5_000_000.0, 10_000_000.0])
        sun = np.array([1.4e11, 2e10, 5e9])
        e_x, e_y, e_z = sat_body_frame(sat, sun)
        cross = np.cross(e_x, e_y)
        self.assertTrue(np.allclose(cross, e_z, atol=1e-6))


class NadirAngleTest(unittest.TestCase):

    def test_zenith_pass_nadir_zero(self):
        """Receiver directly beneath satellite → nadir = 0°."""
        sat = np.array([0.0, 0.0, 26_560_000.0])
        rcv = np.array([0.0, 0.0, 6_378_000.0])   # sub-satellite point
        self.assertAlmostEqual(nadir_angle_deg(sat, rcv), 0.0, places=3)

    def test_gps_limb_grazing_nadir(self):
        """GPS-altitude limb-grazing LOS (receiver at sub-satellite
        tangent point, orthogonal to sat position) has nadir ≈ 14°.
        That's the geometric max because Earth subtends ~28° at GPS
        altitude.
        """
        sat = np.array([0.0, 0.0, 26_560_000.0])
        rcv = np.array([6_378_000.0, 0.0, 0.0])  # tangent point
        nadir = nadir_angle_deg(sat, rcv)
        # Analytical: arctan(R_Earth / r_sat) ≈ arctan(6378/26560)
        # ≈ 13.5°.  Allow a generous band around the theoretical value.
        self.assertGreater(nadir, 12.0)
        self.assertLess(nadir, 15.0)


class EcefToEnuMatrixTest(unittest.TestCase):

    def test_rows_orthonormal(self):
        # Mid-latitude station, roughly 45°N, 0°E
        station = np.array([4_517_590.0, 0.0, 4_487_348.0])
        M = ecef_to_enu_matrix(station)
        # Each row is a unit vector.
        for row in M:
            self.assertAlmostEqual(float(np.linalg.norm(row)),
                                   1.0, places=6)
        # East × North = Up
        e, n, u = M[0], M[1], M[2]
        self.assertTrue(np.allclose(np.cross(e, n), u, atol=1e-6))

    def test_up_points_outward(self):
        """The Up row should point away from Earth center (dot with
        the station position ECEF vector is positive)."""
        station = np.array([4_517_590.0, 0.0, 4_487_348.0])
        M = ecef_to_enu_matrix(station)
        up = M[2]
        self.assertGreater(float(np.dot(up, station)), 0.0)


class ComputePCVCorrectionTest(unittest.TestCase):
    """Integration: compute_pcv_correction on synthetic ANTEX data."""

    def _setup_scenario(self):
        """Satellite near zenith, receiver at a plausible ECEF
        position.  Known pattern values per the synthetic ANTEX."""
        with MiniAntexFixture() as path:
            parser = ANTEXParser(path)
        # Note: parser outlives the tempfile via the closure.
        return parser

    def test_returns_zero_with_unknown_signal(self):
        parser = self._setup_scenario()
        obs = {
            'sv': 'G01',
            'f1_sig_name': 'UNKNOWN_SIGNAL',
            'f2_sig_name': 'GPS-L2CL',
            'wl_f1': 0.1902936,
            'wl_f2': 0.2442102,
        }
        sat = np.array([26_560_000.0, 0.0, 0.0])
        rcv = np.array([6_378_000.0, 0.0, 0.0])
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        delta, ok = compute_pcv_correction(
            obs, sat, rcv, parser, 'TEST_A.00      NONE', t)
        self.assertFalse(ok)
        self.assertEqual(delta, 0.0)

    def test_returns_zero_with_missing_antenna(self):
        parser = self._setup_scenario()
        obs = {
            'sv': 'G01',
            'f1_sig_name': 'GPS-L1CA',
            'f2_sig_name': 'GPS-L2CL',
            'wl_f1': 0.1902936,
            'wl_f2': 0.2442102,
        }
        sat = np.array([26_560_000.0, 0.0, 0.0])
        rcv = np.array([6_378_000.0, 0.0, 0.0])
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        delta, ok = compute_pcv_correction(
            obs, sat, rcv, parser, 'NONEXISTENT_ANT', t)
        self.assertFalse(ok)

    def test_returns_nonzero_correction_with_good_inputs(self):
        parser = self._setup_scenario()
        obs = {
            'sv': 'G01',
            'f1_sig_name': 'GPS-L1CA',
            'f2_sig_name': 'GPS-L2CL',
            'wl_f1': 0.1902936,
            'wl_f2': 0.2442102,
        }
        # Satellite at plausible GPS altitude, receiver at lab-ish ECEF
        sat = np.array([15_000_000.0, 10_000_000.0, 20_000_000.0])
        rcv = np.array([157_462.0, -4_756_183.0, 4_232_768.0])
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        delta, ok = compute_pcv_correction(
            obs, sat, rcv, parser, 'TEST_A.00      NONE', t)
        self.assertTrue(ok)
        # Sat PCO U ~2 m for G01 L1, 2.1 m for L2.  IF-combined at
        # GPS L1/L2 frequencies with α₁=2.546 α₂=-1.546:
        # IF = 2.546*2.0 - 1.546*2.1 ≈ 5.09 - 3.25 ≈ 1.84 m × cos(nadir)
        # For a far-from-nadir LOS (most of the geometry), cos(nadir)
        # is close to 1 but less.  Expect correction in range a few
        # tenths of meters at least.
        self.assertGreater(abs(delta), 0.001,
                           f"correction {delta*1000:.1f} mm suspiciously small")
        # And well below the absolute 3 m scale (that would be a bug
        # in IF-combination or sign).
        self.assertLess(abs(delta), 10.0,
                        f"correction {delta*1000:.1f} mm suspiciously large")

    def test_correction_changes_with_sun_position(self):
        """Full 3-axis PCO projection (with sun) differs from U-only
        fallback (without sun).  The difference should be small (body
        N/E are dm scale) but non-zero."""
        parser = self._setup_scenario()
        obs = {
            'sv': 'G01',
            'f1_sig_name': 'GPS-L1CA',
            'f2_sig_name': 'GPS-L2CL',
            'wl_f1': 0.1902936,
            'wl_f2': 0.2442102,
        }
        sat = np.array([15_000_000.0, 10_000_000.0, 20_000_000.0])
        rcv = np.array([157_462.0, -4_756_183.0, 4_232_768.0])
        sun = np.array([1.4e11, 2e10, 5e9])
        t = datetime(2024, 1, 1, tzinfo=timezone.utc)
        delta_with_sun, ok1 = compute_pcv_correction(
            obs, sat, rcv, parser, 'TEST_A.00      NONE', t,
            sun_pos_ecef=sun)
        delta_no_sun, ok2 = compute_pcv_correction(
            obs, sat, rcv, parser, 'TEST_A.00      NONE', t)
        self.assertTrue(ok1 and ok2)
        # They should differ (full projection vs U-only) but not by
        # more than ~dm (body-N/E PCO magnitudes in our test ANTEX
        # are 100 mm and 5 mm for L1; body-Y effect adds dm max).
        diff = abs(delta_with_sun - delta_no_sun)
        self.assertLess(diff, 0.5,
                        f"sun-vs-no-sun diff {diff*1000:.1f} mm too large")
        # But they shouldn't be identical (that would mean sun-aware
        # path degenerates to U-only, indicating a bug).
        self.assertGreater(diff, 1e-6,
                           "sun-aware correction identical to U-only — "
                           "3-axis projection isn't being exercised")


if __name__ == "__main__":
    unittest.main()
