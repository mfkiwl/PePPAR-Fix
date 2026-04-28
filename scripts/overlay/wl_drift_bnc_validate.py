#!/usr/bin/env python3
"""BNC-grounded wl_drift validator.

For each engine `[WL_DRIFT]` event in one or more host logs, look
for a BKG BNC `RESET AMB` event on the same satellite within
±window seconds.  Engine events with a matching BNC reset are
labelled `TP` (true-positive — independent engine on the same RF
also reacted to a real disturbance).  Engine events without a BNC
match are labelled `FP` (BNC-disagreement — engine over-detected).

Why BNC is a reasonable gold-standard reference here:

  - BNC is an independent PPP engine consuming the same antenna
    via the GUS daisy splitter, so it sees the same RF environment
    plus the same sky (atmospheric / multipath / SSR-driven)
    perturbations.
  - BNC's per-SV ambiguity reset is the closest analog to the
    engine's WL-drift demotion.  Both decisions are: "this SV's
    ambiguity is no longer trustworthy, restart its float."
  - Yesterday's MadHat-vs-BNC comparison
    (`project_bnc_drift_comparison_finding_20260427`) showed BNC
    reacts ~60 s earlier and to fewer SVs than the engine.  The
    engine's "extra" SVs in a wave are the FP candidates this
    tool labels.

Caveats — the tool's output is BNC-disagreement, not absolute
proof:

  - BNC F9T-PTP runs TIM 2.20 (L2-only); engines run TIM 2.25
    (full L1/L2/L5).  Different frequency coverage means BNC may
    legitimately miss events that touched only L5.
  - BNC's threshold and detection logic differ from the engine's
    0.25-cyc instantaneous rule.
  - The reverse case (BNC reset, engine didn't) is also
    informative — emitted as an `MISSED_BY_ENGINE` advisory.

Both signals together give the empirical input for choosing
between cohort-median, hysteresis, elev-mask and CN0-gate
mitigations downstream (Bravo's I-133306).

Inputs supported:

  - Engine `[WL_DRIFT]` lines (any number of host logs)
  - BNC `*.ppp` log with `RESET AMB <COMBO> <SV>` lines

Output formats:

  - text (default): per-host summary table + per-SV breakdown +
    optional per-event detail
  - json: full per-event labels, suitable for downstream
    classifier consumption (Bravo's wl_drift_classify.py)

Usage:
    wl_drift_bnc_validate.py --bnc bnc.ppp \\
        --labels MadHat,clkPoC3,TimeHat \\
        --engine madhat.log clkpoc3.log timehat.log

    wl_drift_bnc_validate.py --bnc bnc.ppp --window 60 \\
        --engine *.log --format json > labels.json

Stdlib only.
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Log parsers ──────────────────────────────────────────────────────────── #

# Engine [WL_DRIFT] line, with extras parsed for downstream features:
#   2026-04-27 22:19:19,062 WARNING [WL_DRIFT] E12 drift=+0.317cyc > ±0.25
#   (n=15, window=30ep): flushing MW, demoting to FLOATING, gate@elev=49.0°
_DRIFT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[WL_DRIFT\]\s+(\w\d+)\s+drift=([+-]?\d+\.\d+)cyc"
    r".*?\(n=(\d+),\s*window=(\d+)ep\)"
    r".*?gate@elev=([0-9.]+|\?)"
)

# BNC RESET AMB line:
#   2026-04-28_13:26:02.000 RESET AMB  lIF E12
_BNC_RESET_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ _T](\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"\s+RESET\s+AMB\s+\S*\s+(\w\d+)"
)


def parse_engine_drift(path: str, tz_offset_h: float = 0.0) -> list[dict]:
    """Return [{'ts', 'sv', 'drift_cyc', 'n_samples', 'window_ep',
                'elev_deg'}, ...].

    Engine timestamps are emitted in the lab host's local time.  The
    lab is in US Central (CDT = UTC-5 in summer).  Pass
    `tz_offset_h=-5` to convert engine timestamps to UTC for matching
    against BNC's UTC timestamps.
    """
    offset_s = tz_offset_h * 3600.0
    out = []
    with open(path) as f:
        for line in f:
            m = _DRIFT_RE.search(line)
            if not m:
                continue
            d, t, sv, dcyc, n, win, elev = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp() - offset_s
            out.append({
                'ts': ts,
                'sv': sv,
                'drift_cyc': float(dcyc),
                'n_samples': int(n),
                'window_ep': int(win),
                'elev_deg': float(elev) if elev != '?' else None,
            })
    return out


def parse_bnc_reset(path: str) -> list[dict]:
    """Return [{'ts', 'sv'}, ...] in time order."""
    out = []
    with open(path) as f:
        for line in f:
            m = _BNC_RESET_RE.search(line)
            if not m:
                continue
            d, t, sv = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp()
            out.append({'ts': ts, 'sv': sv})
    out.sort(key=lambda r: r['ts'])
    return out


# ── Matching ─────────────────────────────────────────────────────────────── #

def _index_by_sv(events: list[dict]) -> dict[str, list[float]]:
    """Sorted timestamp lists per SV, for fast nearest-time lookup."""
    by_sv: dict[str, list[float]] = {}
    for e in events:
        by_sv.setdefault(e['sv'], []).append(e['ts'])
    for lst in by_sv.values():
        lst.sort()
    return by_sv


def _nearest_within(sorted_ts: list[float], target: float,
                    window: float) -> float | None:
    """Return signed Δt to the nearest entry in window, or None.

    Signed: +Δ if BNC came after, -Δ if BNC came first.  Yesterday's
    finding had BNC reacting ~60 s earlier than the engine, so we
    expect mostly negative Δ on TP matches.
    """
    if not sorted_ts:
        return None
    # Linear scan is fine — typically <1000 entries per SV per night.
    best = None
    for t in sorted_ts:
        dt = t - target
        if dt < -window:
            continue
        if dt > window:
            break
        if best is None or abs(dt) < abs(best):
            best = dt
    return best


def label_engine_events(
    engine_events: list[dict],
    bnc_events: list[dict],
    window_s: float,
) -> list[dict]:
    """Annotate each engine event with TP/FP/OOS label + matched Δt.

    OOS ('out-of-scope') applies when BNC tracks no satellites in
    the engine event's GNSS system anywhere in the log — for example,
    today's BNC F9T-PTP / TIM 2.20 logs zero GPS RESETs across 14 h,
    so all engine GPS events are unvalidatable.  OOS events are kept
    in the per-event listing for downstream consumers but excluded
    from the TP / FP / FP-rate aggregate to avoid distortion.
    """
    # System letter set from BNC events (G/E/C/R/J ...).
    bnc_systems = {sv[0] for sv in {e['sv'] for e in bnc_events}}
    bnc_by_sv = _index_by_sv(bnc_events)
    out = []
    for e in engine_events:
        sv_sys = e['sv'][0]
        labelled = dict(e)
        if sv_sys not in bnc_systems:
            labelled['bnc_match'] = False
            labelled['bnc_match_dt_s'] = None
            labelled['label'] = 'OOS'
            out.append(labelled)
            continue
        dt = _nearest_within(bnc_by_sv.get(e['sv'], []), e['ts'], window_s)
        labelled['bnc_match'] = dt is not None
        labelled['bnc_match_dt_s'] = dt
        labelled['label'] = 'TP' if dt is not None else 'FP'
        out.append(labelled)
    return out


def find_engine_misses(
    engine_events: list[dict],
    bnc_events: list[dict],
    window_s: float,
) -> list[dict]:
    """BNC RESETs with no engine [WL_DRIFT] within window on same SV.

    Asymmetry note: this looks at one host's engine log against
    BNC.  A BNC reset that's missed on host A might be caught on
    host B; the caller should aggregate across hosts to avoid
    double-counting "misses".
    """
    eng_by_sv = _index_by_sv(engine_events)
    out = []
    for r in bnc_events:
        dt = _nearest_within(eng_by_sv.get(r['sv'], []), r['ts'], window_s)
        if dt is None:
            out.append({'ts': r['ts'], 'sv': r['sv']})
    return out


# ── Aggregation ──────────────────────────────────────────────────────────── #

def aggregate_per_sv(labelled: list[dict]) -> dict[str, dict]:
    """Aggregate per SV.  OOS events are counted separately; FP rate
    is over in-scope events only."""
    by_sv: dict[str, dict] = {}
    for e in labelled:
        sv = e['sv']
        rec = by_sv.setdefault(sv, {'n': 0, 'tp': 0, 'fp': 0, 'oos': 0,
                                     'mean_drift': 0.0,
                                     'mean_elev': None,
                                     '_elev_sum': 0.0,
                                     '_elev_n': 0})
        rec['n'] += 1
        if e['label'] == 'TP':
            rec['tp'] += 1
        elif e['label'] == 'FP':
            rec['fp'] += 1
        else:
            rec['oos'] += 1
        rec['mean_drift'] += abs(e['drift_cyc'])
        if e['elev_deg'] is not None:
            rec['_elev_sum'] += e['elev_deg']
            rec['_elev_n'] += 1
    for rec in by_sv.values():
        if rec['n']:
            rec['mean_drift'] /= rec['n']
        in_scope = rec['tp'] + rec['fp']
        rec['fp_rate'] = rec['fp'] / in_scope if in_scope else None
        if rec['_elev_n']:
            rec['mean_elev'] = rec['_elev_sum'] / rec['_elev_n']
        del rec['_elev_sum']
        del rec['_elev_n']
    return by_sv


def aggregate_per_hour(labelled: list[dict],
                       t0: float, t1: float) -> list[dict]:
    """One-hour bins from t0 to t1.  FP rate is over in-scope events only."""
    if t1 <= t0 or not labelled:
        return []
    n_bins = int((t1 - t0) // 3600) + 1
    bins = [{'t_start': t0 + i * 3600,
             'n': 0, 'tp': 0, 'fp': 0, 'oos': 0}
            for i in range(n_bins)]
    for e in labelled:
        idx = int((e['ts'] - t0) // 3600)
        if 0 <= idx < n_bins:
            bins[idx]['n'] += 1
            if e['label'] == 'TP':
                bins[idx]['tp'] += 1
            elif e['label'] == 'FP':
                bins[idx]['fp'] += 1
            else:
                bins[idx]['oos'] += 1
    for b in bins:
        in_scope = b['tp'] + b['fp']
        b['fp_rate'] = b['fp'] / in_scope if in_scope else None
        b['t_start_iso'] = datetime.fromtimestamp(
            b['t_start'], timezone.utc).isoformat()
    return bins


# ── Reporting ────────────────────────────────────────────────────────────── #

def _format_dt(ts: float) -> str:
    return datetime.fromtimestamp(ts, timezone.utc).strftime("%H:%M:%S")


def render_text(report: dict, show_events: int = 0) -> str:
    out = []
    out.append("# wl_drift BNC-grounded validation")
    out.append(f"# generated: {datetime.now(timezone.utc).isoformat()}")
    out.append(f"# match window: ±{report['window_s']:.0f}s")
    out.append(f"# bnc resets:   {report['n_bnc']}")
    out.append("")

    out.append("## Per-host summary")
    out.append("  (FP% over in-scope only; OOS = engine SV outside "
               "BNC's tracked systems)")
    out.append(f"  {'host':>10}  {'events':>7}  {'in-sc':>6}  "
               f"{'TP':>5}  {'FP':>5}  {'FP%':>6}  {'OOS':>5}  "
               f"{'BNC missed':>11}")
    for host, h in report['hosts'].items():
        fp_pct = (f"{h['fp_rate']*100:>5.1f}%"
                  if h['fp_rate'] is not None else "  n/a")
        out.append(
            f"  {host:>10}  {h['n']:>7d}  {h['in_scope']:>6d}  "
            f"{h['tp']:>5d}  {h['fp']:>5d}  {fp_pct:>6}  "
            f"{h['oos']:>5d}  {h['n_bnc_missed']:>11d}")
    out.append("")

    out.append("## Top-FP SVs across all hosts (worst RX-side suspects)")
    flat: dict[str, dict] = {}
    for host, h in report['hosts'].items():
        for sv, r in h['per_sv'].items():
            agg = flat.setdefault(sv, {'n': 0, 'fp': 0, 'tp': 0,
                                       'mean_drift': 0.0,
                                       '_drift_sum': 0.0,
                                       '_drift_n': 0,
                                       'hosts': set()})
            agg['n'] += r['n']
            agg['fp'] += r['fp']
            agg['tp'] += r['tp']
            agg['_drift_sum'] += r['mean_drift'] * r['n']
            agg['_drift_n'] += r['n']
            agg['hosts'].add(host)
    for sv, r in flat.items():
        in_scope = r['tp'] + r['fp']
        r['fp_rate'] = r['fp'] / in_scope if in_scope else None
        r['mean_drift'] = (r['_drift_sum'] / r['_drift_n']
                           if r['_drift_n'] else 0.0)
    # Rank in-scope SVs by FP count (worst absolute over-detector first).
    in_scope_only = [(sv, r) for sv, r in flat.items()
                     if r['fp_rate'] is not None]
    ranked = sorted(in_scope_only,
                    key=lambda kv: (-kv[1]['fp'], -(kv[1]['fp_rate'] or 0)))
    out.append(f"  {'sv':>4}  {'n':>5}  {'TP':>4}  {'FP':>5}  "
               f"{'FP%':>6}  {'mean|drift|':>11}  hosts")
    for sv, r in ranked[:15]:
        if r['fp'] == 0:
            break
        hosts = ",".join(sorted(r['hosts']))
        out.append(
            f"  {sv:>4}  {r['n']:>5d}  {r['tp']:>4d}  {r['fp']:>5d}  "
            f"{r['fp_rate']*100:>5.1f}%  {r['mean_drift']:>11.3f}  "
            f"{hosts}")
    out.append("")

    out.append("## Per-hour FP rate (in-scope only)")
    for host, h in report['hosts'].items():
        if not h['per_hour']:
            continue
        out.append(f"  {host}:")
        for b in h['per_hour']:
            in_scope = b['tp'] + b['fp']
            if in_scope == 0:
                continue
            label = b['t_start_iso'][11:16]
            fp_pct = (f"{b['fp_rate']*100:>4.1f}%"
                      if b['fp_rate'] is not None else "  n/a")
            out.append(
                f"    {label}Z  in-scope={in_scope:>3d}  "
                f"FP={b['fp']:>3d} ({fp_pct})")
    out.append("")

    if show_events > 0:
        out.append(f"## Sample labelled events (first {show_events})")
        for host, h in report['hosts'].items():
            out.append(f"  {host}:")
            for e in h['events'][:show_events]:
                dt_str = (f"BNC{e['bnc_match_dt_s']:+.0f}s"
                          if e['bnc_match'] else "—")
                elev = (f"{e['elev_deg']:.0f}°"
                        if e['elev_deg'] is not None else "?")
                out.append(
                    f"    {_format_dt(e['ts'])}  {e['sv']:>3}  "
                    f"drift={e['drift_cyc']:+.3f}cyc  "
                    f"elev={elev:>4}  [{e['label']}] {dt_str}")
    return "\n".join(out) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────── #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bnc", required=True, help="BNC .ppp log path")
    ap.add_argument("--engine", nargs='+', required=True,
                    help="engine log file(s)")
    ap.add_argument("--labels", default=None,
                    help="host labels, comma-separated "
                         "(default: log filename stems)")
    ap.add_argument("--window", type=float, default=30.0,
                    help="match window ±s (default 30)")
    ap.add_argument("--engine-tz-offset-hours", type=float, default=-5.0,
                    help="engine local-time offset from UTC in hours "
                         "(default -5 = US Central CDT). BNC is always "
                         "interpreted as UTC.")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    ap.add_argument("--show-events", type=int, default=0,
                    help="text mode: dump first N labelled events per host")
    args = ap.parse_args()

    paths = [Path(p) for p in args.engine]
    if args.labels is None:
        labels = [p.stem for p in paths]
    else:
        labels = [s.strip() for s in args.labels.split(",")]
        if len(labels) != len(paths):
            print(f"--labels count ({len(labels)}) must match "
                  f"--engine count ({len(paths)})", file=sys.stderr)
            return 2

    bnc_events = parse_bnc_reset(args.bnc)
    if not bnc_events:
        print(f"warning: no RESET AMB lines parsed from {args.bnc}",
              file=sys.stderr)

    hosts: dict[str, dict] = {}
    all_eng_combined: list[dict] = []
    t_min = float('inf')
    t_max = float('-inf')
    for lbl, p in zip(labels, paths):
        eng_events = parse_engine_drift(
            str(p), tz_offset_h=args.engine_tz_offset_hours)
        labelled = label_engine_events(eng_events, bnc_events, args.window)
        misses = find_engine_misses(eng_events, bnc_events, args.window)
        if labelled:
            t_min = min(t_min, min(e['ts'] for e in labelled))
            t_max = max(t_max, max(e['ts'] for e in labelled))
        n_tp = sum(1 for e in labelled if e['label'] == 'TP')
        n_fp = sum(1 for e in labelled if e['label'] == 'FP')
        n_oos = sum(1 for e in labelled if e['label'] == 'OOS')
        in_scope = n_tp + n_fp
        hosts[lbl] = {
            'n': len(labelled),
            'tp': n_tp,
            'fp': n_fp,
            'oos': n_oos,
            'in_scope': in_scope,
            'fp_rate': (n_fp / in_scope) if in_scope else None,
            'n_bnc_missed': len(misses),
            'events': labelled,
            'misses': misses,
        }
        all_eng_combined.extend(labelled)

    # Per-SV and per-hour aggregation, computed after t-range known.
    if t_min == float('inf'):
        t_min = t_max = 0.0
    for lbl, h in hosts.items():
        h['per_sv'] = aggregate_per_sv(h['events'])
        h['per_hour'] = aggregate_per_hour(h['events'], t_min, t_max)

    report = {
        'window_s': args.window,
        'n_bnc': len(bnc_events),
        'span_iso': (
            datetime.fromtimestamp(t_min, timezone.utc).isoformat()
            if t_min else None,
            datetime.fromtimestamp(t_max, timezone.utc).isoformat()
            if t_max else None,
        ),
        'hosts': hosts,
    }

    if args.format == "json":
        # Strip the verbose 'misses' detail for top-level JSON brevity;
        # caller can derive from events if needed.
        for h in report['hosts'].values():
            h['misses'] = [{'ts': m['ts'], 'sv': m['sv']}
                           for m in h['misses']]
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report, show_events=args.show_events))
    return 0


if __name__ == "__main__":
    sys.exit(main())
