#!/usr/bin/env python3
"""Run a peppar-fix servo smoke test with optional timing fault injection."""

import argparse
import csv
import os
import subprocess
import sys
from collections import Counter
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser(
        description="Run unified servo smoke test with optional fault injection",
    )
    ap.add_argument("--serial", required=True)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ntrip-conf", required=True)
    ap.add_argument("--eph-mount", required=True)
    ap.add_argument("--systems", default="gps,gal")
    ap.add_argument("--position-file", required=True)
    ap.add_argument("--ptp-profile", required=True)
    ap.add_argument("--servo", required=True)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--servo-log", required=True)
    ap.add_argument("--gate-stats", default="")
    ap.add_argument("--delay-log", default="")
    ap.add_argument("--thread-delay-prob-pct", type=float)
    ap.add_argument("--thread-delay-mean-ms", type=float)
    ap.add_argument("--thread-delay-range-ms", type=float)
    ap.add_argument("--sys-delay-prob-pct", type=float)
    ap.add_argument("--sys-delay-mean-ms", type=float)
    ap.add_argument("--sys-delay-range-ms", type=float)
    ap.add_argument("--min-servo-rows", type=int, default=1)
    ap.add_argument("--max-abs-epoch-offset", type=int, default=None)
    ap.add_argument("--max-nonzero-epoch-offsets", type=int, default=None)
    ap.add_argument("--min-gate-consumed", type=int, default=None)
    ap.add_argument("--max-gate-dropped-unmatched", type=int, default=None)
    ap.add_argument("--min-correction-consumed-fresh", type=int, default=None)
    ap.add_argument("--max-correction-deferred-waiting", type=int, default=None)
    ap.add_argument("--max-correction-dropped-stale", type=int, default=None)
    return ap.parse_args()


def add_env(env, key, value):
    if value is not None:
        env[key] = str(value)


def summarize_servo_log(path):
    rows = list(csv.DictReader(open(path)))
    summary = {
        "rows": len(rows),
        "epoch_offsets": Counter(),
    }
    if rows:
        summary["epoch_offsets"] = Counter(
            r["epoch_offset_s"] for r in rows if r.get("epoch_offset_s")
        )
    return summary


def summarize_delay_log(path):
    rows = list(csv.DictReader(open(path)))
    return {
        "rows": len(rows),
        "by_source_kind": Counter((r["source"], r["kind"]) for r in rows),
    }


def summarize_gate_stats(path):
    import json
    with open(path) as f:
        raw = json.load(f)
    if "strict_correlation" not in raw and "correction_freshness" not in raw:
        raw = {"strict_correlation": raw}
    return raw


