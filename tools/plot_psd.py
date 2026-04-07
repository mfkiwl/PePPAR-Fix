#!/usr/bin/env python3
"""Compute and plot Welch PSDs of servo error signals.

Reads a peppar-fix servo CSV and computes the power spectral density
of each candidate error source (PPS, PPS+qErr, dt_rx, Carrier, TICC).
Reveals the noise structure as actually observed — useful for picking
loop bandwidth and per-source servo gains.

The PSD shape tells you the noise type:
  slope  0  →  white phase noise (pure measurement noise)
  slope -1  →  flicker phase noise
  slope -2  →  white FM (random walk in phase, oscillator drift)
  slope -3  →  flicker FM
  slope -4  →  random walk FM

Usage:
    python3 tools/plot_psd.py data/servo_log.csv
    python3 tools/plot_psd.py --label run1 r1.csv --label run2 r2.csv
    python3 tools/plot_psd.py --output psd.html servo.csv
"""

import argparse
import csv
import os
import sys

import numpy as np

try:
    from scipy import signal
except ImportError:
    print("pip install scipy", file=sys.stderr)
    sys.exit(1)

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    print("pip install plotly", file=sys.stderr)
    sys.exit(1)


# ── Loading ────────────────────────────────────────────────────────────── #

def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return np.nan


def load_error_sources(path):
    """Load candidate error signals from a servo CSV.

    Returns dict of {name: (array_of_values, units)} where units is
    'ns' or 'ppb'.  All-zero or empty columns are dropped (e.g., PPS
    is all-zero in TICC-drive mode).
    """
    rows = []
    with open(path) as f:
        for r in csv.DictReader(f):
            rows.append(r)
    if not rows:
        return {}

    def col(name):
        return np.array([_to_float(r.get(name, '')) for r in rows])

    def has_signal(arr, threshold=100):
        finite = np.isfinite(arr)
        if finite.sum() < threshold:
            return False
        # Reject all-zero columns (e.g., PPS in TICC-drive mode)
        nonzero = (arr != 0) & finite
        if nonzero.sum() < threshold // 10:
            return False
        return True

    pps = col('pps_error_ns')
    qerr = col('qerr_ns')
    dt_rx = col('dt_rx_ns')
    carrier = col('carrier_error_ns')
    ticc = col('ticc_diff_ns')
    adjfine = col('adjfine_ppb')

    sources = {}
    if has_signal(pps):
        sources['PPS'] = (pps, 'ns')
    if has_signal(pps) and has_signal(qerr):
        sources['PPS+qErr'] = (pps + qerr, 'ns')
    if has_signal(dt_rx):
        sources['dt_rx (PPP)'] = (dt_rx, 'ns')
    if has_signal(carrier):
        sources['Carrier'] = (carrier, 'ns')
    if has_signal(ticc):
        sources['TICC diff'] = (ticc, 'ns')
    if has_signal(adjfine):
        sources['adjfine'] = (adjfine, 'ppb')
    if has_signal(qerr):
        sources['qerr (TIM-TP)'] = (qerr, 'ns')
    return sources


# ── PSD computation ────────────────────────────────────────────────────── #

