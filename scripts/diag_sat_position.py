#!/usr/bin/env python3
"""
diag_sat_position.py — Compare broadcast ephemeris satellite positions
against observed pseudoranges from a known antenna position.

For each satellite with both ephemeris and a RAWX observation:
  1. Compute satellite ECEF position from broadcast ephemeris
  2. Compute geometric range from known antenna position
  3. expected_pr = geometric_range + sat_clock * C + tropo
  4. residual = observed_pr - expected_pr (absorbs receiver clock)
  5. After removing mean residual (= receiver clock), per-satellite
     deviations reveal satellite position or clock errors.

A healthy broadcast ephemeris should show per-satellite deviations < 10m
(dominated by L1 code noise ~3m + orbit error ~2m + tropo mismatch).
Deviations > 50m indicate a systematic error in sat_position() or
sat_clock().

Usage:
    python diag_sat_position.py --serial /dev/gnss-top --baud 9600 \
        --known-pos "LAT,LON,ALT" \
        --ntrip-conf ntrip.conf --eph-mount BCEP00BKG0

Runs on a lab host with access to the GNSS receiver and NTRIP.
"""

import argparse
import logging
import math
import sys
import os
import time
from collections import defaultdict
from datetime import datetime, timezone

import numpy as np

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

from broadcast_eph import BroadcastEphemeris, C, OMEGA_E
from solve_pseudorange import lla_to_ecef, timestamp_to_gpstime
from ntrip_client import NtripStream
from solve_dualfreq import F_L1, F_L2, F_L5

log = logging.getLogger(__name__)

# Signal name map (gnssId, sigId) → name
SIG_NAMES = {
    (0, 0): "GPS-L1CA",
    (0, 3): "GPS-L2CL",
    (0, 7): "GPS-L5Q",
    (2, 0): "GAL-E1C",
    (2, 4): "GAL-E5aQ",
}


def saastamoinen_zenith(lat_rad, h_m):
    """Simple Saastamoinen zenith troposphere delay in meters."""
    p = 1013.25 * (1 - 2.2557e-5 * h_m) ** 5.2568
    T = 15.0 - 6.5e-3 * h_m + 273.15
    e = 6.108 * math.exp(17.15 * (T - 273.15) / (T - 38.45)) * 0.5
    return 0.002277 * (p + (1255.0 / T + 0.05) * e)


def elev_angle(user_ecef, sat_ecef):
    """Compute elevation angle in degrees from user to satellite."""
    dx = sat_ecef - user_ecef
    r = np.linalg.norm(user_ecef)
    up = user_ecef / r
    return math.degrees(math.asin(np.dot(dx, up) / np.linalg.norm(dx)))


def tropo_delay(elev_deg, zenith_delay):
    """Map zenith delay to slant using simple 1/sin(el) mapping."""
    if elev_deg < 5:
        return zenith_delay / math.sin(math.radians(5))
    return zenith_delay / math.sin(math.radians(elev_deg))


