#!/usr/bin/env python3
"""Quick test: capture TIM-TP qErr values from F9T and show PPS error budget.

Reads TIM-TP messages for --duration seconds. Reports:
  - qErr statistics (mean, std, min, max)
  - Expected PPS variance reduction from qErr correction
  - If PPP filter converges, also shows carrier-phase dt_rx stats

No PHC or PTP device needed — just the serial port.
"""

import argparse
import math
import queue
import sys
import threading
import time

import numpy as np


def main():
    ap = argparse.ArgumentParser(description="TIM-TP qErr variance test")
    ap.add_argument("--serial", required=True, help="F9T serial port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--duration", type=int, default=60)
    ap.add_argument("--known-pos", default="41.8430626,-88.1037190,201.671")
    ap.add_argument("--ntrip-conf", default=None)
    ap.add_argument("--caster", default="ntrip.data.gnss.ga.gov.au")
    ap.add_argument("--port", type=int, default=443)
    ap.add_argument("--user", default="")
    ap.add_argument("--password", default="")
    ap.add_argument("--tls", action="store_true")
    ap.add_argument("--eph-mount", default="BCEP00BKG0")
    ap.add_argument("--ssr-mount", default=None)
    ap.add_argument("--systems", default="gps,gal")
    args = ap.parse_args()

    # Late imports (same dir)
    from solve_pseudorange import C, lla_to_ecef
    from solve_ppp import FixedPosFilter
    from ntrip_client import NtripStream
    from broadcast_eph import BroadcastEphemeris
    from ssr_corrections import SSRState, RealtimeCorrections
    from realtime_ppp import serial_reader, ntrip_reader, QErrStore

    parts = args.known_pos.split(',')
    known_ecef = lla_to_ecef(float(parts[0]), float(parts[1]), float(parts[2]))

    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    obs_queue = queue.Queue(maxsize=100)
    stop_event = threading.Event()
    qerr_store = QErrStore()

    # NTRIP
    ntrip_kwargs = {}
    if args.ntrip_conf:
        import configparser
        cfg = configparser.ConfigParser()
        cfg.read(args.ntrip_conf)
        ntrip_kwargs = {
            'caster': cfg.get('ntrip', 'caster', fallback=args.caster),
            'port': cfg.getint('ntrip', 'port', fallback=args.port),
            'user': cfg.get('ntrip', 'user', fallback=''),
            'password': cfg.get('ntrip', 'password', fallback=''),
            'tls': cfg.getboolean('ntrip', 'tls', fallback=False),
        }
    else:
        ntrip_kwargs = {
            'caster': args.caster, 'port': args.port,
            'user': args.user, 'password': args.password, 'tls': args.tls,
        }

    eph_stream = NtripStream(
        caster=ntrip_kwargs['caster'], port=ntrip_kwargs['port'],
        mountpoint=args.eph_mount,
        user=ntrip_kwargs['user'], password=ntrip_kwargs['password'],
        tls=ntrip_kwargs['tls'],
    )
    t_eph = threading.Thread(
        target=ntrip_reader,
        args=(eph_stream, beph, ssr, stop_event, "EPH"),
        daemon=True,
    )
    t_eph.start()

    if args.ssr_mount:
        ssr_stream = NtripStream(
            caster=ntrip_kwargs['caster'], port=ntrip_kwargs['port'],
            mountpoint=args.ssr_mount,
            user=ntrip_kwargs['user'], password=ntrip_kwargs['password'],
            tls=ntrip_kwargs['tls'],
        )
        t_ssr = threading.Thread(
            target=ntrip_reader,
            args=(ssr_stream, beph, ssr, stop_event, "SSR"),
            daemon=True,
        )
        t_ssr.start()

    print("Waiting for broadcast ephemeris...")
    while beph.n_satellites < 8 and not stop_event.is_set():
        time.sleep(2)
        print(f"  Warmup: {beph.summary()}")
    print(f"Warmup complete: {beph.summary()}")

    systems = set(args.systems.split(',')) if args.systems else None
    t_serial = threading.Thread(
        target=serial_reader,
        args=(args.serial, args.baud, obs_queue, stop_event, beph, systems, ssr),
        kwargs={'qerr_store': qerr_store},
        daemon=True,
    )
    t_serial.start()
    print(f"Serial: {args.serial} at {args.baud}")

    # PPP filter
    filt = FixedPosFilter(known_ecef)
    filt.prev_clock = 0.0

    qerr_vals = []
    dt_rx_vals = []
    dt_rx_sigma_vals = []
    prev_t = None
    n_epochs = 0
    start = time.time()

    print(f"\nCollecting data for {args.duration}s...\n")

    try:
        while time.time() - start < args.duration and not stop_event.is_set():
            try:
                gps_time, observations = obs_queue.get(timeout=5)
            except queue.Empty:
                continue

            if prev_t is not None:
                dt = (gps_time - prev_t).total_seconds()
                if dt <= 0 or dt > 30:
                    prev_t = gps_time
                    continue
                filt.predict(dt)
            prev_t = gps_time

            n_used, resid, n_td = filt.update(
                observations, corrections, gps_time,
                clk_file=corrections,
            )
            if n_used < 4:
                continue

            dt_rx_ns = filt.x[filt.IDX_CLK] / C * 1e9
            p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
            dt_rx_sigma = math.sqrt(max(0, p_clk)) / C * 1e9
            n_epochs += 1

            # Capture qErr
            qerr_ns, _ = qerr_store.get()
            if qerr_ns is not None:
                qerr_vals.append(qerr_ns)

            dt_rx_vals.append(dt_rx_ns)
            dt_rx_sigma_vals.append(dt_rx_sigma)

            if n_epochs % 10 == 0:
                qstat = f"qErr={qerr_ns:+.1f}ns" if qerr_ns is not None else "qErr=N/A"
                print(f"  [{n_epochs:3d}] n_sv={n_used:2d}  "
                      f"dt_rx={dt_rx_ns:+10.1f}ns ±{dt_rx_sigma:6.1f}  "
                      f"{qstat}")

    except KeyboardInterrupt:
        pass
    finally:
        stop_event.set()

    elapsed = time.time() - start
    print(f"\n{'='*60}")
    print(f"  M6 qErr Variance Test")
    print(f"  Duration: {elapsed:.0f}s, Epochs: {n_epochs}")
    print(f"{'='*60}")

    if not qerr_vals:
        print("\n  ❌ No TIM-TP qErr values received!")
        print("     Check: is TIM-TP enabled? Run configure_f9t.py first.")
        sys.exit(1)

    qerr = np.array(qerr_vals)
    print(f"\n  TIM-TP qErr ({len(qerr)} samples):")
    print(f"    mean:  {np.mean(qerr):+.2f} ns")
    print(f"    std:   {np.std(qerr):.2f} ns")
    print(f"    min:   {np.min(qerr):+.2f} ns")
    print(f"    max:   {np.max(qerr):+.2f} ns")

    print(f"\n  Expected PPS error budget:")
    print(f"    PPS-only:     ~20 ns (receiver alignment + qErr jitter)")
    print(f"    PPS+qErr:     ~{max(0.5, 20.0 - np.std(qerr)):.1f} ns "
          f"(qErr std={np.std(qerr):.2f}ns removed)")

    if dt_rx_vals:
        dt = np.array(dt_rx_vals)
        sig = np.array(dt_rx_sigma_vals)
        # dt_rx drifts (receiver clock), so detrend for variance
        dt_detrended = dt - np.polyval(np.polyfit(range(len(dt)), dt, 1), range(len(dt)))
        print(f"\n  PPP carrier-phase dt_rx ({len(dt)} samples):")
        print(f"    mean:      {np.mean(dt):+.1f} ns")
        print(f"    raw std:   {np.std(dt):.1f} ns (includes clock drift)")
        print(f"    detrended: {np.std(dt_detrended):.2f} ns (noise floor)")
        print(f"    σ range:   {np.min(sig):.1f} – {np.max(sig):.1f} ns")
        print(f"    last σ:    {sig[-1]:.2f} ns")

    print(f"\n  Error source confidence comparison:")
    print(f"    PPS-only:    ±20 ns")
    if qerr_vals:
        print(f"    PPS+qErr:    ±{np.std(qerr):.1f} ns")
    if dt_rx_vals and sig[-1] < 50:
        print(f"    Carrier:     ±{sig[-1]:.2f} ns")

    print()


if __name__ == "__main__":
    main()