def main():
    args = parse_args()

    env = os.environ.copy()
    preserve = []

    for key, value in [
        ("THREAD_DELAY_PROB_PCT", args.thread_delay_prob_pct),
        ("THREAD_DELAY_MEAN_MS", args.thread_delay_mean_ms),
        ("THREAD_DELAY_RANGE_MS", args.thread_delay_range_ms),
        ("SYS_DELAY_PROB_PCT", args.sys_delay_prob_pct),
        ("SYS_DELAY_MEAN_MS", args.sys_delay_mean_ms),
        ("SYS_DELAY_RANGE_MS", args.sys_delay_range_ms),
    ]:
        if value is not None:
            add_env(env, key, value)
            preserve.append(key)

    if args.delay_log:
        env["DELAY_LOG_PATH"] = args.delay_log
        preserve.append("DELAY_LOG_PATH")

    servo_log = Path(args.servo_log)
    servo_log.parent.mkdir(parents=True, exist_ok=True)
    if servo_log.exists():
        servo_log.unlink()

    if args.delay_log:
        delay_log = Path(args.delay_log)
        delay_log.parent.mkdir(parents=True, exist_ok=True)
        if delay_log.exists():
            delay_log.unlink()

    if args.gate_stats:
        gate_stats = Path(args.gate_stats)
        gate_stats.parent.mkdir(parents=True, exist_ok=True)
        if gate_stats.exists():
            gate_stats.unlink()

    cmd = [
        "sudo",
        f"--preserve-env={','.join(preserve)}" if preserve else "--preserve-env",
        sys.executable,
        "scripts/peppar_fix_cmd.py",
        "--serial", args.serial,
        "--baud", str(args.baud),
        "--ntrip-conf", args.ntrip_conf,
        "--eph-mount", args.eph_mount,
        "--systems", args.systems,
        "--position-file", args.position_file,
        "--ptp-profile", args.ptp_profile,
        "--servo", args.servo,
        "--servo-log", args.servo_log,
        "--duration", str(args.duration),
    ]
    if args.gate_stats:
        cmd.extend(["--gate-stats", args.gate_stats])

    result = subprocess.run(cmd, env=env)
    if result.returncode != 0:
        return result.returncode

    servo_summary = summarize_servo_log(args.servo_log)
    print(f"servo_rows={servo_summary['rows']}")
    print(f"epoch_offsets={dict(servo_summary['epoch_offsets'])}")
    if servo_summary["rows"] < args.min_servo_rows:
        print(
            f"servo rows {servo_summary['rows']} below minimum {args.min_servo_rows}",
            file=sys.stderr,
        )
        return 2

    offset_counts = {
        int(k): v for k, v in servo_summary["epoch_offsets"].items()
        if k not in ("", None)
    }
    max_abs_offset = max((abs(k) for k in offset_counts), default=0)
    nonzero_offsets = sum(v for k, v in offset_counts.items() if k != 0)
    print(f"max_abs_epoch_offset={max_abs_offset}")
    print(f"nonzero_epoch_offset_rows={nonzero_offsets}")

    if (
        args.max_abs_epoch_offset is not None
        and max_abs_offset > args.max_abs_epoch_offset
    ):
        print(
            f"max abs epoch offset {max_abs_offset} exceeds "
            f"{args.max_abs_epoch_offset}",
            file=sys.stderr,
        )
        return 3

    if (
        args.max_nonzero_epoch_offsets is not None
        and nonzero_offsets > args.max_nonzero_epoch_offsets
    ):
        print(
            f"nonzero epoch offset rows {nonzero_offsets} exceed "
            f"{args.max_nonzero_epoch_offsets}",
            file=sys.stderr,
        )
        return 4

    if args.delay_log:
        delay_summary = summarize_delay_log(args.delay_log)
        print(f"delay_rows={delay_summary['rows']}")
        print(f"delay_sources={dict(delay_summary['by_source_kind'])}")

    if args.gate_stats:
        gate_summary = summarize_gate_stats(args.gate_stats)
        print(f"gate_stats={gate_summary}")
        strict_gate = gate_summary.get("strict_correlation", {})
        correction_gate = gate_summary.get("correction_freshness", {})
        if (
            args.min_gate_consumed is not None
            and strict_gate.get("consumed_correlated", 0) < args.min_gate_consumed
        ):
            print(
                f"gate consumed {strict_gate.get('consumed_correlated', 0)} below "
                f"{args.min_gate_consumed}",
                file=sys.stderr,
            )
            return 5
        if (
            args.max_gate_dropped_unmatched is not None
            and strict_gate.get("dropped_unmatched", 0) > args.max_gate_dropped_unmatched
        ):
            print(
                f"gate dropped_unmatched {strict_gate.get('dropped_unmatched', 0)} exceeds "
                f"{args.max_gate_dropped_unmatched}",
                file=sys.stderr,
            )
            return 6
        if (
            args.min_correction_consumed_fresh is not None
            and correction_gate.get("consumed_fresh", 0) < args.min_correction_consumed_fresh
        ):
            print(
                f"correction gate consumed_fresh {correction_gate.get('consumed_fresh', 0)} below "
                f"{args.min_correction_consumed_fresh}",
                file=sys.stderr,
            )
            return 7
        if (
            args.max_correction_deferred_waiting is not None
            and correction_gate.get("deferred_waiting", 0) > args.max_correction_deferred_waiting
        ):
            print(
                f"correction gate deferred_waiting {correction_gate.get('deferred_waiting', 0)} exceeds "
                f"{args.max_correction_deferred_waiting}",
                file=sys.stderr,
            )
            return 8
        if (
            args.max_correction_dropped_stale is not None
            and correction_gate.get("dropped_stale", 0) > args.max_correction_dropped_stale
        ):
            print(
                f"correction gate dropped_stale {correction_gate.get('dropped_stale', 0)} exceeds "
                f"{args.max_correction_dropped_stale}",
                file=sys.stderr,
            )
            return 9

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