def run_diagnostic(args):
    logging.basicConfig(level=logging.INFO,
                        format='%(asctime)s %(levelname)s %(message)s')

    # Parse known position
    lat, lon, alt = [float(x) for x in args.known_pos.split(',')]
    user_ecef = np.array(lla_to_ecef(lat, lon, alt))
    lat_rad = math.radians(lat)
    zenith_tropo = saastamoinen_zenith(lat_rad, alt)
    log.info("Antenna position: %.7f, %.7f, %.1f m", lat, lon, alt)
    log.info("ECEF: %.3f, %.3f, %.3f", *user_ecef)
    log.info("Zenith tropo delay: %.3f m", zenith_tropo)

    # Start NTRIP ephemeris stream
    beph = BroadcastEphemeris()
    log.info("Connecting to ephemeris stream: %s/%s", args.ntrip_caster, args.eph_mount)

    import configparser
    ntrip_cfg = configparser.ConfigParser()
    ntrip_cfg.read(args.ntrip_conf)
    section = ntrip_cfg.sections()[0] if ntrip_cfg.sections() else 'ntrip'
    caster = ntrip_cfg.get(section, 'caster', fallback='ntrip.data.gnss.ga.gov.au')
    port = ntrip_cfg.getint(section, 'port', fallback=443)
    user = ntrip_cfg.get(section, 'user', fallback='')
    passwd = ntrip_cfg.get(section, 'password', fallback='')

    import serial as pyserial

    eph_stream = NtripStream(caster, port, args.eph_mount, user, passwd, tls=(port == 443))
    eph_stream.connect()
    log.info("Ephemeris stream connected")

    # Collect broadcast ephemeris for 15 seconds
    log.info("Collecting broadcast ephemeris (15s)...")
    deadline = time.monotonic() + 15
    eph_count = 0
    for msg in eph_stream.messages():
        if time.monotonic() > deadline:
            break
        if hasattr(msg, 'identity'):
            mt = msg.identity.split('(')[0].strip()
            if mt in ('1019', '1042', '1045', '1046'):
                beph.update_from_rtcm(msg)
                eph_count += 1
    eph_stream.disconnect()
    log.info("Collected %d ephemeris messages: %s", eph_count, beph.summary())

    # Open GNSS receiver and get one RAWX epoch
    log.info("Opening receiver %s @ %d...", args.serial, args.baud)
    from pyubx2 import UBXReader
    ser = pyserial.Serial(args.serial, args.baud, timeout=2)
    ubr = UBXReader(ser, protfilter=2)  # UBX only

    log.info("Waiting for RAWX epoch...")
    rawx = None
    deadline = time.monotonic() + 30
    while time.monotonic() < deadline:
        try:
            raw, parsed = ubr.read()
        except Exception:
            continue
        if parsed is None:
            continue
        if hasattr(parsed, 'identity') and parsed.identity == 'RXM-RAWX':
            rawx = parsed
            break
    ser.close()

    if rawx is None:
        log.error("No RAWX received in 30s")
        return 1

    # Parse RAWX epoch time
    rcvTow = rawx.rcvTow
    week = rawx.week
    leapS = rawx.leapS
    numMeas = rawx.numMeas
    # GPS time of reception
    gps_tow = rcvTow

    from datetime import timedelta
    gps_epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)
    t_rx = gps_epoch + timedelta(weeks=week, seconds=gps_tow)
    log.info("RAWX epoch: week=%d tow=%.3f (%s UTC) leapS=%d numMeas=%d",
             week, gps_tow, t_rx.strftime('%H:%M:%S'), leapS, numMeas)

    # Collect all pseudoranges by SV and signal
    raw_pr = defaultdict(dict)  # sv → {sig_name: pr}
    for i in range(1, numMeas + 1):
        i2 = f"{i:02d}"
        gnss_id = getattr(rawx, f'gnssId_{i2}', None)
        sig_id = getattr(rawx, f'sigId_{i2}', None)
        sv_id = getattr(rawx, f'svId_{i2}', None)
        pr = getattr(rawx, f'prMes_{i2}', None)
        pr_valid = getattr(rawx, f'prValid_{i2}', 0)
        cno = getattr(rawx, f'cno_{i2}', 0)
        if gnss_id is None or sig_id is None or not pr_valid or pr is None:
            continue
        if pr < 1e6 or pr > 4e7:
            continue

        sig_name = SIG_NAMES.get((gnss_id, sig_id))
        if sig_name is None:
            continue

        prefix = 'G' if gnss_id == 0 else 'E'
        sv = f"{prefix}{int(sv_id):02d}"
        raw_pr[sv][sig_name] = {'pr': pr, 'cno': cno}

    # Build observation dict: L1 single-freq + IF where dual-freq available
    # IF coefficients: P_IF = α1*P_L1 - α2*P_f2
    ALPHA_L1_L5 = F_L1**2 / (F_L1**2 - F_L5**2)    # ≈ 2.261
    ALPHA_L5    = F_L5**2 / (F_L1**2 - F_L5**2)     # ≈ 1.261
    ALPHA_L1_L2 = F_L1**2 / (F_L1**2 - F_L2**2)     # ≈ 2.546
    ALPHA_L2x   = F_L2**2 / (F_L1**2 - F_L2**2)     # ≈ 1.546

    obs = {}
    for sv, sigs in raw_pr.items():
        # L1 pseudorange
        l1_sig = 'GPS-L1CA' if sv[0] == 'G' else 'GAL-E1C'
        if l1_sig not in sigs:
            continue
        pr_l1 = sigs[l1_sig]['pr']
        cno = sigs[l1_sig]['cno']

        # Try to form IF combination
        pr_if = None
        if_type = None
        if sv[0] == 'G':
            if 'GPS-L5Q' in sigs:
                pr_if = ALPHA_L1_L5 * pr_l1 - ALPHA_L5 * sigs['GPS-L5Q']['pr']
                if_type = 'L1/L5'
            elif 'GPS-L2CL' in sigs:
                pr_if = ALPHA_L1_L2 * pr_l1 - ALPHA_L2x * sigs['GPS-L2CL']['pr']
                if_type = 'L1/L2'
        elif sv[0] == 'E':
            if 'GAL-E5aQ' in sigs:
                pr_if = ALPHA_L1_L5 * pr_l1 - ALPHA_L5 * sigs['GAL-E5aQ']['pr']
                if_type = 'E1/E5a'

        obs[sv] = {'pr': pr_l1, 'sig': l1_sig, 'cno': cno,
                   'pr_if': pr_if, 'if_type': if_type}

    log.info("L1 pseudoranges: %d SVs: %s", len(obs), ' '.join(sorted(obs.keys())))
    n_if = sum(1 for o in obs.values() if o['pr_if'] is not None)
    log.info("IF pseudoranges: %d SVs", n_if)

    if len(obs) < 4:
        log.error("Not enough observations")
        return 1

    # Compute satellite positions and expected pseudoranges
    results = []
    for sv, o in sorted(obs.items()):
        # Compute satellite position at TRANSMISSION time, not reception time.
        # Signal travel time ≈ pseudorange / C ≈ 77 ms.  In that time the
        # satellite moves ~300m along its orbit.  Computing at t_rx instead
        # of t_tx introduces a per-satellite range error that depends on
        # radial velocity (±800 m/s → ±62m range error), creating the
        # observed 50-100m pseudorange residual spread.
        tau_approx = o['pr'] / C  # approximate signal travel time
        t_tx = t_rx - timedelta(seconds=tau_approx)
        pos, clk = beph.sat_position(sv, t_tx)
        if pos is None:
            log.warning("%s: no ephemeris", sv)
            continue

        # Sagnac correction (Earth rotation during signal travel time)
        geo_range_approx = np.linalg.norm(pos - user_ecef)
        tau = geo_range_approx / C
        # Rotate satellite position back by Earth rotation during travel time
        angle = OMEGA_E * tau
        rot = np.array([
            [math.cos(angle), math.sin(angle), 0],
            [-math.sin(angle), math.cos(angle), 0],
            [0, 0, 1]
        ])
        sat_rot = rot @ pos
        geo_range = np.linalg.norm(sat_rot - user_ecef)

        # Elevation angle
        elev = elev_angle(user_ecef, sat_rot)
        if elev < 5:
            continue

        # Troposphere
        tropo = tropo_delay(elev, zenith_tropo)

        # Expected pseudorange (single-frequency L1, includes TGD effect)
        expected_pr = geo_range - clk * C + tropo

        residual = o['pr'] - expected_pr
        results.append({
            'sv': sv, 'pr': o['pr'], 'expected': expected_pr,
            'residual': residual, 'geo_range': geo_range,
            'clk_m': clk * C, 'elev': elev, 'tropo': tropo,
            'cno': o['cno'],
            'sat_ecef': sat_rot,
            'pr_if': o.get('pr_if'),
            'if_type': o.get('if_type'),
        })

    if not results:
        log.error("No valid results")
        return 1

    # Remove mean residual (= receiver clock + constant biases)
    residuals = [r['residual'] for r in results]
    mean_res = sum(residuals) / len(residuals)

    # Also compute IF residuals for dual-freq satellites
    if_results = []
    for r in results:
        if r.get('pr_if') is not None:
            if_res = r['pr_if'] - r['expected']
            if_results.append({**r, 'if_residual': if_res})

    log.info("")
    log.info("=" * 80)
    log.info("SATELLITE POSITION DIAGNOSTIC (L1 single-frequency)")
    log.info("=" * 80)
    log.info("Mean residual (≈ receiver clock): %.3f m (%.3f ns)",
             mean_res, mean_res / C * 1e9)
    log.info("")

    # Per-system means
    gps_res = [r['residual'] for r in results if r['sv'][0] == 'G']
    gal_res = [r['residual'] for r in results if r['sv'][0] == 'E']
    if gps_res:
        gps_mean = sum(gps_res) / len(gps_res)
        log.info("GPS mean residual: %.3f m (%d SVs)", gps_mean, len(gps_res))
    if gal_res:
        gal_mean = sum(gal_res) / len(gal_res)
        log.info("GAL mean residual: %.3f m (%d SVs)", gal_mean, len(gal_res))
    if gps_res and gal_res:
        log.info("ISB (GAL-GPS): %.3f m",
                 sum(gal_res)/len(gal_res) - sum(gps_res)/len(gps_res))

    log.info("")
    log.info("%-5s %8s %8s %10s %8s %7s %6s  %s" % (
        "SV", "elev", "C/N0", "residual", "dev", "clk_m", "tropo", "sat_pos_km"))
    log.info("-" * 80)
    for r in sorted(results, key=lambda x: x['sv']):
        dev = r['residual'] - mean_res
        log.info("%-5s %7.1f° %6.1f  %+10.1f %+8.1f %+7.0f %6.1f  (%.0f, %.0f, %.0f)",
                 r['sv'], r['elev'], r['cno'],
                 r['residual'], dev,
                 r['clk_m'], r['tropo'],
                 r['sat_ecef'][0]/1e3, r['sat_ecef'][1]/1e3, r['sat_ecef'][2]/1e3)

    devs = [r['residual'] - mean_res for r in results]
    log.info("-" * 80)
    log.info("L1 deviation stats: min=%.1f max=%.1f spread=%.1f RMS=%.1f m",
             min(devs), max(devs), max(devs)-min(devs),
             math.sqrt(sum(d*d for d in devs) / len(devs)))

    # IF combination residuals — ionosphere removed
    if if_results:
        if_mean = sum(r['if_residual'] for r in if_results) / len(if_results)
        log.info("")
        log.info("=" * 80)
        log.info("IF COMBINATION RESIDUALS (iono-free)")
        log.info("=" * 80)
        log.info("Mean IF residual (≈ receiver clock): %.3f m", if_mean)
        log.info("")
        log.info("%-5s %8s %7s %12s %8s %s" % (
            "SV", "elev", "C/N0", "IF_residual", "IF_dev", "IF_type"))
        log.info("-" * 70)
        for r in sorted(if_results, key=lambda x: x['sv']):
            if_dev = r['if_residual'] - if_mean
            log.info("%-5s %7.1f° %5.1f  %+12.1f %+8.1f  %s",
                     r['sv'], r['elev'], r['cno'],
                     r['if_residual'], if_dev, r.get('if_type', '?'))
        if_devs = [r['if_residual'] - if_mean for r in if_results]
        log.info("-" * 70)
        log.info("IF deviation stats: min=%.1f max=%.1f spread=%.1f RMS=%.1f m",
                 min(if_devs), max(if_devs), max(if_devs)-min(if_devs),
                 math.sqrt(sum(d*d for d in if_devs) / len(if_devs)))

        if_spread = max(if_devs) - min(if_devs)
        if if_spread > 50:
            log.warning("LARGE IF spread (%.0fm) — satellite position or "
                        "clock computation error (NOT ionosphere)", if_spread)
        elif if_spread > 15:
            log.info("Moderate IF spread (%.0fm) — code noise + orbit error", if_spread)
        else:
            log.info("IF spread (%.0fm) looks healthy", if_spread)

    # Also print raw satellite positions for manual cross-check
    log.info("")
    log.info("Satellite ECEF positions for cross-reference:")
    log.info("%-5s %15s %15s %15s %12s" % ("SV", "X (m)", "Y (m)", "Z (m)", "clk (s)"))
    for r in sorted(results, key=lambda x: x['sv']):
        pos, clk = beph.sat_position(r['sv'], t_rx)
        log.info("%-5s %15.3f %15.3f %15.3f %12.9f",
                 r['sv'], pos[0], pos[1], pos[2], clk)

    return 0


if __name__ == '__main__':
    ap = argparse.ArgumentParser(description="Satellite position diagnostic")
    ap.add_argument("--serial", required=True, help="GNSS serial port")
    ap.add_argument("--baud", type=int, default=9600)
    ap.add_argument("--known-pos", required=True,
                    help="Known antenna position as lat,lon,alt")
    ap.add_argument("--ntrip-conf", required=True, help="NTRIP config file")
    ap.add_argument("--eph-mount", default="BCEP00BKG0",
                    help="NTRIP mount point for broadcast ephemeris")
    ap.add_argument("--ntrip-caster", default=None,
                    help="Override caster from ntrip.conf")
    sys.exit(run_diagnostic(ap.parse_args()))
