#!/usr/bin/env python3
"""Run a peppar-fix servo smoke test with optional timing fault injection."""

import argparse
import csv
import os
import signal
import subprocess
import sys
import time
from collections import Counter
from pathlib import Path


def parse_args():
    ap = argparse.ArgumentParser(
        description="Run unified servo smoke test with optional fault injection",
    )
    ap.add_argument("--serial", required=True)
    ap.add_argument("--receiver", default=None)
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--ntrip-conf", required=True)
    ap.add_argument("--eph-mount", required=True)
    ap.add_argument("--systems", default="gps,gal")
    ap.add_argument("--known-pos", default=None)
    ap.add_argument("--ptp-profile", required=True)
    ap.add_argument("--servo", required=True)
    ap.add_argument("--duration", type=int, default=20)
    ap.add_argument("--servo-log", required=True)
    ap.add_argument("--gate-stats", default="")
    ap.add_argument("--delay-log", default="")
    ap.add_argument("--thread-delay-prob-pct", type=float)
    ap.add_argument("--thread-delay-mean-ms", type=float)
    ap.add_argument("--thread-delay-range-ms", type=float)
    ap.add_argument("--thread-delay-sources", default=None,
                    help="Comma-separated source substrings for per-thread delays")
    ap.add_argument("--sys-delay-prob-pct", type=float)
    ap.add_argument("--sys-delay-mean-ms", type=float)
    ap.add_argument("--sys-delay-range-ms", type=float)
    ap.add_argument("--sys-delay-sources", default=None,
                    help="Comma-separated source substrings for correlated SYS delays")
    ap.add_argument("--min-servo-rows", type=int, default=1)
    ap.add_argument("--max-abs-epoch-offset", type=int, default=None)
    ap.add_argument("--max-nonzero-epoch-offsets", type=int, default=None)
    ap.add_argument("--min-gate-consumed", type=int, default=None)
    ap.add_argument("--max-gate-deferred-waiting", type=int, default=None)
    ap.add_argument("--max-gate-dropped-outside-window", type=int, default=None)
    ap.add_argument("--max-gate-dropped-unmatched", type=int, default=None)
    ap.add_argument("--min-correction-consumed-fresh", type=int, default=None)
    ap.add_argument("--max-correction-deferred-waiting", type=int, default=None)
    ap.add_argument("--max-correction-dropped-stale", type=int, default=None)
    ap.add_argument("--allow-holdover", action="store_true",
                    help="Do not fail the run if holdover is entered")
    ap.add_argument("--max-holdover-entered", type=int, default=None,
                    help="Maximum allowed holdover-entered count")
    ap.add_argument("--mute-gnss-at-s", type=float, default=None,
                    help="Send SIGUSR1 to mute GNSS delivery at this many seconds into the run")
    ap.add_argument("--unmute-gnss-at-s", type=float, default=None,
                    help="Send SIGUSR2 to unmute GNSS delivery at this many seconds into the run")
    return ap.parse_args()


def add_env(env, key, value):
    if value is not None:
        env[key] = str(value)


