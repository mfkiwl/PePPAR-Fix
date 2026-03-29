#!/usr/bin/env python3
"""Compute and plot ADEV/TDEV from peppar-fix servo CSV files.

Overlays multiple error sources (PPS, PPS+qErr, PPS+PPP) or multiple
runs for comparison.  Produces interactive Plotly HTML.

Usage:
    # Single run, overlay error sources:
    python3 tools/plot_deviation.py data/tdev-overnight.csv

    # Compare two runs:
    python3 tools/plot_deviation.py --label "before" run1.csv --label "after" run2.csv

    # Custom output:
    python3 tools/plot_deviation.py data/*.csv -o comparison.html
"""

import argparse
import csv
import math
import os
import sys

import numpy as np


# ── Deviation computation ──────────────────────────────────────────────── #

def tdev(phase_s, rate, taus):
    """Time Deviation from phase data (seconds)."""
    N = len(phase_s)
    results = {}
    for tau in taus:
        n = int(tau * rate)
        if n < 1 or 3 * n >= N:
            continue
        sd = phase_s[2*n:] - 2*phase_s[n:-n] + phase_s[:-(2*n)]
        M = len(sd)
        if M < 1:
            continue
        results[tau] = np.sqrt(np.sum(sd**2) / (6.0 * M)) / tau
    return results


def adev(phase_s, rate, taus):
    """Allan Deviation from phase data (seconds)."""
    N = len(phase_s)
    results = {}
    for tau in taus:
        n = int(tau * rate)
        if n < 1 or 2 * n >= N:
            continue
        d = phase_s[2*n:] - 2*phase_s[n:-n] + phase_s[:-(2*n)]
        M = len(d)
        if M < 1:
            continue
        results[tau] = np.sqrt(np.sum(d**2) / (2.0 * M * tau**2))
    return results


def log_taus(duration_s, rate, n_points=30):
    """Generate logarithmically spaced tau values."""
    min_tau = max(1, 1.0 / rate)
    max_tau = duration_s / 3
    if max_tau <= min_tau:
        return [min_tau]
    taus = np.logspace(np.log10(min_tau), np.log10(max_tau), n_points)
    return sorted(set(int(round(t)) for t in taus if t >= min_tau))


# ── Data extraction ────────────────────────────────────────────────────── #

