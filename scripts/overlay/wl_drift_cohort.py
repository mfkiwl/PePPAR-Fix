#!/usr/bin/env python3
"""Cross-host WL-drift cohort analysis for shared-antenna overnight runs.

Built for the 04-27/04-28 overnight (I-030109-main) where MadHat,
clkPoC3 and TimeHat all sit on UFO1 via a splitter.  The hypothesis
under test is which side of the chain causes today's wl_drift cycling:

    (a) sky/AC-side  — atmosphere, multipath, code-bias from SSR
    (b) RX-side      — per-F9T noise, receiver-tracker artifacts
    (c) inconclusive — WL never stabilises on any host

Same antenna means epoch-by-epoch correlation is a direct
discriminator: shared-cause artefacts must show up on every host
within seconds of each other; per-receiver artefacts will not.

Two complementary signals are computed per host pair:

  1. **WL-fixed-count Pearson r**.  Each host's WL fixed count is
     extracted from `[AntPosEst]` lines (cadence: every 10 epochs,
     ≈ every 10 s at 1 Hz raw data) and aligned to a common 10 s
     grid by log timestamp.  r ≳ 0.7 ⇒ shared cause; r ≲ 0.3 ⇒
     independent.

  2. **[WL_DRIFT] event coincidence rate vs chance**.  Drift events
     for the same SV on different hosts are paired if their
     timestamps fall within a configurable Δt (default 30 s).  The
     observed pair rate is compared to the rate expected under
     independence (Poisson, λ = N₁·N₂·Δt / T).  Coincidence ratio
     > 3× chance ⇒ shared cause; ≈ 1× ⇒ independent.

The two signals point at the same conclusion in clean cases.  When
they disagree, trust the event coincidence — it ignores the slow
NL-amb component of WL fixed count and looks only at the cycling
events themselves.

Verdict triage at the bottom of the report:

  - high r AND high coincidence              → (a) sky/AC-side
  - low r AND low coincidence                → (b) RX-side
  - mean WL fixed < 4 across the board       → (c) inconclusive
  - signals disagree                         → see "Notes" + decide
                                               by hand

Usage:
    wl_drift_cohort.py madhat.log clkpoc3.log timehat.log
    wl_drift_cohort.py --labels MadHat,clkPoC3,TimeHat *.log
    wl_drift_cohort.py --window 60 *.log    # widen coincidence Δt
    wl_drift_cohort.py --format json *.log > cohort.json

Stdlib only.
"""
from __future__ import annotations

import argparse
import itertools
import json
import math
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path

# ── Log parsers ──────────────────────────────────────────────────────────── #

# Engine [AntPosEst] line (cadence: every 10 epochs).  We parse only
# the fields we need: timestamp, epoch number, WL fixed count.
#
#   [AntPosEst 4830] positionσ=0.042m pos=(41.83, -87.61, 178.2) n=14
#   amb=12 WL: 9/14 fixed NL: 0 fixed ...
_ANTPOS_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[AntPosEst\s+(\d+)\]"
    r".*?WL:\s*(\d+)/(\d+)\s+fixed"
)

# Engine [WL_DRIFT] event (cadence: as many as get triggered).
#
#   [WL_DRIFT] G05 drift=+0.273cyc > ±0.25 (n=8, window=30ep): ...
_DRIFT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[WL_DRIFT\]\s+(\w\d+)\s+drift=([+-]?\d+\.\d+)cyc"
)


def parse_log(path: str) -> dict:
    """Pull both [AntPosEst] WL series and [WL_DRIFT] events from one log.

    Returns:
        {
          'antpos': [(ts_unix, epoch_n, wl_fixed, wl_total), ...],
          'drift':  [(ts_unix, sv, drift_cyc), ...],
        }
    """
    antpos: list[tuple[float, int, int, int]] = []
    drift: list[tuple[float, str, float]] = []
    with open(path) as f:
        for line in f:
            m = _ANTPOS_RE.search(line)
            if m:
                d, t, ep, fix, tot = m.groups()
                ts = datetime.fromisoformat(f"{d}T{t}").timestamp()
                antpos.append((ts, int(ep), int(fix), int(tot)))
                continue
            m = _DRIFT_RE.search(line)
            if m:
                d, t, sv, dcyc = m.groups()
                ts = datetime.fromisoformat(f"{d}T{t}").timestamp()
                drift.append((ts, sv, float(dcyc)))
    return {'antpos': antpos, 'drift': drift}


# ── Time-series alignment ────────────────────────────────────────────────── #

