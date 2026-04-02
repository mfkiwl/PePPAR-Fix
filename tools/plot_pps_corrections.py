#!/usr/bin/env python3
"""Plot TDEV improvement from PPS corrections: raw → +qErr → +PPP.

Shows how each correction layer reduces the PPS timing noise,
establishing the achievable discipline floor at each tau.

Usage:
    python3 tools/plot_pps_corrections.py data/freerun-timehat-5m-v2.csv
    python3 tools/plot_pps_corrections.py data/freerun-timehat-2h.csv -o data/corrections.html
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
    return phase - np.polyval(np.polyfit(x, phase, 1), x)


def load_three_sources(path):
    """Extract raw PPS, PPS+qErr, PPS+PPP phase arrays from freerun CSV."""
    with open(path) as f:
        rows = list(csv.DictReader(f))

    pps_raw = []
    pps_qerr = []
    pps_ppp = []

    for r in rows:
        try:
            p = float(r['pps_error_ns'])
        except (ValueError, KeyError):
            continue

        pps_raw.append(p)

        q = r.get('qerr_ns', '')
        if q:
            pps_qerr.append(p + float(q))
        else:
            pps_qerr.append(float('nan'))

        src = r.get('source', '')
        se = r.get('source_error_ns', '')
        if se and 'PPP' in src:
            pps_ppp.append(float(se))
        else:
            pps_ppp.append(float('nan'))

    sources = {}

    raw = np.array(pps_raw) * 1e-9
    if len(raw) > 10:
        sources['F9T PPS (raw)'] = detrend(raw)

    qerr = np.array(pps_qerr) * 1e-9
    valid = np.isfinite(qerr)
    if np.sum(valid) > 10:
        sources['F9T PPS + qErr'] = detrend(qerr[valid])

    ppp = np.array(pps_ppp) * 1e-9
    valid = np.isfinite(ppp)
    if np.sum(valid) > 10:
        sources['F9T PPS + PPP'] = detrend(ppp[valid])

    return sources


def main():
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    ap = argparse.ArgumentParser(
        description="Plot PPS correction TDEV improvement")
    ap.add_argument("files", nargs="+", help="Freerun servo CSV(s)")
    ap.add_argument("-o", "--output", default=None)
    args = ap.parse_args()

    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=("TDEV — PPS Corrections Improving Stability",
                        "ADEV — PPS Corrections Improving Stability"),
        vertical_spacing=0.12,
    )

    styles = {
        'F9T PPS (raw)':   dict(color='#d62728', dash='solid',  width=2.5),
        'F9T PPS + qErr':  dict(color='#ff7f0e', dash='dash',   width=2.5),
        'F9T PPS + PPP':   dict(color='#2ca02c', dash='solid',  width=2.5),
    }

    all_tdev1 = {}

    for path in args.files:
        label = os.path.splitext(os.path.basename(path))[0]
        sources = load_three_sources(path)

        prefix = f"{label}: " if len(args.files) > 1 else ""

        for name, phase in sources.items():
            taus = log_taus(len(phase))
            td = tdev(phase, taus)
            ad = adev(phase, taus)
            style = styles.get(name, dict(color='gray', dash='solid', width=2))
            display_name = f"{prefix}{name}"

            if 1 in td:
                all_tdev1[display_name] = td[1] * 1e9

            if td:
                t_taus = sorted(td.keys())
                fig.add_trace(go.Scatter(
                    x=t_taus, y=[td[t] * 1e9 for t in t_taus],
                    mode='lines+markers', name=display_name,
                    line=style, marker=dict(size=5),
                    legendgroup=display_name,
                ), row=1, col=1)

            if ad:
                a_taus = sorted(ad.keys())
                fig.add_trace(go.Scatter(
                    x=a_taus, y=[ad[t] for t in a_taus],
                    mode='lines+markers', name=display_name,
                    line=style, marker=dict(size=5),
                    legendgroup=display_name, showlegend=False,
                ), row=2, col=1)

        # Add shaded region between raw and best correction (PPP if available)
        raw_name = f"{prefix}F9T PPS (raw)"
        best_name = f"{prefix}F9T PPS + PPP" if f"{prefix}F9T PPS + PPP" in [
            f"{prefix}{n}" for n in sources] else f"{prefix}F9T PPS + qErr"

        raw_phase = sources.get('F9T PPS (raw)')
        best_phase = sources.get('F9T PPS + PPP', sources.get('F9T PPS + qErr'))
        if raw_phase is not None and best_phase is not None:
            raw_taus = log_taus(len(raw_phase))
            best_taus = log_taus(len(best_phase))
            raw_td = tdev(raw_phase, raw_taus)
            best_td = tdev(best_phase, best_taus)
            common = sorted(set(raw_td.keys()) & set(best_td.keys()))
            if common:
                raw_vals = [raw_td[t] * 1e9 for t in common]
                best_vals = [best_td[t] * 1e9 for t in common]
                fig.add_trace(go.Scatter(
                    x=list(common) + list(reversed(common)),
                    y=raw_vals + list(reversed(best_vals)),
                    fill='toself',
                    fillcolor='rgba(44, 160, 44, 0.12)',
                    line=dict(width=0),
                    name='Correction improvement (shaded)',
                    showlegend=True,
                    hoverinfo='skip',
                ), row=1, col=1)

    fig.update_xaxes(type="log", title_text="τ (seconds)", row=1, col=1)
    fig.update_xaxes(type="log", title_text="τ (seconds)", row=2, col=1)
    fig.update_yaxes(type="log", title_text="TDEV (ns)", row=1, col=1)
    fig.update_yaxes(type="log", title_text="ADEV (fractional)", row=2, col=1)

    # Build subtitle with TDEV(1s) values
    tdev_parts = [f"{n}: {v:.2f} ns" for n, v in all_tdev1.items()]
    subtitle = "TDEV(1s): " + " | ".join(tdev_parts) if tdev_parts else ""

    fig.update_layout(
        title="PPS Corrections Improving TDEV<br>"
              f"<sub>{subtitle}</sub>",
        height=900, width=1000,
    )

    output = args.output
    if output is None:
        output = os.path.splitext(args.files[0])[0] + "-corrections.html"
    fig.write_html(output)
    print(f"Written: {output}")


if __name__ == "__main__":
    main()
