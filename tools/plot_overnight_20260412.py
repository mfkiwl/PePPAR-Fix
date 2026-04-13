#!/usr/bin/env python3
"""Analyze overnight 2026-04-12 three-host runs.

Plots TDEV and ADEV with:
- gnss_pps_ticc (F9T PPS on TICC timescale) — undisciplined reference
- do_pps_ticc for each host (disciplined PEROUT on TICC timescale)

The gnss_pps is chB on all TICCs (F9T PPS edge).
The do_pps is chA (i226 PEROUT edge).
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

DATA_DIR = "data/overnight-20260412"

HOSTS = {
    "TimeHat": {
        "ticc": f"{DATA_DIR}/overnight-20260412-timehat-ticc.csv",
        "servo": f"{DATA_DIR}/overnight-20260412-timehat-servo.csv",
        "color": "#1f77b4",
        "note": "EXTTS fallback (PEROUT died after 52 chA events — TX timeout bug)",
    },
    "MadHat": {
        "ticc": f"{DATA_DIR}/overnight-20260412-madhat-ticc.csv",
        "servo": f"{DATA_DIR}/overnight-20260412-madhat-servo.csv",
        "color": "#ff7f0e",
        "note": "EXTTS fallback (~10% TICC pair rate)",
    },
    "ptpmon": {
        "ticc": f"{DATA_DIR}/overnight-20260412-ptpmon-ticc.csv",
        "servo": None,
        "color": "#2ca02c",
        "note": "EXTTS fallback (stock igc, PEROUT drifted 302ms)",
    },
}

GPS_PPS_COLOR = "#d62728"  # red for undisciplined reference


def load_ticc(path):
    """Load TICC CSV, return dict of channel -> list of (ref_sec, ref_ps)."""
    channels = {}
    with open(path) as f:
        reader = csv.DictReader(f)
        for row in reader:
            ch = row["channel"]
            if ch not in channels:
                channels[ch] = []
            channels[ch].append((int(row["ref_sec"]), int(row["ref_ps"])))
    return channels


def ticc_to_phase_ns(events):
    """Convert (ref_sec, ref_ps) list to phase array in nanoseconds.

    Phase = timestamp relative to a linear fit (detrended).
    """
    if len(events) < 10:
        return None
    t = np.array([s + ps * 1e-12 for s, ps in events])
    # Remove linear trend (frequency offset)
    n = len(t)
    x = np.arange(n, dtype=float)
    slope, intercept = np.polyfit(x, t, 1)
    residual = t - (slope * x + intercept)
    return residual * 1e9  # seconds -> nanoseconds


def overlapping_adev(phase_ns, tau0=1.0, max_tau_factor=None):
    """Compute overlapping Allan deviation from phase data.

    phase_ns: detrended phase in nanoseconds, sampled at tau0.
    Returns (taus, adevs) arrays.
    """
    N = len(phase_ns)
    if max_tau_factor is None:
        max_tau_factor = N // 4

    taus = []
    adevs = []
    m = 1
    while m <= max_tau_factor:
        tau = m * tau0
        # Overlapping ADEV: 1/(2*tau^2*(N-2m)) * sum((x[i+2m] - 2*x[i+m] + x[i])^2)
        diffs = phase_ns[2*m:] - 2*phase_ns[m:len(phase_ns)-m] + phase_ns[:len(phase_ns)-2*m]
        if len(diffs) < 1:
            break
        adev = np.sqrt(np.mean(diffs**2) / (2 * tau**2))
        taus.append(tau)
        adevs.append(adev)

        # Octave spacing
        if m < 4:
            m += 1
        elif m < 16:
            m *= 2
        else:
            m = int(m * 1.5)
    return np.array(taus), np.array(adevs)


def tdev_from_adev(taus, adevs):
    """TDEV = tau/sqrt(3) * ADEV (for MDEV; approximation for ADEV)."""
    return taus / np.sqrt(3) * adevs


def main():
    fig_tdev = go.Figure()
    fig_adev = go.Figure()

    # Use TimeHat chB as the gnss_pps reference (undisciplined F9T PPS)
    ref_host = "TimeHat"
    ref_data = load_ticc(HOSTS[ref_host]["ticc"])
    chB_phase = ticc_to_phase_ns(ref_data.get("chB", []))
    if chB_phase is not None:
        taus, adevs = overlapping_adev(chB_phase)
        tdevs = tdev_from_adev(taus, adevs)
        fig_tdev.add_trace(go.Scatter(
            x=taus, y=tdevs,
            mode="lines+markers", name=f"gnss_pps (F9T PPS, undisciplined)",
            line=dict(color=GPS_PPS_COLOR, width=2, dash="dash"),
            marker=dict(size=4),
        ))
        fig_adev.add_trace(go.Scatter(
            x=taus, y=adevs,
            mode="lines+markers", name=f"gnss_pps (F9T PPS, undisciplined)",
            line=dict(color=GPS_PPS_COLOR, width=2, dash="dash"),
            marker=dict(size=4),
        ))
        print(f"gnss_pps ({ref_host} chB): {len(chB_phase)} samples, "
              f"TDEV(1s)={tdevs[0]:.3f} ns, ADEV(1s)={adevs[0]:.3f} ns")

    # do_pps for each host — use chA-chB differential (DO phase error
    # measured on TICC timescale).  This cancels the TICC timescale drift
    # and directly measures how well the DO tracks GPS.
    for host, cfg in HOSTS.items():
        data = load_ticc(cfg["ticc"])
        chA_events = data.get("chA", [])
        chB_events = data.get("chB", [])

        # Try TICC differential first (chA-chB paired by ref_sec)
        if len(chA_events) >= 100 and len(chB_events) >= 100:
            # Build lookup by ref_sec
            chA_by_sec = {}
            for s, ps in chA_events:
                chA_by_sec[s] = ps
            diffs_ns = []
            for s, ps_b in chB_events:
                ps_a = chA_by_sec.get(s)
                if ps_a is not None:
                    diff_ps = ps_a - ps_b  # do_pps - gnss_pps
                    diffs_ns.append(diff_ps * 1e-3)  # ps -> ns
            if len(diffs_ns) >= 100:
                phase = np.array(diffs_ns)
                # Filter out any |diff| > 100ms (PEROUT misalignment)
                mask = np.abs(phase) < 100_000_000  # 100ms in ns
                phase = phase[mask]
                if len(phase) >= 100:
                    # Detrend
                    x = np.arange(len(phase), dtype=float)
                    slope, intercept = np.polyfit(x, phase, 1)
                    phase_det = phase - (slope * x + intercept)
                    taus, adevs = overlapping_adev(phase_det)
                    tdevs = tdev_from_adev(taus, adevs)
                    fig_tdev.add_trace(go.Scatter(
                        x=taus, y=tdevs,
                        mode="lines+markers", name=f"{host} do_pps (TICC chA-chB)",
                        line=dict(color=cfg["color"], width=2),
                        marker=dict(size=4),
                    ))
                    fig_adev.add_trace(go.Scatter(
                        x=taus, y=adevs,
                        mode="lines+markers", name=f"{host} do_pps (TICC chA-chB)",
                        line=dict(color=cfg["color"], width=2),
                        marker=dict(size=4),
                    ))
                    print(f"{host} do_pps (TICC diff): {len(phase)} paired samples "
                          f"(of {len(diffs_ns)} raw, {len(diffs_ns)-len(phase)} filtered), "
                          f"TDEV(1s)={tdevs[0]:.3f} ns, ADEV(1s)={adevs[0]:.3f} ns")
                    continue
                else:
                    print(f"{host}: only {len(phase)} valid diffs after filtering — "
                          f"falling back to servo CSV")

        # Fallback: servo CSV pps_err_ticc_ns
        if cfg["servo"]:
            pps_errs = []
            with open(cfg["servo"]) as f:
                reader = csv.DictReader(f)
                for row in reader:
                    val = row.get("pps_err_ticc_ns", "")
                    if val and val != "None":
                        try:
                            v = float(val)
                            if abs(v) < 100_000_000:
                                pps_errs.append(v)
                        except ValueError:
                            pass
            if len(pps_errs) > 100:
                phase = np.array(pps_errs)
                x = np.arange(len(phase), dtype=float)
                slope, intercept = np.polyfit(x, phase, 1)
                phase_det = phase - (slope * x + intercept)
                taus, adevs = overlapping_adev(phase_det)
                tdevs = tdev_from_adev(taus, adevs)
                fig_tdev.add_trace(go.Scatter(
                    x=taus, y=tdevs,
                    mode="lines+markers",
                    name=f"{host} do_pps (servo CSV)",
                    line=dict(color=cfg["color"], width=2),
                    marker=dict(size=4),
                ))
                fig_adev.add_trace(go.Scatter(
                    x=taus, y=adevs,
                    mode="lines+markers",
                    name=f"{host} do_pps (servo CSV)",
                    line=dict(color=cfg["color"], width=2),
                    marker=dict(size=4),
                ))
                print(f"{host} do_pps (servo CSV): {len(pps_errs)} samples, "
                      f"TDEV(1s)={tdevs[0]:.3f} ns, ADEV(1s)={adevs[0]:.3f} ns")
            else:
                print(f"{host}: insufficient servo data ({len(pps_errs)} samples)")
        else:
            print(f"{host}: no chA data and no servo CSV — skipping")

    # Annotations
    notes = "<br>".join(f"<b>{h}</b>: {c['note']}" for h, c in HOSTS.items())
    annotation = dict(
        text=notes,
        xref="paper", yref="paper", x=0.02, y=0.02,
        showarrow=False, font=dict(size=10),
        bgcolor="rgba(255,255,255,0.8)",
        bordercolor="gray", borderwidth=1,
    )

    for fig, metric in [(fig_tdev, "TDEV"), (fig_adev, "ADEV")]:
        fig.update_layout(
            title=f"Overnight 2026-04-12 — {metric} (7.5 hours, 3 hosts)",
            xaxis_title="τ (seconds)",
            yaxis_title=f"{metric} (ns)",
            xaxis_type="log",
            yaxis_type="log",
            template="plotly_white",
            legend=dict(x=0.6, y=0.98),
            annotations=[annotation],
        )

    fig_tdev.write_html("plots/overnight-20260412-tdev.html")
    fig_adev.write_html("plots/overnight-20260412-adev.html")
    print("\nPlots written to plots/overnight-20260412-tdev.html and -adev.html")


if __name__ == "__main__":
    main()
