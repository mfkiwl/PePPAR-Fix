#!/usr/bin/env python3
"""Filter-stiffness-redesign validation harness.

Consumes one peppar-fix engine log and reports the metrics that the
Phase A redesign (NAV2 seed + physics-tight ZTD prior + Phase 1/2
collapse) targets.  Output is JSON + a human-readable markdown
table; a `--baseline` flag lets the same run produce a side-by-side
comparison against an earlier JSON.

Use:
  python3 scripts/overlay/filter_stiffness_validation.py LOG.log
  python3 scripts/overlay/filter_stiffness_validation.py LOG.log --json out.json
  python3 scripts/overlay/filter_stiffness_validation.py NEW.log --baseline BASE.json

Metrics map to docs/filter-stiffness-redesign.md "Validation plan":
  - integrity-trip count + ztd-class share
  - NL_ADMIT/BLOCK/EVICT + tier distribution
  - σ + altitude + nav2Δ trajectory stats
  - max sustained NL ANCHORED count
  - cold-start time to first NL_ADMIT
  - post-SO_POS-reset altitude walk (the 22m signature we want to
    NOT see after Phase A)
"""

from __future__ import annotations

import argparse
import json
import re
import sys
from collections import Counter
from dataclasses import dataclass, field, asdict
from datetime import datetime
from pathlib import Path
from statistics import median


_TS_RE = re.compile(r'^(\d{4}-\d\d-\d\d \d\d:\d\d:\d\d)')

_RE_ANTPOS = re.compile(
    r'\[AntPosEst\s+(?P<epoch>\d+)\]\s+'
    r'positionσ=(?P<sigma>[\d.]+)m\s+'
    r'pos=\(\s*(?P<lat>[-\d.]+),\s*(?P<lon>[-\d.]+),\s*(?P<alt>[-\d.]+)\s*\)\s+'
    r'n=(?P<n>\d+)\s+amb=(?P<amb>\d+)\s+'
    r'WL:\s*(?P<wl_fixed>\d+)/(?P<wl_total>\d+)\s+fixed\s+'
    r'NL:\s*(?P<nl_fixed>\d+)\s+fixed.*?'
    r'nav2Δ=(?P<nav2d>[\d.]+)m\s+'
    r'ZTD=(?P<ztd_sign>[-+]?)(?P<ztd_mag>\d+)±(?P<ztd_sigma>\d+)mm'
)

_RE_NL_ADMIT = re.compile(
    r'\[NL_ADMIT\]\s+sv=(?P<sv>\S+).*?'
    r'method=(?P<method>\w+)\s+'
    r'(?:tier=(?P<tier>\w+)\s+)?'
    r'int_history=\[(?P<hist>[^\]]*)\]'
)
_RE_NL_BLOCK = re.compile(
    r'\[NL_ADMIT_BLOCK\]\s+sv=(?P<sv>\S+).*?'
    r'tier=(?P<tier>\w+)\s+reason=(?P<reason>\S+)'
)
_RE_NL_EVICT = re.compile(r'\[NL_EVICT\]\s+sv=(?P<sv>\S+)')

_RE_INTEGRITY = re.compile(
    r'\[FIX_SET_INTEGRITY\]\s+TRIPPED\s+reason=(?P<reason>\w+)'
)
_RE_SO_POS = re.compile(
    r'\[SECOND_OPINION_POS\]\s+tripped:\s+nav2Δ=(?P<nav2d>[\d.]+)m'
)
_RE_PROMOTED = re.compile(r'Promoted\s+(?P<sv>\S+)\s+→\s+ANCHORED')
_RE_STATE_TR = re.compile(
    r'\[STATE\]\s+AntPosEst:\s+(?P<from>\w+)\s+→\s+(?P<to>\w+)\s+after\s+(?P<dur>\d+)s'
)
_RE_OBS_ADMIT = re.compile(
    r'\[OBS_ADMIT\]\s+epoch=\d+\s+raw=(?P<raw>\d+)\s+'
    r'untracked=(?P<untracked>\d+)\s+tracking=(?P<tracking>\d+)\s+dual=(?P<dual>\d+)'
)


