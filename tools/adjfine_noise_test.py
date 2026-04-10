#!/usr/bin/env python3
"""Measure phase noise introduced by adjfine() corrections.

Answers: how much noise does calling adjfine() add to the DO output,
as a function of the correction magnitude?

Protocol:
  1. Baseline: hold adjfine constant for N seconds, record TICC chA
     phase.  This is the DO's free-running noise floor.
  2. For each test magnitude M in [0.01, 0.1, 1, 10, 100] ppb:
     Alternate adjfine between (base + M) and (base - M) every second
     for N seconds.  Record TICC chA phase.
  3. Compute TDEV at each magnitude and compare to baseline.

If adjfine() calls introduce noise, the TDEV at τ=1s will increase
with M.  If the noise is purely from the frequency change itself
(no call overhead), the TDEV increase should scale as M × 1s
(the phase step from one second at a different frequency).

Usage:
    sudo python3 tools/adjfine_noise_test.py --ptp-dev /dev/ptp0 \
        --ticc-port /dev/ticc1 --base-ppb 130 --duration 120

Output: CSV + summary to stderr.
"""

import argparse
import csv
import math
import sys
import time

sys.path.insert(0, "scripts")
from peppar_fix.ptp_device import PtpDevice
from ticc import Ticc


def collect_phase(ticc_iter, channel, duration_s):
    """Collect TICC phase samples for duration_s seconds.
    Returns list of (ref_sec, ref_ps) tuples."""
    samples = []
    deadline = time.monotonic() + duration_s
    for ch, ref_sec, ref_ps in ticc_iter:
        if ch != channel:
            continue
        samples.append((ref_sec, ref_ps))
        if time.monotonic() >= deadline:
            break
    return samples


def compute_tdev(samples, taus=[1, 2, 3, 5, 10]):
    """Compute TDEV from (ref_sec, ref_ps) samples."""
    import numpy as np
    import allantools
    # Convert to phase in seconds
    phase_s = np.array([s + ps * 1e-12 for s, ps in samples])
    # Detrend
    t = np.arange(len(phase_s))
    p = np.polyfit(t, phase_s, 1)
    phase_d = phase_s - np.polyval(p, t)
    t2, td, _, _ = allantools.tdev(phase_d, rate=1.0,
                                    data_type='phase', taus=taus)
    return {tau: tdev * 1e9 for tau, tdev in zip(t2, td)}


def main():
    ap = argparse.ArgumentParser(description="Measure adjfine noise cost")
    ap.add_argument("--ptp-dev", default="/dev/ptp0")
    ap.add_argument("--ticc-port", required=True)
    ap.add_argument("--ticc-channel", default="chA")
    ap.add_argument("--base-ppb", type=float, required=True,
                    help="Base adjfine value (the host's current operating point)")
    ap.add_argument("--duration", type=int, default=120,
                    help="Seconds per test phase (default: 120)")
    ap.add_argument("--magnitudes", default="0,0.01,0.1,1,10,100",
                    help="Comma-separated ppb magnitudes to test (default: 0,0.01,0.1,1,10,100)")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    mags = [float(m) for m in args.magnitudes.split(',')]
    ptp = PtpDevice(args.ptp_dev)

    # Enable PEROUT
    ptp.set_pin_function(0, 2, 0)
    ptp.enable_perout(0)
    print(f"PEROUT enabled, base adjfine={args.base_ppb:.2f} ppb",
          file=sys.stderr)

    out_f = open(args.output, 'w', newline='') if args.output else sys.stdout
    writer = csv.writer(out_f)
    writer.writerow(['magnitude_ppb', 'tau_s', 'tdev_ns', 'n_samples',
                     'phase_std_ns'])

    taus = [1, 2, 3, 5, 10, 20]

    with Ticc(args.ticc_port, wait_for_boot=True) as ticc:
        ticc_iter = iter(ticc)

        for mag in mags:
            if mag == 0:
                label = "baseline (no adjfine calls)"
                # Hold constant for the full duration
                ptp.adjfine(args.base_ppb)
                print(f"\n{label}: holding at {args.base_ppb:.2f} ppb "
                      f"for {args.duration}s...", file=sys.stderr)
                time.sleep(2)  # settle
                samples = collect_phase(ticc_iter, args.ticc_channel,
                                        args.duration)
            else:
                label = f"±{mag} ppb alternating each second"
                print(f"\n{label}: {args.duration}s...",
                      file=sys.stderr)
                # Alternate adjfine ±mag around base, once per second
                samples = []
                deadline = time.monotonic() + args.duration
                toggle = True
                while time.monotonic() < deadline:
                    ppb = args.base_ppb + (mag if toggle else -mag)
                    ptp.adjfine(ppb)
                    toggle = not toggle
                    # Collect one second of TICC edges
                    sec_deadline = time.monotonic() + 1.0
                    for ch, ref_sec, ref_ps in ticc_iter:
                        if ch != args.ticc_channel:
                            continue
                        samples.append((ref_sec, ref_ps))
                        if time.monotonic() >= sec_deadline:
                            break

            if len(samples) < 10:
                print(f"  Only {len(samples)} samples, skipping",
                      file=sys.stderr)
                continue

            # Compute phase noise
            import numpy as np
            phase_ns = np.array([ps * 1e-3 for _, ps in samples])
            phase_d = phase_ns - np.polyval(
                np.polyfit(np.arange(len(phase_ns)), phase_ns, 1),
                np.arange(len(phase_ns)))
            std_ns = float(phase_d.std())

            tdev_results = compute_tdev(samples,
                                        [t for t in taus if t < len(samples)//3])

            print(f"  {len(samples)} samples, phase std={std_ns:.2f} ns",
                  file=sys.stderr)
            for tau, td in sorted(tdev_results.items()):
                print(f"    TDEV(τ={tau:.0f}s) = {td:.3f} ns",
                      file=sys.stderr)
                writer.writerow([f"{mag:.3f}", f"{tau:.0f}", f"{td:.3f}",
                                 len(samples), f"{std_ns:.2f}"])

            if out_f != sys.stdout:
                out_f.flush()

    # Restore base frequency
    ptp.adjfine(args.base_ppb)

    if out_f != sys.stdout:
        out_f.close()
    ptp.close()
    print("\nDone. Base frequency restored.", file=sys.stderr)


if __name__ == "__main__":
    main()
