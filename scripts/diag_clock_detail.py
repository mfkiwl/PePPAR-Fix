#!/usr/bin/env python3
"""Detailed clock computation diagnostic: show every term for each satellite."""
import sys, time, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from broadcast_eph import (BroadcastEphemeris, C, OMEGA_E, F_REL,
                           _kepler_ecef, _check_week_crossover, GM_GPS, GM_GAL)
from solve_pseudorange import lla_to_ecef
from ntrip_client import NtripStream
from datetime import datetime, timezone, timedelta
import configparser
import numpy as np
import serial as pyserial
from pyubx2 import UBXReader

from peppar_fix.lab_position import load_lab_position
KNOWN_POS = list(load_lab_position())
user_ecef = np.array(lla_to_ecef(*KNOWN_POS))

# Collect ephemeris
beph = BroadcastEphemeris()
cfg = configparser.ConfigParser()
cfg.read('ntrip.conf')
sec = cfg.sections()[0]
stream = NtripStream(
    cfg.get(sec, 'caster'), cfg.getint(sec, 'port', fallback=443),
    'BCEP00BKG0', cfg.get(sec, 'user', fallback=''),
    cfg.get(sec, 'password', fallback=''), tls=True)
stream.connect()
deadline = time.monotonic() + 12
for msg in stream.messages():
    if time.monotonic() > deadline: break
    if hasattr(msg, 'identity'):
        mt = msg.identity.split('(')[0].strip()
        if mt in ('1019', '1045', '1046'):
            beph.update_from_rtcm(msg)
stream.disconnect()
print(f"Ephemeris: {beph.summary()}")

# Get one RAWX for timing
ser = pyserial.Serial('/dev/gnss-top', 9600, timeout=2)
ubr = UBXReader(ser, protfilter=2)
for _ in range(100):
    try:
        raw, parsed = ubr.read()
    except: continue
    if parsed and hasattr(parsed, 'identity') and parsed.identity == 'RXM-RAWX':
        week = parsed.week
        rcvTow = parsed.rcvTow
        break
ser.close()

GPS_EPOCH = datetime(1980, 1, 6, tzinfo=timezone.utc)
t_rx = GPS_EPOCH + timedelta(weeks=week, seconds=rcvTow)
print(f"Epoch: week={week} tow={rcvTow:.3f} = {t_rx.strftime('%Y-%m-%d %H:%M:%S')} UTC")
print()

# For selected satellites, show detailed clock computation
gps_delta = (t_rx - GPS_EPOCH).total_seconds()
computed_week = int(gps_delta // 604800)
sow = gps_delta - computed_week * 604800
print(f"Computed: week={computed_week} sow={sow:.3f}")
print()

# Show per-satellite detail
fmt = "%-5s %10s %10s %10s %12s %12s %12s %12s %12s %12s"
print(fmt % ("SV", "toc(s)", "toe(s)", "sow(s)", "dt_clk(s)",
             "af0(ns)", "af1*dt(ns)", "af2*dt2(ns)", "dtr(ns)", "TGD(ns)"))
print("-" * 130)

for sv in sorted(beph._ephs.keys()):
    if sv[0] not in ('G', 'E'): continue
    eph = beph._ephs[sv]
    sys_name = eph['system']
    gm = eph['gm']

    # Compute exactly as sat_position does
    tok = _check_week_crossover(sow - eph['toe'])
    dt_clk = _check_week_crossover(sow - eph['toc'])

    # Keplerian model to get Ek
    _, Ek = _kepler_ecef(eph, tok, gm)

    # Clock terms
    af0 = eph['af0']
    af1 = eph.get('af1', 0.0)
    af2 = eph.get('af2', 0.0)
    tgd = eph.get('tgd', 0.0)
    delta_tr = F_REL * eph['e'] * eph['sqrt_a'] * math.sin(Ek)

    term_af0 = af0
    term_af1 = af1 * dt_clk
    term_af2 = af2 * dt_clk ** 2
    total = term_af0 + term_af1 + term_af2 + delta_tr - tgd

    print(fmt % (
        sv,
        f"{eph['toc']:.0f}",
        f"{eph['toe']:.0f}",
        f"{sow:.0f}",
        f"{dt_clk:.0f}",
        f"{term_af0*1e9:.3f}",
        f"{term_af1*1e9:.3f}",
        f"{term_af2*1e9:.6f}",
        f"{delta_tr*1e9:.3f}",
        f"{tgd*1e9:.3f}",
    ))

print()
print("Checking: are toc/toe in plausible range for current sow?")
print(f"Current sow = {sow:.0f} s")
for sv in sorted(beph._ephs.keys()):
    if sv[0] not in ('G', 'E'): continue
    eph = beph._ephs[sv]
    dt = _check_week_crossover(sow - eph['toc'])
    if abs(dt) > 7200:  # More than 2 hours stale
        print(f"  WARNING: {sv} toc={eph['toc']:.0f} dt_clk={dt:.0f}s ({dt/3600:.1f}h old)")
