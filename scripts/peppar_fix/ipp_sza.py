"""Ionospheric-pierce-point solar zenith angle (IPP-SZA).

Cheap proxy for "is this signal's ionospheric pierce point near the
solar terminator."  Complements elevation and azimuth in slip records:

    SZA = 90°        : pierce point sits on the terminator — maximum
                        dTEC/dt, highest slip risk
    SZA  <  85°      : pierce point in sunlight, TEC ionizing but
                        relatively stable once past initial sunrise
    SZA  >  100°     : pierce point in night umbra, stable low TEC
    85°  <  SZA < 100° : twilight band around the terminator, the
                          "sunrise/sunset storm" window

Thin-shell ionosphere model at 350 km altitude (standard Klobuchar
assumption).  Accuracy is ~1° SZA — enough to diagnose whether a
slip happened under terminator conditions, not enough for rigorous
TEC modelling.  For the latter we'd need real TEC-map data (CNES,
IGS); for slip-detection priors the coarse SZA is sufficient.

Reuses ``solid_tide.sun_pos_ecef`` for the sun position (same
low-accuracy solar ephemeris the tide correction uses).  IPP
position is geodetic at shell altitude; SZA is the angle between
the IPP's geodetic-up vector and the direction from IPP to sun.
"""

from __future__ import annotations

import math
from datetime import datetime
from typing import Optional

import numpy as np


IONO_SHELL_ALT_M = 350_000.0  # standard thin-shell altitude
EARTH_RADIUS_M = 6_378_137.0  # WGS84 semi-major


def ipp_solar_zenith_deg(
    receiver_lat_deg: float,
    receiver_lon_deg: float,
    sv_azimuth_deg: float,
    sv_elevation_deg: float,
    utc_dt: datetime,
    shell_alt_m: float = IONO_SHELL_ALT_M,
) -> Optional[float]:
    """Return solar zenith angle at the SV's ionospheric pierce point.

    Arguments:
        receiver_lat_deg / receiver_lon_deg: WGS84 geodetic, degrees.
        sv_azimuth_deg: satellite azimuth from receiver, 0°=N, 90°=E.
        sv_elevation_deg: satellite elevation above horizon, degrees.
        utc_dt: observation epoch in UTC.  Tz-aware or naive — treated
            as UTC either way.
        shell_alt_m: thin-shell ionosphere altitude (default 350 km).

    Returns SZA in degrees in [0, 180], or None when the geometry is
    degenerate (SV below horizon, or sun ECEF unavailable).
    """
    if sv_elevation_deg <= 0.0:
        return None

    # Earth's central angle from receiver to IPP (Klobuchar thin shell).
    E = math.radians(sv_elevation_deg)
    A = math.radians(sv_azimuth_deg)
    # Sanity: cos(E) / (1 + h/R) must be in [-1, 1] for asin.  Always
    # is for physical shell altitudes and elevations.
    psi = (math.pi / 2) - E - math.asin(
        math.cos(E) / (1.0 + shell_alt_m / EARTH_RADIUS_M))

    phi_u = math.radians(receiver_lat_deg)
    lam_u = math.radians(receiver_lon_deg)
    phi_p = math.asin(
        math.sin(phi_u) * math.cos(psi)
        + math.cos(phi_u) * math.sin(psi) * math.cos(A))
    # Guard against cos(phi_p) == 0 at pole.
    cos_phi_p = math.cos(phi_p)
    if abs(cos_phi_p) < 1e-9:
        lam_p = lam_u
    else:
        lam_p = lam_u + math.asin(
            math.sin(psi) * math.sin(A) / cos_phi_p)

    # IPP ECEF — geodetic lat/lon at shell altitude.  lla_to_ecef uses
    # WGS84 ellipsoid, so this is an approximation (true IPP shell is
    # spherical, not ellipsoidal); error is < 20 km in position which
    # maps to ~0.2° in SZA at the terminator — well inside our
    # tolerance.
    from solve_pseudorange import lla_to_ecef
    ipp_lat = math.degrees(phi_p)
    ipp_lon = math.degrees(lam_p)
    ipp_ecef = np.array(lla_to_ecef(ipp_lat, ipp_lon, shell_alt_m))

    # Sun ECEF at epoch.  Reuses solid_tide's sun ephemeris.
    from solid_tide import sun_pos_ecef
    sun_ecef = sun_pos_ecef(utc_dt)
    if sun_ecef is None:
        return None

    # Zenith-up direction at IPP (outward normal of the WGS84 ellipsoid
    # at IPP lat/lon — for our accuracy target the geodetic "up" and
    # geocentric "up" are within ~0.2° at mid-latitude; using the
    # ellipsoidal normal to stay consistent with lla_to_ecef).
    zen_east_north_up = np.array([
        0.0, 0.0, 1.0,  # up in local ENU
    ])
    # Convert ENU up vector to ECEF.
    lat_r = math.radians(ipp_lat)
    lon_r = math.radians(ipp_lon)
    zenith_ecef = np.array([
        math.cos(lat_r) * math.cos(lon_r),
        math.cos(lat_r) * math.sin(lon_r),
        math.sin(lat_r),
    ])

    # Sun direction from IPP.
    sun_from_ipp = sun_ecef - ipp_ecef
    norm = np.linalg.norm(sun_from_ipp)
    if norm < 1.0:
        return None
    sun_dir = sun_from_ipp / norm

    cos_sza = float(np.dot(zenith_ecef, sun_dir))
    cos_sza = max(-1.0, min(1.0, cos_sza))
    return math.degrees(math.acos(cos_sza))
