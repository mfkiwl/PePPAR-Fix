#!/usr/bin/env python3
"""Overlay two external PPP solution logs and report differences.

Engine-agnostic: takes any two JSONL files conforming to
docs/external-ppp-log-schema.md and reports time-aligned position
deltas plus fix-mode timelines.

Stdlib only — no numpy / pandas / matplotlib dependency, so this can
run on a bare lab host or a fresh venv without setup.

Usage:
    overlay_engine_solutions.py A.jsonl B.jsonl

    overlay_engine_solutions.py \\
        --align-tolerance-s 0.5 \\
        --csv-out aligned.csv \\
        peppar-fix-clkPoC3.jsonl bnc-pppw-ptpmon.jsonl

Output:
    - Stdout: summary statistics (count, mean / RMS / max ENU deltas,
      fix-mode crosstab, corrections_source labels).
    - Optional CSV at --csv-out: one row per matched epoch with
      epoch_unix, both engines' positions in ECEF and ENU-from-A,
      delta in ENU, fix_mode_a, fix_mode_b.

Exits non-zero if either input has < 1 valid record.
"""

from __future__ import annotations

import argparse
import csv
import json
import math
import sys
from dataclasses import dataclass
from pathlib import Path

# WGS-84
_A = 6378137.0
_F = 1.0 / 298.257223563
_E2 = _F * (2.0 - _F)


@dataclass
class Record:
    epoch_unix: float
    engine: str
    corrections_source: str
    fix_mode: str
    ecef: tuple[float, float, float]  # always populated; LLH-only inputs converted
    sigma_enu: tuple[float, float, float] | None
    n_used: int | None
    ar_ratio: float | None
    host: str | None


def llh_to_ecef(lat_deg: float, lon_deg: float, alt_m: float) -> tuple[float, float, float]:
    lat = math.radians(lat_deg)
    lon = math.radians(lon_deg)
    s = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * s * s)
    x = (n + alt_m) * math.cos(lat) * math.cos(lon)
    y = (n + alt_m) * math.cos(lat) * math.sin(lon)
    z = (n * (1.0 - _E2) + alt_m) * s
    return (x, y, z)


def ecef_to_llh(x: float, y: float, z: float) -> tuple[float, float, float]:
    """Bowring closed-form approximation, good to mm at terrestrial alts."""
    lon = math.atan2(y, x)
    p = math.hypot(x, y)
    if p < 1e-9:
        return (math.copysign(90.0, z), math.degrees(lon), abs(z) - _A)
    b = _A * (1.0 - _F)
    ep2 = (_A * _A - b * b) / (b * b)
    theta = math.atan2(z * _A, p * b)
    s, c = math.sin(theta), math.cos(theta)
    lat = math.atan2(z + ep2 * b * s * s * s, p - _E2 * _A * c * c * c)
    sl = math.sin(lat)
    n = _A / math.sqrt(1.0 - _E2 * sl * sl)
    alt = p / math.cos(lat) - n
    return (math.degrees(lat), math.degrees(lon), alt)


def ecef_delta_to_enu(
    dx: float, dy: float, dz: float, ref_lat_deg: float, ref_lon_deg: float
) -> tuple[float, float, float]:
    lat = math.radians(ref_lat_deg)
    lon = math.radians(ref_lon_deg)
    sl, cl = math.sin(lat), math.cos(lat)
    so, co = math.sin(lon), math.cos(lon)
    e = -so * dx + co * dy
    n = -sl * co * dx - sl * so * dy + cl * dz
    u = cl * co * dx + cl * so * dy + sl * dz
    return (e, n, u)


