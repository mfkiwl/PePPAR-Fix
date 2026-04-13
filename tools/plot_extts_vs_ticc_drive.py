#!/usr/bin/env python3
"""Compare DO output stability: EXTTS+qErr driven servo vs TICC+qErr driven servo.

Plots TDEV and ADEV of TICC chA (DO PPS, detrended) — the absolute phase
stability of the disciplined oscillator output.

Data sources:
- Overnight 2026-04-12: ~7.5 hours, EXTTS+qErr drove the servo, TICC observed
- TICC-drive-v2 2026-04-13: ~15 minutes, TICC+qErr drove the servo

TICC chB (gnss_pps, undisciplined) shown as reference.

Note: TICC chA alone (not chA-chB) is the correct metric.  chA-chB measures
servo tracking fidelity, not output quality.  A downstream consumer sees chA.
"""

import csv
import sys
import numpy as np

try:
    import plotly.graph_objects as go
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
    """Convert (ref_sec, ref_ps) to detrended phase array in nanoseconds.

    Sorts by full timestamp and removes non-monotonic entries (misordered
    events from TICC serial interleaving).
    """
    if len(events) < 10:
        return None
    t = np.array([s + ps * 1e-12 for s, ps in events])
    # Sort and remove non-monotonic (handles interleaved channel reads)
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
    """Overlapping Allan deviation from phase data sampled at tau0."""
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


# --- Data sources ---

OVERNIGHT_DIR = "data/overnight-20260412"
TICC_DRIVE_DIR = "data/ticc-drive-20260413"

# Colors: solid for TICC-driven, dashed for EXTTS-driven
COLORS = {
    "TimeHat": "#1f77b4",
    "MadHat": "#ff7f0e",
    "pi4ptpmon": "#2ca02c",
}
GPS_PPS_COLOR = "#d62728"

OVERNIGHT = {
    # TimeHat chA died (52 events) — exclude
    "MadHat": f"{OVERNIGHT_DIR}/overnight-20260412-madhat-ticc.csv",
    "pi4ptpmon": f"{OVERNIGHT_DIR}/overnight-20260412-pi4ptpmon-ticc.csv",
}

TICC_DRIVE = {
    "TimeHat": f"{TICC_DRIVE_DIR}/ticc-drive-v2-20260413-timehat-ticc.csv",
    "MadHat": f"{TICC_DRIVE_DIR}/ticc-drive-v2-20260413-madhat-ticc.csv",
    "pi4ptpmon": f"{TICC_DRIVE_DIR}/ticc-drive-v2-20260413-pi4ptpmon-ticc.csv",
}


