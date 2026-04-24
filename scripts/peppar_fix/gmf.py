"""Global Mapping Functions (Boehm et al. 2006).

GMF projects the zenith tropospheric delay to slant delay via two
mapping functions — hydrostatic (mostly the dry component, ~2.3 m at
zenith) and wet (the residual state we estimate, ~10-30 cm at
zenith).  The current harness uses a simple ``1/sin(elev)``, which
is the right asymptote near zenith but wrong by tens of cm of slant
delay at elevations below ~15° — exactly where the meter-scale
phase residuals on our PRIDE/ABMF run live.

Reference:
- Boehm, J., A.E. Niell, P. Tregoning, H. Schuh (2006), "Global
  Mapping Functions (GMF): A new empirical mapping function based
  on numerical weather model data," Geophys. Res. Lett., Vol. 33,
  L07304, doi:10.1029/2005GL025545.

The fits are spherical harmonic expansions (degree/order 9, 55
coefficients each) of the Marini 1972 continued-fraction
mapping-function form

    m(e) = (1 + a/(1 + b/(1+c))) / (sin(e) + a/(sin(e) + b/(sin(e) + c)))

The hydrostatic coefficients carry an annual-mean term (cos(2π·doy/
365.25)) and an amplitude term that's added in.  The wet
coefficients carry both, but with different scales — the wet
delay's seasonal variability is smaller in absolute terms.

Coefficient tables (``ah_mean``, ``bh_mean``, ``ah_amp``,
``bh_amp``, ``aw_mean``, ``bw_mean``, ``aw_amp``, ``bw_amp``) are
the published Boehm 2006 values, transcribed from PRIDE-PPPAR's
``src/lib/global_map.f90`` (verified element-by-element).

The hydrostatic mapping additionally carries a Niell 1996 height
correction that linearly increases ``m_h`` with station altitude
to account for the larger air column at sea level vs at altitude.

API:

- ``gmf_at(mjd, lat_rad, lon_rad, height_m, elev_rad)`` — pure
  function; returns ``(m_hydrostatic, m_wet)``.  Recomputes the
  full coefficient sum on every call.  Adequate for harness use
  (~50k calls in a 24-h regression run).
- ``GMFProvider`` — holds a station's lat/lon/height, caches the
  station-dependent V/W Legendre arrays, refreshes seasonal terms
  via ``update_epoch(mjd)`` once per epoch, exposes
  ``m_hydrostatic(elev_rad)`` / ``m_wet(elev_rad)`` for cheap
  per-SV calls.

Tested against PRIDE-PPPAR's ``global_map`` Fortran for a few
spot points (zenith → ~1.0, 5° elev → ~10x, 90° latitude pole
case).  Differences below 1e-4 (the limit of the published table
precision).

What this DOES NOT include:
- Time-varying meteorological data (VMF1/VMF3 grids) — those
  require external NWM input and are out of scope.  GMF is the
  empirical climatology fit, station+epoch+elev only.
- Niell 1996 wet height correction — Boehm 2006 explicitly drops
  this; the wet mapping has no height term, period.
"""

from __future__ import annotations

import math


