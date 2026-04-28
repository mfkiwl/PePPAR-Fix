#!/usr/bin/env python3
"""BNC-grounded validator v2 — AMB integer-jump ground truth.

v1 (`wl_drift_bnc_validate.py`) used BNC `RESET AMB` events as the
reference signal.  Bob's challenge plus the day0427night data
showed that's the wrong signal: 912 RESET events vs many more
integer-ambiguity jumps in the AMB stream that BNC repaired
silently.  RESET undercounts real cycle slips.

v2 uses **AMB integer jumps as the ground truth**.  For each
satellite, walk the `AMB lIF` lines in time order; any change in
the integer-ambiguity column between adjacent samples is one
cycle-slip event with an exact timestamp and a slip magnitude
(in IF-combination cycles).

Each engine event (`[WL_DRIFT]` or `cycle slip flush`) is then
matched against the BNC ground-truth slip stream within a
configurable window.  The output reports:

  - Per-host **wl_drift** TP / FP rate (does each demotion line
    up with a real BNC slip?)
  - Per-host **cycle-slip-flush** TP / FP rate (engine's existing
    slip detector vs BNC ground truth)
  - **BNC misses** (BNC saw a slip; neither engine signal fired)
  - Per-SV breakdown for outlier-spotting

Three of these together discriminate the open question from the
2026-04-28 finding:

  - If wl_drift TP rate is high (most events line up with real
    slips), wl_drift catches wrong-integer cases the cycle-slip
    detector misses; current threshold is roughly right.
  - If wl_drift TP rate is low and cycle-slip-flush TP rate is
    high, wl_drift over-reacts to noise; the cycle-slip detector
    is doing the real work and wl_drift can be tuned down or
    repurposed.
  - If both engine signals miss many BNC slips, the engine's
    detection is too conservative.

Usage:
    wl_drift_bnc_validate_v2.py --bnc bnc.ppp \\
        --labels MadHat,clkPoC3,TimeHat \\
        --engine madhat.log clkpoc3.log timehat.log

    wl_drift_bnc_validate_v2.py --bnc bnc.ppp --window 30 \\
        --engine madhat.log --format json > labels.json

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

# Engine [WL_DRIFT]:
#   2026-04-27 22:19:19,062 WARNING [WL_DRIFT] E12 drift=+0.317cyc > ±0.25
#   (n=15, window=30ep): flushing MW, demoting to FLOATING, gate@elev=49.0°
_WLDRIFT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[WL_DRIFT\]\s+(\w\d+)\s+drift=([+-]?\d+\.\d+)cyc"
    r".*?gate@elev=([0-9.]+|\?)"
)

# Engine cycle-slip flush:
#   2026-04-27 22:17:56,033 INFO cycle slip flush: sv=G01 epoch=23 reason=mw_jump
_CYCSLIP_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?cycle slip flush:\s+sv=(\w\d+)\s+epoch=(\d+)\s+reason=(\S+)"
)

# BNC AMB lIF:
#   2026-04-28_00:08:48.001 AMB  lIF E07    21.0000    -0.7867 +-  19.9215
#   el =  57.42 epo =    1
_BNC_AMB_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ _T](\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"\s+AMB\s+lIF\s+(\w\d+)"
    r"\s+([+-]?\d+\.\d+)"          # integer
    r"\s+([+-]?\d+\.\d+)"          # float correction
    r"\s+\+-\s+([+-]?\d+\.\d+)"    # sigma (mm)
    r"\s+el\s*=\s*([0-9.]+)"
    r"\s+epo\s*=\s*(\d+)"
)

# BNC RESET (still parsed, used as a tertiary signal):
_BNC_RESET_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ _T](\d{2}:\d{2}:\d{2})(?:\.\d+)?"
    r"\s+RESET\s+AMB\s+\S*\s+(\w\d+)"
)


def parse_engine_drift(path: str, tz_offset_h: float) -> list[dict]:
    out: list[dict] = []
    offset_s = tz_offset_h * 3600.0
    with open(path) as f:
        for line in f:
            m = _WLDRIFT_RE.search(line)
            if not m:
                continue
            d, t, sv, dcyc, elev = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp() - offset_s
            out.append({
                'ts': ts, 'sv': sv,
                'drift_cyc': float(dcyc),
                'elev_deg': float(elev) if elev != '?' else None,
                'kind': 'wl_drift',
            })
    return out


def parse_engine_cycslip(path: str, tz_offset_h: float) -> list[dict]:
    out: list[dict] = []
    offset_s = tz_offset_h * 3600.0
    with open(path) as f:
        for line in f:
            m = _CYCSLIP_RE.search(line)
            if not m:
                continue
            d, t, sv, ep, reason = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp() - offset_s
            out.append({
                'ts': ts, 'sv': sv,
                'epoch': int(ep),
                'reason': reason,
                'kind': 'cyc_slip',
            })
    return out


def parse_bnc_amb(path: str) -> list[dict]:
    """Return AMB observations in time order (one per SV per epoch)."""
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            m = _BNC_AMB_RE.search(line)
            if not m:
                continue
            d, t, sv, integer, _flt, _sig, _el, _epo = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp()
            out.append({
                'ts': ts, 'sv': sv, 'integer': float(integer),
            })
    return out


def parse_bnc_reset(path: str) -> list[dict]:
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            m = _BNC_RESET_RE.search(line)
            if not m:
                continue
            d, t, sv = m.groups()
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp()
            out.append({'ts': ts, 'sv': sv})
    return out


# ── BNC slip-event extraction from AMB stream ────────────────────────────── #

def detect_amb_slips(amb_obs: list[dict],
                     min_cycles: float = 1.0) -> list[dict]:
    """Walk AMB observations per SV; emit one slip event per integer jump.

    `min_cycles`: ignore micro-noise integer changes below this magnitude.
    Default 1.0 cycle catches all real slips while skipping the rare
    unit jitter artefact (none observed in day0427night).

    Returns list of {ts, sv, prev_int, new_int, jump_cyc, gap_s} where
    `gap_s` is the time gap to the previous observation on this SV
    (NaN-equivalent → None for first observation; large values mark
    arc gaps where the slip may be normal re-acquisition).
    """
    by_sv: dict[str, list[dict]] = {}
    for obs in amb_obs:
        by_sv.setdefault(obs['sv'], []).append(obs)
    slips: list[dict] = []
    for sv, lst in by_sv.items():
        lst.sort(key=lambda r: r['ts'])
        prev = None
        for cur in lst:
            if prev is not None and cur['integer'] != prev['integer']:
                jump = cur['integer'] - prev['integer']
                if abs(jump) >= min_cycles:
                    slips.append({
                        'ts': cur['ts'],
                        'sv': sv,
                        'prev_int': prev['integer'],
                        'new_int': cur['integer'],
                        'jump_cyc': jump,
                        'gap_s': cur['ts'] - prev['ts'],
                    })
            prev = cur
    slips.sort(key=lambda r: r['ts'])
    return slips


# ── Matching ─────────────────────────────────────────────────────────────── #

def _index_by_sv(events: list[dict]) -> dict[str, list[float]]:
    by_sv: dict[str, list[float]] = {}
    for e in events:
        by_sv.setdefault(e['sv'], []).append(e['ts'])
    for lst in by_sv.values():
        lst.sort()
    return by_sv


def _nearest_within(sorted_ts: list[float], target: float,
                    window: float) -> float | None:
    if not sorted_ts:
        return None
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
    bnc_slips: list[dict],
    bnc_systems: set[str],
    bnc_tracked_svs: set[str],
    bnc_slip_counts: dict[str, int],
    window_s: float,
) -> list[dict]:
    """Annotate each engine event with a refined label.

    Labels (in order of precedence):
      OOS_SYS  — engine SV's GNSS system not tracked by BNC at all
      OOS_SV   — system tracked, but this specific SV never observed
                 by BNC (different elev mask, PRN exclusion, etc.)
      NO_OPP   — BNC observed the SV but had zero integer-jump events
                 over the run.  Stable BNC arc; engine demoted anyway.
                 Strong-evidence FP.
      TP / FP  — SV tracked, BNC had slip events; classify by match.

    The split disentangles "BNC didn't have an opinion" (OOS_*, NO_OPP
    when BNC saw nothing) from "BNC had an opinion and disagreed"
    (FP among had-opp SVs).
    """
    bnc_by_sv = _index_by_sv(bnc_slips)
    out = []
    for e in engine_events:
        sv_sys = e['sv'][0]
        labelled = dict(e)
        if sv_sys not in bnc_systems:
            labelled['label'] = 'OOS_SYS'
            labelled['bnc_match'] = False
            labelled['bnc_match_dt_s'] = None
            out.append(labelled)
            continue
        if e['sv'] not in bnc_tracked_svs:
            labelled['label'] = 'OOS_SV'
            labelled['bnc_match'] = False
            labelled['bnc_match_dt_s'] = None
            out.append(labelled)
            continue
        if bnc_slip_counts.get(e['sv'], 0) == 0:
            # BNC observed this SV but never declared a slip.  Engine
            # event has no chance of matching; treat as a strong FP
            # (stable BNC arc → engine over-detection on this SV).
            labelled['label'] = 'NO_OPP'
            labelled['bnc_match'] = False
            labelled['bnc_match_dt_s'] = None
            out.append(labelled)
            continue
        dt = _nearest_within(bnc_by_sv.get(e['sv'], []), e['ts'], window_s)
        labelled['bnc_match'] = dt is not None
        labelled['bnc_match_dt_s'] = dt
        labelled['label'] = 'TP' if dt is not None else 'FP'
        out.append(labelled)
    return out


def find_bnc_misses(
    bnc_slips: list[dict],
    engine_events_by_kind: dict[str, list[dict]],
    window_s: float,
) -> list[dict]:
    """BNC slip events not covered by ANY engine signal within window.

    `engine_events_by_kind` maps signal name → events list (e.g.
    {'wl_drift': [...], 'cyc_slip': [...]}).  A BNC slip is "missed"
    only if no engine signal — across hosts and kinds — fires on
    the same SV within ±window_s.
    """
    out: list[dict] = []
    pooled_by_sv: dict[str, list[float]] = {}
    for events in engine_events_by_kind.values():
        for e in events:
            pooled_by_sv.setdefault(e['sv'], []).append(e['ts'])
    for lst in pooled_by_sv.values():
        lst.sort()
    for s in bnc_slips:
        dt = _nearest_within(pooled_by_sv.get(s['sv'], []), s['ts'],
                             window_s)
        if dt is None:
            out.append({
                'ts': s['ts'], 'sv': s['sv'],
                'jump_cyc': s['jump_cyc'],
            })
    return out


# ── Aggregation ──────────────────────────────────────────────────────────── #

def aggregate_per_sv(labelled: list[dict]) -> dict[str, dict]:
    by_sv: dict[str, dict] = {}
    for e in labelled:
        sv = e['sv']
        rec = by_sv.setdefault(sv, {'n': 0, 'tp': 0, 'fp': 0,
                                     'no_opp': 0,
                                     'oos_sys': 0, 'oos_sv': 0})
        rec['n'] += 1
        lbl = e['label']
        if lbl == 'TP':
            rec['tp'] += 1
        elif lbl == 'FP':
            rec['fp'] += 1
        elif lbl == 'NO_OPP':
            rec['no_opp'] += 1
        elif lbl == 'OOS_SYS':
            rec['oos_sys'] += 1
        elif lbl == 'OOS_SV':
            rec['oos_sv'] += 1
    for rec in by_sv.values():
        in_scope = rec['tp'] + rec['fp']
        rec['fp_rate'] = rec['fp'] / in_scope if in_scope else None
        rec['tp_rate'] = rec['tp'] / in_scope if in_scope else None
    return by_sv


def expected_chance_tps(
    engine_events: list[dict],
    bnc_slip_counts: dict[str, int],
    window_s: float,
    t_total: float,
) -> float:
    """Expected number of chance matches under independence.

    For each engine event on SV X at time t, treating BNC slips on
    SV X as a uniform Poisson point process with rate
    λ_X = N_BNC_X / T, the probability that a random window of
    length 2W around t catches at least one BNC event is
    approximately min(1, 2 W λ_X) for thin events.  Sum over engine
    events gives the expected count under independence.

    Only events labelled TP or FP contribute (in-scope subset).
    """
    if t_total <= 0:
        return 0.0
    expected = 0.0
    for e in engine_events:
        if e['label'] not in ('TP', 'FP'):
            continue
        n_bnc = bnc_slip_counts.get(e['sv'], 0)
        if n_bnc == 0:
            continue
        lam = n_bnc / t_total
        p = min(1.0, 2.0 * window_s * lam)
        expected += p
    return expected


def excess_tps(observed_tp: int, expected_chance: float) -> dict:
    """Above-chance TPs: observed minus expected, plus a normalised
    excess rate on the in-scope denominator the caller manages."""
    return {
        'observed_tp': observed_tp,
        'expected_chance': expected_chance,
        'excess_tp': observed_tp - expected_chance,
    }


# ── Reporting ────────────────────────────────────────────────────────────── #

def render_text(report: dict) -> str:
    out = []
    out.append("# wl_drift / cycle-slip BNC-AMB-jump validation (v2)")
    out.append(f"# generated: {datetime.now(timezone.utc).isoformat()}")
    out.append(f"# match window: ±{report['window_s']:.0f}s")
    out.append(f"# BNC systems tracked: {','.join(sorted(report['bnc_systems']))}")
    out.append(f"# BNC AMB integer-jump events (ground truth): "
               f"{report['n_bnc_slips']}")
    out.append(f"# BNC RESET events (subset, advisory): {report['n_bnc_resets']}")
    out.append("")

    def _block(kind: str, title: str) -> list[str]:
        lines = []
        lines.append(f"## Per-host: {title} vs BNC ground truth")
        lines.append("  Event partition: TP/FP (in-scope: SV had BNC slips); "
                     "NO_OPP (BNC observed SV but zero slips, stable arc — "
                     "strong-evidence FP); OOS_SV (system tracked, this SV "
                     "not observed by BNC); OOS_SYS (system not tracked).")
        lines.append("  Excess_TP = observed_TP − expected_chance_TP "
                     f"(Poisson under independence at ±{report['window_s']:.0f}s).")
        lines.append(
            f"  {'host':>10}  {'all':>5}  {'in-sc':>6}  "
            f"{'TP':>4}  {'FP':>4}  {'TP%':>5}  "
            f"{'chance':>6}  {'excess':>6}  "
            f"{'NO_OPP':>6}  {'OOS_SV':>6}  {'OOS_SYS':>7}")
        for host, h in report['hosts'].items():
            d = h[kind]
            in_scope = d['tp'] + d['fp']
            tp_pct = (f"{(d['tp']/in_scope*100):>4.1f}%"
                      if in_scope else "  n/a")
            chance = d['expected_chance_tp']
            excess = d['tp'] - chance
            lines.append(
                f"  {host:>10}  {d['n']:>5d}  {in_scope:>6d}  "
                f"{d['tp']:>4d}  {d['fp']:>4d}  {tp_pct:>5}  "
                f"{chance:>6.1f}  {excess:>+6.1f}  "
                f"{d['no_opp']:>6d}  {d['oos_sv']:>6d}  "
                f"{d['oos_sys']:>7d}")
        lines.append("")
        return lines

    out.extend(_block('wl_drift', 'WL_DRIFT'))
    out.extend(_block('cyc_slip', 'cycle-slip-flush'))

    out.append("## BNC misses (BNC saw a slip; no engine signal fired)")
    out.append(f"  pooled across all hosts + both engine signals: "
               f"{report['n_bnc_missed']} events "
               f"({report['n_bnc_missed']/report['n_bnc_slips']*100:.1f}% "
               f"of BNC slips unflagged)")
    out.append("")

    # Per-SV listing — wl_drift FPs sorted by absolute count, capped 15.
    out.append("## Top-FP SVs for WL_DRIFT (worst over-detectors)")
    flat: dict[str, dict] = {}
    for host, h in report['hosts'].items():
        for sv, r in h['wl_drift_per_sv'].items():
            agg = flat.setdefault(sv, {'n': 0, 'tp': 0, 'fp': 0,
                                       'hosts': set()})
            agg['n'] += r['n']
            agg['tp'] += r['tp']
            agg['fp'] += r['fp']
            agg['hosts'].add(host)
    for sv, r in flat.items():
        in_scope = r['tp'] + r['fp']
        r['fp_rate'] = r['fp'] / in_scope if in_scope else None
        r['tp_rate'] = r['tp'] / in_scope if in_scope else None
    in_scope_only = [(sv, r) for sv, r in flat.items()
                     if r['fp_rate'] is not None]
    ranked = sorted(in_scope_only,
                    key=lambda kv: (-kv[1]['fp'], -(kv[1]['fp_rate'] or 0)))
    out.append(f"  {'sv':>4}  {'n':>5}  {'TP':>4}  {'FP':>5}  "
               f"{'TP%':>6}  hosts")
    for sv, r in ranked[:15]:
        if r['fp'] == 0:
            break
        hosts = ",".join(sorted(r['hosts']))
        tp_pct = (f"{r['tp_rate']*100:>5.1f}%"
                  if r['tp_rate'] is not None else "  n/a")
        out.append(
            f"  {sv:>4}  {r['n']:>5d}  {r['tp']:>4d}  {r['fp']:>5d}  "
            f"{tp_pct:>6}  {hosts}")
    out.append("")

    # Headline using chance-adjusted excess.
    wl_combined = report['wl_drift_combined']
    cs_combined = report['cyc_slip_combined']
    out.append("## Headline (chance-adjusted)")
    out.append("  Excess_TP = observed_TP − expected_chance_TP.  Above-chance")
    out.append("  fraction = excess_TP / in_scope — the fraction of in-scope")
    out.append("  events that are correlated above what random independent")
    out.append("  events would produce at this window size.")
    out.append("")

    def _hl(label: str, c: dict) -> str:
        n = c['in_scope']
        if n == 0:
            return f"  {label:18s}: no in-scope events"
        raw = c['tp'] / n * 100
        excess_pct = c['excess_tp'] / n * 100
        return (f"  {label:18s}: raw_TP={raw:>4.1f}% "
                f"chance={c['expected_chance_tp']:>5.1f} "
                f"excess={c['excess_tp']:>+5.1f} "
                f"({excess_pct:>+5.1f}% above chance, "
                f"n_in_scope={n})")

    out.append(_hl("WL_DRIFT", wl_combined))
    out.append(_hl("cycle-slip-flush", cs_combined))
    out.append("")

    n_no_opp_wl = wl_combined['no_opp']
    n_oos_sv_wl = wl_combined['oos_sv']
    n_oos_sys_wl = wl_combined['oos_sys']
    out.append(f"  WL_DRIFT non-validatable / strong-FP partition:")
    out.append(f"    OOS_SYS  (GPS, BNC didn't track):     {n_oos_sys_wl}")
    out.append(f"    OOS_SV   (sys tracked, this SV not):  {n_oos_sv_wl}")
    out.append(f"    NO_OPP   (BNC saw SV, never slipped): {n_no_opp_wl}")
    out.append("")

    # Interpretation guidance.
    if wl_combined['in_scope'] > 0:
        wl_excess_frac = wl_combined['excess_tp'] / wl_combined['in_scope']
    else:
        wl_excess_frac = 0.0
    if cs_combined['in_scope'] > 0:
        cs_excess_frac = cs_combined['excess_tp'] / cs_combined['in_scope']
    else:
        cs_excess_frac = 0.0
    out.append("## Interpretation")
    if cs_excess_frac > 0.20 and wl_excess_frac < 0.05:
        out.append("  → cycle-slip-flush has real correlation with BNC "
                   "(>20% above chance); WL_DRIFT does not (<5% above "
                   "chance).  WL_DRIFT is not a slip detector.")
    elif wl_excess_frac > 0.20:
        out.append("  → WL_DRIFT has real correlation with BNC.  "
                   "Catches wrong-integer cases the slip detector misses.")
    elif cs_excess_frac < 0.10:
        out.append("  → both engine signals near chance vs BNC.  "
                   "Different signal classes; redesign needed.")
    else:
        out.append("  → mixed signal; see per-SV breakdown.")

    return "\n".join(out) + "\n"


# ── CLI ──────────────────────────────────────────────────────────────────── #

def main() -> int:
    ap = argparse.ArgumentParser(description=__doc__.split("\n\n")[0])
    ap.add_argument("--bnc", required=True)
    ap.add_argument("--engine", nargs='+', required=True)
    ap.add_argument("--labels", default=None,
                    help="comma-separated host labels")
    ap.add_argument("--window", type=float, default=30.0)
    ap.add_argument("--engine-tz-offset-hours", type=float, default=-5.0)
    ap.add_argument("--min-jump-cycles", type=float, default=1.0,
                    help="ignore AMB integer changes below this magnitude")
    ap.add_argument("--format", choices=("text", "json"), default="text")
    args = ap.parse_args()

    paths = [Path(p) for p in args.engine]
    if args.labels is None:
        labels = [p.stem for p in paths]
    else:
        labels = [s.strip() for s in args.labels.split(",")]
        if len(labels) != len(paths):
            print(f"--labels count ({len(labels)}) != --engine count "
                  f"({len(paths)})", file=sys.stderr)
            return 2

    print("parsing BNC AMB stream...", file=sys.stderr)
    bnc_amb = parse_bnc_amb(args.bnc)
    bnc_resets = parse_bnc_reset(args.bnc)
    bnc_slips = detect_amb_slips(bnc_amb, min_cycles=args.min_jump_cycles)
    bnc_systems = {sv[0] for sv in {o['sv'] for o in bnc_amb}}
    bnc_tracked_svs = {o['sv'] for o in bnc_amb}
    bnc_slip_counts: dict[str, int] = {}
    for s in bnc_slips:
        bnc_slip_counts[s['sv']] = bnc_slip_counts.get(s['sv'], 0) + 1
    if bnc_amb:
        bnc_t_min = min(o['ts'] for o in bnc_amb)
        bnc_t_max = max(o['ts'] for o in bnc_amb)
        bnc_t_total = bnc_t_max - bnc_t_min
    else:
        bnc_t_total = 0.0
    print(f"  {len(bnc_amb)} AMB observations; {len(bnc_slips)} integer jumps; "
          f"{len(bnc_resets)} RESETs; systems={sorted(bnc_systems)}; "
          f"SVs tracked={len(bnc_tracked_svs)}; span={bnc_t_total/3600:.2f}h",
          file=sys.stderr)

    hosts: dict[str, dict] = {}
    pooled_engine: dict[str, list[dict]] = {'wl_drift': [], 'cyc_slip': []}
    for lbl, p in zip(labels, paths):
        print(f"parsing engine log {lbl}...", file=sys.stderr)
        wl = parse_engine_drift(str(p), args.engine_tz_offset_hours)
        cs = parse_engine_cycslip(str(p), args.engine_tz_offset_hours)
        wl_lbl = label_engine_events(
            wl, bnc_slips, bnc_systems, bnc_tracked_svs,
            bnc_slip_counts, args.window)
        cs_lbl = label_engine_events(
            cs, bnc_slips, bnc_systems, bnc_tracked_svs,
            bnc_slip_counts, args.window)
        pooled_engine['wl_drift'].extend(wl_lbl)
        pooled_engine['cyc_slip'].extend(cs_lbl)

        def _bin(events: list[dict]) -> dict:
            return {
                'n': len(events),
                'tp': sum(1 for e in events if e['label'] == 'TP'),
                'fp': sum(1 for e in events if e['label'] == 'FP'),
                'no_opp': sum(1 for e in events if e['label'] == 'NO_OPP'),
                'oos_sys': sum(1 for e in events if e['label'] == 'OOS_SYS'),
                'oos_sv': sum(1 for e in events if e['label'] == 'OOS_SV'),
                'events': events,
            }
        wl_bin = _bin(wl_lbl)
        cs_bin = _bin(cs_lbl)
        wl_bin['expected_chance_tp'] = expected_chance_tps(
            wl_lbl, bnc_slip_counts, args.window, bnc_t_total)
        cs_bin['expected_chance_tp'] = expected_chance_tps(
            cs_lbl, bnc_slip_counts, args.window, bnc_t_total)
        hosts[lbl] = {
            'wl_drift': wl_bin,
            'cyc_slip': cs_bin,
            'wl_drift_per_sv': aggregate_per_sv(wl_lbl),
            'cyc_slip_per_sv': aggregate_per_sv(cs_lbl),
        }

    bnc_misses = find_bnc_misses(bnc_slips, pooled_engine, args.window)

    def _sum(kind: str, field: str) -> int | float:
        return sum(h[kind][field] for h in hosts.values())
    wl_combined = {
        'tp': _sum('wl_drift', 'tp'),
        'fp': _sum('wl_drift', 'fp'),
        'no_opp': _sum('wl_drift', 'no_opp'),
        'oos_sys': _sum('wl_drift', 'oos_sys'),
        'oos_sv': _sum('wl_drift', 'oos_sv'),
        'expected_chance_tp': _sum('wl_drift', 'expected_chance_tp'),
    }
    wl_combined['in_scope'] = wl_combined['tp'] + wl_combined['fp']
    wl_combined['excess_tp'] = (
        wl_combined['tp'] - wl_combined['expected_chance_tp'])
    cs_combined = {
        'tp': _sum('cyc_slip', 'tp'),
        'fp': _sum('cyc_slip', 'fp'),
        'no_opp': _sum('cyc_slip', 'no_opp'),
        'oos_sys': _sum('cyc_slip', 'oos_sys'),
        'oos_sv': _sum('cyc_slip', 'oos_sv'),
        'expected_chance_tp': _sum('cyc_slip', 'expected_chance_tp'),
    }
    cs_combined['in_scope'] = cs_combined['tp'] + cs_combined['fp']
    cs_combined['excess_tp'] = (
        cs_combined['tp'] - cs_combined['expected_chance_tp'])

    report = {
        'window_s': args.window,
        'min_jump_cycles': args.min_jump_cycles,
        'bnc_systems': sorted(bnc_systems),
        'n_bnc_slips': len(bnc_slips),
        'n_bnc_resets': len(bnc_resets),
        'n_bnc_missed': len(bnc_misses),
        'hosts': hosts,
        'wl_drift_combined': wl_combined,
        'cyc_slip_combined': cs_combined,
        'bnc_misses': bnc_misses,
    }

    if args.format == "json":
        # Drop heavy event lists for JSON brevity; summary suffices.
        for h in report['hosts'].values():
            h['wl_drift']['events'] = (
                h['wl_drift']['events'][:1000])  # keep first 1000
            h['cyc_slip']['events'] = (
                h['cyc_slip']['events'][:1000])
        json.dump(report, sys.stdout, indent=2, default=str)
        sys.stdout.write("\n")
    else:
        sys.stdout.write(render_text(report))
    return 0


if __name__ == "__main__":
    sys.exit(main())
