#!/usr/bin/env python3
"""Probe the relationship between RXM-RAWX rcvTow and PPP dt_rx.

Short experiment: run the FixedPosFilter for ~60 seconds, logging
rcvTow at full double precision alongside dt_rx from the filter.

Goal: find how to reference dt_rx to the top of the GNSS second,
so PPS+PPP can compete as a servo error source.
"""

import csv
import json
import logging
import math
import os
import queue
import signal
import sys
import threading
import time
from datetime import datetime, timezone, timedelta

import numpy as np

# Add scripts dir to path
sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from solve_pseudorange import C, ecef_to_lla, lla_to_ecef
from solve_ppp import FixedPosFilter
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections
from ntrip_client import NtripStream
from realtime_ppp import serial_reader, ntrip_reader, QErrStore

log = logging.getLogger("rcvtow_probe")

# Assumed F9T clock frequency
F9T_CLOCK_HZ = 125_000_000
F9T_TICK_NS = 1e9 / F9T_CLOCK_HZ  # 8 ns


def main():
    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s %(message)s")

    # Load position from the existing position file on ocxo
    pos_file = os.path.join(os.path.dirname(__file__), "..", "data", "position.json")
    if not os.path.exists(pos_file):
        pos_file = "/home/bob/PePPAR-Fix/data/position.json"
    with open(pos_file) as f:
        pdata = json.load(f)
    lat, lon = pdata["lat"], pdata["lon"]
    alt = pdata.get("alt", pdata.get("alt_m"))
    if "ecef_m" in pdata:
        known_ecef = pdata["ecef_m"]
    else:
        known_ecef = lla_to_ecef(lat, lon, alt)
    log.info(f"Position: {lat:.6f}, {lon:.6f}, {alt:.1f}m")

    # Load NTRIP config (INI format)
    import configparser
    ntrip_conf = "/home/bob/PePPAR-Fix/ntrip.conf"
    conf = configparser.ConfigParser()
    conf.read(ntrip_conf)
    nconf = conf["ntrip"]

    # Set up correction streams
    broadcast = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(broadcast, ssr)

    eph_mount = "BCEP00BKG0"
    ssr_mount = "SSRA00BKG0"
    caster = nconf["caster"]
    port = int(nconf["port"])
    user = nconf["user"]
    password = nconf["password"]
    tls = nconf.getboolean("tls", True)

    # Start NTRIP streams
    eph_stream = NtripStream(caster, port, eph_mount, user, password, tls=tls)
    ssr_stream = NtripStream(caster, port, ssr_mount, user, password, tls=tls)

    obs_queue = queue.Queue(maxsize=50)
    qerr_store = QErrStore()
    stop = threading.Event()

    # Track raw rcvTow from RAWX messages
    rawx_info = {"rcvTow": None, "week": None, "lock": threading.Lock()}

    def raw_callback(parsed):
        """Capture rcvTow at full precision from each RAWX message."""
        with rawx_info["lock"]:
            rawx_info["rcvTow"] = parsed.rcvTow
            rawx_info["week"] = parsed.week

    # Start NTRIP readers
    ntrip_eph_thread = threading.Thread(
        target=ntrip_reader,
        args=(eph_stream, broadcast, ssr),
        kwargs={"stop_event": stop},
        daemon=True,
    )
    ntrip_ssr_thread = threading.Thread(
        target=ntrip_reader,
        args=(ssr_stream, broadcast, ssr),
        kwargs={"stop_event": stop},
        daemon=True,
    )
    ntrip_eph_thread.start()
    ntrip_ssr_thread.start()

    # Wait for ephemeris
    log.info("Waiting for broadcast ephemeris...")
    systems = {"gps", "gal"}
    needed = {"G", "E"}
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        have = set()
        for sv in broadcast.satellites:
            have.add(sv[0])
        if needed <= have:
            break
        time.sleep(0.5)
    log.info(f"Ephemeris ready: {broadcast}")

    # Start serial reader
    serial_port = "/dev/gnss0"
    serial_thread = threading.Thread(
        target=serial_reader,
        args=(serial_port, 9600, obs_queue, stop, broadcast),
        kwargs={
            "systems": systems,
            "ssr": ssr,
            "qerr_store": qerr_store,
            "raw_callback": raw_callback,
        },
        daemon=True,
    )
    serial_thread.start()
    log.info("Serial reader started")

    # Set up filter - seed at pseudorange clock (NOT zero)
    filt = FixedPosFilter(known_ecef)
    # Let the filter self-seed from pseudoranges (don't force dt_rx=0)

    # Signal handling
    def _stop(sig, frame):
        stop.set()
    signal.signal(signal.SIGINT, _stop)

    # CSV output
    out_path = "/tmp/rcvtow_probe.csv"
    out_f = open(out_path, "w", newline="")
    writer = csv.writer(out_f)
    writer.writerow([
        "epoch", "utc",
        "rcvTow", "rcvTow_frac_s", "rcvTow_frac_ns",
        "dt_rx_m", "dt_rx_ns", "dt_rx_sigma_ns",
        "true_time_frac_ns",
        "rcvTow_mod_8ns",
        "qerr_ppp_ns", "qerr_timtp_ns", "qerr_delta_ns",
        "n_used", "n_td",
        "source",
    ])

    prev_t = None
    n_epochs = 0
    max_epochs = 60  # ~1 minute at 1 Hz

    log.info(f"Logging to {out_path}, max {max_epochs} epochs")

    while not stop.is_set() and n_epochs < max_epochs:
        try:
            obs_event = obs_queue.get(timeout=5.0)
        except queue.Empty:
            log.info("No observations, waiting...")
            continue

        gps_time, observations = obs_event

        # Grab the rcvTow that corresponds to this epoch
        with rawx_info["lock"]:
            rcvTow = rawx_info["rcvTow"]

        if rcvTow is None:
            continue

        # EKF predict + update
        if prev_t is not None:
            dt = (gps_time - prev_t).total_seconds()
            if 0 < dt < 30:
                filt.predict(dt)
        prev_t = gps_time

        n_used, resid, n_td = filt.update(
            observations, corrections, gps_time,
            clk_file=corrections,
        )

        if n_used < 4:
            continue

        n_epochs += 1

        # Extract clock state
        dt_rx_m = filt.x[filt.IDX_CLK]
        dt_rx_ns = dt_rx_m / C * 1e9
        p_clk = filt.P[filt.IDX_CLK, filt.IDX_CLK]
        dt_rx_sigma_ns = math.sqrt(max(0, p_clk)) / C * 1e9

        # Source info
        source = "SSR" if ssr.n_clock > 0 else "broadcast"

        # TIM-TP qErr for this epoch
        qerr_ns, _qerr_tow, qerr_age_s, _qerr_delta = qerr_store.match_gps_time(gps_time)

        # rcvTow analysis
        rcvTow_frac_s = rcvTow % 1.0             # fractional second
        rcvTow_frac_ns = rcvTow_frac_s * 1e9      # in nanoseconds

        # True GNSS time = rcvTow - dt_rx (in seconds)
        dt_rx_s = dt_rx_m / C
        true_time_s = rcvTow - dt_rx_s
        true_time_frac_ns = (true_time_s % 1.0) * 1e9

        # rcvTow modulo one clock tick (8 ns at 125 MHz)
        rcvTow_mod_tick = (rcvTow_frac_ns % F9T_TICK_NS)

        # PPP-derived quantization error:
        # Receiver clock fractional second at the GNSS integer second
        # = dt_rx_ns mod 1e9 (distance of rx second boundary from GNSS second)
        D_ns = dt_rx_ns % 1_000_000_000  # Python % always non-negative for positive divisor
        qerr_ppp = round(D_ns / F9T_TICK_NS) * F9T_TICK_NS - D_ns

        ts_str = gps_time.strftime('%Y-%m-%d %H:%M:%S')

        writer.writerow([
            n_epochs, ts_str,
            f"{rcvTow:.12f}", f"{rcvTow_frac_s:.12f}", f"{rcvTow_frac_ns:.3f}",
            f"{dt_rx_m:.6f}", f"{dt_rx_ns:.3f}", f"{dt_rx_sigma_ns:.3f}",
            f"{true_time_frac_ns:.3f}",
            f"{rcvTow_mod_tick:.3f}",
            f"{qerr_ppp:.3f}",
            f"{qerr_ns:.3f}" if qerr_ns is not None else "",
            f"{qerr_ppp - qerr_ns:.3f}" if qerr_ns is not None else "",
            n_used, n_td,
            source,
        ])
        out_f.flush()

        if n_epochs % 10 == 0 or n_epochs <= 5:
            qerr_str = f"qErr_timtp={qerr_ns:+.3f}" if qerr_ns is not None else "qErr=N/A"
            log.info(
                f"  [{n_epochs:3d}] dt_rx={dt_rx_ns:+12.3f}ns "
                f"qerr_ppp={qerr_ppp:+.3f}ns {qerr_str} "
                f"delta={qerr_ppp - qerr_ns:+.3f}ns "
                if qerr_ns is not None else
                f"  [{n_epochs:3d}] dt_rx={dt_rx_ns:+12.3f}ns "
                f"qerr_ppp={qerr_ppp:+.3f}ns qErr=N/A "
                f"n={n_used}({n_td}td)"
            )

    out_f.close()
    stop.set()
    log.info(f"Done. {n_epochs} epochs logged to {out_path}")


if __name__ == "__main__":
    main()
