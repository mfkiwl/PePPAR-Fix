#!/usr/bin/env python3
"""Compare rx TCXO phase estimates: qErr unwrapped vs PPP dt_rx.

Reads the servo CSV from a beat-note test run and plots:
1. Both phase estimates (qerr_unwrapped_ns and dt_rx_ns) over time
2. Their derivatives (frequency estimates) over time
3. Residuals after detrending (smoothness comparison)
4. ADEV/TDEV of both residuals

The key metric for "which is better" is the short-tau TDEV of the
detrended residuals — lower = smoother = less noise.
"""

import csv
import sys
import numpy as np

try:
    import plotly.graph_objects as go
    from plotly.subplots import make_subplots
except ImportError:
    print("pip install plotly")
    sys.exit(1)


def load_servo_csv(path):
    """Load servo CSV, return dict of arrays."""
    cols = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        rows = list(reader)
    for key in ['dt_rx_ns', 'dt_rx_sigma_ns', 'qerr_unwrapped_ns',
                'qerr_freq_ns_s', 'qerr_dt_rx_rate_discrep_ns_s', 'qerr_ns',
                'synth_phase_ns']:
        vals = []
        for r in rows:
            v = r.get(key, '')
            if v and v != 'None':
                try:
                    vals.append(float(v))
                except ValueError:
                    vals.append(None)
            else:
                vals.append(None)
        cols[key] = vals
    cols['n'] = len(rows)
    return cols


def detrend(arr):
    """Remove linear trend, return residuals in same units."""
    valid = [(i, v) for i, v in enumerate(arr) if v is not None]
    if len(valid) < 10:
        return None, None
    idx, vals = zip(*valid)
    x = np.array(idx, dtype=float)
    y = np.array(vals, dtype=float)
    slope, intercept = np.polyfit(x, y, 1)
    residual = y - (slope * x + intercept)
    return np.array(idx), residual


def overlapping_adev(phase_ns, tau0=1.0):
    N = len(phase_ns)
    max_m = N // 4
    taus, adevs = [], []
    m = 1
    while m <= max_m:
        tau = m * tau0
        d = phase_ns[2*m:] - 2*phase_ns[m:N-m] + phase_ns[:N-2*m]
        if len(d) < 1:
            break
        adevs.append(np.sqrt(np.mean(d**2) / (2 * tau**2)))
        taus.append(tau)
        if m < 4:
            m += 1
        elif m < 16:
            m *= 2
        else:
            m = int(m * 1.5)
    return np.array(taus), np.array(adevs)


def tdev(taus, adevs):
    return taus / np.sqrt(3) * adevs


