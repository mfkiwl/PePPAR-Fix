#!/usr/bin/env python3
"""Sensitivity sweep for the seed-error / no-SSR pull-direction test.

For each (offset, run_idx) in the matrix:
  1. SSH-kill engines on all 4 lab hosts
  2. SSH-launch with --seed-pos-offset OFFSET,0,0 --no-ssr (+ standard config)
  3. Sleep RUN_DURATION_S
  4. SSH-collect position trajectories from each host's log
  5. Score: did filter pull east-back-toward-truth or away?

Output (incremental, written after each run):
  /tmp/seed_sensitivity_<TAG>.csv — one row per (run, host) with offset,
  start_lon, end_lon, delta_lon_m, direction_pulled, run_duration_s.

Designed to survive interruption — partial CSV remains.

Usage:
  ./diag_seed_sensitivity.py [--matrix M] [--duration S] [--dry-run]
"""

from __future__ import annotations

import argparse
import csv
import math
import os
import re
import subprocess
import sys
import time
from datetime import datetime
from pathlib import Path


HOSTS = [
    {
        "name": "TimeHat",
        "ssh": "TimeHat",
        "antenna_ref": "UFO1",
        "extra": ["--servo", "/dev/ptp_i226", "--receiver", "f9t-l2",
                   "--antex-path", "/home/bob/peppar-fix/ngs20.atx",
                   "--receiver-antenna", "SFESPK6618H     NONE"],
    },
    {
        "name": "clkPoC3",
        "ssh": "clkPoC3",
        "antenna_ref": "UFO1",
        "extra": ["--no-do",
                   "--antex-path", "/home/bob/peppar-fix/ngs20.atx",
                   "--receiver-antenna", "SFESPK6618H     NONE"],
    },
    {
        "name": "MadHat",
        "ssh": "MadHat.local",
        "antenna_ref": "UFO1",
        "extra": ["--servo", "/dev/ptp_i226",
                   "--antex-path", "/home/bob/peppar-fix/ngs20.atx",
                   "--receiver-antenna", "SFESPK6618H     NONE"],
    },
    {
        "name": "ptpmon",
        "ssh": "ptpmon",
        "antenna_ref": "PATCH3",
        "extra": ["--no-do"],
    },
]

def _common_flags(known_pos: str, with_ssr: bool, ssr_conf: str | None,
                  ssr_bias_conf: str | None,
                  no_primary_biases: bool,
                  no_ssr_code_bias: bool,
                  no_ssr_phase_bias: bool,
                  systems: str, no_wl_only: bool) -> list:
    flags = [
        "--systems", systems,
        "--known-pos", known_pos,
        "--clock-model", "random_walk",
        "--sigma-phi-if", "1.0",
        "--phase-windup", "--gmf",
        "--peer-bus", "udp-multicast",
        "--peer-site-ref", "DuPage",
    ]
    if not no_wl_only:
        flags.append("--wl-only")
    if not with_ssr:
        flags.append("--no-ssr")
        # --no-ssr no longer nulls ssr_bias_mount, so a bias mount paired
        # with broadcast orbit/clock is the broadcast-O/C corner of the
        # 4-cell 2x2.  Let it flow through.
    elif ssr_conf:
        flags.extend(["--ssr-ntrip-conf", ssr_conf])
    if ssr_bias_conf:
        flags.extend(["--ssr-bias-ntrip-conf", ssr_bias_conf])
    if no_primary_biases:
        flags.append("--no-primary-biases")
    if no_ssr_code_bias:
        flags.append("--no-ssr-code-bias")
    if no_ssr_phase_bias:
        flags.append("--no-ssr-phase-bias")
    return flags


# Default matrix: (offset_magnitude_m, repeats, alternate_signs)
DEFAULT_MATRIX = [
    (30.0, 1),
    (10.0, 2),
    (3.0, 3),
    (1.0, 3),
    (0.3, 3),
    (0.1, 3),
]


_ANT_POS_RE = re.compile(
    r"\[AntPosEst (\d+)\] positionσ=([\d.]+)m pos=\(([\d.]+), ([-\d.]+), ([\d.]+)\)"
)


def ssh(host_ssh: str, cmd: str, timeout: float = 15.0) -> tuple[int, str]:
    """Run a remote command. Returns (returncode, combined-output)."""
    try:
        r = subprocess.run(
            ["ssh", "-o", "BatchMode=yes", host_ssh, cmd],
            capture_output=True, text=True, timeout=timeout, check=False,
        )
        return r.returncode, (r.stdout or "") + (r.stderr or "")
    except subprocess.TimeoutExpired:
        return 124, "timeout"