def welch_psd(signal_ns, fs=1.0, nperseg=None):
    """Compute Welch PSD of a signal in ns.

    Returns (freqs_hz, psd_ns2_per_hz).  NaNs are dropped (longest
    contiguous segment).  Linear trend is removed (detrend='linear').
    """
    # Find longest contiguous valid segment
    valid = np.isfinite(signal_ns)
    if not valid.any():
        return None, None
    # Identify runs of valid samples
    starts = []
    ends = []
    i = 0
    n = len(valid)
    while i < n:
        if valid[i]:
            j = i
            while j < n and valid[j]:
                j += 1
            starts.append(i)
            ends.append(j)
            i = j
        else:
            i += 1
    if not starts:
        return None, None
    # Pick the longest segment
    longest = max(range(len(starts)), key=lambda k: ends[k] - starts[k])
    seg = signal_ns[starts[longest]:ends[longest]].astype(np.float64)
    if len(seg) < 64:
        return None, None
    if nperseg is None:
        # Default: 256 samples per segment for good frequency resolution,
        # halved if signal is short
        nperseg = min(256, len(seg) // 4)
        nperseg = max(64, nperseg)
    f, p = signal.welch(seg, fs=fs, nperseg=nperseg, detrend='linear',
                         scaling='density', window='hann')
    return f, p


def fit_slope(freqs, psd, fmin=None, fmax=None):
    """Fit log-log slope of a PSD over a frequency range."""
    mask = np.isfinite(psd) & (psd > 0) & (freqs > 0)
    if fmin is not None:
        mask &= freqs >= fmin
    if fmax is not None:
        mask &= freqs <= fmax
    if mask.sum() < 5:
        return None
    lf = np.log10(freqs[mask])
    lp = np.log10(psd[mask])
    slope, intercept = np.polyfit(lf, lp, 1)
    return slope


# ── Plotting ───────────────────────────────────────────────────────────── #

COLORS = {
    'PPS': '#d62728',
    'PPS+qErr': '#ff7f0e',
    'dt_rx (PPP)': '#1f77b4',
    'Carrier': '#2ca02c',
    'TICC': '#9467bd',
    'adjfine (servo output)': '#7f7f7f',
}


def make_plot(all_psds, output_path, title=None):
    """Plot PSDs from multiple runs and sources.

    Two stacked panels: ns sources (top) and ppb sources (bottom).
    """
    fig = make_subplots(
        rows=2, cols=1,
        subplot_titles=('Phase error sources (ns)', 'Frequency control (ppb)'),
        vertical_spacing=0.12,
        shared_xaxes=True,
    )

    dashes = ['solid', 'dot', 'dash', 'longdash']
    run_labels = list(all_psds.keys())

    for run_idx, (run_label, sources) in enumerate(all_psds.items()):
        dash = dashes[run_idx % len(dashes)]
        for src_name, info in sources.items():
            if info is None:
                continue
            f, p, slope, units, _ = info
            if f is None:
                continue
            label = src_name if len(run_labels) == 1 else f"{src_name} [{run_label}]"
            if slope is not None:
                label += f" (slope≈{slope:+.1f})"
            row = 1 if units == 'ns' else 2
            fig.add_trace(go.Scatter(
                x=f, y=np.sqrt(p),
                mode='lines',
                name=label,
                line=dict(color=COLORS.get(src_name, None), width=2, dash=dash),
                legendgroup=src_name,
            ), row=row, col=1)

    fig.update_xaxes(type='log', title_text='frequency (Hz)',
                     showgrid=True, gridcolor='lightgray', row=2, col=1)
    fig.update_xaxes(type='log', showgrid=True, gridcolor='lightgray', row=1, col=1)
    fig.update_yaxes(type='log', title_text='ASD (ns/√Hz)',
                     showgrid=True, gridcolor='lightgray', row=1, col=1)
    fig.update_yaxes(type='log', title_text='ASD (ppb/√Hz)',
                     showgrid=True, gridcolor='lightgray', row=2, col=1)
    fig.update_layout(
        title=title or 'Error signal noise spectra (Welch ASD)',
        hovermode='x unified',
        height=900,
    )
    fig.write_html(output_path)
    print(f"Wrote {output_path}")


# ── Reporting ──────────────────────────────────────────────────────────── #

def print_report(label, sources_psd):
    print(f"\n=== {label} ===")
    print(f"{'source':<24} {'units':>5} {'σ':>10} "
          f"{'ASD@0.1Hz':>14} {'ASD@0.01Hz':>14} {'slope':>8}")
    print("-" * 80)
    for src_name, info in sources_psd.items():
        if info is None:
            print(f"{src_name:<24} {'-':>5} {'-':>10} {'-':>14} {'-':>14} {'-':>8}")
            continue
        f, p, slope, units, sample_count = info
        if f is None:
            print(f"{src_name:<24} {units:>5} {'-':>10} {'-':>14} {'-':>14} {'-':>8}")
            continue
        # ASD at 0.1 Hz and 0.01 Hz (closest bins)
        i_01 = np.argmin(np.abs(f - 0.1))
        i_001 = np.argmin(np.abs(f - 0.01))
        asd_01 = np.sqrt(p[i_01])
        asd_001 = np.sqrt(p[i_001])
        # Total RMS (integral of PSD over the band)
        rms = np.sqrt(np.trapezoid(p, f))
        slope_str = f"{slope:+.2f}" if slope is not None else "n/a"
        print(f"{src_name:<24} {units:>5} {rms:>10.3f} "
              f"{asd_01:>11.3f} /√Hz {asd_001:>11.3f} /√Hz {slope_str:>8}")


# ── Main ───────────────────────────────────────────────────────────────── #

def main():
    ap = argparse.ArgumentParser()
    ap.add_argument('files', nargs='+', help='servo CSV files')
    ap.add_argument('--label', action='append', default=[],
                    help='label for each file (in order)')
    ap.add_argument('-o', '--output', default='psd.html',
                    help='output HTML path')
    ap.add_argument('-t', '--title', default=None)
    ap.add_argument('--nperseg', type=int, default=None,
                    help='Welch segment length (default: auto)')
    args = ap.parse_args()

    # Pad labels with file basenames if not enough provided
    labels = list(args.label)
    while len(labels) < len(args.files):
        labels.append(os.path.basename(args.files[len(labels)]))

    all_psds = {}
    for label, path in zip(labels, args.files):
        sources = load_error_sources(path)
        psds = {}
        for src_name, (sig, units) in sources.items():
            n_valid = int(np.sum(np.isfinite(sig)))
            f, p = welch_psd(sig, nperseg=args.nperseg)
            if f is None:
                psds[src_name] = (None, None, None, units, n_valid)
                continue
            slope = fit_slope(f, p, fmin=0.01, fmax=0.3)
            psds[src_name] = (f, p, slope, units, n_valid)
        all_psds[label] = psds
        print_report(label, psds)

    make_plot(all_psds, args.output, args.title)


if __name__ == '__main__':
    main()
