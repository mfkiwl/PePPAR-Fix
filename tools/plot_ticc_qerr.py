#!/usr/bin/env python3
"""Plot TDEV of raw TICC F9T PPS vs TICC + qErr corrected.

Usage:
    python3 plot_ticc_qerr.py data/ticc-qerr-30m.csv -o data/ticc-qerr-tdev.html
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


def adev(phase_s, taus):
    N = len(phase_s)
    results = {}
    for tau in taus:
        n = int(tau)
        if n < 1 or 2 * n >= N:
            continue
        d = phase_s[2*n:] - 2*phase_s[n:-n] + phase_s[:-(2*n)]
        M = len(d)
        if M < 1:
            continue
        results[tau] = np.sqrt(np.sum(d**2) / (2.0 * M * tau**2))
    return results


def log_taus(n_samples, n_points=40):
    max_tau = n_samples / 3
    if max_tau < 2:
        return [1]
    taus = np.logspace(0, np.log10(max_tau), n_points)
    return sorted(set(max(1, int(round(t))) for t in taus))


def detrend(phase):
    x = np.arange(len(phase), dtype=float)
    slope = np.polyfit(x, phase, 1)[0]
    return phase - np.polyval(np.polyfit(x, phase, 1), x), slope


def main():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser()
    ap.add_argument("file", help="TICC+qErr CSV from ticc_qerr_capture.py")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    with open(args.file) as f:
        rows = list(csv.DictReader(f))

    raw_ps = np.array([float(r['ticc_phase_ps']) for r in rows])
    corrected_ps = np.array([float(r['corrected_phase_ps']) for r in rows])

    raw_s = raw_ps * 1e-12
    corrected_s = corrected_ps * 1e-12

    raw_dt, raw_slope = detrend(raw_s)
    corr_dt, corr_slope = detrend(corrected_s)

    taus = log_taus(len(raw_dt))

    raw_td = tdev(raw_dt, taus)
    corr_td = tdev(corr_dt, taus)
    raw_ad = adev(raw_dt, taus)
    corr_ad = adev(corr_dt, taus)

    # Print table
    print(f"TICC + qErr TDEV Comparison ({len(rows)} epochs)")
    print(f"{'tau':>5} {'Raw TICC':>10} {'TICC+qErr':>10} {'Improvement':>12}")
    for tau in sorted(raw_td.keys()):
        r = raw_td[tau] * 1e9
        c = corr_td.get(tau)
        if c is not None:
            c_ns = c * 1e9
            impr = r / c_ns if c_ns > 0 else float('inf')
            print(f"{tau:>5} {r:>10.3f} {c_ns:>10.3f} {impr:>11.2f}x")
        else:
            print(f"{tau:>5} {r:>10.3f} {'n/a':>10}")

    # Plot
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("TDEV — qErr Correction on TICC-Measured F9T PPS",
                        "ADEV — qErr Correction on TICC-Measured F9T PPS"),
        vertical_spacing=0.12,
    )

    # Raw TICC
    t_taus = sorted(raw_td.keys())
    fig.add_trace(go.Scatter(
        x=t_taus, y=[raw_td[t] * 1e9 for t in t_taus],
        mode='lines+markers', name='F9T PPS (raw TICC)',
        line=dict(color='#d62728', width=2.5),
        marker=dict(size=5),
    ), row=1, col=1)

    # TICC + qErr
    c_taus = sorted(corr_td.keys())
    fig.add_trace(go.Scatter(
        x=c_taus, y=[corr_td[t] * 1e9 for t in c_taus],
        mode='lines+markers', name='F9T PPS + qErr (TICC)',
        line=dict(color='#2ca02c', width=2.5),
        marker=dict(size=5),
    ), row=1, col=1)

    # Shaded improvement region
    common = sorted(set(raw_td.keys()) & set(corr_td.keys()))
    if common:
        raw_vals = [raw_td[t] * 1e9 for t in common]
        corr_vals = [corr_td[t] * 1e9 for t in common]
        fig.add_trace(go.Scatter(
            x=list(common) + list(reversed(common)),
            y=raw_vals + list(reversed(corr_vals)),
            fill='toself',
            fillcolor='rgba(44, 160, 44, 0.12)',
            line=dict(width=0),
            name='qErr improvement',
            showlegend=True,
            hoverinfo='skip',
        ), row=1, col=1)

    # ADEV traces
    a_taus = sorted(raw_ad.keys())
    fig.add_trace(go.Scatter(
        x=a_taus, y=[raw_ad[t] for t in a_taus],
        mode='lines+markers', name='F9T PPS (raw TICC)',
        line=dict(color='#d62728', width=2.5),
        marker=dict(size=5), showlegend=False,
    ), row=2, col=1)

    ca_taus = sorted(corr_ad.keys())
    fig.add_trace(go.Scatter(
        x=ca_taus, y=[corr_ad[t] for t in ca_taus],
        mode='lines+markers', name='F9T PPS + qErr (TICC)',
        line=dict(color='#2ca02c', width=2.5),
        marker=dict(size=5), showlegend=False,
    ), row=2, col=1)

    fig.update_xaxes(type="log", title_text="τ (seconds)", row=1, col=1)
    fig.update_xaxes(type="log", title_text="τ (seconds)", row=2, col=1)
    fig.update_yaxes(type="log", title_text="TDEV (ns)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="ADEV (fractional)", row=2, col=1)

    raw_t1 = raw_td.get(1, 0) * 1e9
    corr_t1 = corr_td.get(1, 0) * 1e9
    impr_t1 = raw_t1 / corr_t1 if corr_t1 > 0 else 0

    fig.update_layout(
        title="qErr Correction Improving TICC-Measured F9T PPS<br>"
              f"<sub>TDEV(1s): raw={raw_t1:.2f} ns → corrected={corr_t1:.2f} ns "
              f"({impr_t1:.1f}x improvement). "
              f"{len(rows)} epochs, TICC at 60 ps resolution.</sub>",
        height=900, width=1000,
    )

    output = args.output or os.path.splitext(args.file)[0] + "-tdev.html"
    fig.write_html(output)
    print(f"\nWritten: {output}")


if __name__ == "__main__":
    main()
