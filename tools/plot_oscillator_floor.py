#!/usr/bin/env python3
"""Plot oscillator noise floors and EXTTS measurement noise.

Generates two HTML plots from TICC captures and freerun CSVs:

1. PHC PEROUT stability (oscillator floor):
   TICC chA TDEV/ADEV for each host's free-running oscillator.
   Title: "Best-possible disciplined oscillator noise floors"

2. F9T PPS measurement comparison:
   TICC chB vs EXTTS for PPS IN on each host.
   Shows EXTTS measurement noise floor vs TICC ground truth.

Usage:
    python3 tools/plot_oscillator_floor.py \
        --ticc-timehat data/ticc-baseline-2h-1.csv \
        --ticc-ocxo data/ticc-ocxo-2h.csv \
        --freerun-timehat data/freerun-timehat-2h.csv \
        --freerun-ocxo data/freerun-ocxo-2h.csv \
        -o data/oscillator-floors.html
"""

import argparse
import csv
import os
import sys

import numpy as np


# ── Deviation computation ──────────────────────────────────────────────── #

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


# ── TICC data loading ─────────────────────────────────────────────────── #

def load_ticc_channel(path, channel):
    """Load one channel from a TICC capture CSV, return phase array (seconds)."""
    timestamps = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["channel"] == channel:
                ts_ps = int(row["ref_sec"]) * 1_000_000_000_000 + int(row["ref_ps"])
                timestamps.append(ts_ps)

    if len(timestamps) < 10:
        return None

    # Convert to 1-second phase via intervals
    intervals = np.diff(timestamps)
    phase = [0.0]
    for iv in intervals:
        n_sec = round(iv / 1_000_000_000_000)
        if n_sec < 1 or n_sec > 10:
            continue
        residual = iv - n_sec * 1_000_000_000_000
        for _ in range(n_sec):
            phase.append(phase[-1] + residual / n_sec)

    return np.array(phase) * 1e-12  # ps → seconds


def load_freerun_pps(path):
    """Load PPS error from freerun servo CSV, return phase array (seconds)."""
    with open(path) as f:
        rows = list(csv.DictReader(f))
    pps = []
    for r in rows:
        try:
            pps.append(float(r['pps_error_ns']))
        except (ValueError, KeyError):
            continue
    return np.array(pps) * 1e-9  # ns → seconds


# ── Plotting ───────────────────────────────────────────────────────────── #

