#!/usr/bin/env python3
"""NL admission classifier — per-SV admit/evict feature analysis.

Workstream C2 of dayplan I-115539-main.  Consumes the [NL_ADMIT] /
[NL_EVICT] log lines emitted by the engine after commit 04f366e
(workstream B), and produces a per-SV characterization of NL fix
behaviour suitable for:

  - Validating Charlie's incoming phase-only admission monitor
    (workstream A) — does admission cycling stop when gated on
    carrier consistency?
  - Identifying chronic Pop B SVs that wander integers across
    re-admissions (e.g. clkPoC3's E27 admitted 9× overnight with
    LAMBDA ratio 5-11)
  - Per-event JSON output that can be joined with
    false_fix_bnc_validate.py / wl_drift_bnc_validate_v2.py BNC
    TP/FP labels — same I-133306-bravo per-event-feature framework
    applied to the admission side

Engine log lines parsed (post-04f366e):

    [NL_ADMIT] sv=Exx n_nl=NN ratio=NN.NN P=0.NNNN
               method=lambda int_history=[a, b, c, d]

    [NL_ADMIT] sv=Exx n_nl=NN frac=N.NNN sigma=N.NNN
               method=rounding int_history=[a, b, c, d]

    [NL_EVICT] sv=Exx n_nl=NN duration_s=NN.N int_history=[a, b, c, d]

Pop classifier on int_history (Bob's terminology, dayplan
2026-04-28):

  HIGH (Pop A): all entries match (range = 0) — SV reproducibly
                fixes to the same integer; admission is durable.
  MEDIUM:       entries within ±1 (range = 1) — boundary case,
                often legitimate (cycle wrap on LAMBDA).
  LOW (Pop B):  range >= 2 — SV wanders integers across re-admits;
                each "fix" is contaminating the float.
  UNKNOWN:      < 2 entries to compare.

Output:

  - Per-SV summary table (text or JSON)
  - Per-event JSON (one record per admit / evict) for downstream
    joining with the BNC validators

Stdlib only.

Usage:
    nl_admission_classify.py --engine madhat.log
    nl_admission_classify.py --engine madhat.log --format json > nl_events.json
    nl_admission_classify.py --engine madhat.log clkpoc3.log timehat.log \\
        --labels MadHat,clkPoC3,TimeHat
"""
from __future__ import annotations

import argparse
import json
import re
import statistics
import sys
from datetime import datetime, timezone
from pathlib import Path


# ── Log parsers ──────────────────────────────────────────────────────────── #

# Engine [NL_ADMIT] LAMBDA form:
#   2026-04-29 07:42:13,055 INFO [NL_ADMIT] sv=E07 n_nl=12 ratio=5.83
#                                 P=0.9942 method=lambda int_history=[12, 12]
_NL_ADMIT_LAMBDA_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[NL_ADMIT\]\s+"
    r"sv=(?P<sv>\w\d+)\s+"
    r"n_nl=(?P<n_nl>-?\d+)\s+"
    r"ratio=(?P<ratio>[\d.]+)\s+"
    r"P=(?P<P>[\d.]+)\s+"
    r"method=lambda\s+"
    r"int_history=\[(?P<hist>[^\]]*)\]"
)

# Engine [NL_ADMIT] rounding form:
#   2026-04-29 07:42:13,055 INFO [NL_ADMIT] sv=E07 n_nl=12 frac=0.025
#                                 sigma=0.045 method=rounding int_history=[12]
_NL_ADMIT_ROUNDING_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[NL_ADMIT\]\s+"
    r"sv=(?P<sv>\w\d+)\s+"
    r"n_nl=(?P<n_nl>-?\d+)\s+"
    r"frac=(?P<frac>[+-]?[\d.]+)\s+"
    r"sigma=(?P<sigma>[\d.]+)\s+"
    r"method=rounding\s+"
    r"int_history=\[(?P<hist>[^\]]*)\]"
)

# Engine [NL_EVICT]:
#   2026-04-29 07:42:48,055 INFO [NL_EVICT] sv=E07 n_nl=12 duration_s=35.0
#                                 int_history=[12, 12]
_NL_EVICT_RE = re.compile(
    r"(\d{4}-\d{2}-\d{2})[ T](\d{2}:\d{2}:\d{2})[,.]?\d*"
    r".*?\[NL_EVICT\]\s+"
    r"sv=(?P<sv>\w\d+)\s+"
    r"n_nl=(?P<n_nl>-?\d+|\?|None)\s+"
    r"duration_s=(?P<dur>[\d.]+|\?)\s+"
    r"int_history=\[(?P<hist>[^\]]*)\]"
)