def summarize_servo_log(path):
    rows = list(csv.DictReader(open(path)))
    scalar_fields = [
        "obs_confidence",
        "obs_estimator_residual_s",
        "pps_confidence",
        "pps_estimator_residual_s",
        "match_confidence",
        "broadcast_confidence",
        "ssr_confidence",
        "qerr_var_ratio",
    ]
    summary = {
        "rows": len(rows),
        "epoch_offsets": Counter(),
        "scalars": {},
    }
    if rows:
        summary["epoch_offsets"] = Counter(
            r["epoch_offset_s"] for r in rows if r.get("epoch_offset_s")
        )
        for field in scalar_fields:
            values = []
            for row in rows:
                value = row.get(field)
                if value in ("", None):
                    continue
                values.append(float(value))
            if values:
                values.sort()
                summary["scalars"][field] = {
                    "min": values[0],
                    "median": values[len(values) // 2],
                    "max": values[-1],
                }
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
    if args.thread_delay_sources:
        env["THREAD_DELAY_SOURCES"] = args.thread_delay_sources
        preserve.append("THREAD_DELAY_SOURCES")
    if args.sys_delay_sources:
        env["SYS_DELAY_SOURCES"] = args.sys_delay_sources
        preserve.append("SYS_DELAY_SOURCES")

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
        "scripts/peppar_fix_engine.py",
        "--serial", args.serial,
        "--baud", str(args.baud),
        "--ntrip-conf", args.ntrip_conf,
        "--eph-mount", args.eph_mount,
        "--systems", args.systems,
        "--ptp-profile", args.ptp_profile,
        "--servo", args.servo,
        "--servo-log", args.servo_log,
        "--duration", str(args.duration),
    ]
    if args.receiver:
        cmd.extend(["--receiver", args.receiver])
    if args.known_pos:
        cmd.extend(["--known-pos", args.known_pos])
    if args.gate_stats:
        cmd.extend(["--gate-stats", args.gate_stats])

    pid_file = ""
    if args.mute_gnss_at_s is not None or args.unmute_gnss_at_s is not None:
        pid_file = str(Path(args.servo_log).with_suffix(".pid"))
        cmd.extend(["--pid-file", pid_file])
        preserve.append("PYTHONPATH")

    proc = subprocess.Popen(cmd, env=env)
    start = time.monotonic()
    sent_mute = False
    sent_unmute = False
    engine_pid = None
    while True:
        rc = proc.poll()
        now = time.monotonic()
        if engine_pid is None and pid_file and Path(pid_file).exists():
            try:
                engine_pid = int(Path(pid_file).read_text().strip())
            except (OSError, ValueError):
                engine_pid = None
        if (
            engine_pid is not None and
            args.mute_gnss_at_s is not None and
            not sent_mute and
            now - start >= args.mute_gnss_at_s
        ):
            subprocess.run(["sudo", "kill", "-USR1", str(engine_pid)], check=True)
            sent_mute = True
            print(f"sent_signal=SIGUSR1 pid={engine_pid} at_s={now - start:.3f}")
        if (
            engine_pid is not None and
            args.unmute_gnss_at_s is not None and
            not sent_unmute and
            now - start >= args.unmute_gnss_at_s
        ):
            subprocess.run(["sudo", "kill", "-USR2", str(engine_pid)], check=True)
            sent_unmute = True
            print(f"sent_signal=SIGUSR2 pid={engine_pid} at_s={now - start:.3f}")
        if rc is not None:
            if rc != 0:
                return rc
            break
        time.sleep(0.1)

    servo_summary = summarize_servo_log(args.servo_log)
    print(f"servo_rows={servo_summary['rows']}")
    print(f"epoch_offsets={dict(servo_summary['epoch_offsets'])}")
    if servo_summary["scalars"]:
        print(f"servo_scalars={servo_summary['scalars']}")
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
        holdover = gate_summary.get("holdover", {})
        print(f"holdover={holdover}")
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
            args.max_gate_deferred_waiting is not None
            and strict_gate.get("deferred_waiting", 0) > args.max_gate_deferred_waiting
        ):
            print(
                f"gate deferred_waiting {strict_gate.get('deferred_waiting', 0)} exceeds "
                f"{args.max_gate_deferred_waiting}",
                file=sys.stderr,
            )
            return 6
        if (
            args.max_gate_dropped_outside_window is not None
            and strict_gate.get("dropped_outside_window", 0) > args.max_gate_dropped_outside_window
        ):
            print(
                f"gate dropped_outside_window {strict_gate.get('dropped_outside_window', 0)} exceeds "
                f"{args.max_gate_dropped_outside_window}",
                file=sys.stderr,
            )
            return 7
        if (
            args.max_gate_dropped_unmatched is not None
            and strict_gate.get("dropped_unmatched", 0) > args.max_gate_dropped_unmatched
        ):
            print(
                f"gate dropped_unmatched {strict_gate.get('dropped_unmatched', 0)} exceeds "
                f"{args.max_gate_dropped_unmatched}",
                file=sys.stderr,
            )
            return 8
        if (
            args.min_correction_consumed_fresh is not None
            and correction_gate.get("consumed_fresh", 0) < args.min_correction_consumed_fresh
        ):
            print(
                f"correction gate consumed_fresh {correction_gate.get('consumed_fresh', 0)} below "
                f"{args.min_correction_consumed_fresh}",
                file=sys.stderr,
            )
            return 9
        if (
            args.max_correction_deferred_waiting is not None
            and correction_gate.get("deferred_waiting", 0) > args.max_correction_deferred_waiting
        ):
            print(
                f"correction gate deferred_waiting {correction_gate.get('deferred_waiting', 0)} exceeds "
                f"{args.max_correction_deferred_waiting}",
                file=sys.stderr,
            )
            return 10
        if (
            args.max_correction_dropped_stale is not None
            and correction_gate.get("dropped_stale", 0) > args.max_correction_dropped_stale
        ):
            print(
                f"correction gate dropped_stale {correction_gate.get('dropped_stale', 0)} exceeds "
                f"{args.max_correction_dropped_stale}",
                file=sys.stderr,
            )
            return 11
        holdover_entered = holdover.get("entered", 0)
        if not args.allow_holdover and holdover_entered > 0:
            print(
                f"holdover entered {holdover_entered} times unexpectedly",
                file=sys.stderr,
            )
            return 12
        if (
            args.max_holdover_entered is not None
            and holdover_entered > args.max_holdover_entered
        ):
            print(
                f"holdover entered {holdover_entered} times exceeds "
                f"{args.max_holdover_entered}",
                file=sys.stderr,
            )
            return 13

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