@dataclass
class AntPos:
    ts: datetime
    epoch: int
    sigma_m: float
    alt_m: float
    nav2d_m: float
    nl_fixed: int
    wl_fixed: int
    ztd_residual_mm: int


@dataclass
class Trip:
    ts: datetime
    kind: str           # 'integrity' | 'so_pos'
    detail: str         # reason for integrity, nav2Δ for SO_POS


@dataclass
class NlAdmit:
    ts: datetime
    sv: str
    tier: str | None
    int_history: list[int]


@dataclass
class NlBlock:
    ts: datetime
    sv: str
    tier: str
    reason: str


@dataclass
class StateTransition:
    ts: datetime
    from_state: str
    to_state: str
    dur_s: int


@dataclass
class ParsedLog:
    path: str
    antpos: list[AntPos] = field(default_factory=list)
    trips: list[Trip] = field(default_factory=list)
    nl_admits: list[NlAdmit] = field(default_factory=list)
    nl_blocks: list[NlBlock] = field(default_factory=list)
    nl_evicts: int = 0
    promoted_anchored: int = 0
    transitions: list[StateTransition] = field(default_factory=list)
    obs_dual_samples: list[int] = field(default_factory=list)
    obs_tracking_samples: list[int] = field(default_factory=list)
    log_start: datetime | None = None
    log_end: datetime | None = None


def _parse_ts(line: str) -> datetime | None:
    m = _TS_RE.match(line)
    if not m:
        return None
    try:
        return datetime.strptime(m.group(1), '%Y-%m-%d %H:%M:%S')
    except ValueError:
        return None


def _parse_int_list(s: str) -> list[int]:
    s = s.strip()
    if not s:
        return []
    out = []
    for tok in s.split(','):
        tok = tok.strip()
        if tok:
            try:
                out.append(int(tok))
            except ValueError:
                pass
    return out


def parse_log(path: Path) -> ParsedLog:
    out = ParsedLog(path=str(path))
    with path.open('rb') as f:
        for raw in f:
            try:
                line = raw.decode('utf-8', errors='replace')
            except Exception:
                continue
            ts = _parse_ts(line)
            if ts is None:
                continue
            if out.log_start is None:
                out.log_start = ts
            out.log_end = ts

            m = _RE_ANTPOS.search(line)
            if m:
                ztd_sign = -1 if m.group('ztd_sign') == '-' else 1
                out.antpos.append(AntPos(
                    ts=ts,
                    epoch=int(m.group('epoch')),
                    sigma_m=float(m.group('sigma')),
                    alt_m=float(m.group('alt')),
                    nav2d_m=float(m.group('nav2d')),
                    nl_fixed=int(m.group('nl_fixed')),
                    wl_fixed=int(m.group('wl_fixed')),
                    ztd_residual_mm=ztd_sign * int(m.group('ztd_mag')),
                ))
                continue

            m = _RE_INTEGRITY.search(line)
            if m:
                out.trips.append(Trip(ts=ts, kind='integrity',
                                      detail=m.group('reason')))
                continue

            m = _RE_SO_POS.search(line)
            if m:
                out.trips.append(Trip(ts=ts, kind='so_pos',
                                      detail=m.group('nav2d')))
                continue

            m = _RE_NL_ADMIT.search(line)
            if m and '[NL_ADMIT_BLOCK]' not in line:
                out.nl_admits.append(NlAdmit(
                    ts=ts, sv=m.group('sv'),
                    tier=m.group('tier'),
                    int_history=_parse_int_list(m.group('hist')),
                ))
                continue

            m = _RE_NL_BLOCK.search(line)
            if m:
                out.nl_blocks.append(NlBlock(
                    ts=ts, sv=m.group('sv'),
                    tier=m.group('tier'), reason=m.group('reason'),
                ))
                continue

            if _RE_NL_EVICT.search(line):
                out.nl_evicts += 1
                continue

            if _RE_PROMOTED.search(line):
                out.promoted_anchored += 1
                continue

            m = _RE_STATE_TR.search(line)
            if m:
                out.transitions.append(StateTransition(
                    ts=ts,
                    from_state=m.group('from'), to_state=m.group('to'),
                    dur_s=int(m.group('dur')),
                ))
                continue

            m = _RE_OBS_ADMIT.search(line)
            if m:
                out.obs_dual_samples.append(int(m.group('dual')))
                out.obs_tracking_samples.append(int(m.group('tracking')))
    return out