def parse_record(line: str, source: str, lineno: int) -> Record | None:
    s = line.strip()
    if not s or s.startswith("#"):
        return None
    try:
        d = json.loads(s)
    except json.JSONDecodeError as exc:
        print(f"WARN {source}:{lineno}: invalid JSON ({exc})", file=sys.stderr)
        return None

    required = ("epoch_unix", "engine", "corrections_source", "fix_mode", "pos")
    missing = [k for k in required if k not in d]
    if missing:
        print(f"WARN {source}:{lineno}: missing required fields {missing}", file=sys.stderr)
        return None

    pos = d["pos"]
    if "ecef" in pos:
        ecef = tuple(float(v) for v in pos["ecef"])
        if len(ecef) != 3:
            print(f"WARN {source}:{lineno}: pos.ecef wrong length", file=sys.stderr)
            return None
    elif "llh" in pos:
        llh = pos["llh"]
        ecef = llh_to_ecef(float(llh[0]), float(llh[1]), float(llh[2]))
    else:
        print(f"WARN {source}:{lineno}: pos missing ecef and llh", file=sys.stderr)
        return None

    sig = d.get("sigma")
    sigma_enu: tuple[float, float, float] | None = None
    if isinstance(sig, dict) and {"e", "n", "u"} <= sig.keys():
        sigma_enu = (float(sig["e"]), float(sig["n"]), float(sig["u"]))

    return Record(
        epoch_unix=float(d["epoch_unix"]),
        engine=str(d["engine"]),
        corrections_source=str(d["corrections_source"]),
        fix_mode=str(d["fix_mode"]),
        ecef=ecef,
        sigma_enu=sigma_enu,
        n_used=int(d["n_used"]) if "n_used" in d else None,
        ar_ratio=float(d["ar_ratio"]) if "ar_ratio" in d else None,
        host=str(d["host"]) if "host" in d else None,
    )


def load_jsonl(path: Path) -> list[Record]:
    out: list[Record] = []
    with path.open("r", encoding="utf-8") as f:
        for n, line in enumerate(f, 1):
            r = parse_record(line, str(path), n)
            if r is not None:
                out.append(r)
    out.sort(key=lambda r: r.epoch_unix)
    return out


def align(
    a: list[Record], b: list[Record], tol_s: float
) -> list[tuple[Record, Record]]:
    """Two-pointer time alignment within tolerance.  Each A epoch matches at
    most one B epoch (the closest within tol_s); unmatched epochs are
    silently dropped from the comparison."""
    out: list[tuple[Record, Record]] = []
    j = 0
    for ra in a:
        # advance j until b[j] is within window
        while j < len(b) and b[j].epoch_unix < ra.epoch_unix - tol_s:
            j += 1
        if j >= len(b):
            break
        if b[j].epoch_unix <= ra.epoch_unix + tol_s:
            # check if j+1 is closer
            best = j
            if j + 1 < len(b):
                d_now = abs(b[j].epoch_unix - ra.epoch_unix)
                d_next = abs(b[j + 1].epoch_unix - ra.epoch_unix)
                if d_next < d_now and d_next <= tol_s:
                    best = j + 1
            out.append((ra, b[best]))
    return out


def summarize(pairs: list[tuple[Record, Record]]) -> None:
    if not pairs:
        print("no aligned epochs")
        return

    a0 = pairs[0][0]
    b0 = pairs[0][1]
    print(f"Engine A: {a0.engine}  corrections={a0.corrections_source}  host={a0.host}")
    print(f"Engine B: {b0.engine}  corrections={b0.corrections_source}  host={b0.host}")
    print(f"Aligned epochs: {len(pairs)}")
    print()

    # Reference for ENU = mean of A's ECEF
    ax = sum(p[0].ecef[0] for p in pairs) / len(pairs)
    ay = sum(p[0].ecef[1] for p in pairs) / len(pairs)
    az = sum(p[0].ecef[2] for p in pairs) / len(pairs)
    ref_lat, ref_lon, _ = ecef_to_llh(ax, ay, az)

    enu_deltas: list[tuple[float, float, float]] = []
    for ra, rb in pairs:
        de = rb.ecef[0] - ra.ecef[0]
        dn = rb.ecef[1] - ra.ecef[1]
        du = rb.ecef[2] - ra.ecef[2]
        enu_deltas.append(ecef_delta_to_enu(de, dn, du, ref_lat, ref_lon))

    def stats(vals: list[float]) -> tuple[float, float, float]:
        n = len(vals)
        m = sum(vals) / n
        rms = math.sqrt(sum(v * v for v in vals) / n)
        mx = max(vals, key=abs)
        return (m, rms, mx)

    es = [d[0] for d in enu_deltas]
    ns = [d[1] for d in enu_deltas]
    us = [d[2] for d in enu_deltas]
    h = [math.hypot(d[0], d[1]) for d in enu_deltas]
    three_d = [math.sqrt(d[0] ** 2 + d[1] ** 2 + d[2] ** 2) for d in enu_deltas]

    print("ENU delta (B - A), meters:")
    print(f"            mean       rms       max(|.|)")
    for label, vals in (("east", es), ("north", ns), ("up", us)):
        m, r, mx = stats(vals)
        print(f"  {label:6s}  {m:+8.4f}  {r:8.4f}  {mx:+9.4f}")
    print(f"  horiz   {sum(h)/len(h):8.4f}  {math.sqrt(sum(v*v for v in h)/len(h)):8.4f}  {max(h):9.4f}")
    print(f"  3D      {sum(three_d)/len(three_d):8.4f}  {math.sqrt(sum(v*v for v in three_d)/len(three_d)):8.4f}  {max(three_d):9.4f}")
    print()

    # Fix-mode crosstab
    modes_a = sorted({p[0].fix_mode for p in pairs})
    modes_b = sorted({p[1].fix_mode for p in pairs})
    crosstab: dict[tuple[str, str], int] = {}
    for ra, rb in pairs:
        key = (ra.fix_mode, rb.fix_mode)
        crosstab[key] = crosstab.get(key, 0) + 1

    print("Fix-mode crosstab (rows=A, cols=B):")
    header = f"  {'A\\B':>10s}  " + "  ".join(f"{m:>10s}" for m in modes_b)
    print(header)
    for ma in modes_a:
        row = f"  {ma:>10s}  " + "  ".join(
            f"{crosstab.get((ma, mb), 0):>10d}" for mb in modes_b
        )
        print(row)


