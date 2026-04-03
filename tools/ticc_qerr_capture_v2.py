#!/usr/bin/env python3
"""Capture TICC chB + TIM-TP qErr with monotonic-time correlation.

NOTE: Prefer the engine's built-in TICC capture (--ticc-port + --ticc-log)
over this standalone script.  The engine captures both TICC channels
in-process alongside the servo, with shared lifecycle and no cross-process
coordination issues.  This script is useful only when the engine is not
running (e.g., raw F9T PPS + qErr characterization without PHC discipline).

Correlates TICC PPS edges with TIM-TP qErr by host monotonic time,
using the same 0.9s offset that peppar-fix uses (TIM-TP arrives
~900 ms before the PPS edge it describes).

Usage:
    python3 ticc_qerr_capture_v2.py --duration 1800 --out data/ticc-qerr-v2.csv
"""

import argparse
import csv
import sys
import threading
import time

sys.path.insert(0, "scripts")

from ticc import Ticc
from peppar_fix.gnss_stream import open_gnss
from pyubx2 import UBXReader, UBX_PROTOCOL


QERR_OFFSET_S = 0.9  # TIM-TP arrives ~900ms before the PPS it describes


def ticc_reader(port, baud, stop_event, events, lock):
    """Read TICC chB: append (ref_sec, ref_ps, mono) tuples."""
    with Ticc(port, baud=baud, wait_for_boot=True) as t:
        for ch, sec, ps in t:
            if stop_event.is_set():
                break
            mono = time.monotonic()
            if ch == "chB":
                with lock:
                    events.append((sec, ps, mono))


def timtp_reader(serial_port, serial_baud, stop_event, events, lock):
    """Read TIM-TP: append (qerr_ns, mono) tuples."""
    ser, device_type = open_gnss(serial_port, serial_baud)
    ubr = UBXReader(ser, protfilter=UBX_PROTOCOL)
    try:
        for raw, parsed in ubr:
            if stop_event.is_set():
                break
            if parsed is None:
                continue
            if hasattr(parsed, 'identity') and parsed.identity == 'TIM-TP':
                mono = time.monotonic()
                qerr_ps = getattr(parsed, 'qErr', None)
                if qerr_ps is not None:
                    with lock:
                        events.append((qerr_ps / 1000.0, mono))  # ps → ns
    finally:
        ser.close()


def match_qerr_to_pps(ticc_events, timtp_events):
    """Match each TICC PPS edge to its closest qErr at ~0.9s offset.

    TIM-TP arrives at mono_timtp. The PPS it describes fires at
    mono_timtp + 0.9s. Find the TICC event closest to that predicted
    PPS time.
    """
    matched = []
    timtp_idx = 0

    for sec, ps, pps_mono in ticc_events:
        # Find the TIM-TP whose predicted PPS time is closest to this PPS
        best_qerr = None
        best_dt = float('inf')

        while timtp_idx < len(timtp_events):
            qerr_ns, tp_mono = timtp_events[timtp_idx]
            predicted_pps_mono = tp_mono + QERR_OFFSET_S
            dt = abs(pps_mono - predicted_pps_mono)

            if dt < best_dt:
                best_dt = dt
                best_qerr = qerr_ns

            # If we've gone past the PPS time, stop searching
            if predicted_pps_mono > pps_mono + 1.0:
                break
            timtp_idx += 1

        # Back up index for next PPS (TIM-TP events are reusable)
        if timtp_idx > 0:
            timtp_idx -= 1

        if best_qerr is not None and best_dt < 0.5:
            matched.append((sec, ps, best_qerr, best_dt))

    return matched


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=1800)
    ap.add_argument("--out", default="data/ticc-qerr-v2.csv")
    ap.add_argument("--ticc-port", default="/dev/ticc1")
    ap.add_argument("--ticc-baud", type=int, default=115200)
    ap.add_argument("--serial", default="/dev/gnss-top")
    ap.add_argument("--baud", type=int, default=9600)
    args = ap.parse_args()

    print(f"Capturing TICC chB + TIM-TP qErr for {args.duration}s → {args.out}")

    stop = threading.Event()
    lock = threading.Lock()
    ticc_events = []
    timtp_events = []

    t_ticc = threading.Thread(target=ticc_reader,
                              args=(args.ticc_port, args.ticc_baud, stop, ticc_events, lock))
    t_timtp = threading.Thread(target=timtp_reader,
                               args=(args.serial, args.baud, stop, timtp_events, lock))

    t_ticc.start()
    t_timtp.start()

    t0 = time.monotonic()
    while time.monotonic() - t0 < args.duration:
        time.sleep(30)
        print(f"  {time.monotonic()-t0:.0f}s: {len(ticc_events)} TICC, {len(timtp_events)} qErr")

    stop.set()
    t_ticc.join(timeout=5)
    t_timtp.join(timeout=5)

    print(f"Raw: {len(ticc_events)} TICC, {len(timtp_events)} qErr")

    # Sort by monotonic time
    ticc_events.sort(key=lambda x: x[2])
    timtp_events.sort(key=lambda x: x[1])

    # Correlate
    matched = match_qerr_to_pps(ticc_events, timtp_events)
    print(f"Matched: {len(matched)} pairs")

    if len(matched) < 10:
        print("ERROR: too few matched pairs")
        sys.exit(1)

    # Build phase arrays
    # Phase = cumulative residual of TICC intervals from 1.000000000 s
    with open(args.out, 'w', newline='') as f:
        w = csv.writer(f)
        w.writerow(['epoch', 'ref_sec', 'ticc_phase_ps', 'qerr_ns',
                     'corrected_phase_ps', 'match_dt_s'])

        phase = 0
        prev_sec = None
        prev_ps = None
        epoch = 0

        for sec, ps, qerr_ns, match_dt in matched:
            if prev_sec is not None:
                dt_sec = sec - prev_sec
                if dt_sec == 1:
                    residual_ps = ps - prev_ps
                    if residual_ps < -500_000_000_000:
                        residual_ps += 1_000_000_000_000
                    elif residual_ps > 500_000_000_000:
                        residual_ps -= 1_000_000_000_000
                    phase += residual_ps
                    corrected = phase + qerr_ns * 1000  # ns → ps
                    w.writerow([epoch, sec, phase, f'{qerr_ns:.3f}',
                                f'{corrected:.0f}', f'{match_dt:.4f}'])
                    epoch += 1
                elif dt_sec > 1:
                    # Gap — reset phase tracking
                    pass

            prev_sec = sec
            prev_ps = ps

    print(f"Written: {args.out} ({epoch} epochs)")

    # Quick stats
    if epoch > 10:
        import numpy as np
        with open(args.out) as f:
            rows = list(csv.DictReader(f))
        match_dts = [float(r['match_dt_s']) for r in rows]
        print(f"Match quality: mean dt={np.mean(match_dts)*1000:.1f} ms, "
              f"std={np.std(match_dts)*1000:.1f} ms, "
              f"max={np.max(match_dts)*1000:.1f} ms")


if __name__ == "__main__":
    main()
