#!/usr/bin/env python3
"""Plot 2 from visual-stories.md: PHC PPS IN Time Error and TDEV Measurement.

Shows that neither i226 nor E810 EXTTS accurately measures the true
F9T PPS TDEV.  TICC is the ground truth.  Shaded regions reveal
the measurement error from each EXTTS path.

Usage:
    python3 tools/phc_pps_time_error_tdev.py \
        --ticc data/ticc-baseline-2h-1.csv \
        --extts-i226 data/freerun-timehat-2h.csv \
        --extts-e810 data/freerun-ocxo-2h.csv \
        -o plots/phc-pps-in-time-error-tdev.html
"""

import argparse
import csv
import os
import sys

import numpy as np


def tdev(phase_s, taus):
    N = len(phase_s)
    results = {}
    for tau in taus:
        n = int(tau)
        if n < 1 or 3 * n >= N:
            continue
        sd = phase_s[2*n:] - 2*phase_s[n:-n] + phase_s[:-(2*n)]
        M = len(sd)
        if M < 1:
            continue
        results[tau] = np.sqrt(np.sum(sd**2) / (6.0 * M)) / tau
    return results


def log_taus(n, pts=35):
    mx = n / 3
    if mx < 2:
        return [1]
    t = np.logspace(0, np.log10(mx), pts)
    return sorted(set(max(1, int(round(x))) for x in t))


def detrend(p):
    x = np.arange(len(p), dtype=float)
    return p - np.polyval(np.polyfit(x, p, 1), x)


def load_ticc_chB(path):
    ts = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row['channel'] == 'chB':
                ts.append(int(row['ref_sec']) * 1_000_000_000_000
                          + int(row['ref_ps']))
    phase = [0.0]
    for i in range(1, len(ts)):
        iv = ts[i] - ts[i-1]
        n = round(iv / 1_000_000_000_000)
        if 1 <= n <= 10:
            for _ in range(n):
                phase.append(phase[-1] + (iv - n * 1_000_000_000_000) / n)
    return np.array(phase) * 1e-12  # seconds


def load_extts(path):
    with open(path) as f:
        rows = list(csv.DictReader(f))
    return np.array([float(r['pps_error_ns']) for r in rows]) * 1e-9


def ns_tick_format(val):
    """Format a tick value in ns with units."""
    if val >= 1.0:
        return f"{val:.0f} ns"
    elif val >= 0.1:
        return f"{val:.1f} ns"
    elif val >= 0.01:
        return f"{val:.2f} ns"
    else:
        return f"{val*1000:.0f} ps"


