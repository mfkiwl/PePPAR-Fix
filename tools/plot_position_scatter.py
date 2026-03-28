#!/usr/bin/env python3
"""Plot PPP position repeatability as 3D scatter with confidence ellipsoids.

Reads summary.json from position_repeatability.sh and produces an
interactive Plotly HTML with:
  - Each converged position as a dot
  - 1-sigma confidence sphere per run (if sigma available)
  - A banana (15 cm yellow ellipsoid) at the centroid for scale
  - NEU (North/East/Up) axes in meters relative to the mean position

Usage:
    python3 tools/plot_position_scatter.py data/pos-repeat-*/summary.json
"""

import json
import sys
import numpy as np


def lla_offsets_m(lats, lons, alts, ref_lat, ref_lon, ref_alt):
    """Convert lat/lon/alt arrays to NEU offsets in meters from a reference."""
    north = (lats - ref_lat) * 111319.0
    east = (lons - ref_lon) * 111319.0 * np.cos(np.radians(ref_lat))
    up = alts - ref_alt
    return north, east, up


def make_sphere(cx, cy, cz, r, n=12):
    """Generate mesh coordinates for a sphere."""
    u = np.linspace(0, 2 * np.pi, n)
    v = np.linspace(0, np.pi, n)
    x = cx + r * np.outer(np.cos(u), np.sin(v))
    y = cy + r * np.outer(np.sin(u), np.sin(v))
    z = cz + r * np.outer(np.ones_like(u), np.cos(v))
    return x, y, z


def main():
    if len(sys.argv) < 2:
        print(f"Usage: {sys.argv[0]} summary.json", file=sys.stderr)
        sys.exit(1)

    with open(sys.argv[1]) as f:
        summary = json.load(f)

    runs = summary["runs"]
    if not runs:
        print("No runs in summary", file=sys.stderr)
        sys.exit(1)

    lats = np.array([r["lat"] for r in runs])
    lons = np.array([r["lon"] for r in runs])
    alts = np.array([r["alt_m"] for r in runs])
    sigmas = [r.get("sigma_m") for r in runs]

    ref_lat = summary["mean_lat"]
    ref_lon = summary["mean_lon"]
    ref_alt = summary["mean_alt_m"]

    north, east, up = lla_offsets_m(lats, lons, alts, ref_lat, ref_lon, ref_alt)

    try:
        import plotly.graph_objects as go
    except ImportError:
        print("pip install plotly", file=sys.stderr)
        sys.exit(1)

    fig = go.Figure()

    # Position dots
    labels = [r.get("run", f"run-{i+1}") for i, r in enumerate(runs)]
    hover = [
        f"{lab}<br>{r['lat']:.7f}, {r['lon']:.7f}<br>alt={r['alt_m']:.3f}m"
        + (f"<br>sigma={r['sigma_m']:.4f}m" if r.get('sigma_m') else "")
        for lab, r in zip(labels, runs)
    ]
    fig.add_trace(go.Scatter3d(
        x=east, y=north, z=up,
        mode="markers",
        marker=dict(size=5, color="blue"),
        text=hover,
        hoverinfo="text",
        name="Converged positions",
    ))

    # Confidence spheres (1-sigma)
    for i, sigma in enumerate(sigmas):
        if sigma is None or sigma <= 0:
            continue
        sx, sy, sz = make_sphere(east[i], north[i], up[i], sigma)
        fig.add_trace(go.Surface(
            x=sx, y=sy, z=sz,
            opacity=0.15,
            colorscale=[[0, "royalblue"], [1, "royalblue"]],
            showscale=False,
            hoverinfo="skip",
            name=f"1σ ({sigma:.3f}m)" if i == 0 else None,
            showlegend=(i == 0),
        ))

    # Banana for scale (15 cm yellow ellipsoid at origin)
    bx, by, bz = make_sphere(0, 0, 0, 0.075)  # 7.5 cm radius = 15 cm diameter
    bz_stretched = bz * 2.0  # elongate vertically for banana shape
    fig.add_trace(go.Surface(
        x=bx, y=by, z=bz_stretched,
        opacity=0.6,
        colorscale=[[0, "gold"], [1, "gold"]],
        showscale=False,
        hoverinfo="text",
        text="Banana (15 cm)",
        name="Banana (15 cm)",
    ))

    fig.update_layout(
        title=f"PPP Position Repeatability ({len(runs)} runs)<br>"
              f"<sub>σ_N={summary['std_north_m']:.3f}m  "
              f"σ_E={summary['std_east_m']:.3f}m  "
              f"σ_U={summary['std_up_m']:.3f}m</sub>",
        scene=dict(
            xaxis_title="East (m)",
            yaxis_title="North (m)",
            zaxis_title="Up (m)",
            aspectmode="data",
        ),
        width=900,
        height=700,
    )

    out_html = sys.argv[1].replace(".json", ".html")
    fig.write_html(out_html)
    print(f"Written: {out_html}")
    print(f"Open in browser to interact (pan/rotate/zoom)")


if __name__ == "__main__":
    main()
