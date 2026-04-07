#!/usr/bin/env python3
"""Build a per-host DO characterization file from a freerun servo CSV.

Reads a `peppar-fix --freerun` log, computes PSDs of every error
source, and writes a JSON characterization file with summary stats,
sparse PSD curves, and recommended loop bandwidths.

Usage:
    python3 tools/build_do_characterization.py \\
        --input data/freerun_char.csv \\
        --output data/do_characterization.json \\
        --host TimeHat \\
        --do-label "i226 TCXO"
"""

import argparse
import json
import os
import socket
import sys
import time

import numpy as np

# Import PSD primitives from sibling tool
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from plot_psd import load_error_sources, welch_psd, fit_slope


# Sparse log-spaced frequencies for stored PSD curves.  Enough to see
# crossover frequencies and slopes; not enough to plot a smooth curve.
SPARSE_FREQS_HZ = [
    0.001, 0.002, 0.003, 0.005, 0.007,
    0.01, 0.02, 0.03, 0.05, 0.07,
    0.1, 0.15, 0.2, 0.3, 0.4, 0.5,
]


def _classify_slope(slope):
    """Map a log-log PSD slope to a noise type label."""
    if slope is None:
        return None
    if slope > -0.5:
        return "white_phase"
    if slope > -1.5:
        return "flicker_phase"
    if slope > -2.5:
        return "white_FM"
    if slope > -3.5:
        return "flicker_FM"
    return "random_walk_FM"


def _interpolate_asd(freqs, asd, target_freqs):
    """Log-log interpolate ASD onto target frequencies."""
    valid = np.isfinite(asd) & (asd > 0) & (freqs > 0)
    if valid.sum() < 3:
        return [None] * len(target_freqs)
    lf = np.log10(freqs[valid])
    la = np.log10(asd[valid])
    out = []
    for tf in target_freqs:
        if tf < freqs[valid].min() or tf > freqs[valid].max():
            out.append(None)
            continue
        v = float(10 ** np.interp(np.log10(tf), lf, la))
        out.append(v)
    return out


def _summary_for_source(name, sig, units):
    """Compute PSD and summary stats for one error source."""
    f, p = welch_psd(sig)
    if f is None:
        return None
    asd = np.sqrt(p)
    slope = fit_slope(f, p, fmin=0.01, fmax=0.3)
    asd_at_01 = float(asd[np.argmin(np.abs(f - 0.1))])
    asd_at_001 = float(asd[np.argmin(np.abs(f - 0.01))])
    rms = float(np.sqrt(np.trapezoid(p, f)))
    sparse = _interpolate_asd(f, asd, SPARSE_FREQS_HZ)
    return {
        "units": units,
        "rms": round(rms, 4),
        "asd_at_0.1Hz": round(asd_at_01, 4),
        "asd_at_0.01Hz": round(asd_at_001, 4),
        "slope": round(slope, 3) if slope is not None else None,
        "noise_type": _classify_slope(slope),
        "psd_curve": [
            [tf, (round(v, 4) if v is not None else None)]
            for tf, v in zip(SPARSE_FREQS_HZ, sparse)
        ],
    }


def _crossover_hz(asd_a, asd_b, target_freqs):
    """Find frequency where asd_a crosses asd_b (log-log interp).

    Returns the frequency where the two curves are equal, or None if
    they don't cross within the measured band.
    """
    pairs = [(f, a, b) for f, a, b in zip(target_freqs, asd_a, asd_b)
             if a is not None and b is not None]
    if len(pairs) < 2:
        return None
    # Look for sign change in log(a) - log(b)
    diffs = [(f, np.log10(a) - np.log10(b)) for f, a, b in pairs]
    for i in range(len(diffs) - 1):
        f1, d1 = diffs[i]
        f2, d2 = diffs[i + 1]
        if d1 == 0:
            return float(f1)
        if (d1 > 0) != (d2 > 0):
            # Linear interpolation in log frequency
            lf1, lf2 = np.log10(f1), np.log10(f2)
            cross_lf = lf1 - d1 * (lf2 - lf1) / (d2 - d1)
            return float(round(10 ** cross_lf, 4))
    return None


def build_characterization(servo_csv, host, do_label, phc_dev=None):
    """Build the characterization dict from a servo CSV."""
    sources = load_error_sources(servo_csv)
    if not sources:
        raise RuntimeError(f"no usable error sources in {servo_csv}")

    # Count rows for duration estimate
    with open(servo_csv) as f:
        n_rows = sum(1 for _ in f) - 1  # minus header

    sources_out = {}
    for name, (sig, units) in sources.items():
        summary = _summary_for_source(name, sig, units)
        if summary is not None:
            sources_out[name] = summary

    # Crossovers between candidate inputs (where they intersect)
    # We pick the most useful pair-wise crossovers between phase
    # error sources (units == ns).
    crossovers = {}
    ns_sources = {n: s for n, s in sources_out.items() if s["units"] == "ns"}
    pairs = [
        ("dt_rx (PPP)", "qerr (TIM-TP)"),
        ("dt_rx (PPP)", "PPS"),
        ("dt_rx (PPP)", "PPS+qErr"),
        ("Carrier", "qerr (TIM-TP)"),
    ]
    for a, b in pairs:
        if a in ns_sources and b in ns_sources:
            asd_a = [v for _, v in ns_sources[a]["psd_curve"]]
            asd_b = [v for _, v in ns_sources[b]["psd_curve"]]
            cross = _crossover_hz(asd_a, asd_b, SPARSE_FREQS_HZ)
            if cross is not None:
                crossovers[f"{a} vs {b}"] = cross

    return {
        "host": host or socket.gethostname(),
        "phc": phc_dev,
        "do_label": do_label,
        "captured": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        "duration_s": n_rows,
        "n_samples": n_rows,
        "sources": sources_out,
        "crossovers": crossovers,
        "notes": (
            "PSD slopes: ~0=white phase, -1=flicker phase, -2=white FM, "
            "-3=flicker FM. Lower frequency rows in psd_curve may be "
            "absent if the freerun was too short to resolve them. "
            "Use plot_psd.py for full curves."
        ),
    }


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--input", required=True,
                    help="freerun servo CSV path")
    ap.add_argument("--output", required=True,
                    help="output characterization JSON path")
    ap.add_argument("--host", default=None,
                    help="host label (default: hostname)")
    ap.add_argument("--do-label", default="unknown",
                    help="DO description (e.g., 'i226 TCXO')")
    ap.add_argument("--phc", default=None,
                    help="PHC device path (e.g., /dev/ptp0)")
    args = ap.parse_args()

    char = build_characterization(args.input, args.host, args.do_label,
                                  args.phc)
    os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
    with open(args.output, "w") as f:
        json.dump(char, f, indent=2)

    # Print a brief summary
    print(f"Wrote {args.output}")
    print(f"  host:     {char['host']}")
    print(f"  DO:       {char['do_label']}")
    print(f"  duration: {char['duration_s']} s")
    print(f"  sources:  {len(char['sources'])}")
    for name, s in char["sources"].items():
        slope = f"{s['slope']:+.2f}" if s["slope"] is not None else "n/a"
        print(f"    {name:<24} ASD@0.1Hz={s['asd_at_0.1Hz']:>9.4f} "
              f"{s['units']}/√Hz  slope={slope}  ({s['noise_type']})")
    if char["crossovers"]:
        print("  crossovers:")
        for pair, hz in char["crossovers"].items():
            print(f"    {pair}: {hz} Hz")


if __name__ == "__main__":
    main()
