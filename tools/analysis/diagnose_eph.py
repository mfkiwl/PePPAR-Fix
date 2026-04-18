#!/usr/bin/env python3
"""Diagnostic: check broadcast ephemeris parameters."""

import os
import sys
import time
import threading
import configparser
import logging
import math

logging.basicConfig(level=logging.WARNING, format='%(message)s')
sys.path.insert(0, os.path.join(os.path.dirname(os.path.abspath(__file__)), '..', '..', 'scripts'))

from ntrip_client import NtripStream
from broadcast_eph import BroadcastEphemeris, GPS_EPOCH, SECONDS_PER_WEEK
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

    now = datetime.now(timezone.utc)
    week, sow = beph._gps_seconds_of_week(now)
    print(f"UTC now: {now}")
    print(f"GPS week: {week}, SOW: {sow:.3f}")
    print()

    from peppar_fix.lab_position import load_lab_position
    known = lla_to_ecef(*load_lab_position())

    for sv in ['G01', 'G06', 'G11', 'G19', 'E30', 'C21']:
        eph = beph._ephs.get(sv)
        if not eph:
            print(f"{sv}: no ephemeris")
            continue

        toe = eph['toe']
        toc = eph['toc']
        sqrtA = eph.get('sqrtA', 0)
        e = eph.get('e', 0)
        M0 = eph.get('M0', 0)
        omega = eph.get('omega', 0)
        Omega0 = eph.get('Omega0', 0)
        i0 = eph.get('i0', 0)

        tk = sow - toe
        if tk > 302400:
            tk -= 604800
        if tk < -302400:
            tk += 604800

        print(f"{sv}: toe={toe:.0f} toc={toc:.0f} sow={sow:.0f} tk={tk:.0f}s")
        print(f"  sqrtA={sqrtA:.4f} e={e:.10f} M0={M0:.6f}")
        print(f"  omega={omega:.6f} Omega0={Omega0:.6f} i0={i0:.6f}")

        # Compute position and check
        pos, clk = beph.sat_position(sv, now)
        if pos is not None:
            dx = pos - known
            rho = np.linalg.norm(dx)
            up = known / np.linalg.norm(known)
            sin_elev = np.dot(dx / rho, up)
            elev = math.degrees(math.asin(max(-1, min(1, sin_elev))))
            print(f"  pos=[{pos[0]:.0f}, {pos[1]:.0f}, {pos[2]:.0f}]")
            print(f"  range={rho:.0f}m  elev={elev:.1f}°  clk={clk:.9f}s")

            # Sanity: orbital radius should be ~26600km for GPS, ~29600km for GAL
            r = np.linalg.norm(pos)
            a = sqrtA ** 2
            print(f"  |pos|={r/1e6:.3f} Mm  a={a/1e6:.3f} Mm  a-|pos|={(a-r)/1e3:.1f} km")
        else:
            print(f"  FAILED to compute position")
        print()

    stop.set()


if __name__ == '__main__':
    main()