def kill_all() -> None:
    print("  Killing engines...", flush=True)
    procs = []
    for h in HOSTS:
        p = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", h["ssh"],
             "sudo pkill -9 -f peppar_fix_engine.py 2>/dev/null; true"],
        )
        procs.append(p)
    for p in procs:
        try:
            p.wait(timeout=10)
        except subprocess.TimeoutExpired:
            p.kill()
    time.sleep(3)


def launch_all(tag: str, offset_e_m: float, known_pos: str,
               with_ssr: bool, ssr_conf: str | None,
               ssr_bias_conf: str | None,
               no_primary_biases: bool,
               no_ssr_code_bias: bool,
               no_ssr_phase_bias: bool,
               systems: str, no_wl_only: bool,
               hosts: list) -> None:
    print(f"  Launching {tag} with offset E={offset_e_m:+.3f}m...", flush=True)
    procs = []
    for h in hosts:
        cmd_parts = [
            "cd ~/peppar-fix && "
            "sudo PYTHONPATH=/home/bob/peppar-fix:/home/bob/peppar-fix/scripts "
            "nohup ./venv/bin/python scripts/peppar_fix_engine.py",
        ]
        cmd_parts.extend(_common_flags(known_pos, with_ssr, ssr_conf,
                                       ssr_bias_conf,
                                       no_primary_biases,
                                       no_ssr_code_bias,
                                       no_ssr_phase_bias,
                                       systems, no_wl_only))
        cmd_parts.extend(h["extra"])
        # Use = form so a negative offset doesn't look like a flag to argparse
        cmd_parts.extend([
            f"--seed-pos-offset={offset_e_m},0,0",
            "--peer-antenna-ref", h["antenna_ref"],
            "--servo-log", f"data/{tag}-{h['name'].lower()}-servo.csv",
            "--ticc-log",  f"data/{tag}-{h['name'].lower()}-ticc.csv",
            "--slip-log",  f"data/{tag}-{h['name'].lower()}-slips.csv",
        ])
        cmd_parts.append(f"> data/{tag}-{h['name'].lower()}.log 2>&1 & disown")
        # Quote each arg appropriately for ssh: rebuild as a single shell-line.
        shell_line = " ".join(_shell_quote(x) for x in cmd_parts)
        p = subprocess.Popen(
            ["ssh", "-o", "BatchMode=yes", h["ssh"], shell_line],
        )
        procs.append(p)
    for p in procs:
        try:
            p.wait(timeout=15)
        except subprocess.TimeoutExpired:
            p.kill()


def _shell_quote(s: str) -> str:
    # Don't quote our pre-built shell construct that uses redirection;
    # detect it by leading 'cd' or trailing &.
    if s.startswith("cd ") or s.endswith("disown") or "PYTHONPATH=" in s:
        return s
    if any(ch in s for ch in " '\"$&|;<>(){}*"):
        # Use single-quote with embedded-single-quote escape.
        return "'" + s.replace("'", "'\\''") + "'"
    return s


def collect_trajectory(host: dict, tag: str) -> list[dict]:
    """SSH-grep the host's log for AntPosEst lines."""
    cmd = f"grep '\\[AntPosEst' ~/peppar-fix/data/{tag}-{host['name'].lower()}.log"
    rc, out = ssh(host["ssh"], cmd, timeout=20)
    rows = []
    for line in out.splitlines():
        m = _ANT_POS_RE.search(line)
        if not m:
            continue
        rows.append({
            "epoch": int(m.group(1)),
            "sigma": float(m.group(2)),
            "lat":   float(m.group(3)),
            "lon":   float(m.group(4)),
            "alt":   float(m.group(5)),
        })
    return rows