def _hours(start: datetime, end: datetime) -> float:
    return max(0.001, (end - start).total_seconds() / 3600.0)


def _quantile(xs: list[float], q: float) -> float | None:
    if not xs:
        return None
    s = sorted(xs)
    k = max(0, min(len(s) - 1, int(round((len(s) - 1) * q))))
    return s[k]


def _post_so_pos_alt_walks(parsed: ParsedLog,
                           window_epochs: int = 60) -> list[dict]:
    """For each SO_POS reset, the 60-epoch (~60s) altitude walk after."""
    walks = []
    so_pos_ts = [t.ts for t in parsed.trips if t.kind == 'so_pos']
    for reset_ts in so_pos_ts:
        # find the first AntPos at or after reset_ts; walk window_epochs
        idx = None
        for i, ap in enumerate(parsed.antpos):
            if ap.ts >= reset_ts:
                idx = i
                break
        if idx is None:
            continue
        window = parsed.antpos[idx:idx + window_epochs]
        if not window:
            continue
        alt0 = window[0].alt_m
        alt_min = min(w.alt_m for w in window)
        alt_max = max(w.alt_m for w in window)
        walks.append({
            'reset_ts': reset_ts.isoformat(),
            'alt_at_reset_m': round(alt0, 2),
            'alt_min_m': round(alt_min, 2),
            'alt_max_m': round(alt_max, 2),
            'drop_m': round(alt0 - alt_min, 2),
            'rise_m': round(alt_max - alt0, 2),
        })
    return walks


def _max_sustained_nl(parsed: ParsedLog, n: int,
                      sustained_epochs: int = 60) -> int:
    """Longest run (in epochs) where nl_fixed >= n."""
    best, cur = 0, 0
    for ap in parsed.antpos:
        if ap.nl_fixed >= n:
            cur += 1
            best = max(best, cur)
        else:
            cur = 0
    return best


def _time_to_first_nl_admit(parsed: ParsedLog) -> float | None:
    if not parsed.nl_admits or parsed.log_start is None:
        return None
    return (parsed.nl_admits[0].ts - parsed.log_start).total_seconds()


def _state_dwell_seconds(parsed: ParsedLog) -> dict[str, int]:
    """Approximate dwell time per AntPosEstState from transitions."""
    out: Counter[str] = Counter()
    for t in parsed.transitions:
        # The duration on a transition line is how long the
        # *previous* (from_state) state was held.
        out[t.from_state] += t.dur_s
    return dict(out)