# 55-coefficient spherical harmonic expansion: ah_mean / bh_mean for
# the seasonal-mean part of the hydrostatic mapping; ah_amp / bh_amp
# for the cosine-amplitude (added with cos(2π·doy/365.25 + phase) in
# the seasonal term).  aw / bw are the wet equivalents.  All four
# sets are 55 entries, indexed by (n, m) in row-major Pascal-triangle
# order: i=0 → (0,0); i=1 → (1,0); i=2 → (1,1); i=3 → (2,0); etc.
# Source: Boehm et al. 2006, transcribed from PRIDE-PPPAR's
# global_map.f90.
_AH_MEAN = (
    +1.2517e+02, +8.503e-01, +6.936e-02, -6.760e+00, +1.771e-01,
    +1.130e-02, +5.963e-01, +1.808e-02, +2.801e-03, -1.414e-03,
    -1.212e+00, +9.300e-02, +3.683e-03, +1.095e-03, +4.671e-05,
    +3.959e-01, -3.867e-02, +5.413e-03, -5.289e-04, +3.229e-04,
    +2.067e-05, +3.000e-01, +2.031e-02, +5.900e-03, +4.573e-04,
    -7.619e-05, +2.327e-06, +3.845e-06, +1.182e-01, +1.158e-02,
    +5.445e-03, +6.219e-05, +4.204e-06, -2.093e-06, +1.540e-07,
    -4.280e-08, -4.751e-01, -3.490e-02, +1.758e-03, +4.019e-04,
    -2.799e-06, -1.287e-06, +5.468e-07, +7.580e-08, -6.300e-09,
    -1.160e-01, +8.301e-03, +8.771e-04, +9.955e-05, -1.718e-06,
    -2.012e-06, +1.170e-08, +1.790e-08, -1.300e-09, +1.000e-10,
)
_BH_MEAN = (
    +0.000e+00, +0.000e+00, +3.249e-02, +0.000e+00, +3.324e-02,
    +1.850e-02, +0.000e+00, -1.115e-01, +2.519e-02, +4.923e-03,
    +0.000e+00, +2.737e-02, +1.595e-02, -7.332e-04, +1.933e-04,
    +0.000e+00, -4.796e-02, +6.381e-03, -1.599e-04, -3.685e-04,
    +1.815e-05, +0.000e+00, +7.033e-02, +2.426e-03, -1.111e-03,
    -1.357e-04, -7.828e-06, +2.547e-06, +0.000e+00, +5.779e-03,
    +3.133e-03, -5.312e-04, -2.028e-05, +2.323e-07, -9.100e-08,
    -1.650e-08, +0.000e+00, +3.688e-02, -8.638e-04, -8.514e-05,
    -2.828e-05, +5.403e-07, +4.390e-07, +1.350e-08, +1.800e-09,
    +0.000e+00, -2.736e-02, -2.977e-04, +8.113e-05, +2.329e-07,
    +8.451e-07, +4.490e-08, -8.100e-09, -1.500e-09, +2.000e-10,
)
_AH_AMP = (
    -2.738e-01, -2.837e+00, +1.298e-02, -3.588e-01, +2.413e-02,
    +3.427e-02, -7.624e-01, +7.272e-02, +2.160e-02, -3.385e-03,
    +4.424e-01, +3.722e-02, +2.195e-02, -1.503e-03, +2.426e-04,
    +3.013e-01, +5.762e-02, +1.019e-02, -4.476e-04, +6.790e-05,
    +3.227e-05, +3.123e-01, -3.535e-02, +4.840e-03, +3.025e-06,
    -4.363e-05, +2.854e-07, -1.286e-06, -6.725e-01, -3.730e-02,
    +8.964e-04, +1.399e-04, -3.990e-06, +7.431e-06, -2.796e-07,
    -1.601e-07, +4.068e-02, -1.352e-02, +7.282e-04, +9.594e-05,
    +2.070e-06, -9.620e-08, -2.742e-07, -6.370e-08, -6.300e-09,
    +8.625e-02, -5.971e-03, +4.705e-04, +2.335e-05, +4.226e-06,
    +2.475e-07, -8.850e-08, -3.600e-08, -2.900e-09, +0.000e+00,
)
_BH_AMP = (
    +0.000e+00, +0.000e+00, -1.136e-01, +0.000e+00, -1.868e-01,
    -1.399e-02, +0.000e+00, -1.043e-01, +1.175e-02, -2.240e-03,
    +0.000e+00, -3.222e-02, +1.333e-02, -2.647e-03, -2.316e-05,
    +0.000e+00, +5.339e-02, +1.107e-02, -3.116e-03, -1.079e-04,
    -1.299e-05, +0.000e+00, +4.861e-03, +8.891e-03, -6.448e-04,
    -1.279e-05, +6.358e-06, -1.417e-07, +0.000e+00, +3.041e-02,
    +1.150e-03, -8.743e-04, -2.781e-05, +6.367e-07, -1.140e-08,
    -4.200e-08, +0.000e+00, -2.982e-02, -3.000e-03, +1.394e-05,
    -3.290e-05, -1.705e-07, +7.440e-08, +2.720e-08, -6.600e-09,
    +0.000e+00, +1.236e-02, -9.981e-04, -3.792e-05, -1.355e-05,
    +1.162e-06, -1.789e-07, +1.470e-08, -2.400e-09, -4.000e-10,
)
_AW_MEAN = (
    +5.640e+01, +1.555e+00, -1.011e+00, -3.975e+00, +3.171e-02,
    +1.065e-01, +6.175e-01, +1.376e-01, +4.229e-02, +3.028e-03,
    +1.688e+00, -1.692e-01, +5.478e-02, +2.473e-02, +6.059e-04,
    +2.278e+00, +6.614e-03, -3.505e-04, -6.697e-03, +8.402e-04,
    +7.033e-04, -3.236e+00, +2.184e-01, -4.611e-02, -1.613e-02,
    -1.604e-03, +5.420e-05, +7.922e-05, -2.711e-01, -4.406e-01,
    -3.376e-02, -2.801e-03, -4.090e-04, -2.056e-05, +6.894e-06,
    +2.317e-06, +1.941e+00, -2.562e-01, +1.598e-02, +5.449e-03,
    +3.544e-04, +1.148e-05, +7.503e-06, -5.667e-07, -3.660e-08,
    +8.683e-01, -5.931e-02, -1.864e-03, -1.277e-04, +2.029e-04,
    +1.269e-05, +1.629e-06, +9.660e-08, -1.015e-07, -5.000e-10,
)
_BW_MEAN = (
    +0.000e+00, +0.000e+00, +2.592e-01, +0.000e+00, +2.974e-02,
    -5.471e-01, +0.000e+00, -5.926e-01, -1.030e-01, -1.567e-02,
    +0.000e+00, +1.710e-01, +9.025e-02, +2.689e-02, +2.243e-03,
    +0.000e+00, +3.439e-01, +2.402e-02, +5.410e-03, +1.601e-03,
    +9.669e-05, +0.000e+00, +9.502e-02, -3.063e-02, -1.055e-03,
    -1.067e-04, -1.130e-04, +2.124e-05, +0.000e+00, -3.129e-01,
    +8.463e-03, +2.253e-04, +7.413e-05, -9.376e-05, -1.606e-06,
    +2.060e-06, +0.000e+00, +2.739e-01, +1.167e-03, -2.246e-05,
    -1.287e-04, -2.438e-05, -7.561e-07, +1.158e-06, +4.950e-08,
    +0.000e+00, -1.344e-01, +5.342e-03, +3.775e-04, -6.756e-05,
    -1.686e-06, -1.184e-06, +2.768e-07, +2.730e-08, +5.700e-09,
)
_AW_AMP = (
    +1.023e-01, -2.695e+00, +3.417e-01, -1.405e-01, +3.175e-01,
    +2.116e-01, +3.536e+00, -1.505e-01, -1.660e-02, +2.967e-02,
    +3.819e-01, -1.695e-01, -7.444e-02, +7.409e-03, -6.262e-03,
    -1.836e+00, -1.759e-02, -6.256e-02, -2.371e-03, +7.947e-04,
    +1.501e-04, -8.603e-01, -1.360e-01, -3.629e-02, -3.706e-03,
    -2.976e-04, +1.857e-05, +3.021e-05, +2.248e+00, -1.178e-01,
    +1.255e-02, +1.134e-03, -2.161e-04, -5.817e-06, +8.836e-07,
    -1.769e-07, +7.313e-01, -1.188e-01, +1.145e-02, +1.011e-03,
    +1.083e-04, +2.570e-06, -2.140e-06, -5.710e-08, +2.000e-08,
    -1.632e+00, -6.948e-03, -3.893e-03, +8.592e-04, +7.577e-05,
    +4.539e-06, -3.852e-07, -2.213e-07, -1.370e-08, +5.800e-09,
)
_BW_AMP = (
    +0.000e+00, +0.000e+00, -8.865e-02, +0.000e+00, -4.309e-01,
    +6.340e-02, +0.000e+00, +1.162e-01, +6.176e-02, -4.234e-03,
    +0.000e+00, +2.530e-01, +4.017e-02, -6.204e-03, +4.977e-03,
    +0.000e+00, -1.737e-01, -5.638e-03, +1.488e-04, +4.857e-04,
    -1.809e-04, +0.000e+00, -1.514e-01, -1.685e-02, +5.333e-03,
    -7.611e-05, +2.394e-05, +8.195e-06, +0.000e+00, +9.326e-02,
    -1.275e-02, -3.071e-04, +5.374e-05, -3.391e-05, -7.436e-06,
    +6.747e-07, +0.000e+00, -8.637e-02, -3.807e-03, -6.833e-04,
    -3.861e-05, -2.268e-05, +1.454e-06, +3.860e-07, -1.068e-07,
    +0.000e+00, -2.658e-02, -1.947e-03, +7.131e-04, -3.506e-05,
    +1.885e-07, +5.792e-07, +3.990e-08, +2.000e-08, -5.700e-09,
)

