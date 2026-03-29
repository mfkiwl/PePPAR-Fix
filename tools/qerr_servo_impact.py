#!/usr/bin/env python3
"""Analyze how much variance the servo adds vs qErr signal at each run phase.

Reads a servo CSV and computes first-difference variance of:
  - raw pps_error (includes servo drift)
  - rate-compensated pps_error (adjfine subtracted)
  - rate-compensated pps+qErr

The ratio compensated/compensated+qErr shows qErr improvement
independent of servo state.
"""
import csv
import sys
from statistics import pvariance


def analyze(csv_path):
    with open(csv_path) as f:
        rows = list(csv.DictReader(f))

    print("Total epochs:", len(rows))
    print()

    windows = [
        ("Pull-in (1-100)", 0, 100),
        ("Glide (100-300)", 100, 300),
        ("Settling (300-600)", 300, 600),
        ("Tracking (600-1200)", 600, 1200),
        ("Steady (1200-2400)", 1200, 2400),
        ("Late steady (2400+)", 2400, len(rows)),
    ]

    fmt = "%25s %10s %10s %10s %10s %8s %8s"
    print(fmt % ("Phase", "|dadj| ppb", "dv_raw", "dv_comp", "dv_+q_c",
                 "r_raw", "r_comp"))
    print("-" * 90)

    for label, start, end in windows:
        subset = rows[start:end]
        if len(subset) < 22:
            continue

        # Use last 21 rows -> 20 diffs
        n = min(21, len(subset))
        tail = subset[-n:]

        diffs_raw = []
        diffs_comp = []
        diffs_plus_comp = []
        adj_changes = []

        for i in range(1, len(tail)):
            pps0 = float(tail[i - 1]["pps_error_ns"])
            pps1 = float(tail[i]["pps_error_ns"])
            adj0 = float(tail[i - 1]["adjfine_ppb"])
            adj1 = float(tail[i]["adjfine_ppb"])

            d_raw = pps1 - pps0
            d_comp = d_raw - adj0  # remove expected PHC drift
            diffs_raw.append(d_raw)
            diffs_comp.append(d_comp)
            adj_changes.append(abs(adj1 - adj0))

            q0_s = tail[i - 1].get("qerr_ns", "")
            q1_s = tail[i].get("qerr_ns", "")
            if q0_s and q1_s:
                q0 = float(q0_s)
                q1 = float(q1_s)
                d_plus_comp = (pps1 + q1) - (pps0 + q0) - adj0
                diffs_plus_comp.append(d_plus_comp)

        if len(diffs_raw) < 3 or len(diffs_plus_comp) < 3:
            continue

        vr = pvariance(diffs_raw)
        vc = pvariance(diffs_comp)
        vp = pvariance(diffs_plus_comp[:len(diffs_comp)])
        avg_adj_change = sum(adj_changes) / len(adj_changes)

        rr = vr / vp if vp > 0 else 0
        rc = vc / vp if vp > 0 else 0

        print(fmt % (label,
                     "%.1f" % avg_adj_change,
                     "%.1f" % vr,
                     "%.1f" % vc,
                     "%.1f" % vp,
                     "%.3f" % rr,
                     "%.3f" % rc))

    print()
    print("dv_raw    = first-diff variance of raw pps_error (includes servo)")
    print("dv_comp   = first-diff variance after subtracting adjfine rate")
    print("dv_+q_c   = first-diff variance of (pps+qErr) after subtracting adjfine rate")
    print("r_raw     = dv_raw / dv_+q_c   (>1 = qErr helps, swamped by servo noise)")
    print("r_comp    = dv_comp / dv_+q_c   (>1 = qErr helps, servo removed)")


if __name__ == "__main__":
    analyze(sys.argv[1] if len(sys.argv) > 1 else "/tmp/1hr-servo.csv")