def compute_metrics(parsed: ParsedLog) -> dict:
    if parsed.log_start is None or parsed.log_end is None:
        return {'path': parsed.path, 'error': 'no parsable timestamps'}

    duration_h = _hours(parsed.log_start, parsed.log_end)
    sigmas = [a.sigma_m for a in parsed.antpos]
    alts = [a.alt_m for a in parsed.antpos]
    nav2ds = [a.nav2d_m for a in parsed.antpos]
    ztds = [a.ztd_residual_mm for a in parsed.antpos]
    nl_fixed_seq = [a.nl_fixed for a in parsed.antpos]

    integrity_trips = [t for t in parsed.trips if t.kind == 'integrity']
    so_pos_trips = [t for t in parsed.trips if t.kind == 'so_pos']
    integrity_by_reason = Counter(t.detail for t in integrity_trips)

    ztd_class = (integrity_by_reason.get('ztd_cycling', 0)
                 + integrity_by_reason.get('ztd_impossible', 0))
    ztd_share = (ztd_class / len(integrity_trips)) if integrity_trips else 0.0

    tier_dist = Counter(a.tier or 'UNKNOWN' for a in parsed.nl_admits)
    block_reason_dist = Counter(b.reason for b in parsed.nl_blocks)
    block_tier_dist = Counter(b.tier for b in parsed.nl_blocks)

    walks = _post_so_pos_alt_walks(parsed)
    max_drop = max((w['drop_m'] for w in walks), default=0.0)
    median_drop = median([w['drop_m'] for w in walks]) if walks else 0.0

    metrics = {
        'path': parsed.path,
        'duration_h': round(duration_h, 2),
        'log_start': parsed.log_start.isoformat(),
        'log_end': parsed.log_end.isoformat(),

        'integrity_trips': {
            'total': len(integrity_trips),
            'per_hour': round(len(integrity_trips) / duration_h, 2),
            'by_reason': dict(integrity_by_reason),
            'ztd_class_count': ztd_class,
            'ztd_class_share': round(ztd_share, 3),
        },
        'so_pos_resets': {
            'total': len(so_pos_trips),
            'per_hour': round(len(so_pos_trips) / duration_h, 2),
        },

        'nl_admit': {
            'admit_count': len(parsed.nl_admits),
            'block_count': len(parsed.nl_blocks),
            'evict_count': parsed.nl_evicts,
            'admit_to_evict_ratio': (
                round(len(parsed.nl_admits) / parsed.nl_evicts, 3)
                if parsed.nl_evicts else None),
            'tier_dist': dict(tier_dist),
            'block_reason_dist': dict(block_reason_dist),
            'block_tier_dist': dict(block_tier_dist),
        },

        'cold_start': {
            'time_to_first_nl_admit_s': _time_to_first_nl_admit(parsed),
            'first_admit_tier': (parsed.nl_admits[0].tier
                                 if parsed.nl_admits else None),
            'first_provisional_or_trusted_s': next(
                ((a.ts - parsed.log_start).total_seconds()
                 for a in parsed.nl_admits
                 if a.tier in ('PROVISIONAL', 'TRUSTED')), None),
        },

        'position_quality': {
            'sigma_m_median': round(median(sigmas), 4) if sigmas else None,
            'sigma_m_p95': round(_quantile(sigmas, 0.95) or 0, 4),
            'alt_m_median': round(median(alts), 2) if alts else None,
            'alt_m_min': round(min(alts), 2) if alts else None,
            'alt_m_max': round(max(alts), 2) if alts else None,
            'nav2d_m_median': round(median(nav2ds), 2) if nav2ds else None,
            'nav2d_m_p95': round(_quantile(nav2ds, 0.95) or 0, 2),
            'ztd_mm_median': round(median(ztds)) if ztds else None,
            'ztd_mm_p95_abs': round(_quantile(
                [abs(z) for z in ztds], 0.95) or 0),
        },

        'nl_sustained': {
            'max_sustained_ge_1_epochs': _max_sustained_nl(parsed, 1),
            'max_sustained_ge_3_epochs': _max_sustained_nl(parsed, 3),
            'max_sustained_ge_4_epochs': _max_sustained_nl(parsed, 4),
            'max_nl_fixed_seen': max(nl_fixed_seq) if nl_fixed_seq else 0,
        },

        'state_dwell_s': _state_dwell_seconds(parsed),
        'anchored_promotions': parsed.promoted_anchored,

        'observations': {
            'dual_median': median(parsed.obs_dual_samples)
                if parsed.obs_dual_samples else None,
            'tracking_median': median(parsed.obs_tracking_samples)
                if parsed.obs_tracking_samples else None,
            'samples': len(parsed.obs_dual_samples),
        },

        'post_so_pos_alt_walks': {
            'count': len(walks),
            'max_drop_m': round(max_drop, 2),
            'median_drop_m': round(median_drop, 2) if walks else 0.0,
            'worst': sorted(walks, key=lambda w: -w['drop_m'])[:5],
        },
    }
    return metrics


