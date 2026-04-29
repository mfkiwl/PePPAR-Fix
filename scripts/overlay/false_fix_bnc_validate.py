#!/usr/bin/env python3
"""BNC-grounded validator for engine FalseFixMonitor `false-fix rejection` events.

NL-layer companion to ``wl_drift_bnc_validate_v2.py``.  Same architectural
question: when the engine fires a FalseFixMonitor trip on an NL ambiguity,
does that line up with a real cycle slip (or integer change) in the BNC
AMB stream?

Workstream C of dayplan I-221332-main.  Today's morning finding showed
the engine's WL_DRIFT signal had −0.2% chance-corrected excess vs BNC
AMB integer jumps (Z=−0.17, indistinguishable from random).  The same
pattern is suspected at the NL layer: FalseFixMonitor uses PR-residual
rolling-mean (base 2 m); PR-domain noise drives the trips, not real
wrong-integer commitments.  This validator gives the empirical answer.

Engine signal parsed:

    2026-04-28 15:17:14,055 WARNING false-fix rejection: E30 |PR|=3.21m > 2.37m
    (n=11, elev=37°, expected, squelch=60s) {NL LAMBDA ratio=17.6 P=0.970;
    WL frac=0.002 n=60}

Per-event features extracted (for the I-133306-bravo classifier framework
applied to the GOOD signal once Charlie's IF-residual NL eviction lands):

    sv, ts, pr_resid_m, threshold_m, n, elev_deg, tag,
    nl_method (LAMBDA|rounding|...), ratio, P_IB, wl_frac, wl_n

BNC ground truth: same AMB integer-jump stream that v2 uses.  Reused via
direct import from ``wl_drift_bnc_validate_v2``.

Output:

  - Per-host TP/FP breakdown
  - Chance-corrected excess (analog of WL's −0.2%)
  - Per-event JSON (for downstream feature-classifier joining)

Stdlib only.

Usage:
    false_fix_bnc_validate.py --bnc bnc.ppp \\
        --labels MadHat,clkPoC3,TimeHat \\
        --engine madhat.log clkpoc3.log timehat.log

    false_fix_bnc_validate.py --bnc bnc.ppp \\
        --engine madhat.log --format json > false_fix_labels.json
"""
from __future__ import annotations

import argparse
import json
import re
import sys
from datetime import datetime, timezone
from pathlib import Path

# Reuse v2's BNC parsing + matching + chance-correction helpers.  Both
# scripts live in scripts/overlay/, so importlib via the same dir works
# without packaging.
sys.path.insert(0, str(Path(__file__).parent))
import importlib.util as _ilu

_v2_path = Path(__file__).parent / "wl_drift_bnc_validate_v2.py"
_v2_spec = _ilu.spec_from_file_location("wl_drift_bnc_validate_v2", _v2_path)
_v2 = _ilu.module_from_spec(_v2_spec)
_v2_spec.loader.exec_module(_v2)

parse_bnc_amb = _v2.parse_bnc_amb
parse_bnc_reset = _v2.parse_bnc_reset
detect_amb_slips = _v2.detect_amb_slips
label_engine_events = _v2.label_engine_events
find_bnc_misses = _v2.find_bnc_misses
aggregate_per_sv = _v2.aggregate_per_sv
expected_chance_tps = _v2.expected_chance_tps
excess_tps = _v2.excess_tps


# ── FalseFixMonitor engine event parser ───────────────────────────────────── #

