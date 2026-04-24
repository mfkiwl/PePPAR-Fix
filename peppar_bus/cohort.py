"""Cohort grouping + median computations.

Pure functions — no bus handle, no threads, no state.  Input is a
list of "peer view" objects (duck-typed: we read ``identity``,
``position``, ``ztd`` attributes), output is grouped dicts and
scalar medians.  Callers feed their own peer snapshots and get
back the cohort arithmetic.

Cohort semantics come from ``docs/fleet-consensus-monitors.md``:

- **Shared-ARP cohort** — hosts whose ``identity.antenna_ref``
  match exactly.  Share an antenna via splitter; position is
  directly comparable.
- **Shared-atmosphere cohort** — hosts whose ``identity.site_ref``
  match (superset of shared-ARP when hosts on different antennas
  at the same site both declare the same site_ref).  ZTD is
  directly comparable across this cohort; position is NOT.
- **Separate** — everyone else.

These functions include the SELF peer by default.  The consensus
monitor is a group of ≥ 2 peers (self + one or more others), so
snapshots passed in should include self if self is meant to
participate in the cohort.  Callers that just want "the other
hosts" can exclude self before calling.

Robustness choices:

- **Median, not mean** — a single outlier (the host in null-mode
  drift) shouldn't drag the consensus.  Median is the natural
  robust statistic for "what do the good ones agree on."
- **Per-axis median for ECEF** — median(x), median(y), median(z).
  Geometric median would be more correct for 3D but is iterative;
  per-axis is an acceptable approximation at the mm scale we care
  about.
"""

from __future__ import annotations

import math
from typing import Iterable, Optional


def group_by_antenna_ref(snapshots: Iterable) -> dict[str, list]:
    """Group snapshots by their identity's antenna_ref.

    Hosts whose identity is None or antenna_ref is empty are
    skipped — they haven't declared membership in any cohort.
    Returns ``{antenna_ref: [snap, ...], ...}``.  Empty dict if
    nothing to group.
    """
    out: dict[str, list] = {}
    for s in snapshots:
        ident = getattr(s, "identity", None)
        if ident is None:
            continue
        ref = getattr(ident, "antenna_ref", "")
        if not ref:
            continue
        out.setdefault(ref, []).append(s)
    return out


def group_by_site_ref(snapshots: Iterable) -> dict[str, list]:
    """Group snapshots by their identity's site_ref.  Same rules
    as ``group_by_antenna_ref`` but using the coarser site scope.
    """
    out: dict[str, list] = {}
    for s in snapshots:
        ident = getattr(s, "identity", None)
        if ident is None:
            continue
        ref = getattr(ident, "site_ref", "")
        if not ref:
            continue
        out.setdefault(ref, []).append(s)
    return out


def cohort_median_position(
    snapshots: Iterable, antenna_ref: str,
) -> Optional[tuple[float, float, float, int]]:
    """Median (lat, lon, alt) across snapshots whose
    ``identity.antenna_ref == antenna_ref``.

    Returns ``(lat_med, lon_med, alt_med, n)`` or None if fewer
    than 2 peers have a position payload (median of 1 equals that
    peer's value, which isn't a consensus).
    """
    cohort = [s for s in snapshots
              if getattr(getattr(s, "identity", None), "antenna_ref", "")
              == antenna_ref]
    positions = [
        s.position for s in cohort
        if getattr(s, "position", None) is not None
        and s.position.lat_deg is not None
        and s.position.lon_deg is not None
        and s.position.alt_m is not None
    ]
    if len(positions) < 2:
        return None
    lats = sorted(p.lat_deg for p in positions)
    lons = sorted(p.lon_deg for p in positions)
    alts = sorted(p.alt_m for p in positions)
    return (_median_of_sorted(lats), _median_of_sorted(lons),
            _median_of_sorted(alts), len(positions))


def cohort_median_ztd(
    snapshots: Iterable, site_ref: str,
) -> Optional[tuple[float, int]]:
    """Median ZTD residual (metres) across snapshots whose
    ``identity.site_ref == site_ref``.

    Returns ``(ztd_med_m, n)`` or None if fewer than 2 peers have
    a ZTD payload.
    """
    cohort = [s for s in snapshots
              if getattr(getattr(s, "identity", None), "site_ref", "")
              == site_ref]
    ztds = [
        s.ztd.ztd_m for s in cohort
        if getattr(s, "ztd", None) is not None
        and s.ztd.ztd_m is not None
    ]
    if len(ztds) < 2:
        return None
    ztds_sorted = sorted(ztds)
    return (_median_of_sorted(ztds_sorted), len(ztds))


def ecef_distance_m(
    lat_a_deg: float, lon_a_deg: float, alt_a_m: float,
    lat_b_deg: float, lon_b_deg: float, alt_b_m: float,
) -> tuple[float, float]:
    """Return ``(horizontal_m, 3d_m)`` distance between two LLA
    points.  Flat-earth approximation at the mean latitude —
    accurate to sub-mm at intra-fleet scales (< 100 m apart).

    Used by consensus monitors that need ``|self − cohort_median|``
    as a trip condition.  Flat-earth is fine because we're
    measuring mm-to-dm across hosts sharing an antenna or at the
    same site.
    """
    d_lat_m = (lat_a_deg - lat_b_deg) * 111_320.0
    d_lon_m = ((lon_a_deg - lon_b_deg) * 111_320.0
               * math.cos(math.radians((lat_a_deg + lat_b_deg) / 2)))
    d_alt_m = alt_a_m - alt_b_m
    d_h = math.hypot(d_lat_m, d_lon_m)
    return d_h, math.sqrt(d_h * d_h + d_alt_m * d_alt_m)


def _median_of_sorted(xs: list[float]) -> float:
    """Median of an already-sorted list.  Returns the middle
    element for odd counts; mean of the two middle elements for
    even counts."""
    n = len(xs)
    mid = n // 2
    if n % 2 == 1:
        return xs[mid]
    return (xs[mid - 1] + xs[mid]) / 2.0