def render_markdown(m: dict, label: str | None = None) -> str:
    if 'error' in m:
        return f"## {label or m['path']}\n\nERROR: {m['error']}\n"

    out = []
    out.append(f"## {label or m['path']}")
    out.append(f"  duration: {m['duration_h']} h "
               f"({m['log_start']} → {m['log_end']})")
    out.append("")

    out.append("### Integrity")
    it = m['integrity_trips']
    out.append(f"  trips: {it['total']} total ({it['per_hour']}/hr), "
               f"ZTD-class: {it['ztd_class_count']} "
               f"({it['ztd_class_share'] * 100:.1f}%)")
    out.append("  by reason: " + ", ".join(
        f"{k}={v}" for k, v in sorted(it['by_reason'].items())))
    out.append(f"  SO_POS resets: {m['so_pos_resets']['total']} "
               f"({m['so_pos_resets']['per_hour']}/hr)")
    out.append("")

    out.append("### NL admission")
    na = m['nl_admit']
    out.append(f"  admit/block/evict: {na['admit_count']} / "
               f"{na['block_count']} / {na['evict_count']}")
    out.append(f"  admit:evict ratio: {na['admit_to_evict_ratio']}")
    out.append("  tier dist: " + ", ".join(
        f"{k}={v}" for k, v in sorted(na['tier_dist'].items())))
    if na['block_reason_dist']:
        out.append("  block reasons: " + ", ".join(
            f"{k}={v}" for k, v in sorted(na['block_reason_dist'].items())))
    out.append("")

    cs = m['cold_start']
    out.append("### Cold start")
    if cs['time_to_first_nl_admit_s'] is not None:
        out.append(f"  first NL_ADMIT: {cs['time_to_first_nl_admit_s']:.0f}s "
                   f"(tier={cs['first_admit_tier']})")
    else:
        out.append("  first NL_ADMIT: never")
    if cs['first_provisional_or_trusted_s'] is not None:
        out.append(f"  first PROV/TRUSTED: "
                   f"{cs['first_provisional_or_trusted_s']:.0f}s")
    out.append("")

    pq = m['position_quality']
    out.append("### Position quality")
    out.append(f"  σ: median={pq['sigma_m_median']} m, p95={pq['sigma_m_p95']} m")
    out.append(f"  alt: median={pq['alt_m_median']} m, "
               f"min={pq['alt_m_min']}, max={pq['alt_m_max']}")
    out.append(f"  nav2Δ: median={pq['nav2d_m_median']} m, "
               f"p95={pq['nav2d_m_p95']} m")
    out.append(f"  ZTD residual: median={pq['ztd_mm_median']} mm, "
               f"|p95|={pq['ztd_mm_p95_abs']} mm")
    out.append("")

    ns = m['nl_sustained']
    out.append("### NL sustained (epochs ≈ seconds)")
    out.append(f"  longest run NL≥1: {ns['max_sustained_ge_1_epochs']} ep")
    out.append(f"  longest run NL≥3: {ns['max_sustained_ge_3_epochs']} ep")
    out.append(f"  longest run NL≥4: {ns['max_sustained_ge_4_epochs']} ep")
    out.append(f"  max NL_fixed seen: {ns['max_nl_fixed_seen']}")
    out.append("")

    sd = m['state_dwell_s']
    if sd:
        total = sum(sd.values())
        out.append("### AntPosEst state dwell")
        for k, v in sorted(sd.items(), key=lambda kv: -kv[1]):
            pct = 100.0 * v / total if total else 0.0
            out.append(f"  {k}: {v}s ({pct:.1f}%)")
        out.append(f"  ANCHORED promotions: {m['anchored_promotions']}")
        out.append("")

    obs = m['observations']
    if obs['samples']:
        out.append("### Observations (median)")
        out.append(f"  dual: {obs['dual_median']}, "
                   f"tracking: {obs['tracking_median']} "
                   f"(n={obs['samples']})")
        out.append("")

    walks = m['post_so_pos_alt_walks']
    out.append("### Post-SO_POS-reset altitude walk")
    out.append(f"  resets: {walks['count']}, "
               f"max drop: {walks['max_drop_m']} m, "
               f"median drop: {walks['median_drop_m']} m")
    if walks['worst']:
        out.append("  worst-5 drops:")
        for w in walks['worst']:
            out.append(f"    {w['reset_ts']}  "
                       f"alt {w['alt_at_reset_m']}→{w['alt_min_m']} "
                       f"(Δ -{w['drop_m']} m)")
    return "\n".join(out)


