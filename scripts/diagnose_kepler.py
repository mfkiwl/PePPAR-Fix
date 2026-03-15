#!/usr/bin/env python3
"""Diagnostic: verify Keplerian orbit computation."""

import sys
import time
import threading
import configparser
import logging
import math

logging.basicConfig(level=logging.WARNING, format='%(message)s')
sys.path.insert(0, 'scripts')

from ntrip_client import NtripStream
from broadcast_eph import (BroadcastEphemeris, GPS_EPOCH, SECONDS_PER_WEEK,
                           _kepler_ecef, _check_week_crossover, GM_GPS)
from ssr_corrections import SSRState
from realtime_ppp import ntrip_reader
from solve_pseudorange import lla_to_ecef
from datetime import datetime, timezone
import numpy as np


def main():
    conf = configparser.ConfigParser()
    conf.read('ntrip.conf')
    s = conf['ntrip']

    beph = BroadcastEphemeris()
    ssr = SSRState()
    stop = threading.Event()

    eph_stream = NtripStream(
        caster=s['caster'], port=int(s['port']),
        mountpoint='BCEP00BKG0', user=s['user'], password=s['password'],
        tls=s.getboolean('tls', False), timeout=15)
    t1 = threading.Thread(target=ntrip_reader,
                          args=(eph_stream, beph, ssr, stop, 'EPH'), daemon=True)
    t1.start()
    time.sleep(15)

    known = lla_to_ecef(41.8430626, -88.1037190, 201.671)
    now = datetime.now(timezone.utc)
    week, sow = beph._gps_seconds_of_week(now)

    print(f"UTC: {now}")
    print(f"GPS week: {week}, SOW: {sow:.3f}")
    print(f"Known pos: {known}")
    print()

    for sv in ['G06', 'G11', 'G19']:
        eph = beph._ephs.get(sv)
        if not eph:
            print(f"{sv}: no ephemeris")
            continue

        print(f"=== {sv} ===")
        print(f"  eph week (from RTCM): {eph['week']}")
        print(f"  toe: {eph['toe']}")
        print(f"  sqrt_a: {eph['sqrt_a']:.6f}")
        print(f"  omega0: {eph['omega0']:.6f}")

        a = eph['sqrt_a'] ** 2
        tk = _check_week_crossover(sow - eph['toe'])
        print(f"  a: {a:.0f} m ({a/1000:.0f} km)")
        print(f"  tk: {tk:.0f} s")

        # Compute position
        pos, Ek = _kepler_ecef(eph, tk, GM_GPS)
        r = np.linalg.norm(pos)
        print(f"  |pos|: {r/1e6:.3f} Mm (expected ~26.56 Mm)")

        # Elevation from known position
        dx = pos - known
        rho = np.linalg.norm(dx)
        up = known / np.linalg.norm(known)
        sin_elev = np.dot(dx / rho, up)
        elev = math.degrees(math.asin(max(-1, min(1, sin_elev))))
        print(f"  Computed elev: {elev:.1f}°")

        # Also compute via sat_position() to make sure it matches
        pos2, clk2 = beph.sat_position(sv, now)
        if pos2 is not None:
            diff = np.linalg.norm(pos2 - pos)
            print(f"  sat_position() vs manual: diff={diff:.3f}m")
        print()

    stop.set()


if __name__ == '__main__':
    main()