# Engine emission (scripts/peppar_fix_engine.py — log.warning around the
# false-fix rejection block).  Format string:
#
#   "false-fix rejection: %s |PR|=%.2fm > %.2fm (n=%d, elev=%s, %s, "
#   "squelch=%ds)%s"
#
# where the trailing %s is the provenance suffix that looks like:
#
#   {NL LAMBDA ratio=17.6 P=0.970; WL frac=0.002 n=60}
#
# The provenance carries the NL resolution method (LAMBDA / rounding / etc.),
# the LAMBDA ratio + P_IB if available, and the WL fractional + sample count
# at trip time.  Some emissions have a partial provenance (rounding-only
# events lack ratio/P, LAMBDA-only events have no fixed-WL stats).  Parser
# tolerates missing fields per the I-133306 classifier needs.
#
# Engine source: scripts/peppar_fix_engine.py (search for "false-fix rejection")
_FALSE_FIX_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?false-fix rejection:\s+(?P<sv>\w\d+)\s+"
    r"\|PR\|=(?P<pr_m>[+-]?\d+\.\d+)m\s+"
    r">\s+(?P<thresh_m>[+-]?\d+\.\d+)m\s+"
    r"\(n=(?P<n>\d+),\s+"
    r"elev=(?P<elev>[0-9.]+|\?)°?,\s+"
    # Tag spans 'expected', 'unexpected #1', 'unexpected #2', etc.
    # Match anything up to the next comma so the engine's tag
    # taxonomy can grow without re-tuning this regex.
    r"(?P<tag>[^,]+),\s+"
    r"squelch=(?P<squelch>\d+)s\)"
    r"(?P<provenance>.*)"
)

# Provenance sub-parsers — each optional, applied to the trailing ``{...}``
# substring captured above.
_PROV_NL_LAMBDA_RE = re.compile(
    r"NL\s+LAMBDA\s+ratio=(?P<ratio>[\d.]+)\s+P=(?P<P>[\d.]+)"
)
_PROV_NL_ROUNDING_RE = re.compile(
    r"NL\s+rounding\s+frac=(?P<frac>[+-]?[\d.]+)\s+σ=(?P<sigma>[\d.]+)"
)
_PROV_WL_RE = re.compile(
    r"WL\s+frac=(?P<wl_frac>[+-]?[\d.]+)\s+n=(?P<wl_n>\d+)"
)


def parse_engine_false_fix(path: str, tz_offset_h: float) -> list[dict]:
    """Parse engine ``false-fix rejection`` log lines.

    Per-event extracted fields cover the I-133306-bravo per-event-feature
    classifier needs: PR magnitude + threshold for context, n + elev for
    geometry, tag for engine's own confidence label, plus provenance fields
    (NL method, ratio/P_IB or rounding frac/σ, WL frac/n).
    """
    out: list[dict] = []
    offset_s = tz_offset_h * 3600.0
    with open(path) as f:
        for line in f:
            m = _FALSE_FIX_RE.search(line)
            if not m:
                continue
            d = m.group(1)
            t = m.group(2)
            ts = datetime.fromisoformat(f"{d}T{t}").replace(
                tzinfo=timezone.utc).timestamp() - offset_s
            elev = m.group("elev")
            ev: dict = {
                'ts': ts,
                'sv': m.group("sv"),
                'pr_m': float(m.group("pr_m")),
                'thresh_m': float(m.group("thresh_m")),
                'n': int(m.group("n")),
                'elev_deg': float(elev) if elev != '?' else None,
                'tag': m.group("tag"),
                'squelch_s': int(m.group("squelch")),
                'kind': 'false_fix',
            }
            prov = m.group("provenance") or ""
            mp = _PROV_NL_LAMBDA_RE.search(prov)
            if mp:
                ev['nl_method'] = 'LAMBDA'
                ev['ratio'] = float(mp.group("ratio"))
                ev['P_IB'] = float(mp.group("P"))
            else:
                mp = _PROV_NL_ROUNDING_RE.search(prov)
                if mp:
                    ev['nl_method'] = 'rounding'
                    ev['frac'] = float(mp.group("frac"))
                    ev['sigma'] = float(mp.group("sigma"))
            mp = _PROV_WL_RE.search(prov)
            if mp:
                ev['wl_frac'] = float(mp.group("wl_frac"))
                ev['wl_n'] = int(mp.group("wl_n"))
            out.append(ev)
    return out


# ── Reporting ────────────────────────────────────────────────────────────── #