def render_diff(base: dict, cur: dict) -> str:
    """Side-by-side baseline vs current for the headline numbers."""
    if 'error' in base or 'error' in cur:
        return ""

    def fmt(v):
        return "—" if v is None else str(v)

    rows = [
        ('integrity trips/hr',
         base['integrity_trips']['per_hour'],
         cur['integrity_trips']['per_hour']),
        ('ZTD-class share',
         f"{base['integrity_trips']['ztd_class_share'] * 100:.1f}%",
         f"{cur['integrity_trips']['ztd_class_share'] * 100:.1f}%"),
        ('SO_POS resets/hr',
         base['so_pos_resets']['per_hour'],
         cur['so_pos_resets']['per_hour']),
        ('NL admit:evict ratio',
         base['nl_admit']['admit_to_evict_ratio'],
         cur['nl_admit']['admit_to_evict_ratio']),
        ('first NL_ADMIT (s)',
         base['cold_start']['time_to_first_nl_admit_s'],
         cur['cold_start']['time_to_first_nl_admit_s']),
        ('σ p95 (m)',
         base['position_quality']['sigma_m_p95'],
         cur['position_quality']['sigma_m_p95']),
        ('nav2Δ p95 (m)',
         base['position_quality']['nav2d_m_p95'],
         cur['position_quality']['nav2d_m_p95']),
        ('alt min (m)',
         base['position_quality']['alt_m_min'],
         cur['position_quality']['alt_m_min']),
        ('alt max (m)',
         base['position_quality']['alt_m_max'],
         cur['position_quality']['alt_m_max']),
        ('|ZTD| p95 (mm)',
         base['position_quality']['ztd_mm_p95_abs'],
         cur['position_quality']['ztd_mm_p95_abs']),
        ('max sustained NL≥4 (ep)',
         base['nl_sustained']['max_sustained_ge_4_epochs'],
         cur['nl_sustained']['max_sustained_ge_4_epochs']),
        ('max post-SO_POS drop (m)',
         base['post_so_pos_alt_walks']['max_drop_m'],
         cur['post_so_pos_alt_walks']['max_drop_m']),
        ('ANCHORED promotions',
         base['anchored_promotions'],
         cur['anchored_promotions']),
    ]
    out = ["## Baseline vs current", ""]
    out.append(f"  baseline: {base['path']} ({base['duration_h']} h)")
    out.append(f"  current:  {cur['path']} ({cur['duration_h']} h)")
    out.append("")
    out.append("| metric | baseline | current |")
    out.append("|---|---|---|")
    for name, b, c in rows:
        out.append(f"| {name} | {fmt(b)} | {fmt(c)} |")
    return "\n".join(out)


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    p.add_argument('logs', nargs='+', type=Path,
                   help='engine log files to analyze (one or more)')
    p.add_argument('--baseline', type=Path,
                   help='JSON metrics from a prior run; emits a diff section')
    p.add_argument('--json', dest='json_out', type=Path,
                   help='write metrics as JSON (single log) or list (multi)')
    p.add_argument('--label', action='append', default=[],
                   help='label per log (positional, repeat to label each)')
    args = p.parse_args(argv)

    metrics = []
    for i, path in enumerate(args.logs):
        if not path.exists():
            print(f"ERROR: {path} does not exist", file=sys.stderr)
            return 2
        parsed = parse_log(path)
        m = compute_metrics(parsed)
        label = args.label[i] if i < len(args.label) else None
        m['_label'] = label or path.name
        metrics.append(m)

    for m in metrics:
        print(render_markdown(m, m.get('_label')))
        print()

    if args.baseline:
        if not args.baseline.exists():
            print(f"WARN: baseline {args.baseline} not found", file=sys.stderr)
        else:
            base = json.loads(args.baseline.read_text())
            base_list = base if isinstance(base, list) else [base]
            for cur in metrics:
                # Match by label if available, else first
                match = next((b for b in base_list
                              if b.get('_label') == cur.get('_label')), None)
                if match is None and len(base_list) == 1:
                    match = base_list[0]
                if match:
                    print(render_diff(match, cur))
                    print()

    if args.json_out:
        payload = metrics if len(metrics) > 1 else metrics[0]
        args.json_out.write_text(json.dumps(payload, indent=2, default=str))
        print(f"# JSON written: {args.json_out}", file=sys.stderr)

    return 0


if __name__ == '__main__':
    sys.exit(main())