def write_csv(pairs: list[tuple[Record, Record]], path: Path) -> None:
    if not pairs:
        return
    ax = sum(p[0].ecef[0] for p in pairs) / len(pairs)
    ay = sum(p[0].ecef[1] for p in pairs) / len(pairs)
    az = sum(p[0].ecef[2] for p in pairs) / len(pairs)
    ref_lat, ref_lon, _ = ecef_to_llh(ax, ay, az)

    with path.open("w", encoding="utf-8", newline="") as f:
        w = csv.writer(f)
        w.writerow(
            [
                "epoch_unix",
                "a_ecef_x", "a_ecef_y", "a_ecef_z",
                "b_ecef_x", "b_ecef_y", "b_ecef_z",
                "delta_e_m", "delta_n_m", "delta_u_m",
                "delta_3d_m",
                "a_fix_mode", "b_fix_mode",
                "a_ar_ratio", "b_ar_ratio",
                "a_n_used", "b_n_used",
            ]
        )
        for ra, rb in pairs:
            de = rb.ecef[0] - ra.ecef[0]
            dn = rb.ecef[1] - ra.ecef[1]
            du = rb.ecef[2] - ra.ecef[2]
            e, n, u = ecef_delta_to_enu(de, dn, du, ref_lat, ref_lon)
            w.writerow(
                [
                    f"{ra.epoch_unix:.3f}",
                    *(f"{v:.6f}" for v in ra.ecef),
                    *(f"{v:.6f}" for v in rb.ecef),
                    f"{e:.6f}", f"{n:.6f}", f"{u:.6f}",
                    f"{math.sqrt(e*e + n*n + u*u):.6f}",
                    ra.fix_mode, rb.fix_mode,
                    "" if ra.ar_ratio is None else f"{ra.ar_ratio:.3f}",
                    "" if rb.ar_ratio is None else f"{rb.ar_ratio:.3f}",
                    "" if ra.n_used is None else ra.n_used,
                    "" if rb.n_used is None else rb.n_used,
                ]
            )


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.split("\n\n", 1)[0])
    p.add_argument("a", type=Path, help="JSONL file from engine A")
    p.add_argument("b", type=Path, help="JSONL file from engine B")
    p.add_argument(
        "--align-tolerance-s",
        type=float,
        default=0.5,
        help="max time gap (seconds) to consider two epochs aligned (default: 0.5)",
    )
    p.add_argument(
        "--csv-out",
        type=Path,
        default=None,
        help="write per-epoch aligned CSV here",
    )
    args = p.parse_args()

    a = load_jsonl(args.a)
    b = load_jsonl(args.b)
    if not a:
        print(f"ERROR: no valid records in {args.a}", file=sys.stderr)
        return 1
    if not b:
        print(f"ERROR: no valid records in {args.b}", file=sys.stderr)
        return 1

    pairs = align(a, b, args.align_tolerance_s)
    summarize(pairs)
    if args.csv_out is not None:
        write_csv(pairs, args.csv_out)
        print(f"\nwrote {len(pairs)} aligned epochs to {args.csv_out}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
