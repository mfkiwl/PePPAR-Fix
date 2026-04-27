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

    rows = []
    skipped = []
    for tau in args.taus:
        m_samples = max(1, int(tau / args.regrid_dt))
        sig_e = adev_pos_overlapping(es, m_samples)
        sig_n = adev_pos_overlapping(ns, m_samples)
        sig_u = adev_pos_overlapping(us, m_samples)
        if sig_e is None or sig_n is None or sig_u is None:
            skipped.append(tau)
            continue
        sig_h = math.sqrt(sig_e * sig_e + sig_n * sig_n)
        rows.append({
            "tau_s": tau,
            "sigma_e_m": sig_e,
            "sigma_n_m": sig_n,
            "sigma_u_m": sig_u,
            "sigma_h_m": sig_h,
        })

    if args.format == "json":
        out = {
            "label": label,
            "mode": mode,
            "samples": len(grid),
            "duration_s": duration,
            "regrid_dt_s": args.regrid_dt,
            "rows": rows,
            "skipped_taus": skipped,
        }
        json.dump(out, sys.stdout, indent=2)
        sys.stdout.write("\n")
    else:
        print(f"# {label}: {len(grid)} samples on {args.regrid_dt:g}-s grid, "
              f"{duration / 60:.1f} min ({mode})")
        print(f"# tau (s)   σ_E (m)   σ_N (m)   σ_U (m)   σ_H (m)")
        for r in rows:
            print(
                f"  {r['tau_s']:>7d}   {r['sigma_e_m']:>7.3f}   "
                f"{r['sigma_n_m']:>7.3f}   {r['sigma_u_m']:>7.3f}   "
                f"{r['sigma_h_m']:>7.3f}"
            )
        if skipped:
            print(
                f"# skipped τ (insufficient data, need ≥2τ window): "
                f"{', '.join(str(t) for t in skipped)}",
                file=sys.stderr,
            )
    return 0


if __name__ == "__main__":
    sys.exit(main())