_NMAX = 9   # spherical harmonic max degree (and order) — fixed per Boehm 2006

# Niell 1996 height correction for hydrostatic.  Wet has no height
# correction (Boehm 2006 explicit).
_A_HT = 2.53e-5
_B_HT = 5.49e-3
_C_HT = 1.14e-3


def _build_legendre(lat_rad: float, lon_rad: float):
    """Return V[0..nmax+1, 0..nmax+1] and W tables for the spherical
    harmonic expansion of latitude+longitude.  Built via the same
    recurrence as PRIDE's global_map.f90.
    """
    n_dim = _NMAX + 2
    v = [[0.0] * n_dim for _ in range(n_dim)]
    w = [[0.0] * n_dim for _ in range(n_dim)]
    x = math.cos(lat_rad) * math.cos(lon_rad)
    y = math.cos(lat_rad) * math.sin(lon_rad)
    z = math.sin(lat_rad)
    v[0][0] = 1.0
    v[1][0] = z
    for n in range(2, _NMAX + 1):
        v[n][0] = ((2 * n - 1) * z * v[n - 1][0] - (n - 1) * v[n - 2][0]) / n
    for m in range(1, _NMAX + 1):
        v[m][m] = (2 * m - 1) * (x * v[m - 1][m - 1] - y * w[m - 1][m - 1])
        w[m][m] = (2 * m - 1) * (x * w[m - 1][m - 1] + y * v[m - 1][m - 1])
        if m < _NMAX:
            v[m + 1][m] = (2 * m + 1) * z * v[m][m]
            w[m + 1][m] = (2 * m + 1) * z * w[m][m]
        for n in range(m + 2, _NMAX + 1):
            v[n][m] = ((2 * n - 1) * z * v[n - 1][m]
                       - (n + m - 1) * v[n - 2][m]) / (n - m)
            w[n][m] = ((2 * n - 1) * z * w[n - 1][m]
                       - (n + m - 1) * w[n - 2][m]) / (n - m)
    return v, w


