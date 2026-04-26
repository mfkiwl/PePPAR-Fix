"""Regression test for the IGS-SSR orbit/clock unit-scaling bug.

The bug (fixed 2026-04-25): pyrtcm decodes both standard RTCM SSR
(DF365-378) and IGS SSR (IDF013-021) orbit/clock fields with their
spec's per-LSB scale factor — both return mm and mm/s.  The engine
correctly /1000-converted the DF* path but used IDF* values raw, so
every IGS-SSR-format AC's orbit and clock corrections were 1000× too
large.  Surfaced as 280m position divergence on CAS (SSRA01CAS1, our
only IGS-SSR test AC) in the cross-AC diagnostic 2026-04-25.

This test verifies both decoders land in metres (orbit) and metres-
per-second (rates) and that an IGS-SSR-encoded radial of 50 mm parses
as 0.05 m, not 50 m.
"""

import sys
from pathlib import Path
from unittest.mock import MagicMock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent / "scripts"))
from ssr_corrections import SSRState


class _Msg:
    """Plain-attribute object used in place of pyrtcm message.

    Avoids MagicMock auto-attributes, which would make getattr return
    truthy MagicMock instances for fields the engine probes (e.g.
    IDF011_*) and break the _get_sat_id field-probe ladder.
    """
    def __init__(self, identity, **fields):
        self.identity = identity
        self.payload = b""
        for k, v in fields.items():
            setattr(self, k, v)


def _make_orbit_msg(identity, sat_id, **fields):
    return _Msg(identity, **fields)


def _state_for_orbit_test(identity, sys_prefix, sat_id, fields):
    state = SSRState()
    msg = _make_orbit_msg(identity, sat_id, **fields)
    # Drive _parse_orbit directly with epoch_s=0, n_sats=1
    state._parse_orbit(msg, sys_prefix, epoch_s=0.0, n_sats=1)
    return state


# ── Sat-ID + IOD lookup helpers ─────────────────────────────────────── #
# The engine's _get_sat_id and _get_iod try several pyrtcm field names.
# We just set DF068 (GPS) / DF252 (GAL) / IDF011 (IGS-SSR sat id) and
# DF071 / IDF012 for IOD.  Using IDF011/012 keeps the test independent
# of GPS-vs-GAL detail.


def test_igs_ssr_orbit_radial_scaled_to_metres():
    """50 mm IGS-SSR radial correction must land as 0.050 m, not 50 m."""
    # Encode 50 mm radial (a typical orbit correction magnitude).
    fields = {
        "IDF011_01": 16,        # sat id
        "IDF012_01": 7,         # IOD
        "IDF013_01": 50.0,      # radial (pyrtcm returns mm)
        "IDF014_01": 0.0,
        "IDF015_01": 0.0,
        "IDF016_01": 0.0,
        "IDF017_01": 0.0,
        "IDF018_01": 0.0,
    }
    state = _state_for_orbit_test("4076_021", "G", 16, fields)
    orbit = state._orbit.get("G16")
    assert orbit is not None, "orbit not stored"
    assert abs(orbit.radial - 0.050) < 1e-6, (
        f"radial should be 0.050 m, got {orbit.radial} m "
        f"(off by {orbit.radial / 0.050:.0f}× — the 1000× bug)")


def test_igs_ssr_orbit_along_cross_scaled():
    """Along + cross also need /1000 (different LSB but same convention)."""
    fields = {
        "IDF011_01": 24,
        "IDF012_01": 1,
        "IDF013_01": 0.0,
        "IDF014_01": 200.0,     # 200 mm along
        "IDF015_01": -100.0,    # -100 mm cross
        "IDF016_01": 0.0,
        "IDF017_01": 0.0,
        "IDF018_01": 0.0,
    }
    state = _state_for_orbit_test("4076_061", "E", 24, fields)
    orbit = state._orbit.get("E24")
    assert orbit is not None
    assert abs(orbit.along - 0.200) < 1e-6
    assert abs(orbit.cross - (-0.100)) < 1e-6


def test_igs_ssr_orbit_rates_scaled():
    """Dot rates: pyrtcm returns mm/s; engine should land in m/s."""
    fields = {
        "IDF011_01": 5,
        "IDF012_01": 0,
        "IDF013_01": 0.0,
        "IDF014_01": 0.0,
        "IDF015_01": 0.0,
        "IDF016_01": 1.0,       # 1 mm/s radial rate
        "IDF017_01": 2.0,       # 2 mm/s along rate
        "IDF018_01": 4.0,       # 4 mm/s cross rate
    }
    state = _state_for_orbit_test("4076_021", "G", 5, fields)
    orbit = state._orbit.get("G05")
    assert orbit is not None
    assert abs(orbit.dot_radial - 0.001) < 1e-9
    assert abs(orbit.dot_along  - 0.002) < 1e-9
    assert abs(orbit.dot_cross  - 0.004) < 1e-9


def test_igs_ssr_clock_c0_scaled_to_metres():
    """50 mm IGS-SSR clock C0 must land as 0.050 m, not 50 m."""
    state = SSRState()
    msg = _Msg("4076_022",
               IDF011_01=12, IDF019_01=50.0, IDF020_01=1.0, IDF021_01=0.5)
    state._parse_clock(msg, "G", epoch_s=0.0, n_sats=1)
    clock = state._clock.get("G12")
    assert clock is not None, "clock not stored"
    assert abs(clock.c0 - 0.050) < 1e-6, (
        f"c0 should be 0.050 m, got {clock.c0} m")
    assert abs(clock.c1 - 0.001) < 1e-9
    assert abs(clock.c2 - 0.0005) < 1e-9


def test_standard_rtcm_orbit_unchanged():
    """Sanity: existing RTCM standard DF365-370 path still scales correctly."""
    state = SSRState()
    # Use DF068 (GPS sat-id) to force engine into the standard-RTCM branch
    # by leaving IDF011/IDF013 unset.
    msg = _Msg("1057",
               DF068_01=16, DF071_01=7,
               DF365_01=50.0, DF366_01=0.0, DF367_01=0.0,
               DF368_01=0.0,  DF369_01=0.0, DF370_01=0.0)
    state._parse_orbit(msg, "G", epoch_s=0.0, n_sats=1)
    orbit = state._orbit.get("G16")
    assert orbit is not None
    assert abs(orbit.radial - 0.050) < 1e-6


if __name__ == "__main__":
    import pytest
    sys.exit(pytest.main([__file__, "-v"]))
