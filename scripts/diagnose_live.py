#!/usr/bin/env python3
"""Diagnostic: check why filter gets 0 measurements in live mode."""

import sys
import time
import threading
import configparser
import queue
import logging
import math

logging.basicConfig(level=logging.WARNING, format='%(message)s')
sys.path.insert(0, 'scripts')

from ntrip_client import NtripStream
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections
from realtime_ppp import ntrip_reader, serial_reader
from solve_pseudorange import C, OMEGA_E, lla_to_ecef
from solve_ppp import ELEV_MASK
import numpy as np


def main():
    conf = configparser.ConfigParser()
    conf.read('ntrip.conf')
    s = conf['ntrip']

    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    stop = threading.Event()
    obs_queue = queue.Queue(maxsize=100)

    # Start NTRIP
    eph_stream = NtripStream(
        caster=s['caster'], port=int(s['port']),
        mountpoint='BCEP00BKG0', user=s['user'], password=s['password'],
        tls=s.getboolean('tls', False), timeout=15)
    ssr_stream = NtripStream(
        caster=s['caster'], port=int(s['port']),
        mountpoint=s['mount'], user=s['user'], password=s['password'],
        tls=s.getboolean('tls', False), timeout=15)

    t1 = threading.Thread(target=ntrip_reader,
                          args=(eph_stream, beph, ssr, stop, 'EPH'), daemon=True)
    t2 = threading.Thread(target=ntrip_reader,
                          args=(ssr_stream, beph, ssr, stop, 'SSR'), daemon=True)
    t1.start()
    t2.start()

    print("Warming up NTRIP (15s)...")
    time.sleep(15)
    print(f"  {beph.summary()}")
    print(f"  {ssr.summary()}")

    # Start serial
    t_ser = threading.Thread(
        target=serial_reader,
        args=('/dev/gnss-bot', 115200, obs_queue, stop, beph),
        daemon=True)
    t_ser.start()
    print("Waiting for serial observations (5s)...")
    time.sleep(5)

    # Known position
    known_ecef = lla_to_ecef(41.8430626, -88.1037190, 201.671)

    # Grab 3 epochs
    for epoch_n in range(3):
        try:
            gps_time, observations = obs_queue.get(timeout=10)
        except queue.Empty:
            print("No observations from serial!")
            break

        print(f"\n--- Epoch {epoch_n+1}: {gps_time} ({len(observations)} obs) ---")

        for obs in observations:
            sv = obs['sv']
            pr_if = obs['pr_if']
            cno = obs['cno']

            # Step 1: Can we get satellite position?
            sat_pos, sat_clk = corrections.sat_position(sv, gps_time)
            if sat_pos is None:
                # Why?
                bpos, bclk = beph.sat_position(sv, gps_time)
                oc = ssr.get_orbit(sv)
                cc = ssr.get_clock(sv)
                print(f"  {sv}: NO POSITION  beph={bpos is not None}  "
                      f"ssr_orbit={oc is not None}  ssr_clock={cc is not None}")
                continue

            # Step 2: Clock sanity
            if sat_clk is None or abs(sat_clk) > 0.9:
                print(f"  {sv}: BAD CLOCK  sat_clk={sat_clk}")
                continue

            # Step 3: Elevation
            dx = sat_pos - known_ecef
            rho = np.linalg.norm(dx)
            up = known_ecef / np.linalg.norm(known_ecef)
            sin_elev = np.dot(dx / rho, up)
            elev = math.degrees(math.asin(max(-1, min(1, sin_elev))))

            if elev < ELEV_MASK:
                print(f"  {sv}: LOW ELEV {elev:.1f}° < {ELEV_MASK}°")
                continue

            # Step 4: Range residual
            sat_clk_m = sat_clk * C
            tropo = 2.3 / math.sin(math.radians(max(5, elev)))
            rho_corr = rho - sat_clk_m + tropo
            resid = pr_if - rho_corr
            print(f"  {sv}: OK  elev={elev:.1f}°  cno={cno}  "
                  f"resid={resid:.1f}m  clk={sat_clk*1e6:.1f}µs")

    stop.set()
    print("\nDone.")


if __name__ == '__main__':
    main()