def render_text(report: dict) -> str:
    """Compact text summary mirroring v2's layout, scoped to false_fix."""
    lines = []
    lines.append("# false-fix rejection vs BNC AMB integer-jump validation")
    lines.append(f"# generated: {datetime.now(timezone.utc).isoformat()}")
    lines.append(f"# match window: ±{report['window_s']:.0f}s")
    lines.append(f"# BNC systems tracked: {','.join(sorted(report['bnc_systems']))}")
    lines.append(f"# BNC AMB integer-jump events (ground truth): "
                 f"{report['n_bnc_slips']}")
    lines.append("")
    lines.append("## Per-host: false-fix rejection vs BNC ground truth")
    lines.append(
        "  Event partition: TP/FP (in-scope: SV had BNC slips); "
        "NO_OPP (BNC observed SV but zero slips, stable arc — "
        "strong-evidence FP); OOS_SV (system tracked, this SV not "
        "observed by BNC); OOS_SYS (system not tracked)."
    )
    lines.append(
        f"  Excess_TP = observed_TP − expected_chance_TP "
        f"(Poisson under independence at ±{report['window_s']:.0f}s)."
    )
    lines.append(
        f"  {'host':>10}  {'all':>5}  {'in-sc':>6}  "
        f"{'TP':>4}  {'FP':>4}  {'TP%':>5}  "
        f"{'chance':>6}  {'excess':>6}  "
        f"{'NO_OPP':>6}  {'OOS_SV':>6}  {'OOS_SYS':>7}"
    )
    for host, h in report['hosts'].items():
        d = h['false_fix']
        in_scope = d['tp'] + d['fp']
        tp_pct = (100.0 * d['tp'] / in_scope) if in_scope > 0 else 0.0
        lines.append(
            f"  {host:>10}  {d['n']:>5}  {in_scope:>6}  "
            f"{d['tp']:>4}  {d['fp']:>4}  {tp_pct:>4.1f}%  "
            f"{d['expected_chance']:>6.1f}  {d['excess_tp']:>+6.1f}  "
            f"{d['no_opp']:>6}  {d['oos_sv']:>6}  {d['oos_sys']:>7}"
        )
    lines.append("")
    lines.append(
        "## Verdict guidance (mirrors today's WL_DRIFT verdict structure):"
    )
    lines.append(
        "  excess >> 0 → false-fix is catching real wrong-integer commits "
        "BNC also disagrees with; signal is real, threshold tunable."
    )
    lines.append(
        "  excess ≈ 0  → false-fix events are uncorrelated with BNC's "
        "integer-jump truth; signal is noise (analog of today's WL finding)."
    )
    lines.append(
        "  excess << 0 → events fire AWAY from BNC slips; signal is "
        "anti-correlated, suggesting it's reacting to PR-domain artefacts "
        "uncoupled from carrier-side reality."
    )
    return "\n".join(lines) + "\n"


def render_json(report: dict, labelled_events: dict) -> str:
    """Emit per-event JSON consumable by the I-133306-bravo classifier
    framework.  Each entry carries the per-event features extracted from
    the false-fix rejection line plus the BNC TP/FP label."""
    out = {
        'window_s': report['window_s'],
        'bnc_systems': sorted(report['bnc_systems']),
        'n_bnc_slips': report['n_bnc_slips'],
        'hosts': report['hosts'],
        'events': [
            {**e, 'host': host}
            for host, evs in labelled_events.items()
            for e in evs
        ],
    }
    return json.dumps(out, indent=2, sort_keys=True, default=float)


