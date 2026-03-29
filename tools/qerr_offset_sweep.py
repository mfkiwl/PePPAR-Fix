#!/usr/bin/env python3
"""Sweep TIM-TP→PPS offset to find correct qErr correlation.

Captures PPS EXTTS edges and TIM-TP qErr samples simultaneously,
then tests which monotonic-time offset minimizes the variance of
fractional-second PPS error corrected by qErr.

No servo, no GNSS observations, no PPP filter — just PPS + TIM-TP.
"""

import argparse
import os
import sys
import threading
import time
from collections import deque
from statistics import pvariance

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..', 'scripts'))

from peppar_fix.ptp_device import PtpDevice
from realtime_ppp import QErrStore


def extts_reader(ptp, channel, pps_log, stop):
    """Capture PPS EXTTS events with recv_mono."""
    while not stop.is_set():
        ev = ptp.read_extts(timeout_ms=1500)
        if ev is None:
            continue
        sec, nsec, index, recv_mono, queue_remains, _ = ev
        pps_log.append({
            'nsec': nsec, 'recv_mono': recv_mono,
            'queue_remains': queue_remains,
        })


def serial_reader(port, baud, qerr_store, stop):
    """Read TIM-TP from GNSS, store qErr with host_time."""
    import serial
    from pyubx2 import UBXReader
    ser = serial.Serial(port, baud, timeout=2)
    ubr = UBXReader(ser, protfilter=2)
    while not stop.is_set():
        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue
        if parsed.identity == 'TIM-TP':
            qerr_ps = getattr(parsed, 'qErr', None)
            tow_ms = getattr(parsed, 'towMS', None)
            flags = getattr(parsed, 'flags', 0)
            qerr_invalid = bool(flags & 0x10) if isinstance(flags, int) else False
            if qerr_ps is not None and not qerr_invalid:
                qerr_store.update(qerr_ps, tow_ms)
    ser.close()


def pps_fractional_error(nsec):
    if nsec < 500_000_000:
        return float(nsec)
    return float(nsec) - 1_000_000_000


def rolling_variance(values):
    """Population variance — no detrending, just raw scatter."""
    if len(values) < 2:
        return None
    return float(pvariance(values))


def main():
    ap = argparse.ArgumentParser(description="Sweep qErr→PPS offset")
    ap.add_argument("--serial", required=True)
    ap.add_argument("--baud", type=int, default=921600)
    ap.add_argument("--ptp-dev", required=True)
    ap.add_argument("--extts-channel", type=int, default=0)
    ap.add_argument("--duration", type=int, default=60,
                    help="Capture duration in seconds (default 60)")
    ap.add_argument("--window", type=int, default=20,
                    help="Rolling window size (default 20)")
    args = ap.parse_args()

    ptp = PtpDevice(args.ptp_dev)
    qerr_store = QErrStore(maxlen=8192)
    pps_log = deque(maxlen=8192)
    stop = threading.Event()

    t_pps = threading.Thread(target=extts_reader, daemon=True,
                             args=(ptp, args.extts_channel, pps_log, stop))
    t_serial = threading.Thread(target=serial_reader, daemon=True,
                                args=(args.serial, args.baud, qerr_store, stop))
    t_pps.start()
    t_serial.start()

    print(f"Capturing for {args.duration}s...")
    time.sleep(args.duration)
    stop.set()
    t_pps.join(timeout=3)
    t_serial.join(timeout=3)

    with qerr_store._lock:
        qerr_samples = list(qerr_store._samples)
    pps_list = [p for p in pps_log if not p['queue_remains']]

    print(f"Captured {len(list(pps_log))} PPS events ({len(pps_list)} non-queued), "
          f"{len(qerr_samples)} qErr samples\n")
    if len(pps_list) < args.window or len(qerr_samples) < 10:
        print("Not enough data")
        return

    # Build per-PPS fractional error
    pps_data = []
    for p in pps_list:
        pps_data.append({
            'recv_mono': p['recv_mono'],
            'frac_ns': pps_fractional_error(p['nsec']),
        })

    # Sweep offsets
    offsets = [x * 0.05 for x in range(-4, 41)]
    tolerance = 0.1
    W = args.window

    # Match each PPS to its best qErr at each candidate offset
    # Then compute first-differences to remove clock drift:
    #   Δfrac[n] = frac[n] - frac[n-1]
    #   Δcorrected[n] = (frac[n] + qerr[n]) - (frac[n-1] + qerr[n-1])
    # Variance of Δcorrected over last W pairs is the litmus.

    print(f"Window={W} first-difference pairs.  No detrending needed.\n")
    print(f"{'offset_s':>10} {'pairs':>6} {'var_Δraw':>10} {'var_Δ+qErr':>10} "
          f"{'var_Δ-qErr':>10} {'ratio(+)':>9} {'ratio(-)':>9}")
    print("-" * 78)

    best_ratio = 0
    best_offset = None
    best_sign = None

    for offset in offsets:
        # For each PPS, find matching qErr
        matched = []
        for p in pps_data:
            best_qerr = None
            best_err = tolerance
            for q in qerr_samples:
                dt = p['recv_mono'] - q['host_time']
                err = abs(dt - offset)
                if err < best_err:
                    best_err = err
                    best_qerr = q['qerr_ns']
            if best_qerr is not None:
                matched.append((p['frac_ns'], best_qerr))

        if len(matched) < W + 1:
            continue

        # First-differences over last W+1 samples → W pairs
        tail = matched[-(W + 1):]
        d_raw = [tail[i+1][0] - tail[i][0] for i in range(W)]
        d_plus = [(tail[i+1][0] + tail[i+1][1]) - (tail[i][0] + tail[i][1])
                  for i in range(W)]
        d_minus = [(tail[i+1][0] - tail[i+1][1]) - (tail[i][0] - tail[i][1])
                   for i in range(W)]

        vr = rolling_variance(d_raw)
        vp = rolling_variance(d_plus)
        vm = rolling_variance(d_minus)

        if vr and vp and vm and vp > 0 and vm > 0:
            rp = vr / vp
            rm = vr / vm
            marker = ""
            if rp > best_ratio:
                best_ratio = rp
                best_offset = offset
                best_sign = "+"
            if rm > best_ratio:
                best_ratio = rm
                best_offset = offset
                best_sign = "-"
            if rp > 1.05 or rm > 1.05:
                marker = " ←" if max(rp, rm) == best_ratio else ""
            print(f"{offset:10.3f} {len(matched):6d} {vr:10.1f} {vp:10.1f} "
                  f"{vm:10.1f} {rp:9.3f} {rm:9.3f}{marker}")

    if best_offset is not None:
        print(f"\nBest: offset={best_offset:.3f}s sign={best_sign} "
              f"ratio={best_ratio:.3f} "
              f"(variance reduced {(1 - 1/best_ratio)*100:.0f}%)")
    else:
        print("\nNo improvement found at any offset.")


if __name__ == "__main__":
    main()
