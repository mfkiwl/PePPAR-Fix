#!/usr/bin/env python3
"""Characterize PHC step accuracy using PTP_SYS_OFFSET_PRECISE.

Measures how precisely userspace can set the PHC time by repeatedly:
  1. Reading the PHC time (cross-timestamped with CLOCK_MONOTONIC)
  2. Computing a target PHC time
  3. Setting the PHC via clock_settime()
  4. Reading the PHC again
  5. Recording the residual (actual - target)

The mean residual is the systematic latency of clock_settime().
The variance determines how accurately we can step the PHC.

No TICC, no PPS, no external reference needed.

Usage:
    python3 characterize_phc_step.py --ptp-dev /dev/ptp0 --trials 50
    python3 characterize_phc_step.py --ptp-dev /dev/ptp0 --trials 100 --out data/phc_char.json
"""

import argparse
import ctypes
import ctypes.util
import fcntl
import json
import math
import os
import random
import statistics
import struct
import sys
import time

# ── ioctl constants ──────────────────────────────────────────────── #

PTP_CLK_MAGIC = ord('=')

_IOC_WRITE = 1
_IOC_READ = 2
_IOC_READWRITE = 3


def _IOC(direction, typ, nr, size):
    return (direction << 30) | (size << 16) | (typ << 8) | nr


def _IOW(typ, nr, size):
    return _IOC(_IOC_WRITE, typ, nr, size)


def _IOWR(typ, nr, size):
    return _IOC(_IOC_READWRITE, typ, nr, size)


# struct ptp_sys_offset_precise { ptp_clock_time device, sys_realtime, monoraw; }
# Each ptp_clock_time is { __s64 sec; __u32 nsec; __u32 reserved; } = 16 bytes
# Total: 3 * 16 = 48 bytes
PTP_SYS_OFFSET_PRECISE_SIZE = 48
PTP_SYS_OFFSET_PRECISE2 = _IOWR(PTP_CLK_MAGIC, 17, PTP_SYS_OFFSET_PRECISE_SIZE)
PTP_SYS_OFFSET_PRECISE = _IOWR(PTP_CLK_MAGIC, 8, PTP_SYS_OFFSET_PRECISE_SIZE)

# struct ptp_sys_offset { unsigned int n_samples; unsigned int rsv[3];
#   struct ptp_clock_time ts[2*PTP_MAX_SAMPLES+1]; }
# PTP_MAX_SAMPLES = 25, ptp_clock_time = 16 bytes
# Header: 16 bytes, ts: 51 * 16 = 816 bytes, total = 832
PTP_SYS_OFFSET_SIZE = 832
PTP_SYS_OFFSET2 = _IOW(PTP_CLK_MAGIC, 14, PTP_SYS_OFFSET_SIZE)
PTP_SYS_OFFSET = _IOW(PTP_CLK_MAGIC, 5, PTP_SYS_OFFSET_SIZE)

CLOCK_MONOTONIC = 1
CLOCK_MONOTONIC_RAW = 4


# ── libc wrappers ────────────────────────────────────────────────── #

_libc = ctypes.CDLL(ctypes.util.find_library("c"), use_errno=True)


class Timespec(ctypes.Structure):
    _fields_ = [("tv_sec", ctypes.c_long), ("tv_nsec", ctypes.c_long)]


def _clock_id_from_fd(fd):
    return (~fd << 3) | 3


def clock_gettime_ns(clock_id):
    """Read a clock, return nanoseconds."""
    ts = Timespec()
    ret = _libc.clock_gettime(ctypes.c_int(clock_id), ctypes.byref(ts))
    if ret != 0:
        raise OSError(ctypes.get_errno(), "clock_gettime failed")
    return ts.tv_sec * 1_000_000_000 + ts.tv_nsec


