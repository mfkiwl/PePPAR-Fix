#!/usr/bin/env python3
"""Sweep adjfine and measure actual PEROUT frequency via TICC.

Answers: is the i226 PHC output frequency quantized, or smooth?
If quantized, you'll see a staircase (same TICC period for multiple
adjfine values).  If smooth, you'll see a linear ramp.

Usage:
    sudo python3 tools/adjfine_sweep.py --ptp-dev /dev/ptp0 \
        --ticc-port /dev/ticc1 --start-ppb -100 --stop-ppb 100 \
        --step-ppb 1 --dwell-s 10

Each step:
  1. Set adjfine to the current value
  2. Wait dwell_s seconds for the TICC to measure ~dwell_s chA edges
  3. Compute the mean chA period from consecutive edges
  4. Record (adjfine_ppb, measured_period_ps, n_edges)

Output: CSV to stdout + optional plot.
"""

import argparse
import csv
import sys
import time

sys.path.insert(0, "scripts")
from peppar_fix.ptp_device import PtpDevice
from ticc import Ticc


def main():
    ap = argparse.ArgumentParser(description="Sweep adjfine, measure TICC")
    ap.add_argument("--ptp-dev", default="/dev/ptp0")
    ap.add_argument("--ticc-port", required=True)
    ap.add_argument("--ticc-channel", default="chA",
                    help="TICC channel carrying PEROUT (default: chA)")
    ap.add_argument("--start-ppb", type=float, default=-100)
    ap.add_argument("--stop-ppb", type=float, default=100)
    ap.add_argument("--step-ppb", type=float, default=1.0)
    ap.add_argument("--dwell-s", type=float, default=10.0,
                    help="Seconds to measure at each adjfine step")
    ap.add_argument("--settle-s", type=float, default=2.0,
                    help="Seconds to wait after adjfine change before measuring")
    ap.add_argument("-o", "--output", default=None,
                    help="CSV output path (default: stdout)")
    args = ap.parse_args()

    ptp = PtpDevice(args.ptp_dev)

    # Enable PEROUT so TICC chA has edges
    ptp.set_pin_function(0, 2, 0)  # pin 0 = PEROUT channel 0
    ptp.enable_perout(0)
    print(f"PEROUT enabled on {args.ptp_dev} pin 0", file=sys.stderr)

    out_f = open(args.output, 'w', newline='') if args.output else sys.stdout
    writer = csv.writer(out_f)
    writer.writerow(['adjfine_ppb', 'measured_period_ps', 'period_offset_ps',
                     'n_edges', 'std_ps'])

    ppb = args.start_ppb
    with Ticc(args.ticc_port, wait_for_boot=True) as ticc:
        while ppb <= args.stop_ppb + args.step_ppb / 2:
            # Set adjfine
            ptp.set_adjfine(ppb)
            print(f"adjfine={ppb:+.2f} ppb, settling {args.settle_s}s...",
                  end='', flush=True, file=sys.stderr)
            time.sleep(args.settle_s)

            # Collect TICC edges for dwell_s
            edges = []
            deadline = time.monotonic() + args.dwell_s
            for ch, ref_sec, ref_ps in ticc:
                if ch != args.ticc_channel:
                    continue
                edges.append(ref_sec * 1_000_000_000_000 + ref_ps)
                if time.monotonic() >= deadline:
                    break

            if len(edges) < 3:
                print(f" only {len(edges)} edges, skipping", file=sys.stderr)
                ppb += args.step_ppb
                continue

            # Compute period from consecutive edge differences
            diffs = [edges[i+1] - edges[i] for i in range(len(edges)-1)]
            mean_period = sum(diffs) / len(diffs)
            std_period = (sum((d - mean_period)**2 for d in diffs)
                          / len(diffs)) ** 0.5
            # Offset from 1 PPS nominal (1e12 ps)
            offset = mean_period - 1_000_000_000_000

            writer.writerow([f"{ppb:.3f}", f"{mean_period:.1f}",
                             f"{offset:.1f}", len(edges), f"{std_period:.1f}"])
            if out_f != sys.stdout:
                out_f.flush()

            print(f" {len(edges)} edges, period offset={offset:+.1f} ps, "
                  f"std={std_period:.1f} ps", file=sys.stderr)

            ppb += args.step_ppb

    if out_f != sys.stdout:
        out_f.close()

    ptp.close()
    print("Done.", file=sys.stderr)


if __name__ == "__main__":
    main()