def to_grid(
    series: list[tuple[float, int, int, int]],
    t0: float,
    t1: float,
    dt: float,
) -> list[float | None]:
    """Resample WL fixed counts onto a uniform time grid.

    Strategy: nearest-neighbour within ±dt/2, else None.  Engine
    cadence is the same as `dt` by construction, so this is mostly
    a fill-aligned lookup.
    """
    if not series:
        return []
    n_bins = int(math.floor((t1 - t0) / dt)) + 1
    grid: list[float | None] = [None] * n_bins
    half = dt / 2.0
    j = 0
    for i in range(n_bins):
        target = t0 + i * dt
        # Advance j to the closest sample at or after target - half.
        while j < len(series) and series[j][0] < target - half:
            j += 1
        if j < len(series) and series[j][0] <= target + half:
            grid[i] = float(series[j][2])  # WL fixed count
    return grid


def pair_present(a: list[float | None],
                 b: list[float | None]) -> tuple[list[float], list[float]]:
    """Return aligned (xs, ys) keeping only bins where both are present."""
    xs, ys = [], []
    for x, y in zip(a, b):
        if x is not None and y is not None:
            xs.append(x)
            ys.append(y)
    return xs, ys


def pearson(xs: list[float], ys: list[float]) -> float | None:
    n = len(xs)
    if n < 3:
        return None
    mx = sum(xs) / n
    my = sum(ys) / n
    sx = sum((x - mx) ** 2 for x in xs)
    sy = sum((y - my) ** 2 for y in ys)
    sxy = sum((x - mx) * (y - my) for x, y in zip(xs, ys))
    if sx <= 0 or sy <= 0:
        return None
    return sxy / math.sqrt(sx * sy)


# ── Drift-event coincidence ──────────────────────────────────────────────── #

def coincidence(
    a: list[tuple[float, str, float]],
    b: list[tuple[float, str, float]],
    window_s: float,
    t_total: float,
) -> dict:
    """Count [WL_DRIFT] events on the same SV within ±window_s.

    Compares to Poisson chance rate λ = N_a · N_b · (2·window_s) / T,
    treating each host's events as independent uniform-in-time
    point processes (per-SV).  Reported as `obs / chance` ratio.
    """
    n_a, n_b = len(a), len(b)
    if n_a == 0 or n_b == 0 or t_total <= 0:
        return {'pairs': 0, 'expected': 0.0, 'ratio': None,
                'n_a': n_a, 'n_b': n_b}

    # Index b's events by SV for O(N_a · k) lookup, k = events/SV.
    b_by_sv: dict[str, list[tuple[float, float]]] = {}
    for ts, sv, dcyc in b:
        b_by_sv.setdefault(sv, []).append((ts, dcyc))
    for lst in b_by_sv.values():
        lst.sort()

    pairs = 0
    matched_examples: list[tuple[float, str, float, float]] = []
    for ts_a, sv_a, dcyc_a in a:
        for ts_b, dcyc_b in b_by_sv.get(sv_a, ()):
            if abs(ts_a - ts_b) <= window_s:
                pairs += 1
                if len(matched_examples) < 5:
                    matched_examples.append(
                        (ts_a, sv_a, dcyc_a, dcyc_b))
                break  # Don't multi-count the same a-event

    # Expected pair count under independence, summed over SVs.
    # λ_sv = n_a_sv · n_b_sv · 2W / T.  Exact for thin events.
    a_by_sv: dict[str, int] = {}
    for _, sv, _ in a:
        a_by_sv[sv] = a_by_sv.get(sv, 0) + 1
    expected = 0.0
    for sv, n_a_sv in a_by_sv.items():
        n_b_sv = len(b_by_sv.get(sv, ()))
        expected += n_a_sv * n_b_sv * (2 * window_s) / t_total

    ratio = pairs / expected if expected > 0 else None
    return {
        'pairs': pairs,
        'expected': expected,
        'ratio': ratio,
        'n_a': n_a,
        'n_b': n_b,
        'examples': matched_examples,
    }


# ── Verdict triage ───────────────────────────────────────────────────────── #

def _classify_pair(r: float | None, ratio: float | None,
                   pairs_obs: int,
                   r_hi: float, r_lo: float,
                   coinc_hi: float, coinc_lo: float,
                   min_pairs_for_corr: int = 3) -> str:
    """Per-pair label: 'corr', 'indep', 'mixed', or 'unknown'.

    Coincidence is the primary signal — it measures the cycling
    events directly.  r(WL_fixed) is the sanity check: it should
    point in the same direction.  Disagreement between the two is
    flagged by the caller as a 'mixed' class with a note.

    Small-sample guard: a single chance coincidence (1 / 0.15 ≈ 6×)
    can clear the ratio threshold without being statistically
    meaningful.  Require ≥ min_pairs_for_corr observed pairs before
    awarding 'corr'.  Below that threshold, downgrade to 'mixed'.
    """
    if r is None or ratio is None:
        return 'unknown'
    if ratio >= coinc_hi and r >= 0.0:
        return 'corr' if pairs_obs >= min_pairs_for_corr else 'mixed'
    if ratio <= coinc_lo and r <= r_lo:
        return 'indep'
    return 'mixed'