def score_run(traj: list[dict], offset_e_m: float, deg_to_m_lon: float,
              truth_lon_deg: float) -> dict:
    """Did the filter end closer to truth than its seed?

    Seed lon = truth_lon + (offset_e_m / DEG_TO_M_LON).  Compare the
    last-observed lon's distance-from-truth against the seed's
    distance-from-truth (which equals |offset_e_m|).

    Returns first_epoch, last_epoch, first_lon, last_lon, last_dist_m
    (signed: positive=east of truth, negative=west), direction (TOWARD
    / AWAY / NO_DATA).

    NOTE: this metric uses last_lon vs truth, not last vs first_observed.
    By the time the first AntPosEst log line emits (epoch ~10), the
    Kalman gain has already absorbed most of any large seed offset, so
    delta_lon (last − first) measures within-run drift, not seed pull.
    """
    if not traj:
        return {"first_epoch": None, "last_epoch": None,
                "first_lon": None, "last_lon": None,
                "last_dist_m": None, "direction": "NO_DATA"}
    first = traj[0]
    last = traj[-1]
    last_dist_m = (last["lon"] - truth_lon_deg) * deg_to_m_lon  # signed E
    seed_dist_m = offset_e_m  # seed was offset_e_m east of truth
    # TOWARD = filter ended closer to truth than seed.  AWAY = ended
    # farther.  Use absolute distances to truth.
    moved = abs(seed_dist_m) - abs(last_dist_m)
    if abs(offset_e_m) < 1e-9:
        direction = "NONE"  # control run, no offset
    elif moved > 0:
        direction = "TOWARD"
    else:
        direction = "AWAY"
    return {
        "first_epoch": first["epoch"],
        "last_epoch": last["epoch"],
        "first_lon": first["lon"],
        "last_lon": last["lon"],
        "last_dist_m": last_dist_m,
        "direction": direction,
    }


def write_csv_header(path: Path) -> None:
    if path.exists():
        return
    with path.open("w", newline="") as f:
        w = csv.writer(f)
        w.writerow([
            "tag", "run_idx", "offset_m", "host",
            "first_epoch", "last_epoch", "first_lon", "last_lon",
            "last_dist_m", "direction",
        ])


