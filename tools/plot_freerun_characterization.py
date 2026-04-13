#!/usr/bin/env python3
"""Three-host freerun DO characterization: TDEV, ADEV, ASD/PSD.

Reads TICC CSV from each host's freerun run.  Uses chA (DO PPS,
detrended) as the stability metric — this is the free-running
oscillator's phase noise, uncontaminated by servo corrections.

Produces:
1. TDEV comparison (all hosts)
2. ADEV comparison (all hosts)
3. ASD/PSD comparison (all hosts)
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


def load_ticc_channel(path, channel):
    """Load one channel from TICC CSV, return list of (ref_sec, ref_ps)."""
    events = []
    with open(path) as f:
        for row in csv.DictReader(f):
            if row["channel"] == channel:
                events.append((int(row["ref_sec"]), int(row["ref_ps"])))
    return events


def ticc_to_phase_ns(events):
    """Convert (ref_sec, ref_ps) to detrended phase array in nanoseconds."""
    if len(events) < 10:
        return None
    t = np.array([s + ps * 1e-12 for s, ps in events])
    # Sort and remove non-monotonic
    order = np.argsort(t)
    t = t[order]
    mono = np.concatenate(([True], np.diff(t) > 0))
    t = t[mono]
    if len(t) < 10:
        return None
    x = np.arange(len(t), dtype=float)
    slope, intercept = np.polyfit(x, t, 1)
    residual = t - (slope * x + intercept)
    return residual * 1e9


def overlapping_adev(phase_ns, tau0=1.0):
    N = len(phase_ns)
    taus, adevs = [], []
    m = 1
    while m <= N // 4:
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


def compute_psd(phase_ns, tau0=1.0):
    """Compute one-sided PSD of phase noise via FFT.

    Returns (freqs_hz, psd_ns2_hz) — power spectral density in ns^2/Hz.
    """
    N = len(phase_ns)
    # Frequency data (phase -> frequency via first differences)
    freq_ns = np.diff(phase_ns) / tau0  # ns/s = fractional freq * 1e9
    # Window to reduce spectral leakage
    window = np.hanning(len(freq_ns))
    windowed = freq_ns * window
    # FFT
    fft_vals = np.fft.rfft(windowed)
    # One-sided PSD
    freqs = np.fft.rfftfreq(len(freq_ns), d=tau0)
    # Power: |FFT|^2 / (N * fs), scaled for one-sided
    psd = 2.0 * np.abs(fft_vals)**2 / (len(freq_ns) * (1.0 / tau0))
    # Correct for window power
    window_power = np.mean(window**2)
    psd /= window_power
    return freqs[1:], psd[1:]  # skip DC


def main():
    import argparse
    parser = argparse.ArgumentParser(description="Freerun characterization plots")
    parser.add_argument("files", nargs="+",
                        help="host:path pairs, e.g. TimeHat:data/freerun-ticc.csv")
    parser.add_argument("-o", "--output", default="plots/freerun-char-20260413",
                        help="Output prefix (default: plots/freerun-char-20260413)")
    args = parser.parse_args()

    COLORS = {
        "TimeHat": "#1f77b4",
        "MadHat": "#ff7f0e",
        "ptpmon": "#2ca02c",
    }
    GPS_PPS_COLOR = "#d62728"

    fig_tdev = go.Figure()
    fig_adev = go.Figure()
    fig_psd = go.Figure()

    for spec in args.files:
        host, path = spec.split(":", 1)
        color = COLORS.get(host, "#333333")

        # chA = DO PPS (free-running)
        chA_events = load_ticc_channel(path, "chA")
        chA_phase = ticc_to_phase_ns(chA_events)

        # chB = gnss PPS (reference, also free-running in freerun mode)
        chB_events = load_ticc_channel(path, "chB")
        chB_phase = ticc_to_phase_ns(chB_events)

        if chA_phase is not None:
            t, a = overlapping_adev(chA_phase)
            td = tdev(t, a)

            fig_tdev.add_trace(go.Scatter(
                x=t, y=td, mode="lines+markers",
                name=f"{host} DO (chA, {len(chA_phase)} samples)",
                line=dict(color=color, width=2), marker=dict(size=4),
            ))
            fig_adev.add_trace(go.Scatter(
                x=t, y=a, mode="lines+markers",
                name=f"{host} DO (chA, {len(chA_phase)} samples)",
                line=dict(color=color, width=2), marker=dict(size=4),
            ))

            # PSD
            freqs, psd = compute_psd(chA_phase)
            # Convert to ASD (amplitude spectral density) = sqrt(PSD)
            asd = np.sqrt(psd)
            fig_psd.add_trace(go.Scatter(
                x=freqs, y=asd, mode="lines",
                name=f"{host} DO ASD",
                line=dict(color=color, width=1),
            ))

            print(f"{host} DO (chA): {len(chA_phase)} samples, "
                  f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")
        else:
            print(f"{host} DO (chA): insufficient data ({len(chA_events)} events)")

        if chB_phase is not None:
            t, a = overlapping_adev(chB_phase)
            td = tdev(t, a)
            fig_tdev.add_trace(go.Scatter(
                x=t, y=td, mode="lines+markers",
                name=f"{host} gnss_pps (chB)",
                line=dict(color=color, width=1, dash="dash"),
                marker=dict(size=3),
            ))
            fig_adev.add_trace(go.Scatter(
                x=t, y=a, mode="lines+markers",
                name=f"{host} gnss_pps (chB)",
                line=dict(color=color, width=1, dash="dash"),
                marker=dict(size=3),
            ))
            print(f"{host} gnss_pps (chB): {len(chB_phase)} samples, "
                  f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")

    for fig, metric in [(fig_tdev, "TDEV"), (fig_adev, "ADEV")]:
        fig.update_layout(
            title=f"Freerun DO Characterization — {metric} (chA detrended, 30 min)",
            xaxis_title="τ (seconds)",
            yaxis_title=f"{metric} (ns)",
            xaxis_type="log", yaxis_type="log",
            template="plotly_white",
            legend=dict(x=0.55, y=0.98),
        )

    fig_psd.update_layout(
        title="Freerun DO Characterization — Amplitude Spectral Density",
        xaxis_title="Frequency (Hz)",
        yaxis_title="ASD (ns/√Hz)",
        xaxis_type="log", yaxis_type="log",
        template="plotly_white",
        legend=dict(x=0.55, y=0.98),
    )

    for suffix, fig in [("-tdev.html", fig_tdev), ("-adev.html", fig_adev),
                         ("-asd.html", fig_psd)]:
        out = args.output + suffix
        fig.write_html(out)
        print(f"  → {out}")


if __name__ == "__main__":
    main()