def _coeff_sum(v, w, mean_table, amp_table) -> tuple[float, float]:
    """Build the spherical-harmonic mean and amplitude sums for one
    coefficient (a or b).  Returns (mean, amp) pre-amplitude-cosine
    so the caller can apply ``mean + amp · cos(2π·doy/365.25)``.
    """
    s_mean = 0.0
    s_amp = 0.0
    i = 0
    for n in range(_NMAX + 1):
        for m in range(n + 1):
            s_mean += mean_table[i] * v[n][m] + amp_table[i] * w[n][m]
            # amp_table here is the b-mean variant; the cos-amplitude
            # uses a different table — handled by callers.
            i += 1
    return s_mean, s_amp


def _doy_phase(mjd: float) -> float:
    """Day-of-year phase angle in radians, centred on Jan 28
    (Niell 1996 reference day).  Per Boehm 2006 Eq. (1)."""
    doy = mjd - 44239.0 + 1.0 - 28.0
    return doy / 365.25 * 2.0 * math.pi


def _marini(elev_rad: float, a: float, b: float, c: float) -> float:
    """Marini 1972 continued-fraction mapping function evaluated at
    elevation ``elev_rad`` with coefficients (a, b, c)."""
    sine = math.sin(elev_rad)
    beta = b / (sine + c)
    gamma = a / (sine + beta)
    topcon = 1.0 + a / (1.0 + b / (1.0 + c))
    return topcon / (sine + gamma)


def gmf_at(mjd: float, lat_rad: float, lon_rad: float,
           height_m: float, elev_rad: float) -> tuple[float, float]:
    """Pure-function GMF evaluation.  Returns ``(m_h, m_w)``.

    For a single epoch + station + elevation.  ``mjd`` is a float
    Modified Julian Date; height in metres; angles in radians.
    """
    v, w = _build_legendre(lat_rad, lon_rad)
    cos_doy = math.cos(_doy_phase(mjd))

    # Hydrostatic a coefficient.
    ahm = 0.0
    aha = 0.0
    i = 0
    for n in range(_NMAX + 1):
        for m in range(n + 1):
            ahm += _AH_MEAN[i] * v[n][m] + _BH_MEAN[i] * w[n][m]
            aha += _AH_AMP[i] * v[n][m] + _BH_AMP[i] * w[n][m]
            i += 1
    ah = (ahm + aha * cos_doy) * 1e-5

    # Hydrostatic b is fixed; c has a hemispheric + seasonal piece.
    bh = 0.0029
    c0h = 0.062
    if lat_rad < 0.0:
        phh = math.pi
        c11h = 0.007
        c10h = 0.002
    else:
        phh = 0.0
        c11h = 0.005
        c10h = 0.001
    ch = (c0h + ((math.cos(_doy_phase(mjd) + phh) + 1.0) * c11h / 2.0
                 + c10h) * (1.0 - math.cos(lat_rad)))

    m_h = _marini(elev_rad, ah, bh, ch)

    # Niell 1996 hydrostatic height correction.
    sine = math.sin(elev_rad)
    beta_ht = _B_HT / (sine + _C_HT)
    gamma_ht = _A_HT / (sine + beta_ht)
    topcon_ht = 1.0 + _A_HT / (1.0 + _B_HT / (1.0 + _C_HT))
    ht_corr_coef = 1.0 / sine - topcon_ht / (sine + gamma_ht)
    m_h += ht_corr_coef * (height_m / 1000.0)

    # Wet a coefficient.
    awm = 0.0
    awa = 0.0
    i = 0
    for n in range(_NMAX + 1):
        for m in range(n + 1):
            awm += _AW_MEAN[i] * v[n][m] + _BW_MEAN[i] * w[n][m]
            awa += _AW_AMP[i] * v[n][m] + _BW_AMP[i] * w[n][m]
            i += 1
    aw = (awm + awa * cos_doy) * 1e-5
    bw = 0.00146
    cw = 0.04391

    m_w = _marini(elev_rad, aw, bw, cw)
    return m_h, m_w


