#!/usr/bin/env python3
"""Compare PHC step methods: clock_settime vs ADJ_SETOFFSET.

Runs the optimal stopping algorithm with both methods on the same PHC,
collecting |residual| distributions.  Tests whether ADJ_SETOFFSET avoids
the bimodal latency seen with clock_settime on E810.

Usage:
    python3 tools/step_method_comparison.py /dev/ptp1 --search-time 5.0
"""

import argparse
import math
import os
import struct
import sys
import time

sys.path.insert(0, os.path.join(os.path.dirname(__file__), "..", "scripts"))


def read_phc_ns(fd):
    """Read PHC time via clock_gettime."""
    CLOCK_REALTIME = 0
    phc_clkid = (~fd << 3) | 3
    ts = time.clock_gettime_ns(phc_clkid)
    sys_ns = time.clock_gettime_ns(CLOCK_REALTIME)
    return ts, sys_ns


def set_phc_clock_settime(fd, target_ns):
    """Step PHC via clock_settime (absolute)."""
    import ctypes
    import ctypes.util
    librt = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)

    class timespec(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]

    phc_clkid = (~fd << 3) | 3
    sec = target_ns // 1_000_000_000
    nsec = target_ns % 1_000_000_000
    ts = timespec(sec, nsec)
    ret = librt.clock_settime(phc_clkid, ctypes.byref(ts))
    if ret != 0:
        raise OSError(f"clock_settime failed: {ctypes.get_errno()}")


