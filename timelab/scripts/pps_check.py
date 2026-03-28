#!/usr/bin/env python3
"""
pps_check.py — Quick sanity check: verify PPS edges on both TICC channels.

Reports:
  - Per-channel edge rate (should be ~1 Hz)
  - chA−chB difference: mean (cable delay) and std (measurement noise floor)
  - Per-channel ref_ps std (includes reference oscillator wander — common-mode)

Usage:
    python pps_check.py /dev/ticc
    python pps_check.py /dev/ttyACM0 --duration 20
"""

import argparse
import re
import statistics
import sys
import time

import serial

_LINE_RE = re.compile(r"^(\d+)\.(\d{11,12})\s+(ch[AB])$")


def check_pps(port, baud=115200, duration=15):
    print(f"Checking PPS on {port} at {baud} baud for {duration}s...")
    try:
        ser = serial.Serial(port, baudrate=baud, timeout=2)
    except serial.SerialException as e:
        print(f"  FAIL: cannot open {port}: {e}")
        return False

    # Wait for TICC boot sentinel
    print("  Waiting for TICC boot (~12s)...")
    ser.reset_input_buffer()
    boot_start = time.monotonic()
    seen_sentinel = False
    while time.monotonic() - boot_start < 20:
        try:
            line = ser.readline().decode(errors="replace").strip()
        except Exception:
            continue
        if "# timestamp" in line:
            seen_sentinel = True
            continue
        if seen_sentinel and _LINE_RE.match(line):
            break
    if not seen_sentinel:
        print("  WARNING: no boot sentinel — TICC may not have rebooted")
    print(f"  Boot: {time.monotonic() - boot_start:.1f}s")

    # Collect edges keyed by (ref_sec, channel)
    edges = {}  # ref_sec → {chA: ps, chB: ps}
    counts = {"chA": 0, "chB": 0}
    start = time.monotonic()

    while time.monotonic() - start < duration:
        try:
            line = ser.readline().decode(errors="replace").strip()
        except Exception:
            continue
        m = _LINE_RE.match(line)
        if not m:
            continue
        ref_sec = int(m.group(1))
        ref_ps = int(m.group(2).ljust(12, "0"))
        ch = m.group(3)
        counts[ch] += 1
        if ref_sec not in edges:
            edges[ref_sec] = {}
        edges[ref_sec][ch] = ref_ps

    ser.close()
    elapsed = time.monotonic() - start

    # Compute chA - chB for paired seconds
    diffs_ps = []
    for sec in sorted(edges):
        if "chA" in edges[sec] and "chB" in edges[sec]:
            diffs_ps.append(edges[sec]["chA"] - edges[sec]["chB"])

    print(f"\n  Results ({elapsed:.0f}s):")

    # Rate check
    ok = True
    for ch in ("chA", "chB"):
        rate = counts[ch] / elapsed
        status = "OK" if 0.8 < rate < 1.2 else "FAIL"
        if counts[ch] == 0:
            status = "NO EDGES"
            ok = False
        print(f"    {ch}: {counts[ch]} edges, {rate:.2f} Hz — {status}")

    # Cross-channel agreement (the key metric)
    if len(diffs_ps) >= 3:
        mu = statistics.mean(diffs_ps)
        sigma = statistics.stdev(diffs_ps)
        # Outlier-rejected (3-sigma)
        clean = [d for d in diffs_ps if abs(d - mu) < 3 * sigma]
        if len(clean) >= 3:
            mu_c = statistics.mean(clean)
            sigma_c = statistics.stdev(clean)
        else:
            mu_c, sigma_c = mu, sigma
        n_outliers = len(diffs_ps) - len(clean)

        print(f"\n    chA−chB ({len(diffs_ps)} pairs):")
        print(f"      Mean:  {mu_c/1000:+.2f} ns  (cable delay offset)")
        print(f"      Std:   {sigma_c/1000:.3f} ns  ({sigma_c:.0f} ps)")
        if n_outliers:
            print(f"      Outliers: {n_outliers} rejected (3σ)")
        if sigma_c < 200:
            print(f"      → EXCELLENT (<200 ps, near TICC spec)")
        elif sigma_c < 1000:
            print(f"      → GOOD (<1 ns)")
        elif sigma_c < 5000:
            print(f"      → ELEVATED ({sigma_c/1000:.1f} ns — check splitter/cables/EMI)")
        else:
            print(f"      → HIGH ({sigma_c/1000:.1f} ns — likely a wiring problem)")
    else:
        print(f"\n    chA−chB: insufficient pairs ({len(diffs_ps)})")
        ok = False

    if ok:
        print(f"\n  ALL GOOD — both channels receiving PPS at ~1 Hz")
    else:
        print(f"\n  PROBLEM — check wiring")

    return ok


def main():
    ap = argparse.ArgumentParser(description="Quick PPS sanity check on TICC")
    ap.add_argument("port", help="TICC serial port (e.g. /dev/ticc)")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--duration", type=int, default=15,
                    help="Check duration in seconds (default: 15)")
    args = ap.parse_args()
    ok = check_pps(args.port, args.baud, args.duration)
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