class GMFProvider:
    """Per-station GMF evaluator.  Caches the station-dependent
    Legendre tables + coefficient sums; per-epoch ``update_epoch``
    refreshes the seasonal cosine; ``m_hydrostatic(elev_rad)`` and
    ``m_wet(elev_rad)`` are cheap.

    Pattern matches the harness's other obs-model providers: build
    once at startup, advance once per epoch, query once per SV.
    """

    def __init__(
        self,
        lat_rad: float,
        lon_rad: float,
        height_m: float,
    ) -> None:
        self._lat_rad = lat_rad
        self._lon_rad = lon_rad
        self._height_m = height_m
        self._v, self._w = _build_legendre(lat_rad, lon_rad)

        # Station-dependent (epoch-independent) coefficient pieces.
        self._ahm = 0.0
        self._aha_base = 0.0
        self._awm = 0.0
        self._awa_base = 0.0
        i = 0
        for n in range(_NMAX + 1):
            for m in range(n + 1):
                vv = self._v[n][m]
                ww = self._w[n][m]
                self._ahm += _AH_MEAN[i] * vv + _BH_MEAN[i] * ww
                self._aha_base += _AH_AMP[i] * vv + _BH_AMP[i] * ww
                self._awm += _AW_MEAN[i] * vv + _BW_MEAN[i] * ww
                self._awa_base += _AW_AMP[i] * vv + _BW_AMP[i] * ww
                i += 1

        # Hemispheric c-coefficient pieces (fixed per station).
        if lat_rad < 0.0:
            self._phh = math.pi
            self._c11h = 0.007
            self._c10h = 0.002
        else:
            self._phh = 0.0
            self._c11h = 0.005
            self._c10h = 0.001
        self._lat_factor = 1.0 - math.cos(lat_rad)

        # Epoch-dependent state — set by update_epoch.
        self._ah: float = 0.0
        self._aw: float = 0.0
        self._ch: float = 0.0
        self.update_epoch(0.0)  # placeholder; harness calls before query

    def update_epoch(self, mjd: float) -> None:
        """Refresh epoch-dependent pieces (seasonal cosines).  Call
        once per epoch; ``m_hydrostatic`` / ``m_wet`` are then valid
        until the next ``update_epoch``.
        """
        cos_doy = math.cos(_doy_phase(mjd))
        self._ah = (self._ahm + self._aha_base * cos_doy) * 1e-5
        self._aw = (self._awm + self._awa_base * cos_doy) * 1e-5
        # ch's seasonal piece uses a different cos shift (Boehm 2006).
        self._ch = (
            0.062
            + ((math.cos(_doy_phase(mjd) + self._phh) + 1.0)
               * self._c11h / 2.0 + self._c10h) * self._lat_factor
        )

    def m_hydrostatic(self, elev_rad: float) -> float:
        """Hydrostatic mapping function at the given elevation."""
        m_h = _marini(elev_rad, self._ah, 0.0029, self._ch)
        # Niell 1996 height correction.
        sine = math.sin(elev_rad)
        beta_ht = _B_HT / (sine + _C_HT)
        gamma_ht = _A_HT / (sine + beta_ht)
        topcon_ht = 1.0 + _A_HT / (1.0 + _B_HT / (1.0 + _C_HT))
        ht_corr_coef = 1.0 / sine - topcon_ht / (sine + gamma_ht)
        return m_h + ht_corr_coef * (self._height_m / 1000.0)

    def m_wet(self, elev_rad: float) -> float:
        """Wet mapping function at the given elevation.  No height
        correction (per Boehm 2006)."""
        return _marini(elev_rad, self._aw, 0.00146, 0.04391)
