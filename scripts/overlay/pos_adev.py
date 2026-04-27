#!/usr/bin/env python3
"""Allan-deviation-style σ(τ) for position time series.

Borrows the standard ADEV form from oscillator stability analysis
and applies it to E/N/U position estimates from a static GNSS
receiver:

    σ_pos(τ) = sqrt( 0.5 · < (x̄_{k+1}(τ) − x̄_k(τ))² > )

where x̄_k(τ) is the mean position over the k-th window of length τ.
The 0.5 prefactor is the classic Allan convention; the
overlapping-windows estimator (each k advancing by one sample) is
used for statistical efficiency on short records.

At short τ this captures epoch-to-epoch noise; at long τ it
captures systematic drift.  For a perfectly stable receiver,
σ_pos(τ) → 0 as τ grows.  For a drifting filter, σ_pos(τ) either
plateaus (bounded random walk) or grows monotonically
(deterministic drift).

The metric isn't novel — geodesy has been using PSD slope κ
(`P(f) ∝ f^−κ`) and MLE noise model fits since Williams 2003.  See
`memory/project_to_main_bravo_charlie_position_stability_lit_20260426`
for the literature synthesis and how σ_pos(τ) maps to the
geodetic vocabulary.

Inputs supported:
  - peppar-fix engine `[AntPosEst]` log lines (lat, lon, alt)
  - BNC `bnc.log` `F9T_PTPMON X = ...` lines (ECEF)
Auto-detected by default; override with `--mode {engine,bnc}`.

Both inputs are converted to a common ENU frame relative to the
run mean before σ(τ) is computed, then regridded to 1 Hz.

Usage:
    pos_adev.py engine.log
    pos_adev.py --mode bnc bnc.log
    pos_adev.py --label clkPoC3 engine.log
    pos_adev.py --taus 1,5,30,60,300,1800 engine.log
    pos_adev.py --format json engine.log > stability.json

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import math
import re
import sys
from datetime import datetime

# WGS-84
A = 6378137.0
F = 1.0 / 298.257223563
E2 = F * (2 - F)
B = A * (1 - F)


def llh_to_ecef(lat: float, lon: float, alt: float) -> tuple[float, float, float]:
    lat_r, lon_r = math.radians(lat), math.radians(lon)
    s = math.sin(lat_r)
    n = A / math.sqrt(1 - E2 * s * s)
    return (
        (n + alt) * math.cos(lat_r) * math.cos(lon_r),
        (n + alt) * math.cos(lat_r) * math.sin(lon_r),
        (n * (1 - E2) + alt) * s,
    )


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Bowring closed-form approximation, mm-accurate at terrestrial alt."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:
        return (math.copysign(90.0, z), math.degrees(lon), abs(z) - A)
    ep2 = (A * A - B * B) / (B * B)
    th = math.atan2(z * A, p * B)
    sl, cl = math.sin(th), math.cos(th)
    lat = math.atan2(z + ep2 * B * sl ** 3, p - E2 * A * cl ** 3)
    sl2 = math.sin(lat)
    n = A / math.sqrt(1 - E2 * sl2 * sl2)
    alt = p / math.cos(lat) - n
    return (math.degrees(lat), math.degrees(lon), alt)


def ecef_delta_to_enu(
    dx: float, dy: float, dz: float, ref_lat: float, ref_lon: float
) -> tuple[float, float, float]:
    lat_r = math.radians(ref_lat)
    lon_r = math.radians(ref_lon)
    sl, cl = math.sin(lat_r), math.cos(lat_r)
    so, co = math.sin(lon_r), math.cos(lon_r)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    return e, n, u


# ── Log parsers ──────────────────────────────────────────────────────────── #

_ENGINE_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})\s+(\d{2}:\d{2}:\d{2})[,.]?\d*\s+\S+\s+"
    r"AntPosEst position improved:\s+\S+=([\d.]+)m\s+"
    r"\(([-+]?\d+\.\d+),\s*([-+]?\d+\.\d+),\s*([-+]?\d+\.\d+)\)"
)
_BNC_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})_(\d{2}:\d{2}:\d{2}(?:\.\d+)?)\s+F9T_PTPMON\s+"
    r"X = ([-+]?\d+\.\d+) Y = ([-+]?\d+\.\d+) Z = ([-+]?\d+\.\d+)"
)


def parse_engine(path: str) -> list[tuple[float, float, float, float]]:
    """Return [(epoch_unix, ecef_x, ecef_y, ecef_z), ...]."""
    out = []
    with open(path) as f:
        for line in f:
            m = _ENGINE_RE.search(line)
            if m:
                d, t, _sig, lat, lon, alt = m.groups()
                ep = datetime.fromisoformat(f"{d}T{t}").timestamp()
                x, y, z = llh_to_ecef(float(lat), float(lon), float(alt))
                out.append((ep, x, y, z))
    return out


def parse_bnc(path: str) -> list[tuple[float, float, float, float]]:
    """Return [(epoch_unix, ecef_x, ecef_y, ecef_z), ...]."""
    out = []
    with open(path) as f:
        for line in f:
            m = _BNC_RE.search(line)
            if m:
                d, t, x, y, z = m.groups()
                ep = datetime.fromisoformat(f"{d}T{t}").timestamp()
                out.append((ep, float(x), float(y), float(z)))
    return out


def autodetect_mode(path: str) -> str:
    """Sniff the file: count engine vs BNC lines in the first 200 matches.

    Returns 'engine' or 'bnc'.  Raises if neither pattern matches.
    """
    e = b = 0
    with open(path) as f:
        for line in f:
            if _ENGINE_RE.search(line):
                e += 1
            elif _BNC_RE.search(line):
                b += 1
            if e + b >= 200:
                break
    if e == 0 and b == 0:
        raise ValueError(
            f"no engine or BNC log lines matched in {path}; "
            f"specify --mode explicitly if format is unusual"
        )
    return "engine" if e >= b else "bnc"


# ── Frame conversion + regridding ────────────────────────────────────────── #

def to_enu(samples):
    """Convert ECEF samples to ENU around the run-mean position."""
    n = len(samples)
    rx = sum(s[1] for s in samples) / n
    ry = sum(s[2] for s in samples) / n
    rz = sum(s[3] for s in samples) / n
    ref_lat, ref_lon, _ = ecef_to_llh(rx, ry, rz)
    out = []
    for ep, x, y, z in samples:
        e, n_, u = ecef_delta_to_enu(x - rx, y - ry, z - rz, ref_lat, ref_lon)
        out.append((ep, e, n_, u))
    return out


def regrid(samples, dt: float = 1.0):
    """Bucket samples into a uniform grid of dt seconds.

    Engine logs run at 1 Hz; BNC at 0.5 Hz.  Regridding to a common
    cadence (default 1 s) gives a comparable time axis across
    sources without aliasing.  Multiple samples in the same bucket
    are averaged.  Empty buckets aren't emitted (gaps remain gaps).
    """
    if not samples:
        return []
    t0 = samples[0][0]
    buckets: dict[int, list] = {}
    for ep, e, n, u in samples:
        k = int((ep - t0) / dt)
        buckets.setdefault(k, []).append((e, n, u))
    out = []
    for k in sorted(buckets):
        rows = buckets[k]
        nrows = len(rows)
        e = sum(r[0] for r in rows) / nrows
        n = sum(r[1] for r in rows) / nrows
        u = sum(r[2] for r in rows) / nrows
        out.append((t0 + k * dt, e, n, u))
    return out


# ── The estimator ────────────────────────────────────────────────────────── #

def adev_pos_overlapping(values: list[float], m: int) -> float | None:
    """Overlapping Allan deviation of position averages at window m samples.

    σ²(m·τ₀) = 0.5 · < (x̄_{k+m}(m) − x̄_k(m))² >

    where x̄_k(m) is the mean of m consecutive samples starting at k,
    and adjacent windows are separated by m samples (non-overlapping
    in the difference, but overlapping in the start indices).

    Returns None if N < 2m (not enough data for any pair of
    non-overlapping windows).
    """
    n = len(values)
    if n < 2 * m:
        return None
    # Rolling sum for window means.
    csum = [0.0] * (n + 1)
    for i, v in enumerate(values):
        csum[i + 1] = csum[i] + v
    means = [(csum[i + m] - csum[i]) / m for i in range(n - m + 1)]
    diffs = [means[k + m] - means[k] for k in range(len(means) - m)]
    if not diffs:
        return None
    var = 0.5 * sum(d * d for d in diffs) / len(diffs)
    return math.sqrt(var)


def mdev_pos_overlapping(values: list[float], m: int) -> float | None:
    """Modified Allan deviation of position averages at window m samples.

    Canonical MDEV form (Allan & Barnes 1981) applied to position-as-
    value (no τ² normalization, since position is the quantity of
    interest, not phase):

        σ²_pos_mdev(m·τ₀) = 1 / (2 m² (N − 3m + 1)) ·
                            Σ_j ( Σ_{i=j}^{j+m-1}
                                  [x_{i+2m} − 2 x_{i+m} + x_i] )²

    Equivalently: form the m-sample running-mean series y_k, then
    take the second difference y_{k+2m} − 2 y_{k+m} + y_k.  MDEV is
    the RMS of those second differences across all valid starting
    positions k, divided by sqrt(2).

    Why use it: per the lit synthesis in
    project_to_main_bravo_charlie_position_stability_lit_20260426,
    MDEV separates white-PM from flicker-PM that ADEV conflates —
    more diagnostic for the white-noise-plus-walks regime that lab
    position estimates exhibit.  At short τ:
        white-position:    ADEV slope −0.5,  MDEV slope −1.5
        flicker-position:  ADEV slope ~0,    MDEV slope −0.5

    Returns None if N < 3m (not enough data).
    """
    n = len(values)
    if n < 3 * m:
        return None
    csum = [0.0] * (n + 1)
    for i, v in enumerate(values):
        csum[i + 1] = csum[i] + v
    # Running m-sample sums (we'll divide by m at variance time).
    # window_sum[k] = csum[k+m] - csum[k] = Σ_{i=k}^{k+m-1} x_i
    n_dd = n - 3 * m + 1
    if n_dd < 1:
        return None
    total = 0.0
    for k in range(n_dd):
        # Second difference of the m-sample windowed sums.
        # Each sum is m × the running mean; squaring picks up m² which
        # cancels with the m² in the denominator below.
        a = csum[k + m] - csum[k]
        b = csum[k + 2 * m] - csum[k + m]
        c = csum[k + 3 * m] - csum[k + 2 * m]
        dd = c - 2 * b + a
        total += dd * dd
    var = total / (2.0 * m * m * n_dd)
    return math.sqrt(var)


# ── CLI ──────────────────────────────────────────────────────────────────── #

DEFAULT_TAUS = (2, 4, 8, 16, 32, 60, 120, 240, 480, 1000, 1800, 3600)


def parse_taus(s: str) -> list[int]:
    return sorted({int(x) for x in s.split(",") if x.strip()})


def main() -> int:
    p = argparse.ArgumentParser(
        description=__doc__.split("\n\n", 1)[0],
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p.add_argument("path", help="path to engine [AntPosEst] log or BNC bnc.log")
    p.add_argument(
        "--mode", choices=("auto", "engine", "bnc"), default="auto",
        help="input format (default: auto-detect)",
    )
    p.add_argument(
        "--label", default=None,
        help="label for the output (default: filename)",
    )
    p.add_argument(
        "--taus", type=parse_taus, default=list(DEFAULT_TAUS),
        help="comma-separated τ values in seconds (default: %(default)s)",
    )
    p.add_argument(
        "--regrid-dt", type=float, default=1.0,
        help="regrid bucket size in seconds (default: 1.0)",
    )
    p.add_argument(
        "--format", choices=("text", "json"), default="text",
        help="output format (default: text)",
    )
    p.add_argument(
        "--metric", choices=("adev", "mdev", "both"), default="both",
        help="stability metric (default: both — ADEV is intuitive, MDEV "
             "separates white-PM from flicker-PM)",
    )
    args = p.parse_args()

    mode = args.mode
    if mode == "auto":
        try:
            mode = autodetect_mode(args.path)
        except ValueError as e:
            print(f"ERROR: {e}", file=sys.stderr)
            return 2
        print(f"# auto-detected mode: {mode}", file=sys.stderr)

    samples = parse_engine(args.path) if mode == "engine" else parse_bnc(args.path)
    if len(samples) < 30:
        print(
            f"ERROR: only {len(samples)} samples parsed from {args.path}; "
            f"need ≥30 for σ(τ)",
            file=sys.stderr,
        )
        return 1

    enu = to_enu(samples)
    grid = regrid(enu, dt=args.regrid_dt)
    duration = grid[-1][0] - grid[0][0]
    label = args.label or args.path.split("/")[-1]

    es = [r[1] for r in grid]
    ns = [r[2] for r in grid]
    us = [r[3] for r in grid]

    want_adev = args.metric in ("adev", "both")
    want_mdev = args.metric in ("mdev", "both")

    rows = []
    skipped = []
    for tau in args.taus:
        m_samples = max(1, int(tau / args.regrid_dt))
        row: dict = {"tau_s": tau}
        ok = True
        if want_adev:
            ae = adev_pos_overlapping(es, m_samples)
            an = adev_pos_overlapping(ns, m_samples)
            au = adev_pos_overlapping(us, m_samples)
            if ae is None or an is None or au is None:
                ok = False
            else:
                row["adev_e_m"] = ae
                row["adev_n_m"] = an
                row["adev_u_m"] = au
                row["adev_h_m"] = math.sqrt(ae * ae + an * an)
        if want_mdev and ok:
            me = mdev_pos_overlapping(es, m_samples)
            mn = mdev_pos_overlapping(ns, m_samples)
            mu = mdev_pos_overlapping(us, m_samples)
            if me is None or mn is None or mu is None:
                ok = False
            else:
                row["mdev_e_m"] = me
                row["mdev_n_m"] = mn
                row["mdev_u_m"] = mu
                row["mdev_h_m"] = math.sqrt(me * me + mn * mn)
        if not ok:
            skipped.append(tau)
            continue
        rows.append(row)

    if args.format == "json":
        out = {
            "label": label,
            "mode": mode,
            "samples": len(grid),
            "duration_s": duration,
            "regrid_dt_s": args.regrid_dt,
            "metric": args.metric,
            "rows": rows,
            "skipped_taus": skipped,
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"# {label}: {len(grid)} samples on {args.regrid_dt:g}-s grid, "
              f"{duration / 60:.1f} min ({mode}, metric={args.metric})")
        if args.metric == "adev":
            print(f"# tau (s)   σ_E (m)   σ_N (m)   σ_U (m)   σ_H (m)")
            for r in rows:
                print(f"  {r['tau_s']:>7d}   {r['adev_e_m']:>7.3f}   "
                      f"{r['adev_n_m']:>7.3f}   {r['adev_u_m']:>7.3f}   "
                      f"{r['adev_h_m']:>7.3f}")
        elif args.metric == "mdev":
            print(f"# tau (s)   σ_E (m)   σ_N (m)   σ_U (m)   σ_H (m)")
            for r in rows:
                print(f"  {r['tau_s']:>7d}   {r['mdev_e_m']:>7.3f}   "
                      f"{r['mdev_n_m']:>7.3f}   {r['mdev_u_m']:>7.3f}   "
                      f"{r['mdev_h_m']:>7.3f}")
        else:  # both
            print(f"# {'':>7s}   {'ADEV (m)':^33s}   {'MDEV (m)':^33s}")
            print(f"# tau (s)    σ_E      σ_N      σ_U      σ_H        σ_E      σ_N      σ_U      σ_H")
            for r in rows:
                print(
                    f"  {r['tau_s']:>7d}   "
                    f"{r['adev_e_m']:>7.3f}  {r['adev_n_m']:>7.3f}  "
                    f"{r['adev_u_m']:>7.3f}  {r['adev_h_m']:>7.3f}    "
                    f"{r['mdev_e_m']:>7.3f}  {r['mdev_n_m']:>7.3f}  "
                    f"{r['mdev_u_m']:>7.3f}  {r['mdev_h_m']:>7.3f}"
                )
        if skipped:
            need_str = "≥3τ window for MDEV" if want_mdev else "≥2τ window for ADEV"
            print(
                f"# skipped τ (insufficient data, need {need_str}): "
                f"{', '.join(str(t) for t in skipped)}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
