#!/usr/bin/env python3
"""Quick SSR diagnostic: dump correction values to verify units and signs.

Connects to NTRIP SSR + broadcast ephemeris streams, waits for both to populate,
then prints the actual orbit/clock correction values being applied per satellite.
"""

import sys
import time
import threading
import queue
import numpy as np
from datetime import datetime, timezone

sys.path.insert(0, '.')
from broadcast_eph import BroadcastEphemeris
from ssr_corrections import SSRState, RealtimeCorrections, C
from ntrip_client import NtripStream
from realtime_ppp import ntrip_reader

import configparser
import argparse
import logging

logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", stream=sys.stderr)
log = logging.getLogger("diag_ssr")

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--ntrip-conf", required=True)
    ap.add_argument("--eph-mount", default="BCEP00BKG0")
    ap.add_argument("--wait", type=int, default=60, help="Seconds to collect data")
    args = ap.parse_args()

    conf = configparser.ConfigParser()
    conf.read(args.ntrip_conf)
    s = conf['ntrip']

    beph = BroadcastEphemeris()
    ssr = SSRState()
    corrections = RealtimeCorrections(beph, ssr)
    stop = threading.Event()

    use_tls = s.getboolean('tls', False)

    # Ephemeris stream
    eph_stream = NtripStream(
        caster=s['caster'], port=int(s.get('port', 2101)),
        mountpoint=args.eph_mount, user=s['user'], password=s['password'],
        tls=use_tls,
    )
    t1 = threading.Thread(target=ntrip_reader, args=(eph_stream, beph, ssr, stop, "EPH"), daemon=True)
    t1.start()

    # SSR stream
    ssr_stream = NtripStream(
        caster=s['caster'], port=int(s.get('port', 2101)),
        mountpoint=s['mount'], user=s['user'], password=s['password'],
        tls=use_tls,
    )
    t2 = threading.Thread(target=ntrip_reader, args=(ssr_stream, beph, ssr, stop, "SSR"), daemon=True)
    t2.start()

    log.info(f"Collecting data for {args.wait}s...")
    time.sleep(args.wait)

    log.info(f"Broadcast: {beph.summary()}")
    log.info(f"SSR: {ssr.summary()}")

    # Now examine corrections for each satellite
    t = datetime.now(timezone.utc)

    print("\n=== SSR Orbit Corrections (raw values from SSRState) ===")
    print(f"{'PRN':>5} {'IOD':>4} {'Radial_m':>10} {'Along_m':>10} {'Cross_m':>10} {'|delta|':>10}")
    for prn in sorted(ssr._orbit.keys()):
        oc = ssr._orbit[prn]
        mag = (oc.radial**2 + oc.along**2 + oc.cross**2)**0.5
        print(f"{prn:>5} {oc.iod:>4} {oc.radial:>10.4f} {oc.along:>10.4f} {oc.cross:>10.4f} {mag:>10.4f}")

    print("\n=== SSR Clock Corrections (raw values from SSRState) ===")
    print(f"{'PRN':>5} {'c0_m':>12} {'c0_ns':>10} {'c1':>10} {'c2':>10}")
    for prn in sorted(ssr._clock.keys()):
        cc = ssr._clock[prn]
        c0_ns = cc.c0 / C * 1e9
        print(f"{prn:>5} {cc.c0:>12.4f} {c0_ns:>10.3f} {cc.c1:>10.6f} {cc.c2:>10.6f}")

    print("\n=== SSR Code Biases (sample) ===")
    print(f"{'PRN':>5} {'Signal':>6} {'Bias_m':>10}")
    for prn in sorted(list(ssr._code_bias.keys())[:5]):
        for sig, bc in sorted(ssr._code_bias[prn].items()):
            print(f"{prn:>5} {sig:>6} {bc.bias_m:>10.4f}")

    # Compare broadcast vs SSR-corrected positions
    print("\n=== Position Comparison: Broadcast vs SSR-corrected ===")
    print(f"{'PRN':>5} {'Bcast X':>14} {'Bcast Y':>14} {'Bcast Z':>14} "
          f"{'SSR X':>14} {'SSR Y':>14} {'SSR Z':>14} {'|diff|_m':>10} "
          f"{'clk_bcast_ns':>12} {'clk_ssr_ns':>12}")

    for prn in sorted(ssr._orbit.keys()):
        # Broadcast-only
        bcast_pos, bcast_clk = beph.sat_position(prn, t)
        if bcast_pos is None:
            continue

        # SSR-corrected (using RealtimeCorrections)
        ssr_pos, ssr_clk = corrections.sat_position(prn, t)
        if ssr_pos is None:
            continue

        diff = np.linalg.norm(ssr_pos - bcast_pos)
        bcast_clk_ns = bcast_clk * 1e9
        ssr_clk_ns = ssr_clk * 1e9

        print(f"{prn:>5} "
              f"{bcast_pos[0]:>14.2f} {bcast_pos[1]:>14.2f} {bcast_pos[2]:>14.2f} "
              f"{ssr_pos[0]:>14.2f} {ssr_pos[1]:>14.2f} {ssr_pos[2]:>14.2f} "
              f"{diff:>10.3f} "
              f"{bcast_clk_ns:>12.3f} {ssr_clk_ns:>12.3f}")

    stop.set()


if __name__ == "__main__":
    main()
