#!/usr/bin/env python3
"""Probe GNSS delivery cadence and RXM-RAWX lag at the device boundary.

This is intended for platform characterization, especially for kernel GNSS
char devices like /dev/gnss0 where delivery may be bursty.
"""

import argparse
import logging
import statistics
import time
from collections import Counter
from datetime import datetime, timedelta, timezone

log = logging.getLogger("gnss-lag-probe")


def format_stats(values, digits=3):
    """Return min/median/max text for a non-empty numeric list."""
    if not values:
        return "n/a"
    return (
        f"min={min(values):.{digits}f} "
        f"med={statistics.median(values):.{digits}f} "
        f"max={max(values):.{digits}f}"
    )


def main():
    ap = argparse.ArgumentParser(
        description="Measure GNSS packet cadence and RXM-RAWX arrival lag"
    )
    ap.add_argument("device", help="GNSS device, e.g. /dev/gnss0 or /dev/gnss-top")
    ap.add_argument("--baud", type=int, default=115200,
                    help="Baud rate for serial devices (default: 115200)")
    ap.add_argument("--rawx-count", type=int, default=20,
                    help="Stop after this many RAWX epochs (default: 20)")
    ap.add_argument("--duration", type=float, default=30.0,
                    help="Stop after this many seconds if rawx-count not reached")
    ap.add_argument("-v", "--verbose", action="store_true",
                    help="Print every RAWX epoch as it arrives")
    args = ap.parse_args()

    logging.basicConfig(
        level=logging.INFO,
        format="%(asctime)s %(levelname)s %(message)s",
    )

    try:
        from pyubx2 import UBXReader
    except ImportError:
        print("pyubx2 is required; install project dependencies first")
        return 2

    from peppar_fix.gnss_stream import open_gnss

    stream, device_type = open_gnss(args.device, args.baud)
    ubr = UBXReader(stream, protfilter=2)
    gps_epoch = datetime(1980, 1, 6, tzinfo=timezone.utc)

    start_mono = time.monotonic()
    prev_rawx_recv_mono = None
    prev_packet_ts = None

    rawx_rows = []
    packet_intervals = []
    packet_burst_sizes = Counter()
    current_burst_packets = 0

    try:
        while len(rawx_rows) < args.rawx_count and (time.monotonic() - start_mono) < args.duration:
            raw, parsed = ubr.read()
            if parsed is None:
                continue

            now_mono = time.monotonic()
            now_utc = datetime.now(timezone.utc)
            packet_ts = None
            if hasattr(stream, "pop_packet_timestamp"):
                packet_ts = stream.pop_packet_timestamp()
            if packet_ts is None:
                packet_ts = now_mono

            if prev_packet_ts is None or (packet_ts - prev_packet_ts) > 0.200:
                if current_burst_packets:
                    packet_burst_sizes[current_burst_packets] += 1
                current_burst_packets = 1
            else:
                current_burst_packets += 1

            if prev_packet_ts is not None:
                packet_intervals.append(packet_ts - prev_packet_ts)
            prev_packet_ts = packet_ts

            if parsed.identity != "RXM-RAWX":
                continue

            gps_time = gps_epoch + timedelta(
                weeks=parsed.week,
                seconds=parsed.rcvTow,
            )
            leap_s = int(getattr(parsed, "leapS", 18) or 18)
            rawx_utc = gps_time - timedelta(seconds=leap_s)
            packet_utc = now_utc - timedelta(seconds=(now_mono - packet_ts))
            packet_lag_s = (packet_utc - rawx_utc).total_seconds()
            parse_lag_s = (now_utc - rawx_utc).total_seconds()
            rawx_recv_dt_s = None
            if prev_rawx_recv_mono is not None:
                rawx_recv_dt_s = packet_ts - prev_rawx_recv_mono
            prev_rawx_recv_mono = packet_ts

            row = {
                "gps_time": gps_time,
                "rawx_utc": rawx_utc,
                "packet_lag_s": packet_lag_s,
                "parse_lag_s": parse_lag_s,
                "packet_age_s": now_mono - packet_ts,
                "rawx_recv_dt_s": rawx_recv_dt_s,
                "num_meas": getattr(parsed, "numMeas", 0),
            }
            rawx_rows.append(row)

            if args.verbose:
                dt_txt = "n/a" if rawx_recv_dt_s is None else f"{rawx_recv_dt_s:.3f}"
                print(
                    f"{len(rawx_rows) - 1:02d} "
                    f"rawx_utc={rawx_utc.strftime('%H:%M:%S.%f')[:-3]} "
                    f"lag={packet_lag_s:.3f}s "
                    f"recv_dt={dt_txt}s "
                    f"packet_age={row['packet_age_s']:.3f}s "
                    f"n={row['num_meas']}"
                )
    finally:
        if current_burst_packets:
            packet_burst_sizes[current_burst_packets] += 1
        stream.close()

    print("GNSS Lag Probe")
    print(f"  Device: {args.device}")
    print(f"  Type:   {device_type}")
    print(f"  RAWX:   {len(rawx_rows)} epochs")

    if not rawx_rows:
        print("  No RXM-RAWX epochs observed")
        return 1

    packet_lags = [row["packet_lag_s"] for row in rawx_rows]
    parse_lags = [row["parse_lag_s"] for row in rawx_rows]
    packet_ages = [row["packet_age_s"] for row in rawx_rows]
    rawx_recv_dts = [
        row["rawx_recv_dt_s"] for row in rawx_rows
        if row["rawx_recv_dt_s"] is not None
    ]

    print(f"  Packet lag: {format_stats(packet_lags)} s")
    print(f"  Parse lag:  {format_stats(parse_lags)} s")
    print(f"  Packet age: {format_stats(packet_ages)} s")
    if rawx_recv_dts:
        print(f"  RAWX dt:    {format_stats(rawx_recv_dts)} s")
    if packet_intervals:
        print(f"  Packet dt:  {format_stats(packet_intervals)} s")

    common_bursts = ", ".join(
        f"{size}pkt:{count}"
        for size, count in packet_burst_sizes.most_common(6)
    )
    if common_bursts:
        print(f"  Burst histogram: {common_bursts}")

    first = rawx_rows[0]
    last = rawx_rows[-1]
    print(
        f"  First RAWX: {first['rawx_utc'].strftime('%F %T.%f')[:-3]} "
        f"lag={first['packet_lag_s']:.3f}s"
    )
    print(
        f"  Last RAWX:  {last['rawx_utc'].strftime('%F %T.%f')[:-3]} "
        f"lag={last['packet_lag_s']:.3f}s"
    )

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
