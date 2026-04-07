#!/usr/bin/env python3
"""Diagnose Carrier vs PPS+qErr divergence in a peppar-fix servo log.

Both ``carrier_error_ns`` and ``pps_error_ns`` are logged every epoch,
even when only one is the active servo source.  Their *difference*
isolates the systematic error sources unique to the Carrier path,
because the underlying physical quantity they're estimating (PHC vs
GPS) is the same.

Decompose ``carrier_error_ns - pps_error_ns`` into:

* **constant**           — anchor capture noise (one-time bias from the
                           noisy PPS reading used to set
                           ``phase_anchor_ns`` at init)
* **linear ramp**        — adjfine calibration error (1 ppb of adjfine
                           does not produce exactly 1 ns/s of phase
                           change; the slope is the per-ppb error)
* **random walk residual** — accumulator nonlinearity (DAC / PLL
                             nonlinearity in the DO's frequency
                             response)
* **periodic at f_loop**   — closed-loop coupling (Carrier feeds back
                             through adjfine, which updates the
                             accumulator, which feeds back into Carrier)

Usage::

    python3 tools/plot_carrier_vs_pps.py data/run_servo.csv
    python3 tools/plot_carrier_vs_pps.py --label run1 r1.csv \\
                                         --label run2 r2.csv \\
                                         -o carrier_diff.html

The output HTML has three stacked panels:

1. ``carrier_error_ns`` and ``pps_error_ns`` overlaid (raw)
2. ``diff = carrier - pps`` with a least-squares linear fit overlaid
3. ``diff - linear_fit``, the random-walk + periodic residual, and its
   Welch PSD on a log-log inset
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


# Reuse plot_psd's lazy plotly import.
go = None
make_subplots = None


def _ensure_plotly():
    global go, make_subplots
    if go is not None:
        return
    try:
        import plotly.graph_objects as _go
        from plotly.subplots import make_subplots as _ms
        go = _go
        make_subplots = _ms
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)


def _to_float(s):
    try:
        return float(s)
    except (TypeError, ValueError):
        return None


def load_run(path):
    """Return (t_s, carrier_ns, pps_ns) arrays of paired epochs only.

    Epochs missing either column are dropped — the diagnostic only
    makes sense where both quantities exist.
    """
    t, c, p = [], [], []
    with open(path) as f:
        rdr = csv.DictReader(f)
        if 'carrier_error_ns' not in rdr.fieldnames:
            print(f"{path}: no carrier_error_ns column "
                  "(servo log predates the Carrier source)", file=sys.stderr)
            sys.exit(2)
        if 'pps_error_ns' not in rdr.fieldnames:
            print(f"{path}: no pps_error_ns column", file=sys.stderr)
            sys.exit(2)
        for row in rdr:
            ts = _to_float(row.get('gps_second')) or _to_float(row.get('timestamp'))
            ce = _to_float(row.get('carrier_error_ns'))
            pe = _to_float(row.get('pps_error_ns'))
            if ts is None or ce is None or pe is None:
                continue
            t.append(ts)
            c.append(ce)
            p.append(pe)
    if not t:
        print(f"{path}: no rows with both carrier_error_ns and pps_error_ns",
              file=sys.stderr)
        sys.exit(2)
    t = np.array(t)
    return t - t[0], np.array(c), np.array(p)


def decompose(t_s, diff_ns):
    """Linear least-squares fit; return (intercept, slope, residual)."""
    A = np.vstack([np.ones_like(t_s), t_s]).T
    (intercept, slope), *_ = np.linalg.lstsq(A, diff_ns, rcond=None)
    fit = intercept + slope * t_s
    residual = diff_ns - fit
    return intercept, slope, fit, residual


def summarize(label, t_s, carrier, pps):
    diff = carrier - pps
    intercept, slope_ns_per_s, fit, residual = decompose(t_s, diff)
    duration_s = t_s[-1] - t_s[0] if len(t_s) > 1 else 0.0
    print(f"\n=== {label} ({len(t_s)} epochs, {duration_s:.0f} s) ===")
    print(f"  diff mean             = {diff.mean():+.2f} ns")
    print(f"  diff σ                = {diff.std():.2f} ns")
    print(f"  linear fit intercept  = {intercept:+.2f} ns   "
          "(anchor capture noise)")
    print(f"  linear fit slope      = {slope_ns_per_s*1e9:+.3f} ns/Gs "
          f"= {slope_ns_per_s*86400:+.3f} ns/day "
          "(adjfine calibration error)")
    print(f"  residual σ            = {residual.std():.2f} ns   "
          "(actuator nonlinearity + closed-loop coupling)")
    print(f"  residual peak-to-peak = {residual.ptp():.2f} ns")
    return {
        'label': label,
        't_s': t_s,
        'carrier': carrier,
        'pps': pps,
        'diff': diff,
        'fit': fit,
        'residual': residual,
        'intercept': intercept,
        'slope': slope_ns_per_s,
    }


def make_plot(runs, output_path):
    _ensure_plotly()

    fig = make_subplots(
        rows=4, cols=1, shared_xaxes=False,
        vertical_spacing=0.07,
        subplot_titles=(
            'Raw error signals (carrier_error_ns vs pps_error_ns)',
            'diff = carrier − pps  (with linear fit)',
            'residual = diff − linear_fit  (random walk + periodic)',
            'PSD of residual',
        ),
        row_heights=[0.27, 0.27, 0.23, 0.23],
    )

    colors = ['#1f77b4', '#ff7f0e', '#2ca02c', '#d62728', '#9467bd']

    for i, r in enumerate(runs):
        c = colors[i % len(colors)]
        lbl = r['label']
        t = r['t_s']

        fig.add_trace(go.Scatter(x=t, y=r['carrier'], mode='lines',
                                 line=dict(color=c, width=1),
                                 name=f'{lbl} carrier',
                                 legendgroup=lbl),
                      row=1, col=1)
        fig.add_trace(go.Scatter(x=t, y=r['pps'], mode='lines',
                                 line=dict(color=c, width=1, dash='dot'),
                                 name=f'{lbl} pps',
                                 legendgroup=lbl),
                      row=1, col=1)

        fig.add_trace(go.Scatter(x=t, y=r['diff'], mode='lines',
                                 line=dict(color=c, width=1),
                                 name=f'{lbl} diff',
                                 legendgroup=lbl, showlegend=False),
                      row=2, col=1)
        fig.add_trace(go.Scatter(x=t, y=r['fit'], mode='lines',
                                 line=dict(color=c, width=2, dash='dash'),
                                 name=f'{lbl} fit',
                                 legendgroup=lbl, showlegend=False),
                      row=2, col=1)

        fig.add_trace(go.Scatter(x=t, y=r['residual'], mode='lines',
                                 line=dict(color=c, width=1),
                                 name=f'{lbl} residual',
                                 legendgroup=lbl, showlegend=False),
                      row=3, col=1)

        # PSD of the residual.  Assume ~1 Hz sample rate but compute
        # exactly from the timestamps.
        if len(t) > 16:
            dt = np.median(np.diff(t))
            fs = 1.0 / dt if dt > 0 else 1.0
            nperseg = min(256, len(r['residual']) // 4)
            if nperseg >= 16:
                f, psd = signal.welch(r['residual'], fs=fs, nperseg=nperseg)
                # Skip the DC bin so log axes work.
                f, psd = f[1:], np.sqrt(psd[1:])
                fig.add_trace(go.Scatter(x=f, y=psd, mode='lines',
                                         line=dict(color=c, width=1),
                                         name=f'{lbl} ASD',
                                         legendgroup=lbl, showlegend=False),
                              row=4, col=1)

    fig.update_xaxes(title_text='time (s)', row=1, col=1)
    fig.update_xaxes(title_text='time (s)', row=2, col=1)
    fig.update_xaxes(title_text='time (s)', row=3, col=1)
    fig.update_xaxes(title_text='frequency (Hz)', type='log', row=4, col=1)

    fig.update_yaxes(title_text='ns', row=1, col=1)
    fig.update_yaxes(title_text='ns', row=2, col=1)
    fig.update_yaxes(title_text='ns', row=3, col=1)
    fig.update_yaxes(title_text='ns/√Hz', type='log', row=4, col=1)

    fig.update_layout(
        height=1100,
        title_text='Carrier − PPS+qErr divergence diagnostic',
        hovermode='x unified',
    )
    fig.write_html(output_path, include_plotlyjs='cdn')
    print(f"\nWrote {output_path}")


def main():
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument('csv', nargs='+', help='peppar-fix servo log CSV(s)')
    ap.add_argument('--label', action='append', default=[],
                    help='label for each CSV (repeat once per CSV)')
    ap.add_argument('-o', '--output', default='carrier_vs_pps.html',
                    help='output HTML path (default: carrier_vs_pps.html)')
    args = ap.parse_args()

    if args.label and len(args.label) != len(args.csv):
        ap.error('--label must be repeated once per CSV')

    runs = []
    for i, path in enumerate(args.csv):
        label = args.label[i] if args.label else os.path.basename(path)
        t, carrier, pps = load_run(path)
        runs.append(summarize(label, t, carrier, pps))

    make_plot(runs, args.output)


if __name__ == '__main__':
    main()