def append_csv_row(path: Path, row: list) -> None:
    with path.open("a", newline="") as f:
        csv.writer(f).writerow(row)


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--duration", type=int, default=300,
                    help="seconds per run (default: 300 = 5 min)")
    ap.add_argument("--out", type=Path,
                    default=Path("/tmp/seed_sensitivity.csv"),
                    help="CSV output path (appended)")
    ap.add_argument("--tag-prefix", default="day0425b",
                    help="lab tag prefix; per-run tag suffix is "
                         "'-r<NN>-e<+/-X>m' (e.g. day0425b-r01-e+30m)")
    ap.add_argument("--known-pos", required=True,
                    help="Receiver-truth seed position 'LAT,LON,ALT' "
                         "passed to engine --known-pos.  Antenna "
                         "coords aren't committed; pass on CLI or "
                         "from env var SEED_SENS_KNOWN_POS.")
    ap.add_argument("--with-ssr", action="store_true",
                    help="Enable SSR corrections (default: --no-ssr).  "
                         "Use to compare SSR-on vs SSR-off attractor.")
    ap.add_argument("--ssr-conf", default=None,
                    help="With --with-ssr, override default SSR ntrip-conf "
                         "(e.g. ntrip-cas.conf).  Engine reads from "
                         "~/peppar-fix/ unless absolute.")
    ap.add_argument("--ssr-bias-conf", default=None,
                    help="With --with-ssr, separate ntrip-conf for SSR code "
                         "+ phase biases (e.g. ntrip-whu.conf paired with "
                         "CNES orbit/clock).  Same path resolution as "
                         "--ssr-conf.")
    ap.add_argument("--no-primary-biases", action="store_true",
                    help="Drop bias messages from primary SSR mount; "
                         "pair with --ssr-bias-conf for clean "
                         "'orbit/clock from A, biases from B'.")
    ap.add_argument("--no-ssr-code-bias", action="store_true",
                    help="Drop ALL SSR code biases (both mounts).  "
                         "Isolates phase-bias contribution.")
    ap.add_argument("--no-ssr-phase-bias", action="store_true",
                    help="Drop ALL SSR phase biases (both mounts).  "
                         "Isolates code-bias contribution.")
    ap.add_argument("--systems", default="gal",
                    help="Comma-separated constellations passed to engine "
                         "--systems (default: gal).  Examples: 'gps,gal', "
                         "'gal,bds', 'gps,gal,bds'.")
    ap.add_argument("--exclude-hosts", default="",
                    help="Comma-separated host names to skip (e.g. "
                         "'clkPoC3' to leave it free for parallel work).  "
                         "Matched against HOSTS[*]['name'].")
    ap.add_argument("--no-wl-only", action="store_true",
                    help="Drop --wl-only from engine flags, enabling "
                         "narrow-lane (NL) integer resolution on top of "
                         "WL.  Default keeps --wl-only.")
    ap.add_argument("--quick", action="store_true",
                    help="Diagnostic mode: 2 runs (+30m, -30m), single "
                         "magnitude.  ~10 min wall-clock instead of 82.  "
                         "For comparing attractors across SSR providers.")
    ap.add_argument("--dry-run", action="store_true",
                    help="print plan, don't execute")
    args = ap.parse_args()
    # Compute lat0 for longitude→metres conversion from --known-pos.
    try:
        lat0_deg = float(args.known_pos.split(",")[0])
    except (ValueError, IndexError):
        print(f"--known-pos must be 'LAT,LON,ALT'; got {args.known_pos!r}",
              file=sys.stderr)
        return 2
    deg_to_m_lon = 111320.0 * math.cos(math.radians(lat0_deg))
    try:
        truth_lon_deg = float(args.known_pos.split(",")[1])
    except (ValueError, IndexError):
        print(f"--known-pos must be 'LAT,LON,ALT'; got {args.known_pos!r}",
              file=sys.stderr)
        return 2

    # NOTE: ptpmon is on a different antenna (PATCH3) — its truth lon
    # is NOT truth_lon_deg.  Per-host truth would need separate config;
    # for now, ptpmon's TOWARD/AWAY classification is meaningful only
    # relative to the UFO1 truth and should be reinterpreted post-hoc.

    # Build run sequence: alternating signs within each offset's repeats.
    matrix = [(30.0, 2)] if args.quick else DEFAULT_MATRIX
    runs = []  # list of (run_idx, offset_e_m)
    idx = 0
    for mag, repeats in matrix:
        for r in range(repeats):
            sign = 1 if r % 2 == 0 else -1
            idx += 1
            runs.append((idx, mag * sign))

    print(f"Plan: {len(runs)} runs × {args.duration}s = "
          f"{len(runs) * (args.duration + 30) / 60:.1f} min wall-clock.\n"
          f"Output CSV: {args.out}\n", flush=True)
    for i, off in runs:
        print(f"  run {i:02d}: offset = {off:+g} m E", flush=True)
    if args.dry_run:
        return 0

    excluded = {h.strip() for h in (args.exclude_hosts or "").split(",")
                if h.strip()}
    active_hosts = [h for h in HOSTS if h["name"] not in excluded]
    if excluded:
        print(f"Excluded hosts: {sorted(excluded)}; active: "
              f"{[h['name'] for h in active_hosts]}", flush=True)

    write_csv_header(args.out)

    for run_idx, offset_e in runs:
        tag = f"{args.tag_prefix}-r{run_idx:02d}-e{offset_e:+g}m"
        # Sanitize tag for filenames
        tag = tag.replace("+", "p").replace(".", "_")
        print(f"\n=== Run {run_idx} of {len(runs)}: "
              f"offset = {offset_e:+.3f}m E, tag={tag}, "
              f"start={datetime.now().strftime('%H:%M:%S')} ===", flush=True)

        kill_all()
        launch_all(tag, offset_e, args.known_pos, args.with_ssr,
                   args.ssr_conf, args.ssr_bias_conf,
                   args.no_primary_biases,
                   args.no_ssr_code_bias,
                   args.no_ssr_phase_bias,
                   args.systems, args.no_wl_only,
                   active_hosts)
        print(f"  Sleeping {args.duration}s ({args.duration//60} min)...",
              flush=True)
        time.sleep(args.duration)

        print("  Collecting trajectories + scoring...", flush=True)
        for host in active_hosts:
            traj = collect_trajectory(host, tag)
            score = score_run(traj, offset_e, deg_to_m_lon, truth_lon_deg)
            row = [
                tag, run_idx, offset_e, host["name"],
                score["first_epoch"], score["last_epoch"],
                score["first_lon"], score["last_lon"],
                score["last_dist_m"], score["direction"],
            ]
            append_csv_row(args.out, row)
            sigma_str = (f"σ {traj[0]['sigma']:.2f}→{traj[-1]['sigma']:.3f}"
                         if traj else "no data")
            dist_str = (f"end {score['last_dist_m']:+.2f}m E of truth"
                        if score['last_dist_m'] is not None else "")
            print(f"  {host['name']:8s}: {score['direction']:8s}  "
                  f"{sigma_str}  {dist_str}", flush=True)

    # Final cleanup — leave engines running on last config? kill them.
    print("\nMatrix complete. Stopping engines.", flush=True)
    kill_all()
    print(f"Results: {args.out}", flush=True)
    return 0


if __name__ == "__main__":
    sys.exit(main())