def _parse_history(s: str) -> list[int]:
    """Parse the int_history list-of-ints from log format.  Empty
    lists, single elements, and comma-separated all handled."""
    s = s.strip()
    if not s:
        return []
    out = []
    for tok in s.split(","):
        tok = tok.strip()
        if tok:
            try:
                out.append(int(tok))
            except ValueError:
                # Engine occasionally emits placeholders we can't parse;
                # skip without breaking the whole record.
                pass
    return out


def _parse_ts(date: str, time_s: str, tz_offset_h: float) -> float:
    offset_s = tz_offset_h * 3600.0
    return datetime.fromisoformat(f"{date}T{time_s}").replace(
        tzinfo=timezone.utc).timestamp() - offset_s


def parse_engine_admissions(path: str, tz_offset_h: float) -> list[dict]:
    """Return a time-sorted list of admit / evict events from one engine log."""
    out: list[dict] = []
    with open(path) as f:
        for line in f:
            m = _NL_ADMIT_LAMBDA_RE.search(line)
            if m:
                out.append({
                    'ts': _parse_ts(m.group(1), m.group(2), tz_offset_h),
                    'kind': 'admit',
                    'method': 'lambda',
                    'sv': m.group("sv"),
                    'n_nl': int(m.group("n_nl")),
                    'ratio': float(m.group("ratio")),
                    'P_IB': float(m.group("P")),
                    'frac': None, 'sigma': None,
                    'int_history': _parse_history(m.group("hist")),
                })
                continue
            m = _NL_ADMIT_ROUNDING_RE.search(line)
            if m:
                out.append({
                    'ts': _parse_ts(m.group(1), m.group(2), tz_offset_h),
                    'kind': 'admit',
                    'method': 'rounding',
                    'sv': m.group("sv"),
                    'n_nl': int(m.group("n_nl")),
                    'ratio': None, 'P_IB': None,
                    'frac': float(m.group("frac")),
                    'sigma': float(m.group("sigma")),
                    'int_history': _parse_history(m.group("hist")),
                })
                continue
            m = _NL_EVICT_RE.search(line)
            if m:
                n_nl_str = m.group("n_nl")
                try:
                    n_nl = int(n_nl_str)
                except ValueError:
                    n_nl = None
                dur_str = m.group("dur")
                try:
                    dur = float(dur_str)
                except ValueError:
                    dur = None
                out.append({
                    'ts': _parse_ts(m.group(1), m.group(2), tz_offset_h),
                    'kind': 'evict',
                    'sv': m.group("sv"),
                    'n_nl': n_nl,
                    'duration_s': dur,
                    'int_history': _parse_history(m.group("hist")),
                })
    out.sort(key=lambda e: e['ts'])
    return out


# ── Per-SV pop classification ────────────────────────────────────────────── #

def classify_history(history: list[int]) -> str:
    """Classify an int_history list per Bob's Pop A/B taxonomy.

    HIGH:    all entries match (range = 0) — Pop A
    MEDIUM:  range = 1 — boundary
    LOW:     range >= 2 — Pop B (wandering)
    UNKNOWN: < 2 distinct entries to range-compare
    """
    if len(history) < 2:
        return 'UNKNOWN'
    rng = max(history) - min(history)
    if rng == 0:
        return 'HIGH'
    if rng == 1:
        return 'MEDIUM'
    return 'LOW'


def aggregate_per_sv(events: list[dict]) -> dict[str, dict]:
    """Per-SV summary across the event stream.

    Returns ``{sv: {n_admit, n_evict, last_consistency, max_int_range,
    durations_s, lambda_ratios, P_IBs}}`` — enough for both the
    text table and the JSON downstream consumers.
    """
    by_sv: dict[str, dict] = {}
    for e in events:
        rec = by_sv.setdefault(e['sv'], {
            'n_admit': 0, 'n_evict': 0,
            'methods': set(),
            'durations_s': [],
            'lambda_ratios': [],
            'P_IBs': [],
            'last_consistency': 'UNKNOWN',
            'max_int_range': 0,
            'last_int_history': [],
        })
        if e['kind'] == 'admit':
            rec['n_admit'] += 1
            rec['methods'].add(e['method'])
            if e['method'] == 'lambda':
                if e['ratio'] is not None:
                    rec['lambda_ratios'].append(e['ratio'])
                if e['P_IB'] is not None:
                    rec['P_IBs'].append(e['P_IB'])
            hist = e['int_history']
            rec['last_int_history'] = hist
            rec['last_consistency'] = classify_history(hist)
            if len(hist) >= 2:
                rng = max(hist) - min(hist)
                if rng > rec['max_int_range']:
                    rec['max_int_range'] = rng
        else:  # evict
            rec['n_evict'] += 1
            if e['duration_s'] is not None:
                rec['durations_s'].append(e['duration_s'])
            hist = e['int_history']
            if hist:
                rec['last_int_history'] = hist
                rec['last_consistency'] = classify_history(hist)
                if len(hist) >= 2:
                    rng = max(hist) - min(hist)
                    if rng > rec['max_int_range']:
                        rec['max_int_range'] = rng
    return by_sv