def main():
    try:
        import plotly.graph_objects as go
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="Plot 2: PHC PPS IN Time Error and TDEV Measurement")
    ap.add_argument("--ticc", required=True,
                    help="TICC baseline CSV (chB = F9T PPS)")
    ap.add_argument("--extts-i226", required=True,
                    help="Freerun servo CSV from TimeHat (i226 EXTTS)")
    ap.add_argument("--extts-e810", required=True,
                    help="Freerun servo CSV from ocxo (E810 EXTTS)")
    ap.add_argument("-o", "--output",
                    default="plots/phc-pps-in-time-error-tdev.html")
    args = ap.parse_args()

    # Load data
    ticc = detrend(load_ticc_chB(args.ticc))
    extts_i226 = detrend(load_extts(args.extts_i226))
    extts_e810 = detrend(load_extts(args.extts_e810))

    # Limit tau to 200s for clarity
    max_tau = 200
    taus_ticc = [t for t in log_taus(len(ticc)) if t <= max_tau]
    taus_i226 = [t for t in log_taus(len(extts_i226)) if t <= max_tau]
    taus_e810 = [t for t in log_taus(len(extts_e810)) if t <= max_tau]

    td_ticc = tdev(ticc, taus_ticc)
    td_i226 = tdev(extts_i226, taus_i226)
    td_e810 = tdev(extts_e810, taus_e810)

    fig = go.Figure()

    # ── Shaded regions ──────────────────────────────────────────────── #

    # Purple shading: TICC → E810 (E810 is below TICC = masked TDEV)
    common_e810 = sorted(set(td_ticc.keys()) & set(td_e810.keys()))
    if common_e810:
        ticc_ns = [td_ticc[t] * 1e9 for t in common_e810]
        e810_ns = [td_e810[t] * 1e9 for t in common_e810]
        fig.add_trace(go.Scatter(
            x=list(common_e810) + list(reversed(common_e810)),
            y=ticc_ns + list(reversed(e810_ns)),
            fill='toself',
            fillcolor='rgba(148, 103, 189, 0.20)',
            line=dict(width=0),
            name='Actual TDEV masked by E810 EXTTS time error',
        ))

    # Orange shading: TICC → i226 (i226 is above TICC = added noise)
    common_i226 = sorted(set(td_ticc.keys()) & set(td_i226.keys()))
    if common_i226:
        ticc_ns = [td_ticc[t] * 1e9 for t in common_i226]
        i226_ns = [td_i226[t] * 1e9 for t in common_i226]
        fig.add_trace(go.Scatter(
            x=list(common_i226) + list(reversed(common_i226)),
            y=i226_ns + list(reversed(ticc_ns)),
            fill='toself',
            fillcolor='rgba(255, 127, 14, 0.20)',
            line=dict(width=0),
            name='TDEV added by i226 EXTTS time error',
        ))

    # ── Data traces ─────────────────────────────────────────────────── #

    t = sorted(td_ticc.keys())
    fig.add_trace(go.Scatter(
        x=t, y=[td_ticc[k] * 1e9 for k in t],
        mode='lines+markers',
        name='Actual F9T TDEV measured on TICC — ground truth',
        line=dict(color='black', width=3),
        marker=dict(size=6),
    ))

    t = sorted(td_e810.keys())
    fig.add_trace(go.Scatter(
        x=t, y=[td_e810[k] * 1e9 for k in t],
        mode='lines+markers',
        name='F9T PPS TDEV measured by E810 EXTTS',
        line=dict(color='#9467bd', width=2.5),
        marker=dict(size=5),
    ))

    t = sorted(td_i226.keys())
    fig.add_trace(go.Scatter(
        x=t, y=[td_i226[k] * 1e9 for k in t],
        mode='lines+markers',
        name='F9T PPS TDEV measured by i226 EXTTS',
        line=dict(color='#ff7f0e', width=2.5),
        marker=dict(size=5),
    ))

    # ── Axes ────────────────────────────────────────────────────────── #

    # Y-axis: TDEV in ns with proper ns tick labels
    y_tickvals = [0.01, 0.02, 0.05, 0.1, 0.2, 0.5, 1, 2, 5, 10]
    y_ticktext = [ns_tick_format(v) for v in y_tickvals]

    fig.update_xaxes(
        type='log',
        title_text='τ (seconds)',
        tickvals=[1, 2, 5, 10, 20, 50, 100, 200],
        ticktext=['1 s', '2 s', '5 s', '10 s', '20 s', '50 s', '100 s', '200 s'],
        minor=dict(tickvals=[3, 4, 6, 7, 8, 9, 15, 30, 40, 60, 70, 80, 90, 150]),
    )

    fig.update_yaxes(
        type='log',
        title_text='TDEV (ns)',
        tickvals=y_tickvals,
        ticktext=y_ticktext,
        minor=dict(tickvals=[
            0.03, 0.04, 0.06, 0.07, 0.08, 0.09,
            0.15, 0.3, 0.4, 0.6, 0.7, 0.8, 0.9,
            1.5, 3, 4, 6, 7, 8, 9,
        ]),
    )

    # ── Layout ──────────────────────────────────────────────────────── #

    fig.update_layout(
        title=dict(
            text=(
                'PHC PPS IN Time Error and TDEV Measurement<br>'
                '<sub>Both i226 and E810 EXTTS have ~8 ns resolution, but E810 has much less noise '
                '(125 MHz F9T clock period).<br>'
                'Purple shading: actual F9T TDEV hidden by E810 quantization '
                'flatness (77% identical adjacent timestamps).<br>'
                'Orange shading: measurement noise added by i226 '
                '8 ns tick quantization.</sub>'
            ),
        ),
        height=700,
        width=1050,
        legend=dict(
            x=0.30, y=0.99,
            bgcolor='rgba(255,255,255,0.8)',
            bordercolor='#ccc',
            borderwidth=1,
        ),
    )

    os.makedirs(os.path.dirname(args.output), exist_ok=True)
    fig.write_html(args.output)
    print(f"Written: {args.output}")


if __name__ == "__main__":
    main()