def verdict(
    labels: list[str],
    pair_classes: dict[tuple[str, str], str],
    mean_wl_fixed: float,
) -> tuple[str, str]:
    """Synthesize per-pair classifications into a single verdict.

    Three hosts on the same antenna give three pairs; if all three are
    `corr`, the cause is shared (a).  If all three are `indep`, the
    cause is per-host (b).  If exactly one host's pairs are all `indep`
    while the others are `corr`, that host is the rogue — RX-side
    problem on it specifically; the rest are sky/AC-side together.
    Anything else is genuinely mixed and must be read from the
    per-pair breakdown.
    """
    if mean_wl_fixed < 4.0:
        return ("(c) inconclusive",
                f"mean WL fixed = {mean_wl_fixed:.1f} across hosts — "
                "WL never stabilised; correlation result not meaningful.")
    classes = list(pair_classes.values())
    if not classes or all(c == 'unknown' for c in classes):
        return ("(c) inconclusive",
                "insufficient data on host pairs (no overlap "
                "or no drift events).")
    if all(c == 'corr' for c in classes):
        return ("(a) sky/AC-side",
                "all host pairs show correlated WL series + "
                "high drift-event coincidence.  Shared cause "
                "confirmed.")
    if all(c == 'indep' for c in classes):
        return ("(b) RX-side",
                "all host pairs show uncorrelated WL series + "
                "chance-level drift-event coincidence.  Cause is "
                "per-host (receiver-tracker artefact).")
    # Rogue-host detection: a host whose pairs are all `indep` while
    # the rest are `corr` together.
    by_host: dict[str, list[str]] = {h: [] for h in labels}
    for (a, b), cls in pair_classes.items():
        by_host[a].append(cls)
        by_host[b].append(cls)
    rogues = [h for h, cs in by_host.items()
              if cs and all(c == 'indep' for c in cs)]
    others = [h for h in labels if h not in rogues]
    if (len(rogues) == 1 and len(others) >= 2
            and all(pair_classes.get(tuple(sorted((a, b))), 'mixed')
                    == 'corr'
                    for a, b in itertools.combinations(others, 2))):
        rogue = rogues[0]
        return ("(a)+(b) split — rogue host",
                f"{rogue} pairs are all independent; remaining "
                f"hosts ({', '.join(others)}) are correlated.  "
                f"Sky/AC cause shared by the cluster; "
                f"{rogue} has a separate RX-side problem.")
    return ("(c) mixed — see per-pair breakdown",
            "no clean cohort split.  Some pairs correlated, "
            "others independent without a single rogue host.  "
            "Read per-pair table; consider per-SV breakdown "
            "or longer window.")


# ── Reporting ────────────────────────────────────────────────────────────── #

def render_text(report: dict) -> str:
    out = []
    out.append(f"# WL-drift cohort report")
    out.append(f"# generated: {datetime.now(timezone.utc).isoformat()}")
    out.append(f"# coincidence window: ±{report['window_s']:.0f}s")
    out.append(f"# observation span:   {report['t_total']/3600:.2f}h")
    out.append("")

    out.append("## Per-host stats")
    for label, st in report['hosts'].items():
        out.append(
            f"  {label:>10}: WL_mean={st['wl_mean']:.1f} "
            f"WL_max={st['wl_max']} drift_events={st['n_drift']} "
            f"(span {st['span_h']:.2f}h)")
    out.append("")

    out.append("## Per-pair correlation")
    out.append(
        f"  {'pair':>22}  "
        f"{'r(WL_fixed)':>12}  {'pairs':>6}  {'chance':>7}  "
        f"{'ratio':>6}  {'class':>7}")
    for pair, st in report['pairs'].items():
        r_str = f"{st['r']:+.2f}" if st['r'] is not None else "  n/a"
        ratio_str = (f"{st['ratio']:.1f}×" if st['ratio'] is not None
                     else "  n/a")
        out.append(
            f"  {pair:>22}  "
            f"{r_str:>12}  {st['pairs']:>6d}  "
            f"{st['expected']:>7.2f}  {ratio_str:>6}  "
            f"{st.get('class', '?'):>7}")
    out.append("")

    out.append("## Verdict")
    out.append(f"  {report['verdict']}")
    out.append(f"  {report['rationale']}")

    notes = report.get('notes', [])
    if notes:
        out.append("")
        out.append("## Notes")
        for n in notes:
            out.append(f"  - {n}")
    return "\n".join(out) + "\n"


# ── CLI ─────────────────────────────────────────────────────────────────── #