def main():
    fig_tdev = go.Figure()
    fig_adev = go.Figure()

    # --- gnss_pps reference (chB, undisciplined) from overnight MadHat ---
    ref_events = load_ticc_channel(OVERNIGHT["MadHat"], "chB")
    ref_phase = ticc_to_phase_ns(ref_events)
    if ref_phase is not None:
        t, a = overlapping_adev(ref_phase)
        td = tdev(t, a)
        for fig, vals, metric in [(fig_tdev, td, "TDEV"), (fig_adev, a, "ADEV")]:
            fig.add_trace(go.Scatter(
                x=t, y=vals, mode="lines+markers",
                name="gnss_pps (undisciplined, 7.5h)",
                line=dict(color=GPS_PPS_COLOR, width=2, dash="dash"),
                marker=dict(size=4),
            ))
        print(f"gnss_pps (MadHat chB, overnight): {len(ref_phase)} samples, "
              f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")

    # --- Overnight EXTTS+qErr driven: TICC chA (DO output) ---
    for host, path in OVERNIGHT.items():
        events = load_ticc_channel(path, "chA")
        phase = ticc_to_phase_ns(events)
        if phase is None:
            print(f"{host} overnight: insufficient chA data ({len(events)} events)")
            continue
        t, a = overlapping_adev(phase)
        td = tdev(t, a)
        for fig, vals, metric in [(fig_tdev, td, "TDEV"), (fig_adev, a, "ADEV")]:
            fig.add_trace(go.Scatter(
                x=t, y=vals, mode="lines+markers",
                name=f"{host} EXTTS+qErr servo (7.5h)",
                line=dict(color=COLORS[host], width=2, dash="dot"),
                marker=dict(size=4, symbol="square"),
            ))
        print(f"{host} EXTTS+qErr (overnight chA): {len(phase)} samples, "
              f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")

    # --- 15-min TICC+qErr driven: TICC chA (DO output) ---
    for host, path in TICC_DRIVE.items():
        events = load_ticc_channel(path, "chA")
        phase = ticc_to_phase_ns(events)
        if phase is None:
            print(f"{host} ticc-drive: insufficient chA data ({len(events)} events)")
            continue
        t, a = overlapping_adev(phase)
        td = tdev(t, a)
        for fig, vals, metric in [(fig_tdev, td, "TDEV"), (fig_adev, a, "ADEV")]:
            fig.add_trace(go.Scatter(
                x=t, y=vals, mode="lines+markers",
                name=f"{host} TICC+qErr servo (15m)",
                line=dict(color=COLORS[host], width=2),
                marker=dict(size=5),
            ))
        print(f"{host} TICC+qErr (15m chA): {len(phase)} samples, "
              f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")

    # --- Also show gnss_pps from 15-min for short-tau reference ---
    ref15_events = load_ticc_channel(TICC_DRIVE["MadHat"], "chB")
    ref15_phase = ticc_to_phase_ns(ref15_events)
    if ref15_phase is not None:
        t, a = overlapping_adev(ref15_phase)
        td = tdev(t, a)
        for fig, vals, metric in [(fig_tdev, td, "TDEV"), (fig_adev, a, "ADEV")]:
            fig.add_trace(go.Scatter(
                x=t, y=vals, mode="lines+markers",
                name="gnss_pps (undisciplined, 15m)",
                line=dict(color=GPS_PPS_COLOR, width=1, dash="dot"),
                marker=dict(size=3),
            ))
        print(f"gnss_pps (MadHat chB, 15m): {len(ref15_phase)} samples, "
              f"TDEV(1s)={td[0]:.3f} ns, ADEV(1s)={a[0]:.3f} ns")

    # --- Layout ---
    notes = (
        "<b>TimeHat overnight</b>: excluded (PEROUT died after 52 chA events)<br>"
        "<b>pi4ptpmon overnight</b>: stock igc driver (no DKMS ppsfix)<br>"
        "<b>Solid lines</b>: TICC+qErr driven servo (15 min)<br>"
        "<b>Dotted lines</b>: EXTTS+qErr driven servo (7.5 hours)"
    )
    annotation = dict(
        text=notes, xref="paper", yref="paper", x=0.02, y=0.02,
        showarrow=False, font=dict(size=10),
        bgcolor="rgba(255,255,255,0.8)",
        bordercolor="gray", borderwidth=1,
    )

    for fig, metric in [(fig_tdev, "TDEV"), (fig_adev, "ADEV")]:
        fig.update_layout(
            title=f"DO Output Stability: EXTTS+qErr vs TICC+qErr Servo — {metric} (chA detrended)",
            xaxis_title="τ (seconds)",
            yaxis_title=f"{metric} (ns)",
            xaxis_type="log",
            yaxis_type="log",
            template="plotly_white",
            legend=dict(x=0.55, y=0.98),
            annotations=[annotation],
        )

    fig_tdev.write_html("plots/extts-vs-ticc-drive-tdev.html")
    fig_adev.write_html("plots/extts-vs-ticc-drive-adev.html")
    print("\nPlots: plots/extts-vs-ticc-drive-tdev.html, plots/extts-vs-ticc-drive-adev.html")


if __name__ == "__main__":
    main()
