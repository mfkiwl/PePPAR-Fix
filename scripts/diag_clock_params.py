#!/usr/bin/env python3
"""Quick diagnostic: dump broadcast clock parameters and TGD for all SVs."""
import sys, time, os, math
sys.path.insert(0, os.path.join(os.path.dirname(__file__)))

from broadcast_eph import BroadcastEphemeris, C
from ntrip_client import NtripStream
from datetime import datetime, timezone, timedelta
import configparser

beph = BroadcastEphemeris()
cfg = configparser.ConfigParser()
cfg.read('ntrip.conf')
sec = cfg.sections()[0]
caster = cfg.get(sec, 'caster')
port = cfg.getint(sec, 'port', fallback=443)
user = cfg.get(sec, 'user', fallback='')
pw = cfg.get(sec, 'password', fallback='')
stream = NtripStream(caster, port, 'BCEP00BKG0', user, pw, tls=True)
stream.connect()
deadline = time.monotonic() + 12
for msg in stream.messages():
    if time.monotonic() > deadline:
        break
    if hasattr(msg, 'identity'):
        mt = msg.identity.split('(')[0].strip()
        if mt in ('1019', '1045', '1046'):
            beph.update_from_rtcm(msg)
stream.disconnect()

print(f"Summary: {beph.summary()}")
print()

t_now = datetime.now(timezone.utc)

# Header
hdr = f"{'SV':5s} {'af0(ns)':>12s} {'af1(ns/s)':>12s} {'TGD(ns)':>10s} {'clk(ns)':>12s} {'clk_m':>10s} {'TGD_m':>8s}"
print(hdr)
print("-" * len(hdr))

for sv in sorted(beph._ephs.keys()):
    if sv[0] not in ('G', 'E'):
        continue
    eph = beph._ephs[sv]
    pos, clk = beph.sat_position(sv, t_now)
    if pos is None:
        continue
    tgd = eph.get('tgd', 0.0)
    af0 = eph.get('af0', 0.0)
    af1 = eph.get('af1', 0.0)
    # clk already has TGD subtracted (our code does af0+af1*dt+af2*dt^2+dtr-TGD)
    # Show: clk (with TGD subtracted), and TGD separately
    print(f"{sv:5s} {af0*1e9:+12.3f} {af1*1e9:+12.6f} {tgd*1e9:+10.3f} "
          f"{clk*1e9:+12.1f} {clk*C:+10.0f} {tgd*C:+8.1f}")

# Also show what clk would be WITHOUT TGD subtraction
print()
print("What if we DON'T subtract TGD (clock = IF reference)?")
hdr2 = f"{'SV':5s} {'clk_noTGD(ns)':>14s} {'clk_noTGD_m':>12s} {'diff_m':>8s}"
print(hdr2)
print("-" * len(hdr2))
for sv in sorted(beph._ephs.keys()):
    if sv[0] not in ('G', 'E'):
        continue
    eph = beph._ephs[sv]
    pos, clk = beph.sat_position(sv, t_now)
    if pos is None:
        continue
    tgd = eph.get('tgd', 0.0)
    clk_no_tgd = clk + tgd  # undo the subtraction
    print(f"{sv:5s} {clk_no_tgd*1e9:+14.1f} {clk_no_tgd*C:+12.0f} {tgd*C:+8.1f}")