def load_servo_csv(path):
    """Load error sources from a servo CSV.

    Returns dict of {source_name: (taus_applicable, phase_array_s, rate)}.
    """
    with open(path) as f:
        rows = list(csv.DictReader(f))

    if not rows:
        return {}

    # Determine rate from timestamp spacing
    gps_secs = [int(r['gps_second']) for r in rows]
    if len(gps_secs) > 1:
        deltas = [gps_secs[i] - gps_secs[i-1] for i in range(1, min(20, len(gps_secs)))]
        median_delta = sorted(deltas)[len(deltas)//2]
        rate = 1.0 / max(1, median_delta)
    else:
        rate = 1.0

    duration = gps_secs[-1] - gps_secs[0] if len(gps_secs) > 1 else 0
    taus = log_taus(duration, rate)

    sources = {}

    # PPS raw (pps_error_ns)
    pps_err = []
    for r in rows:
        try:
            pps_err.append(float(r['pps_error_ns']))
        except (ValueError, KeyError):
            pps_err.append(np.nan)
    pps = np.array(pps_err) * 1e-9
    if np.sum(np.isfinite(pps)) > 10:
        sources['PPS'] = (taus, pps[np.isfinite(pps)], rate)

    # PPS + qErr (pps_error_ns + qerr_ns when both valid)
    pps_qerr = []
    for r in rows:
        try:
            p = float(r['pps_error_ns'])
            q = r.get('qerr_ns', '')
            if q and q != '':
                q = float(q)
                pps_qerr.append(p - q)  # qErr corrects PPS
            else:
                pps_qerr.append(np.nan)
        except (ValueError, KeyError):
            pps_qerr.append(np.nan)
    pq = np.array(pps_qerr) * 1e-9
    valid = np.isfinite(pq)
    if np.sum(valid) > 10:
        sources['PPS+qErr'] = (taus, pq[valid], rate)

    # PPS+PPP (source_error_ns from the servo's best source)
    ppp_err = []
    for r in rows:
        try:
            src = r.get('source', '')
            se = r.get('source_error_ns', '')
            if se and se != '' and 'PPP' in src:
                ppp_err.append(float(se))
            else:
                ppp_err.append(np.nan)
        except (ValueError, KeyError):
            ppp_err.append(np.nan)
    pp = np.array(ppp_err) * 1e-9
    valid = np.isfinite(pp)
    if np.sum(valid) > 10:
        sources['PPS+PPP'] = (taus, pp[valid], rate)

    return sources


# ── Plotting ───────────────────────────────────────────────────────────── #

def make_plots(all_sources, output_path, title=None):
    """Generate ADEV and TDEV overlay plots."""
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    colors = {
        'PPS':      '#1f77b4',
        'PPS+qErr': '#ff7f0e',
        'PPS+PPP':  '#2ca02c',
    }
    # For multi-file comparison, cycle through distinct colors
    file_colors = [
        '#1f77b4', '#ff7f0e', '#2ca02c', '#d62728',
        '#9467bd', '#8c564b', '#e377c2', '#7f7f7f',
    ]

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("TDEV (Time Deviation)", "ADEV (Allan Deviation)"),
        vertical_spacing=0.12,
    )

    for file_idx, (file_label, sources) in enumerate(all_sources):
        for src_name, (taus_list, phase, rate) in sources.items():
            # Compute deviations
            td = tdev(phase, rate, taus_list)
            ad = adev(phase, rate, taus_list)

            if not td:
                continue

            # Label: "source" for single file, "file: source" for multi
            if len(all_sources) == 1:
                label = src_name
                color = colors.get(src_name, file_colors[file_idx % len(file_colors)])
            else:
                label = f"{file_label}: {src_name}"
                base = file_colors[file_idx % len(file_colors)]
                color = base

            dash = 'solid'
            if 'qErr' in src_name:
                dash = 'dash'
            elif 'PPP' in src_name:
                dash = 'dot'

            # TDEV trace
            t_taus = sorted(td.keys())
            t_vals = [td[t] for t in t_taus]
            fig.add_trace(go.Scatter(
                x=t_taus, y=t_vals,
                mode='lines+markers',
                name=label,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5),
                legendgroup=label,
            ), row=1, col=1)

            # ADEV trace
            a_taus = sorted(ad.keys())
            a_vals = [ad[t] for t in a_taus]
            fig.add_trace(go.Scatter(
                x=a_taus, y=a_vals,
                mode='lines+markers',
                name=label,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5),
                legendgroup=label,
                showlegend=False,
            ), row=2, col=1)

    fig.update_xaxes(type="log", title_text="τ (seconds)", row=1, col=1)
    fig.update_xaxes(type="log", title_text="τ (seconds)", row=2, col=1)
    fig.update_yaxes(type="log", title_text="TDEV (seconds)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="ADEV (fractional)", row=2, col=1)

    if title is None:
        title = "Stability Analysis"
    fig.update_layout(
        title=title,
        height=900,
        width=1000,
        legend=dict(x=0.01, y=0.49),
    )

    fig.write_html(output_path)
    print(f"Written: {output_path}")


# ── CLI ────────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="ADEV/TDEV analysis from peppar-fix servo CSV",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
Examples:
    # Single run, overlay PPS / PPS+qErr / PPS+PPP:
    python3 tools/plot_deviation.py data/tdev-overnight.csv

    # Compare two runs:
    python3 tools/plot_deviation.py --label before old.csv --label after new.csv

    # Custom title and output:
    python3 tools/plot_deviation.py data/*.csv -o comparison.html -t "Overnight TDEV"
""",
    )
    ap.add_argument("files", nargs="+", help="Servo CSV file(s)")
    ap.add_argument("--label", action="append", default=[],
                    help="Label for the next file (use before each file)")
    ap.add_argument("-o", "--output", default=None,
                    help="Output HTML path (default: <first_file>.html)")
    ap.add_argument("-t", "--title", default=None, help="Plot title")

    args = ap.parse_args()

    # Match labels to files
    labels = list(args.label)
    files = args.files
    # If fewer labels than files, generate from filenames
    while len(labels) < len(files):
        base = os.path.basename(files[len(labels)])
        labels.append(os.path.splitext(base)[0])

    all_sources = []
    for label, path in zip(labels, files):
        print(f"Loading {path} ({label})...")
        sources = load_servo_csv(path)
        if sources:
            for name in sources:
                _, phase, rate = sources[name]
                print(f"  {name}: {len(phase)} samples at {rate} Hz")
            all_sources.append((label, sources))
        else:
            print(f"  WARNING: no usable data in {path}")

    if not all_sources:
        print("No data to plot", file=sys.stderr)
        sys.exit(1)

    output = args.output
    if output is None:
        output = os.path.splitext(files[0])[0] + "-deviation.html"

    title = args.title
    if title is None and len(files) == 1:
        title = f"Stability: {os.path.basename(files[0])}"

    make_plots(all_sources, output, title=title)


if __name__ == "__main__":
    main()