# ── Reporting ────────────────────────────────────────────────────────────── #

def _safe_mean(xs: list[float]) -> float | None:
    return statistics.mean(xs) if xs else None


def _safe_median(xs: list[float]) -> float | None:
    return statistics.median(xs) if xs else None


def render_text(host: str, by_sv: dict[str, dict]) -> str:
    lines = []
    lines.append(f"## {host} — NL admission per-SV summary")
    lines.append(
        "  Pop classifier on int_history range: HIGH=range 0 (Pop A), "
        "MEDIUM=range 1, LOW=range >= 2 (Pop B), UNKNOWN=< 2 entries."
    )
    lines.append(
        f"  {'sv':>5}  {'admit':>5}  {'evict':>5}  "
        f"{'pop':>7}  {'rng':>3}  "
        f"{'mean_dur':>9}  {'med_ratio':>9}  {'med_P':>6}  "
        f"{'history':>20}"
    )
    rows = sorted(by_sv.items(), key=lambda kv: -kv[1]['n_admit'])
    for sv, rec in rows:
        mean_dur = _safe_mean(rec['durations_s'])
        med_ratio = _safe_median(rec['lambda_ratios'])
        med_P = _safe_median(rec['P_IBs'])
        hist_str = ",".join(str(x) for x in rec['last_int_history'][-6:])
        if len(rec['last_int_history']) > 6:
            hist_str = "…" + hist_str
        mean_dur_str = (
            f"{mean_dur:>7.1f} s" if mean_dur is not None else f"{'n/a':>9}"
        )
        med_ratio_str = (
            f"{med_ratio:>9.2f}" if med_ratio is not None else f"{'n/a':>9}"
        )
        med_P_str = (
            f"{med_P:>6.4f}" if med_P is not None else f"{'n/a':>6}"
        )
        lines.append(
            f"  {sv:>5}  {rec['n_admit']:>5}  {rec['n_evict']:>5}  "
            f"{rec['last_consistency']:>7}  {rec['max_int_range']:>3}  "
            f"{mean_dur_str}  {med_ratio_str}  {med_P_str}  "
            f"{hist_str:>20}"
        )
    return "\n".join(lines) + "\n"


def render_json_events(host: str, events: list[dict]) -> list[dict]:
    """Per-event JSON consumable by joining with BNC validator output."""
    out = []
    for e in events:
        # Add classification at emission time so downstream consumers
        # can filter by pop without re-deriving.
        rec = dict(e)
        rec['host'] = host
        rec['consistency'] = classify_history(e.get('int_history', []))
        out.append(rec)
    return out


# ── Main ─────────────────────────────────────────────────────────────────── #

def main(argv: list[str]) -> int:
    p = argparse.ArgumentParser(description=__doc__.split('\n\n', 1)[0])
    p.add_argument('--engine', nargs='+', required=True,
                   help='One or more engine .log files')
    p.add_argument('--labels',
                   help='Comma-separated host labels (one per --engine, '
                        'in matching order).  Defaults to engine basename.')
    p.add_argument('--tz-offset-hours', type=float, default=0.0,
                   help='Hours to subtract from engine timestamps to align '
                        'with UTC.  Use 5 for CDT-emitted logs.')
    p.add_argument('--format', choices=('text', 'json'), default='text',
                   help='Output format (default text).  json emits per-event '
                        'records with consistency tag for joining with BNC '
                        'validators.')
    args = p.parse_args(argv)

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
        per_host_events[label] = parse_engine_admissions(
            str(path), args.tz_offset_hours)

    if args.format == 'json':
        out = []
        for host, events in per_host_events.items():
            out.extend(render_json_events(host, events))
        print(json.dumps(out, indent=2, sort_keys=True, default=float))
    else:
        print("# NL admission classifier — per-SV admit/evict + Pop A/B")
        print(f"# generated: {datetime.now(timezone.utc).isoformat()}")
        print()
        for host, events in per_host_events.items():
            by_sv = aggregate_per_sv(events)
            n_admits = sum(1 for e in events if e['kind'] == 'admit')
            n_evicts = sum(1 for e in events if e['kind'] == 'evict')
            print(f"## {host}: {n_admits} admits, {n_evicts} evicts, "
                  f"{len(by_sv)} unique SVs")
            print(render_text(host, by_sv))
    return 0


if __name__ == '__main__':
    sys.exit(main(sys.argv[1:]))