def make_plots(ticc_timehat, ticc_ocxo, freerun_timehat, freerun_ocxo, output_dir):
    try:
        import plotly.graph_objects as go
        from plotly.subplots import make_subplots
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    # ── Plot 1: Oscillator noise floors (TICC chA = PHC PEROUT) ──────── #

    fig1 = make_subplots(
        rows=2, cols=1,
        subplot_titles=("TDEV — Free-Running Oscillator",
                        "ADEV — Free-Running Oscillator"),
        vertical_spacing=0.12,
    )

    traces_1 = []

    if ticc_timehat is not None:
        chA = load_ticc_channel(ticc_timehat, "chA")
        if chA is not None:
            chA_dt = detrend(chA)
            taus = log_taus(len(chA_dt))
            traces_1.append(("TimeHat i226 TCXO (TICC)", chA_dt, taus,
                             "#1f77b4", "solid"))

    if ticc_ocxo is not None:
        chA = load_ticc_channel(ticc_ocxo, "chA")
        if chA is not None:
            chA_dt = detrend(chA)
            taus = log_taus(len(chA_dt))
            traces_1.append(("ocxo E810 OCXO (TICC)", chA_dt, taus,
                             "#d62728", "solid"))

    # Also show EXTTS-based PHC stability from freerun data
    if freerun_timehat is not None:
        pps = load_freerun_pps(freerun_timehat)
        if len(pps) > 10:
            pps_dt = detrend(pps)
            taus = log_taus(len(pps_dt))
            traces_1.append(("TimeHat i226 TCXO (EXTTS)", pps_dt, taus,
                             "#1f77b4", "dot"))

    if freerun_ocxo is not None:
        pps = load_freerun_pps(freerun_ocxo)
        if len(pps) > 10:
            pps_dt = detrend(pps)
            taus = log_taus(len(pps_dt))
            traces_1.append(("ocxo E810 OCXO (EXTTS, quantization-limited*)",
                             pps_dt, taus, "#d62728", "dot"))

    for name, phase, taus, color, dash in traces_1:
        td = tdev(phase, taus)
        ad = adev(phase, taus)
        if td:
            t_taus = sorted(td.keys())
            fig1.add_trace(go.Scatter(
                x=t_taus, y=[td[t] * 1e9 for t in t_taus],
                mode='lines+markers', name=name,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5), legendgroup=name,
            ), row=1, col=1)
        if ad:
            a_taus = sorted(ad.keys())
            fig1.add_trace(go.Scatter(
                x=a_taus, y=[ad[t] for t in a_taus],
                mode='lines+markers', name=name,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5), legendgroup=name, showlegend=False,
            ), row=2, col=1)

    fig1.update_xaxes(type="log", title_text="τ (seconds)", row=1, col=1)
    fig1.update_xaxes(type="log", title_text="τ (seconds)", row=2, col=1)
    fig1.update_yaxes(type="log", title_text="TDEV (ns)", row=1, col=1)
    fig1.update_yaxes(type="log", title_text="ADEV (fractional)", row=2, col=1)
    fig1.update_layout(
        title="Best-Possible Disciplined Oscillator Noise Floors<br>"
              "<sub>Solid = TICC (60 ps, ground truth), Dotted = EXTTS (8 ns bins). "
              "*E810 EXTTS is quantization-limited: ~8 ns bins match F9T's "
              "125 MHz clock, producing falsely low TDEV.</sub>",
        height=900, width=1000,
    )

    path1 = os.path.join(output_dir, "oscillator-noise-floors.html")
    fig1.write_html(path1)
    print(f"Written: {path1}")

    # ── Plot 2: PPS measurement noise (TICC chB vs EXTTS) ───────────── #

    fig2 = make_subplots(
        rows=2, cols=1,
        subplot_titles=("TDEV — F9T PPS: TICC vs EXTTS",
                        "ADEV — F9T PPS: TICC vs EXTTS"),
        vertical_spacing=0.12,
    )

    traces_2 = []

    # TICC ground truth — TimeHat chB
    if ticc_timehat is not None:
        chB = load_ticc_channel(ticc_timehat, "chB")
        if chB is not None:
            chB_dt = detrend(chB)
            taus = log_taus(len(chB_dt))
            traces_2.append(("TimeHat TICC (ground truth)", chB_dt, taus,
                             "#1f77b4", "solid"))

    # EXTTS — TimeHat (from freerun CSV)
    if freerun_timehat is not None:
        pps = load_freerun_pps(freerun_timehat)
        if len(pps) > 10:
            pps_dt = detrend(pps)
            taus = log_taus(len(pps_dt))
            traces_2.append(("TimeHat i226 EXTTS", pps_dt, taus,
                             "#ff7f0e", "dash"))

    # EXTTS — ocxo (from freerun CSV, with caveat)
    if freerun_ocxo is not None:
        pps = load_freerun_pps(freerun_ocxo)
        if len(pps) > 10:
            pps_dt = detrend(pps)
            taus = log_taus(len(pps_dt))
            traces_2.append((
                "ocxo E810 EXTTS (quantization-limited*)",
                pps_dt, taus, "#d62728", "dot"))

    for name, phase, taus, color, dash in traces_2:
        td = tdev(phase, taus)
        ad = adev(phase, taus)
        if td:
            t_taus = sorted(td.keys())
            fig2.add_trace(go.Scatter(
                x=t_taus, y=[td[t] * 1e9 for t in t_taus],
                mode='lines+markers', name=name,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5), legendgroup=name,
            ), row=1, col=1)
        if ad:
            a_taus = sorted(ad.keys())
            fig2.add_trace(go.Scatter(
                x=a_taus, y=[ad[t] for t in a_taus],
                mode='lines+markers', name=name,
                line=dict(color=color, dash=dash, width=2),
                marker=dict(size=5), legendgroup=name, showlegend=False,
            ), row=2, col=1)

    # Add shaded region and DO noise floor where we have both TICC and EXTTS
    ticc_tdev_data = {}
    extts_tdev_data = {}
    for name, phase, taus, color, dash in traces_2:
        td = tdev(phase, taus)
        if "TICC" in name and "TimeHat" in name:
            ticc_tdev_data = td
        elif "EXTTS" in name and "TimeHat" in name:
            extts_tdev_data = td

    if ticc_tdev_data and extts_tdev_data:
        # Find common taus
        common_taus = sorted(set(ticc_tdev_data.keys()) & set(extts_tdev_data.keys()))
        if common_taus:
            # Shaded region: TICC floor to EXTTS measurement
            ticc_vals = [ticc_tdev_data[t] * 1e9 for t in common_taus]
            extts_vals = [extts_tdev_data[t] * 1e9 for t in common_taus]

            # Fill between TICC and EXTTS (DO noise region)
            fig2.add_trace(go.Scatter(
                x=list(common_taus) + list(reversed(common_taus)),
                y=extts_vals + list(reversed(ticc_vals)),
                fill='toself',
                fillcolor='rgba(255, 127, 14, 0.15)',
                line=dict(width=0),
                name='PHC measurement noise (shaded)',
                showlegend=True,
                hoverinfo='skip',
            ), row=1, col=1)

            # Computed DO noise floor: sqrt(EXTTS² - TICC²)
            phc_noise = []
            phc_taus = []
            for t in common_taus:
                e2 = extts_tdev_data[t] ** 2
                t2 = ticc_tdev_data[t] ** 2
                if e2 > t2:
                    phc_noise.append(np.sqrt(e2 - t2) * 1e9)
                    phc_taus.append(t)

            if phc_taus:
                fig2.add_trace(go.Scatter(
                    x=phc_taus, y=phc_noise,
                    mode='lines+markers',
                    name='i226 DO noise (RSS extraction)',
                    line=dict(color='#2ca02c', dash='dashdot', width=2),
                    marker=dict(size=4),
                ), row=1, col=1)

    fig2.update_xaxes(type="log", title_text="τ (seconds)", row=1, col=1)
    fig2.update_xaxes(type="log", title_text="τ (seconds)", row=2, col=1)
    fig2.update_yaxes(type="log", title_text="TDEV (ns)", row=1, col=1)
    fig2.update_yaxes(type="log", title_text="ADEV (fractional)", row=2, col=1)
    fig2.update_layout(
        title="F9T PPS Measurement Noise: TICC vs EXTTS<br>"
              "<sub>Shaded region = PHC measurement noise (gap between TICC and EXTTS). "
              "Green = extracted DO noise floor (RSS). "
              "*E810 EXTTS is quantization-limited: ~8 ns bins match F9T's "
              "125 MHz clock, producing falsely low TDEV. "
              "No PPS IN TICC on ocxo; F9T PPS jitter expected same as TimeHat.</sub>",
        height=900, width=1000,
    )

    path2 = os.path.join(output_dir, "pps-measurement-noise.html")
    fig2.write_html(path2)
    print(f"Written: {path2}")


# ── CLI ────────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser(
        description="Plot oscillator noise floors and EXTTS measurement noise")
    ap.add_argument("--ticc-timehat", help="TICC capture CSV from TimeHat")
    ap.add_argument("--ticc-ocxo", help="TICC capture CSV from ocxo")
    ap.add_argument("--freerun-timehat", help="Freerun servo CSV from TimeHat")
    ap.add_argument("--freerun-ocxo", help="Freerun servo CSV from ocxo")
    ap.add_argument("-o", "--output-dir", default="data",
                    help="Output directory for HTML plots")
    args = ap.parse_args()

    make_plots(
        args.ticc_timehat, args.ticc_ocxo,
        args.freerun_timehat, args.freerun_ocxo,
        args.output_dir,
    )


if __name__ == "__main__":
    main()