def main():
    if len(sys.argv) < 2:
        print("Usage: plot_beat_note_comparison.py <servo.csv>")
        sys.exit(1)

    data = load_servo_csv(sys.argv[1])
    n = data['n']
    epochs = list(range(n))

    # --- Subplot 1: Phase estimates ---
    fig = make_subplots(rows=3, cols=1,
                        subplot_titles=[
                            "rx TCXO Phase: dt_rx vs qErr unwrapped",
                            "Frequency: dt_rx rate vs qErr freq",
                            "Detrended Residuals (smoothness)",
                        ],
                        vertical_spacing=0.08)

    # dt_rx phase
    dt_rx = data['dt_rx_ns']
    dt_rx_valid = [(i, v) for i, v in enumerate(dt_rx) if v is not None]
    if dt_rx_valid:
        idx, vals = zip(*dt_rx_valid)
        fig.add_trace(go.Scatter(x=list(idx), y=list(vals),
                                 mode='lines', name='dt_rx (PPP)',
                                 line=dict(color='#1f77b4', width=1)),
                      row=1, col=1)

    # qerr unwrapped — on secondary y-axis (different scale)
    qerr_uw = data['qerr_unwrapped_ns']
    qerr_valid = [(i, v) for i, v in enumerate(qerr_uw) if v is not None]
    if qerr_valid:
        idx, vals = zip(*qerr_valid)
        fig.add_trace(go.Scatter(x=list(idx), y=list(vals),
                                 mode='lines', name='qErr unwrapped (mod 8ns)',
                                 line=dict(color='#ff7f0e', width=1),
                                 yaxis='y2'),
                      row=1, col=1)

    # --- Subplot 2: Frequency estimates ---
    # dt_rx rate (first differences)
    if len(dt_rx_valid) > 1:
        dt_idx = [dt_rx_valid[i][0] for i in range(1, len(dt_rx_valid))]
        dt_rate = [dt_rx_valid[i][1] - dt_rx_valid[i-1][1]
                   for i in range(1, len(dt_rx_valid))]
        fig.add_trace(go.Scatter(x=dt_idx, y=dt_rate,
                                 mode='lines', name='dt_rx rate (Δ/epoch)',
                                 line=dict(color='#1f77b4', width=1)),
                      row=2, col=1)

    # qerr frequency
    qerr_freq = data['qerr_freq_ns_s']
    qf_valid = [(i, v) for i, v in enumerate(qerr_freq) if v is not None]
    if qf_valid:
        idx, vals = zip(*qf_valid)
        fig.add_trace(go.Scatter(x=list(idx), y=list(vals),
                                 mode='lines', name='qErr freq (30s window)',
                                 line=dict(color='#ff7f0e', width=1)),
                      row=2, col=1)

    # --- Subplot 3: Detrended residuals ---
    dt_rx_idx, dt_rx_resid = detrend(dt_rx)
    qerr_idx, qerr_resid = detrend(qerr_uw)

    synth = data['synth_phase_ns']
    synth_idx, synth_resid = detrend(synth)

    if dt_rx_resid is not None:
        fig.add_trace(go.Scatter(x=dt_rx_idx, y=dt_rx_resid,
                                 mode='lines', name=f'dt_rx residual (σ={np.std(dt_rx_resid):.2f} ns)',
                                 line=dict(color='#1f77b4', width=1)),
                      row=3, col=1)
        print(f"dt_rx residual: σ={np.std(dt_rx_resid):.3f} ns, n={len(dt_rx_resid)}")

    if qerr_resid is not None:
        fig.add_trace(go.Scatter(x=qerr_idx, y=qerr_resid,
                                 mode='lines', name=f'qErr residual (σ={np.std(qerr_resid):.2f} ns)',
                                 line=dict(color='#ff7f0e', width=1)),
                      row=3, col=1)
        print(f"qErr residual: σ={np.std(qerr_resid):.3f} ns, n={len(qerr_resid)}")

    if synth_resid is not None:
        fig.add_trace(go.Scatter(x=synth_idx, y=synth_resid,
                                 mode='lines', name=f'synth residual (σ={np.std(synth_resid):.2f} ns)',
                                 line=dict(color='#2ca02c', width=1)),
                      row=3, col=1)
        print(f"synth residual: σ={np.std(synth_resid):.3f} ns, n={len(synth_resid)}")

    fig.update_layout(
        title="rx TCXO Phase: PPP dt_rx vs qErr Beat Note",
        template="plotly_white",
        height=900,
        showlegend=True,
    )
    fig.update_yaxes(title_text="Phase (ns)", row=1, col=1)
    fig.update_yaxes(title_text="Rate (ns/s)", row=2, col=1)
    fig.update_yaxes(title_text="Residual (ns)", row=3, col=1)
    fig.update_xaxes(title_text="Epoch (seconds)", row=3, col=1)

    out = sys.argv[1].replace('.csv', '-comparison.html')
    fig.write_html(out)
    print(f"\nPlot: {out}")

    # --- TDEV/ADEV comparison ---
    if dt_rx_resid is not None and qerr_resid is not None:
        fig2 = go.Figure()
        traces = [
            ("dt_rx (PPP)", dt_rx_resid, "#1f77b4"),
            ("qErr unwrapped", qerr_resid, "#ff7f0e"),
        ]
        if synth_resid is not None:
            traces.append(("synthesized (dt_rx+qErr)", synth_resid, "#2ca02c"))
        for name, resid, color in traces:
            t, a = overlapping_adev(resid)
            td = tdev(t, a)
            fig2.add_trace(go.Scatter(x=t, y=td, mode='lines+markers',
                                      name=f'{name} TDEV',
                                      line=dict(color=color, width=2),
                                      marker=dict(size=4)))
            if len(td) > 0:
                print(f"{name}: TDEV(1s)={td[0]:.3f} ns")

        fig2.update_layout(
            title="rx TCXO Phase Smoothness: TDEV Comparison",
            xaxis_title="τ (seconds)",
            yaxis_title="TDEV (ns)",
            xaxis_type="log", yaxis_type="log",
            template="plotly_white",
        )
        out2 = sys.argv[1].replace('.csv', '-tdev.html')
        fig2.write_html(out2)
        print(f"TDEV plot: {out2}")


if __name__ == "__main__":
    main()