def clock_settime_ns(clock_id, time_ns):
    """Set a clock to the given time in nanoseconds."""
    sec = int(time_ns // 1_000_000_000)
    nsec = int(time_ns % 1_000_000_000)
    ts = Timespec(sec, nsec)
    ret = _libc.clock_settime(ctypes.c_int(clock_id), ctypes.byref(ts))
    if ret != 0:
        raise OSError(ctypes.get_errno(), "clock_settime failed")


def _parse_ptp_clock_time(buf, offset):
    """Parse a ptp_clock_time struct (16 bytes: s64 sec, u32 nsec, u32 rsv)."""
    sec = struct.unpack_from('<q', buf, offset)[0]
    nsec = struct.unpack_from('<I', buf, offset + 8)[0]
    return sec * 1_000_000_000 + nsec


# ── PHC reading ──────────────────────────────────────────────────── #

def read_phc_precise(fd):
    """Read PHC via PTP_SYS_OFFSET_PRECISE.

    Returns (phc_ns, mono_raw_ns) or raises OSError if not supported.
    """
    buf = bytearray(PTP_SYS_OFFSET_PRECISE_SIZE)
    try:
        fcntl.ioctl(fd, PTP_SYS_OFFSET_PRECISE2, buf, True)
    except OSError:
        fcntl.ioctl(fd, PTP_SYS_OFFSET_PRECISE, buf, True)
    phc_ns = _parse_ptp_clock_time(buf, 0)
    # sys_realtime at offset 16
    mono_raw_ns = _parse_ptp_clock_time(buf, 32)
    return phc_ns, mono_raw_ns


def read_phc_offset(fd, n_samples=5):
    """Read PHC via PTP_SYS_OFFSET (fallback).

    Takes n_samples interleaved system/PHC readings and picks the
    tightest pair. Returns (phc_ns, mono_ns).
    """
    buf = bytearray(PTP_SYS_OFFSET_SIZE)
    struct.pack_into('<I', buf, 0, n_samples)
    try:
        fcntl.ioctl(fd, PTP_SYS_OFFSET2, buf, True)
    except OSError:
        fcntl.ioctl(fd, PTP_SYS_OFFSET, buf, True)

    # Parse interleaved timestamps: sys, phc, sys, phc, ..., sys
    # Total: 2*n_samples + 1 timestamps
    timestamps = []
    for i in range(2 * n_samples + 1):
        ts_ns = _parse_ptp_clock_time(buf, 16 + i * 16)
        timestamps.append(ts_ns)

    # Find tightest sys-phc-sys triplet
    best_span = float('inf')
    best_phc = 0
    best_sys = 0
    for i in range(n_samples):
        sys_before = timestamps[2 * i]
        phc = timestamps[2 * i + 1]
        sys_after = timestamps[2 * i + 2]
        span = sys_after - sys_before
        if span < best_span:
            best_span = span
            best_phc = phc
            best_sys = (sys_before + sys_after) // 2
    return best_phc, best_sys


def read_phc(fd, method):
    """Read PHC using the specified method. Returns (phc_ns, ref_ns)."""
    if method == "precise":
        return read_phc_precise(fd)
    else:
        return read_phc_offset(fd)


# ── Characterization ─────────────────────────────────────────────── #

def set_and_measure(fd, clock_id, method, target_ns):
    """Set PHC to target_ns, read back, return (residual_ns, set_latency_ns)."""
    set_mono_before = clock_gettime_ns(CLOCK_MONOTONIC)
    clock_settime_ns(clock_id, target_ns)
    set_mono_after = clock_gettime_ns(CLOCK_MONOTONIC)
    set_latency_ns = set_mono_after - set_mono_before

    phc_after, _ = read_phc(fd, method)
    elapsed_ns = clock_gettime_ns(CLOCK_MONOTONIC) - set_mono_before
    residual_ns = phc_after - (target_ns + elapsed_ns)
    return residual_ns, set_latency_ns


def run_trial(fd, clock_id, method, target_offset_ns, mean_compensation_ns=0,
              max_time_s=None, target_error_ns=None):
    """Run one set-and-measure trial, optionally retrying within a time budget.

    Repeatedly sets the PHC and reads back, keeping the attempt with the
    smallest |residual|. Stops when |residual| < target_error_ns or the
    time budget expires. Without max_time_s, does a single attempt.
    """
    phc_before, ref_before = read_phc(fd, method)
    target_ns = phc_before + target_offset_ns

    last_residual = None
    last_latency = None
    attempts = 0
    deadline = time.monotonic() + max_time_s if max_time_s else None

    while True:
        attempts += 1
        aim_ns = target_ns - mean_compensation_ns
        residual_ns, set_latency_ns = set_and_measure(fd, clock_id, method, aim_ns)
        last_residual = residual_ns - mean_compensation_ns
        last_latency = set_latency_ns

        # Each clock_settime overwrites the PHC — there is no "best of."
        # We either met the target and stop, or we try again and the
        # previous result is gone.
        if target_error_ns is not None and abs(last_residual) < target_error_ns:
            break  # met target, accept this result

        if deadline is not None and time.monotonic() >= deadline:
            break  # out of time, stuck with this last attempt

        if max_time_s is None:
            break  # no retry budget, single attempt

    return {
        "target_offset_ns": target_offset_ns,
        "residual_ns": last_residual,
        "set_latency_ns": last_latency,
        "attempts": attempts,
        "met_target": target_error_ns is not None and abs(last_residual) < target_error_ns,
    }


def print_summary(label, results, args, method):
    residuals = [r["residual_ns"] for r in results]
    latencies = [r["set_latency_ns"] for r in results]
    attempts = [r.get("attempts", 1) for r in results]

    print()
    print("=" * 60)
    print(f"{label}: {args.ptp_dev} ({method})")
    print(f"  Trials: {len(results)}")
    print(f"  Offset range: [{args.min_offset_ns}, {args.max_offset_ns}] ns")
    print()
    print(f"  Residual (actual - target):")
    print(f"    mean:   {statistics.mean(residuals):+.1f} ns")
    print(f"    stdev:  {statistics.stdev(residuals):.1f} ns")
    print(f"    min:    {min(residuals):+d} ns")
    print(f"    max:    {max(residuals):+d} ns")
    print(f"    median: {statistics.median(residuals):+.1f} ns")
    print()
    print(f"  clock_settime latency:")
    print(f"    mean:   {statistics.mean(latencies):.0f} ns")
    print(f"    stdev:  {statistics.stdev(latencies):.0f} ns")
    print()
    compensated_stdev = statistics.stdev(residuals)
    print(f"  Step uncertainty (3σ after mean compensation): "
          f"{3 * compensated_stdev:.0f} ns")
    if max(attempts) > 1:
        print(f"  Attempts: mean={statistics.mean(attempts):.1f} "
              f"max={max(attempts)}")
    print("=" * 60)

    return statistics.mean(residuals), compensated_stdev


def pps_calibrate_lag(ptp_dev, extts_channel, pps_pin, program_pin,
                      phc_timescale, leap, tai_minus_gps, n_trials=10):
    """Calibrate settime lag using PPS as ground truth.

    Sets the PHC with lag=0, captures PPS events, and measures the
    true lag from the phase error.  PPS_error = -true_lag when lag=0,
    so true_lag = -PPS_error.

    Returns (lag_mean_ns, lag_stdev_ns, n_samples) or (None, None, 0).
    """
    from peppar_fix.ptp_device import PtpDevice, PTP_PF_EXTTS

    # Timescale offset: seconds to add to CLOCK_REALTIME for PHC
    if phc_timescale == "tai":
        offset_s = leap + tai_minus_gps
    elif phc_timescale == "gps":
        offset_s = leap
    else:
        offset_s = 0
    offset_ns = offset_s * 1_000_000_000

    ptp = PtpDevice(ptp_dev)

    if program_pin and pps_pin is not None:
        try:
            ptp.set_pin_function(pps_pin, PTP_PF_EXTTS, extts_channel)
        except OSError:
            pass

    # Get PHC roughly on time so PPS gives meaningful readings
    rt_now = time.clock_gettime_ns(time.CLOCK_REALTIME)
    ptp.set_phc_ns(rt_now + offset_ns)

    ptp.enable_extts(extts_channel, rising_edge=True)

    # Verify epoch_offset=0
    evt = ptp.read_extts(timeout_ms=2000)
    if evt is None:
        print("  No PPS event — cannot calibrate (is PPS connected?)")
        ptp.disable_extts(extts_channel)
        ptp.close()
        return None, None, 0
    rt_check = time.clock_gettime_ns(time.CLOCK_REALTIME)
    phc_sec, phc_nsec = evt[0], evt[1]
    rounded = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
    utc_sec = round(rt_check / 1_000_000_000)
    target_sec = utc_sec + offset_s
    epoch_off = rounded - target_sec
    if abs(epoch_off) > 1:
        print(f"  epoch_offset={epoch_off} — PHC too far off, cannot calibrate")
        ptp.disable_extts(extts_channel)
        ptp.close()
        return None, None, 0

    # Measure: set PHC with lag=0, capture PPS, compute true lag
    lag_samples = []
    for i in range(n_trials):
        # Transfer standard: read CLOCK_REALTIME, set PHC
        rt_ref = time.clock_gettime_ns(time.CLOCK_REALTIME)
        ptp.set_phc_ns(rt_ref + offset_ns)  # lag=0

        # Wait for PPS
        evt = ptp.read_extts(timeout_ms=2000)
        if evt is None:
            continue
        rt_at_pps = time.clock_gettime_ns(time.CLOCK_REALTIME)

        phc_sec, phc_nsec = evt[0], evt[1]
        phase_ns = phc_nsec if phc_nsec < 500_000_000 else phc_nsec - 1_000_000_000

        # Verify epoch_offset
        rounded = phc_sec if phc_nsec < 500_000_000 else phc_sec + 1
        utc_sec = round(rt_at_pps / 1_000_000_000)
        target_sec = utc_sec + offset_s
        if rounded - target_sec != 0:
            continue

        # PPS_error = lag_param - true_lag.  With lag_param=0: true_lag = -PPS_error
        true_lag = -phase_ns
        lag_samples.append(true_lag)
        print(f"  [{i:2d}] PPS phase: {phase_ns:+8d} ns → lag = {true_lag:+8d} ns")

    ptp.disable_extts(extts_channel)
    ptp.close()

    if len(lag_samples) < 3:
        print("  Not enough PPS samples for calibration")
        return None, None, 0

    lag_mean = statistics.mean(lag_samples)
    lag_stdev = statistics.stdev(lag_samples)
    lag_unc = lag_stdev / math.sqrt(len(lag_samples))
    print(f"\n  PPS-calibrated settime lag: {lag_mean:.0f} ±{lag_unc:.0f} ns "
          f"(σ={lag_stdev:.0f}, n={len(lag_samples)})")
    return lag_mean, lag_stdev, len(lag_samples)


def main():
    ap = argparse.ArgumentParser(description="Characterize PHC step accuracy")
    ap.add_argument("--ptp-dev", required=True, help="PHC device (e.g. /dev/ptp0)")
    ap.add_argument("--trials", type=int, default=50, help="Number of trials")
    ap.add_argument("--min-offset-ns", type=int, default=-500_000_000,
                    help="Min random step offset in ns (default: -500ms)")
    ap.add_argument("--max-offset-ns", type=int, default=500_000_000,
                    help="Max random step offset in ns (default: +500ms)")
    ap.add_argument("--max-step-time-ms", type=int, default=500,
                    help="Max wall time per step retry loop in ms (default: 500)")
    ap.add_argument("--target-step-error-ns", type=int, default=5000,
                    help="Target step error in ns — stop retrying when met (default: 5000)")
    ap.add_argument("--seed", type=int, default=42)
    ap.add_argument("--out", help="JSON output path")
    # PPS calibration args
    ap.add_argument("--pps-calibrate", action="store_true",
                    help="Calibrate settime lag using PPS ground truth")
    ap.add_argument("--pps-trials", type=int, default=10,
                    help="PPS calibration trials (default: 10)")
    ap.add_argument("--extts-channel", type=int, default=0)
    ap.add_argument("--pps-pin", type=int, default=None)
    ap.add_argument("--program-pin", action="store_true")
    ap.add_argument("--phc-timescale", default="tai",
                    choices=["gps", "utc", "tai"])
    ap.add_argument("--leap", type=int, default=18)
    ap.add_argument("--tai-minus-gps", type=int, default=19)
    args = ap.parse_args()

    fd = os.open(args.ptp_dev, os.O_RDWR)
    clock_id = _clock_id_from_fd(fd)

    # Detect best method
    method = "offset"
    try:
        read_phc_precise(fd)
        method = "precise"
        print(f"PTP_SYS_OFFSET_PRECISE supported on {args.ptp_dev}")
    except OSError:
        print(f"PTP_SYS_OFFSET_PRECISE not supported, using PTP_SYS_OFFSET")

    # Baseline read check
    phc, ref = read_phc(fd, method)
    print(f"Baseline read OK (method={method}, PHC={phc} ns)")
    print()

    rng = random.Random(args.seed)

    # Pass 1: measure raw mean and stdev (no compensation, no retry)
    print("--- Pass 1: raw characterization ---")
    results_raw = []
    for i in range(args.trials):
        offset = rng.randint(args.min_offset_ns, args.max_offset_ns)
        trial = run_trial(fd, clock_id, method, offset)
        results_raw.append(trial)
        print(f"[{i:3d}] offset={offset:+12d} ns  "
              f"residual={trial['residual_ns']:+8d} ns  "
              f"latency={trial['set_latency_ns']:8d} ns")

    raw_mean, raw_stdev = print_summary("Pass 1 (raw)", results_raw, args, method)

    # Pass 2: compensate for mean, retry within time budget
    max_time_s = args.max_step_time_ms / 1000.0
    target_error_ns = args.target_step_error_ns
    print()
    print(f"--- Pass 2: mean-compensated, target={target_error_ns} ns, "
          f"budget={args.max_step_time_ms} ms ---")
    rng2 = random.Random(args.seed + 1)
    results_comp = []
    for i in range(args.trials):
        offset = rng2.randint(args.min_offset_ns, args.max_offset_ns)
        trial = run_trial(fd, clock_id, method, offset,
                          mean_compensation_ns=int(round(raw_mean)),
                          max_time_s=max_time_s,
                          target_error_ns=target_error_ns)
        results_comp.append(trial)
        print(f"[{i:3d}] offset={offset:+12d} ns  "
              f"residual={trial['residual_ns']:+8d} ns  "
              f"attempts={trial['attempts']:3d}  "
              f"{'HIT' if trial['met_target'] else 'TIMEOUT'}")

    comp_mean, comp_stdev = print_summary(
        f"Pass 2 (target={target_error_ns}ns, budget={args.max_step_time_ms}ms)",
        results_comp, args, method)
    hits = sum(1 for r in results_comp if r['met_target'])
    timeouts = len(results_comp) - hits
    print(f"  Hit rate: {hits}/{len(results_comp)} "
          f"({100*hits/len(results_comp):.0f}%)")
    if timeouts:
        timeout_residuals = [abs(r['residual_ns']) for r in results_comp if not r['met_target']]
        print(f"  Timeouts: {timeouts} — residuals: "
              f"mean={statistics.mean(timeout_residuals):.0f} ns, "
              f"max={max(timeout_residuals):.0f} ns")
    final_results = results_comp
    final_mean = comp_mean
    final_stdev = comp_stdev

    os.close(fd)

    # Pass 3 (optional): PPS-calibrated settime lag
    pps_lag_mean = None
    pps_lag_stdev = None
    pps_lag_n = 0
    if args.pps_calibrate:
        print()
        print("--- Pass 3: PPS-calibrated settime lag ---")
        print("  The readback-based lag (Pass 1) only measures relative to")
        print("  PTP_SYS_OFFSET, which has its own asymmetry.  PPS is the")
        print("  ground truth — a hardware latch with no software bias.")
        print()
        pps_lag_mean, pps_lag_stdev, pps_lag_n = pps_calibrate_lag(
            args.ptp_dev, args.extts_channel, args.pps_pin,
            args.program_pin, args.phc_timescale, args.leap,
            args.tai_minus_gps, n_trials=args.pps_trials)
        if pps_lag_mean is not None:
            readback_asymmetry = pps_lag_mean - (-raw_mean)
            print(f"\n  Readback-relative lag (Pass 1): {-raw_mean:.0f} ns")
            print(f"  PPS-calibrated lag (Pass 3):    {pps_lag_mean:.0f} ns")
            print(f"  PTP_SYS_OFFSET asymmetry:       {readback_asymmetry:.0f} ns")
            print(f"\n  Use --settime-lag-ns {int(round(pps_lag_mean))} "
                  f"with phc_bootstrap.py")

    if args.out:
        import socket
        output = {
            "host": socket.gethostname(),
            "phc": args.ptp_dev,
            "method": method,
            "n_trials": len(final_results),
            "max_step_time_ms": args.max_step_time_ms,
            "target_step_error_ns": args.target_step_error_ns,
            "offset_range_ns": [args.min_offset_ns, args.max_offset_ns],
            "raw_mean_residual_ns": round(raw_mean, 1),
            "raw_stdev_residual_ns": round(raw_stdev, 1),
            "compensated_mean_residual_ns": round(final_mean, 1),
            "compensated_stdev_residual_ns": round(final_stdev, 1),
            "step_uncertainty_3sigma_ns": round(3 * final_stdev, 0),
            "timestamp": time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime()),
        }
        if pps_lag_mean is not None:
            output["pps_calibrated_lag_ns"] = round(pps_lag_mean, 0)
            output["pps_lag_stdev_ns"] = round(pps_lag_stdev, 0)
            output["pps_lag_n_samples"] = pps_lag_n
            output["readback_asymmetry_ns"] = round(pps_lag_mean - (-raw_mean), 0)
        os.makedirs(os.path.dirname(args.out) or ".", exist_ok=True)
        with open(args.out, "w") as f:
            json.dump(output, f, indent=2)
        print(f"\nResults saved to {args.out}")


if __name__ == "__main__":
    main()