def set_phc_adj_setoffset(fd, offset_ns):
    """Step PHC via clock_adjtime(ADJ_SETOFFSET) (relative)."""
    import fcntl

    # struct timex for clock_adjtime
    # We need ADJ_SETOFFSET (0x0100) | ADJ_NANO (0x2000)
    ADJ_SETOFFSET = 0x0100
    ADJ_NANO = 0x2000
    modes = ADJ_SETOFFSET | ADJ_NANO

    phc_clkid = (~fd << 3) | 3

    sec = int(offset_ns // 1_000_000_000)
    nsec = int(offset_ns % 1_000_000_000)
    # Handle negative offsets
    if offset_ns < 0:
        sec = -int((-offset_ns) // 1_000_000_000)
        nsec = -int((-offset_ns) % 1_000_000_000)
        if nsec < 0 and sec == 0:
            sec = -1
            nsec = 1_000_000_000 + nsec

    # Pack struct timex (simplified — only modes and time fields matter)
    # struct timex: modes(u32), offset(long), freq(long), maxerror(long),
    #               esterror(long), status(int), constant(long),
    #               precision(long), tolerance(long),
    #               time(timeval: sec+usec OR sec+nsec with ADJ_NANO),
    #               tick(long), ppsfreq(long), jitter(long), ...
    #
    # We use ctypes for the syscall
    import ctypes
    import ctypes.util

    librt = ctypes.CDLL(ctypes.util.find_library("rt"), use_errno=True)

    class timeval(ctypes.Structure):
        _fields_ = [("tv_sec", ctypes.c_long), ("tv_usec", ctypes.c_long)]

    class timex(ctypes.Structure):
        _fields_ = [
            ("modes", ctypes.c_uint),
            ("offset", ctypes.c_long),
            ("freq", ctypes.c_long),
            ("maxerror", ctypes.c_long),
            ("esterror", ctypes.c_long),
            ("status", ctypes.c_int),
            ("constant", ctypes.c_long),
            ("precision", ctypes.c_long),
            ("tolerance", ctypes.c_long),
            ("time", timeval),
            ("tick", ctypes.c_long),
            ("ppsfreq", ctypes.c_long),
            ("jitter", ctypes.c_long),
            ("shift", ctypes.c_int),
            ("stabil", ctypes.c_long),
            ("jitcnt", ctypes.c_long),
            ("calcnt", ctypes.c_long),
            ("errcnt", ctypes.c_long),
            ("stbcnt", ctypes.c_long),
            ("tai", ctypes.c_int),
        ]

    tx = timex()
    tx.modes = modes
    tx.time.tv_sec = sec
    tx.time.tv_usec = nsec  # nsec when ADJ_NANO is set

    ret = librt.clock_adjtime(phc_clkid, ctypes.byref(tx))
    if ret < 0:
        errno = ctypes.get_errno()
        raise OSError(f"clock_adjtime(ADJ_SETOFFSET) failed: errno={errno}")


def optimal_stop(fd, method, search_time_s, lag_ns=0):
    """Run optimal stopping with the given step method.

    Returns list of |residual_ns| for every attempt.
    """
    deadline = time.monotonic() + search_time_s
    observe_until = time.monotonic() + search_time_s / math.e
    residuals = []
    observing = True
    threshold = None
    accepted_idx = None

    # Read current PHC time as our target
    phc_ns, sys_ns = read_phc_ns(fd)
    target_ns = phc_ns  # Step to current time (measuring the step noise)

    attempt = 0
    while time.monotonic() < deadline:
        attempt += 1

        if method == "settime":
            aim_ns = target_ns + lag_ns
            # Re-read target based on elapsed real time
            rt_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
            aim_ns = target_ns + (rt_now - sys_ns) + lag_ns
            set_phc_clock_settime(fd, aim_ns)
            phc_after, sys_after = read_phc_ns(fd)
            expected = target_ns + (sys_after - sys_ns)
            residual = phc_after - expected

        elif method == "adjsetoffset":
            # Read current PHC, compute how far we are from target
            phc_now, sys_now = read_phc_ns(fd)
            expected = target_ns + (sys_now - sys_ns)
            error = phc_now - expected
            # Apply correction
            set_phc_adj_setoffset(fd, -error + lag_ns)
            # Read back
            phc_after, sys_after = read_phc_ns(fd)
            expected_after = target_ns + (sys_after - sys_ns)
            residual = phc_after - expected_after

        residuals.append(abs(residual))

        if observing:
            if time.monotonic() >= observe_until:
                observing = False
                sorted_obs = sorted(residuals)
                idx = max(0, len(sorted_obs) * 5 // 100 - 1)
                threshold = sorted_obs[idx]
        else:
            if abs(residual) <= threshold:
                accepted_idx = attempt - 1
                break

    return residuals, accepted_idx


def main():
    ap = argparse.ArgumentParser(description="Compare PHC step methods")
    ap.add_argument("ptp_dev", help="PTP device (e.g. /dev/ptp1)")
    ap.add_argument("--search-time", type=float, default=5.0,
                    help="Search budget per trial in seconds")
    ap.add_argument("--lag-ns", type=int, default=0,
                    help="settime_lag_ns compensation")
    ap.add_argument("--trials", type=int, default=3,
                    help="Number of trials per method")
    args = ap.parse_args()

    fd = os.open(args.ptp_dev, os.O_RDWR)
    print(f"Opened {args.ptp_dev} (fd={fd})")

    for method in ["settime", "adjsetoffset"]:
        print(f"\n{'='*60}")
        print(f"Method: {method} (search={args.search_time}s, lag={args.lag_ns}ns)")
        print(f"{'='*60}")

        all_residuals = []
        for trial in range(args.trials):
            residuals, accepted = optimal_stop(
                fd, method, args.search_time, args.lag_ns
            )
            all_residuals.extend(residuals)

            import numpy as np
            r = np.array(residuals)
            acc_str = f"accepted at attempt {accepted+1}" if accepted is not None else "DEADLINE"
            print(f"  Trial {trial+1}: {len(residuals)} attempts, "
                  f"min={np.min(r)/1e6:.3f}ms, "
                  f"median={np.median(r)/1e6:.3f}ms, "
                  f"p5={np.percentile(r,5)/1e6:.3f}ms, "
                  f"p95={np.percentile(r,95)/1e6:.3f}ms, "
                  f"{acc_str}")
            if accepted is not None:
                print(f"         accepted |residual| = {residuals[accepted]/1e6:.3f}ms")

            time.sleep(1)

        import numpy as np
        all_r = np.array(all_residuals)
        print(f"\n  Combined ({len(all_r)} samples):")
        print(f"    min    = {np.min(all_r)/1e6:.3f} ms")
        print(f"    p5     = {np.percentile(all_r, 5)/1e6:.3f} ms")
        print(f"    median = {np.median(all_r)/1e6:.3f} ms")
        print(f"    p95    = {np.percentile(all_r, 95)/1e6:.3f} ms")
        print(f"    max    = {np.max(all_r)/1e6:.3f} ms")

        # Bimodality check: is there a clear gap in the distribution?
        sorted_r = np.sort(all_r)
        gaps = np.diff(sorted_r)
        max_gap_idx = np.argmax(gaps)
        max_gap = gaps[max_gap_idx]
        below = sorted_r[:max_gap_idx+1]
        above = sorted_r[max_gap_idx+1:]
        if max_gap > np.median(all_r) * 0.5 and len(below) > 5 and len(above) > 5:
            print(f"    BIMODAL: gap at {sorted_r[max_gap_idx]/1e6:.3f}ms, "
                  f"{len(below)} below, {len(above)} above")
            print(f"    Mode 1: median={np.median(below)/1e6:.3f}ms")
            print(f"    Mode 2: median={np.median(above)/1e6:.3f}ms")
        else:
            print(f"    Distribution appears unimodal")

    os.close(fd)


if __name__ == "__main__":
    main()
