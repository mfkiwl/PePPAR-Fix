#!/usr/bin/env python3
"""phc_fault_inject.py — Set PHC time and/or frequency for testing.

Modes:
  random    Set PHC to random time between 1970 and 2038
  nudge     Set PHC to current time ± random offset (default ±1s)
  exact     Set PHC to an exact time in nanoseconds
  freq      Set PHC adjfine to a random or exact frequency

Examples:
    # Random time between 1970 and 2038
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 random

    # Current time ± up to 100ms
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 nudge --max-offset-ms 100

    # Current time + exactly 7 seconds
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 nudge --offset-ns 7000000000

    # Random frequency between -500 and +500 ppb
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 freq --max-ppb 500

    # Exact frequency
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 freq --ppb -83.5

    # Trash both phase and frequency
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 random
    python3 phc_fault_inject.py --ptp-dev /dev/ptp0 freq --max-ppb 5000
"""

import argparse
import os
import random
import sys
import time

sys.path.insert(0, os.path.dirname(__file__))
from peppar_fix.ptp_device import PtpDevice


# Unix timestamp ranges
EPOCH_1970 = 0
EPOCH_2038 = 2**31 - 1  # 2038-01-19


def cmd_random(ptp, args):
    """Set PHC to a random time between 1970 and 2038."""
    rng = random.Random(args.seed) if args.seed is not None else random
    target_sec = rng.randint(EPOCH_1970, EPOCH_2038)
    target_ns = target_sec * 1_000_000_000
    phc_before, _ = ptp.read_phc_ns()
    ptp.set_phc_ns(target_ns)
    phc_after, _ = ptp.read_phc_ns()
    ts = time.strftime("%Y-%m-%d %H:%M:%S", time.gmtime(target_sec))
    print(f"PHC set to {ts} UTC ({target_ns} ns)")
    print(f"  before: {phc_before} ns")
    print(f"  after:  {phc_after} ns")
    print(f"  delta:  {phc_after - phc_before:+d} ns")


def cmd_nudge(ptp, args):
    """Set PHC to current time ± offset."""
    phc_before, _ = ptp.read_phc_ns()

    if args.offset_ns is not None:
        offset_ns = args.offset_ns
    else:
        max_ns = args.max_offset_ms * 1_000_000
        rng = random.Random(args.seed) if args.seed is not None else random
        offset_ns = rng.randint(-max_ns, max_ns)

    target_ns = phc_before + offset_ns
    ptp.set_phc_ns(target_ns)
    phc_after, _ = ptp.read_phc_ns()
    print(f"PHC nudged by {offset_ns:+d} ns ({offset_ns / 1e6:+.3f} ms)")
    print(f"  before: {phc_before} ns")
    print(f"  after:  {phc_after} ns")


def cmd_exact(ptp, args):
    """Set PHC to an exact time in nanoseconds."""
    ptp.set_phc_ns(args.time_ns)
    phc_after, _ = ptp.read_phc_ns()
    print(f"PHC set to {args.time_ns} ns")
    print(f"  readback: {phc_after} ns")


def cmd_freq(ptp, args):
    """Set PHC adjfine to a specific or random frequency."""
    if args.ppb is not None:
        ppb = args.ppb
    else:
        rng = random.Random(args.seed) if args.seed is not None else random
        ppb = rng.uniform(-args.max_ppb, args.max_ppb)

    ptp.adjfine(ppb)
    print(f"PHC frequency set to {ppb:+.1f} ppb")


def main():
    ap = argparse.ArgumentParser(
        description="PHC fault injection for bootstrap testing")
    ap.add_argument("--ptp-dev", required=True, help="PHC device")
    ap.add_argument("--seed", type=int, default=None,
                    help="Random seed (omit for truly random)")

    sub = ap.add_subparsers(dest="command", required=True)

    sub.add_parser("random", help="Set PHC to random time (1970-2038)")

    p_nudge = sub.add_parser("nudge", help="Nudge PHC by ± offset")
    p_nudge.add_argument("--max-offset-ms", type=float, default=1000,
                         help="Max random offset in ms (default: 1000)")
    p_nudge.add_argument("--offset-ns", type=int, default=None,
                         help="Exact offset in ns (overrides random)")

    p_exact = sub.add_parser("exact", help="Set PHC to exact time")
    p_exact.add_argument("time_ns", type=int, help="Time in nanoseconds")

    p_freq = sub.add_parser("freq", help="Set PHC frequency")
    p_freq.add_argument("--ppb", type=float, default=None,
                        help="Exact frequency in ppb")
    p_freq.add_argument("--max-ppb", type=float, default=500,
                        help="Max random frequency in ppb (default: 500)")

    args = ap.parse_args()
    ptp = PtpDevice(args.ptp_dev)

    try:
        if args.command == "random":
            cmd_random(ptp, args)
        elif args.command == "nudge":
            cmd_nudge(ptp, args)
        elif args.command == "exact":
            cmd_exact(ptp, args)
        elif args.command == "freq":
            cmd_freq(ptp, args)
    finally:
        ptp.close()


if __name__ == "__main__":
    main()