# ── Main ─────────────────────────────────────────────────────────────────── #

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    p.add_argument('--bnc', required=True,
                   help='BNC .ppp log (ground truth source)')
    p.add_argument('--engine', nargs='+', required=True,
                   help='One or more engine .log files')
    p.add_argument('--labels',
                   help='Comma-separated host labels (one per --engine, '
                        'in matching order).  Defaults to engine basename.')
    p.add_argument('--window', type=float, default=30.0,
                   help='Match window (s) — engine event matches a BNC '
                        'integer-jump if they fall within ±window of the '
                        'same SV.  Default 30 s.')
    p.add_argument('--tz-offset-hours', type=float, default=0.0,
                   help='Engine clock offset from UTC, hours, ISO sign '
                        'convention: positive = engine local clock AHEAD of '
                        'UTC (CEST=+2), negative = BEHIND UTC (CDT=-5). '
                        'Default 0 (engine logs already UTC). '
                        'Use -5 for CDT-emitted engine logs.')
    p.add_argument('--min-cycles', type=float, default=1.0,
                   help='BNC AMB integer-jump magnitude threshold (default 1)')
    p.add_argument('--format', choices=('text', 'json'), default='text',
                   help='Output format (default text)')
    args = p.parse_args(argv)

    # Engine parse
    engine_paths = [Path(e) for e in args.engine]
    if args.labels:
        labels = [s.strip() for s in args.labels.split(',')]
        if len(labels) != len(engine_paths):
            print(f"--labels has {len(labels)} entries; --engine has "
                  f"{len(engine_paths)}", file=sys.stderr)
            return 2
    else:
        labels = [p.stem for p in engine_paths]
    per_host_events: dict[str, list[dict]] = {}
    for label, path in zip(labels, engine_paths):
        per_host_events[label] = parse_engine_false_fix(
            str(path), args.tz_offset_hours)

    # BNC parse
    bnc_amb = parse_bnc_amb(args.bnc)
    bnc_resets = parse_bnc_reset(args.bnc)
    if not bnc_amb:
        print(f"WARN: no AMB lines parsed from {args.bnc}", file=sys.stderr)
    bnc_slips = detect_amb_slips(bnc_amb, min_cycles=args.min_cycles)
    bnc_systems = {sv[0] for sv in (s['sv'] for s in bnc_amb)}
    bnc_tracked_svs = {s['sv'] for s in bnc_amb}
    bnc_slip_counts: dict[str, int] = {}
    for s in bnc_slips:
        bnc_slip_counts[s['sv']] = bnc_slip_counts.get(s['sv'], 0) + 1

    # Determine BNC time span for chance correction
    if bnc_amb:
        t_total = bnc_amb[-1]['ts'] - bnc_amb[0]['ts']
    else:
        t_total = 0.0

    # Per-host labelling
    labelled: dict[str, list[dict]] = {}
    hosts_block: dict[str, dict] = {}
    for host, events in per_host_events.items():
        lbl = label_engine_events(
            events, bnc_slips, bnc_systems, bnc_tracked_svs,
            bnc_slip_counts, args.window,
        )
        labelled[host] = lbl
        # Aggregate
        agg = {'n': len(lbl), 'tp': 0, 'fp': 0, 'no_opp': 0,
               'oos_sv': 0, 'oos_sys': 0}
        for e in lbl:
            label_str = e['label']
            if label_str == 'TP':
                agg['tp'] += 1
            elif label_str == 'FP':
                agg['fp'] += 1
            elif label_str == 'NO_OPP':
                agg['no_opp'] += 1
            elif label_str == 'OOS_SV':
                agg['oos_sv'] += 1
            elif label_str == 'OOS_SYS':
                agg['oos_sys'] += 1
        # Chance correction on the in-scope subset
        expected = expected_chance_tps(
            lbl, bnc_slip_counts, args.window, t_total)
        agg['expected_chance'] = expected
        agg['excess_tp'] = agg['tp'] - expected
        hosts_block[host] = {'false_fix': agg}

    report = {
        'window_s': args.window,
        'bnc_systems': bnc_systems,
        'n_bnc_slips': len(bnc_slips),
        'n_bnc_resets': len(bnc_resets),
        'hosts': hosts_block,
    }

    if args.format == 'text':
        print(render_text(report))
    else:
        print(render_json(report, labelled))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