def main() -> int:
    ap = argparse.ArgumentParser(
        description="Cross-host WL-drift cohort analysis.")
    ap.add_argument("logs", nargs='+', help="engine log files (one per host)")
    ap.add_argument("--labels", default=None,
                    help="host labels, comma-separated "
                         "(default: log filename stems)")
    ap.add_argument("--window", type=float, default=30.0,
                    help="coincidence window ±s (default 30)")
    ap.add_argument("--grid-dt", type=float, default=10.0,
                    help="resample dt for WL series (default 10)")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    paths = [Path(p) for p in args.logs]
    if args.labels is None:
        labels = [p.stem for p in paths]
    else:
        labels = [s.strip() for s in args.labels.split(",")]
        if len(labels) != len(paths):
            print(f"--labels count ({len(labels)}) must match "
                  f"log count ({len(paths)})", file=sys.stderr)
            return 2

    parsed = {lbl: parse_log(str(p)) for lbl, p in zip(labels, paths)}

    # Span common to all hosts (intersection).
    all_ts = []
    for d in parsed.values():
        if d['antpos']:
            all_ts.append((d['antpos'][0][0], d['antpos'][-1][0]))
    if not all_ts:
        print("no [AntPosEst] lines found in any log", file=sys.stderr)
        return 1
    t0 = max(s for s, _ in all_ts)
    t1 = min(e for _, e in all_ts)
    if t1 <= t0:
        print(f"no overlapping observation window across hosts "
              f"(t0={t0:.0f} t1={t1:.0f})", file=sys.stderr)
        return 1
    t_total = t1 - t0

    # Per-host stats over the common window.
    hosts_st: dict[str, dict] = {}
    grids: dict[str, list[float | None]] = {}
    drifts_in_window: dict[str, list[tuple[float, str, float]]] = {}
    for lbl, d in parsed.items():
        ap_in = [r for r in d['antpos'] if t0 <= r[0] <= t1]
        dr_in = [r for r in d['drift'] if t0 <= r[0] <= t1]
        wl_vals = [r[2] for r in ap_in]
        hosts_st[lbl] = {
            'wl_mean': statistics.mean(wl_vals) if wl_vals else 0.0,
            'wl_max': max(wl_vals) if wl_vals else 0,
            'n_drift': len(dr_in),
            'span_h': t_total / 3600.0,
        }
        grids[lbl] = to_grid(ap_in, t0, t1, args.grid_dt)
        drifts_in_window[lbl] = dr_in

    # Pairwise.
    pairs_st: dict[str, dict] = {}
    pair_classes: dict[tuple[str, str], str] = {}
    notes: list[str] = []
    R_HI, R_LO, C_HI, C_LO = 0.7, 0.3, 3.0, 1.5
    for la, lb in itertools.combinations(labels, 2):
        xs, ys = pair_present(grids[la], grids[lb])
        r = pearson(xs, ys)
        coinc = coincidence(drifts_in_window[la], drifts_in_window[lb],
                            args.window, t_total)
        pair_key = f"{la}↔{lb}"
        cls = _classify_pair(r, coinc['ratio'], coinc['pairs'],
                             R_HI, R_LO, C_HI, C_LO)
        pair_classes[tuple(sorted((la, lb)))] = cls
        pairs_st[pair_key] = {
            'r': r,
            'pairs': coinc['pairs'],
            'expected': coinc['expected'],
            'ratio': coinc['ratio'],
            'n_overlap': len(xs),
            'examples': coinc['examples'],
            'class': cls,
        }
        # Genuine disagreement: signals point in opposite directions.
        if r is not None and coinc['ratio'] is not None:
            if r <= 0.1 and coinc['ratio'] >= 5.0:
                notes.append(
                    f"{pair_key}: r={r:+.2f} but coincidence="
                    f"{coinc['ratio']:.1f}× — events line up but slow "
                    f"WL trend doesn't.  Cycling is shared, warmup "
                    f"or NL-amb trajectory differs.")
            elif r >= 0.7 and coinc['ratio'] <= 1.5:
                notes.append(
                    f"{pair_key}: r={r:+.2f} but coincidence="
                    f"{coinc['ratio']:.1f}× — slow trends agree but "
                    f"individual cycling events don't.  Common warmup "
                    f"trajectory, independent per-event noise.")

    mean_wl = statistics.mean(s['wl_mean'] for s in hosts_st.values())
    label, rationale = verdict(labels, pair_classes, mean_wl)

    report = {
        'window_s': args.window,
        't_total': t_total,
        't0_iso': datetime.fromtimestamp(t0, timezone.utc).isoformat(),
        't1_iso': datetime.fromtimestamp(t1, timezone.utc).isoformat(),
        'hosts': hosts_st,
        'pairs': pairs_st,
        'verdict': label,
        'rationale': rationale,
        'notes': notes,
    }
    if args.format == "json":
        # Drop unserialisable example tuples for JSON brevity.
        for p in report['pairs'].values():
            p['examples'] = [list(e) for e in p['examples']]
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
