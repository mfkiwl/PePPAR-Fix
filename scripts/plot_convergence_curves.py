#!/usr/bin/env python3
"""Generate theoretical convergence curves for GNSS positioning scenarios.

Produces log-log plots of 3D position accuracy vs survey time for:
  1. F9T standalone survey-in (cold start)
  2. PPP with broadcast ephemeris
  3. PPP with precise products (SP3/CLK)
  4. PPP-AR with SSR corrections
  5. Local NTRIP RTK (F9P-class, for reference)

Each scenario shown for cold and warm start where the distinction matters.
Vertical (height) accuracy emphasized with separate panel.

Output: docs/convergence-curves.png
"""

import numpy as np
import matplotlib
matplotlib.use('Agg')
import matplotlib.pyplot as plt
from pathlib import Path

# Time axis: 1 second to 24 hours
t = np.logspace(0, np.log10(86400), 500)  # seconds

def survey_in_cold(t):
    """F9T standalone survey-in from cold start.
    NIST 3280: 18m initial, 2m at 6h, 1m at 18h, 0.5m at 24h (vertical).
    Model: combination of 1/sqrt(t) averaging + geometry rotation benefit."""
    # Initial SPP accuracy ~10m 3D, drops as 1/sqrt with geometry bonus
    base = 12.0 / np.sqrt(t / 60 + 1)  # 1/sqrt averaging
    # Geometry rotation kicks in after ~2h, gives extra improvement
    geo_bonus = 3.0 * np.exp(-t / 7200)  # exponential decay over 2h
    floor = 0.3  # multipath/systematic floor
    return np.maximum(base + geo_bonus, floor)

def survey_in_warm(t):
    """F9T survey-in with warm start (has almanac, approx position).
    Saves ~20s of TTFF, similar asymptotic behavior."""
    # Shift time by ~20s advantage
    return survey_in_cold(t + 20)

def ppp_broadcast(t):
    """PPP with broadcast ephemeris (dual-freq IF).
    Limited by broadcast orbit errors ~1-2m. Slow convergence."""
    # EKF convergence with 1-2m orbit errors as floor
    initial = 8.0
    tau = 1800  # 30 min time constant
    floor = 0.5  # broadcast orbit/clock limit
    return floor + (initial - floor) * np.exp(-t / tau)

def ppp_precise(t):
    """PPP with precise SP3/CLK products (post-processing).
    Converges to cm-level in 30-60 min."""
    initial = 5.0
    # Two-phase: fast pseudorange convergence + slow ambiguity convergence
    fast = 3.0 * np.exp(-t / 300)   # 5 min fast phase
    slow = 2.0 * np.exp(-t / 1200)  # 20 min slow (ambiguity) phase
    floor = 0.02  # 2 cm limit
    return floor + fast + slow

def ppp_ar(t):
    """PPP-AR with SSR phase biases.
    Integer ambiguity resolution speeds convergence 3-10x."""
    initial = 5.0
    # Rapid convergence once ambiguities resolve (~5-10 min)
    fast = 3.0 * np.exp(-t / 120)    # 2 min fast phase
    ar_phase = 2.0 * np.exp(-t / 300) # 5 min AR convergence
    floor = 0.015  # 1.5 cm limit
    return floor + fast + ar_phase

def rtk_local(t):
    """Local NTRIP RTK (<10km baseline, F9P-class receiver).
    Near-instantaneous cm-level after ambiguity fix."""
    # Float solution converges fast, fix in 10-60s
    float_phase = 1.5 * np.exp(-t / 5)    # SPP → float in seconds
    fix_transition = 0.15 * np.exp(-t / 30) # float → fix in ~30s
    floor = 0.02  # 2 cm at 10km baseline
    return floor + float_phase + fix_transition

def ppp_broadcast_cold_ntrip_eph(t):
    """PPP broadcast with NTRIP ephemeris (instant eph, cold receiver).
    Saves ~30s of ephemeris acquisition."""
    return ppp_broadcast(t)

def ppp_broadcast_cold_f9t_eph(t):
    """PPP broadcast with F9T SFRBX ephemeris (must decode, cold start).
    30-60s delay for ephemeris before filter can start."""
    delay = 45  # seconds to collect ephemeris from signals
    return np.where(t < delay, 10.0, ppp_broadcast(t - delay))


# ── Vertical accuracy (multiply by VDOP/HDOP ratio ~1.8) ──
def to_vertical(accuracy_3d):
    """Approximate vertical component from 3D accuracy.
    Vertical is typically 1.5-2x worse than horizontal due to geometry."""
    return accuracy_3d * 0.75  # vertical is ~75% of 3D error

def to_horizontal(accuracy_3d):
    return accuracy_3d * 0.5


# ── Plot ──
fig, (ax1, ax2) = plt.subplots(1, 2, figsize=(14, 6.5), sharey=True)

scenarios = [
    ('F9T survey-in (cold)',      survey_in_cold,   '#888888', '-',  2.0),
    ('F9T survey-in (warm)',      survey_in_warm,   '#888888', '--', 1.5),
    ('PPP + broadcast eph',       ppp_broadcast,    '#2196F3', '-',  2.0),
    ('PPP + precise SP3/CLK',     ppp_precise,      '#4CAF50', '-',  2.0),
    ('PPP-AR + SSR',              ppp_ar,           '#FF9800', '-',  2.5),
    ('RTK (<10 km, reference)',   rtk_local,        '#F44336', '-',  2.0),
]

for label, func, color, ls, lw in scenarios:
    y = func(t)
    ax1.loglog(t / 60, to_vertical(y), color=color, ls=ls, lw=lw, label=label)
    ax2.loglog(t / 60, to_horizontal(y), color=color, ls=ls, lw=lw, label=label)

# Vertical accuracy panel
ax1.set_xlabel('Survey time (minutes)')
ax1.set_ylabel('Vertical accuracy (m, 95%)')
ax1.set_title('Vertical (Height) Accuracy')
ax1.set_xlim(1/60, 24*60)
ax1.set_ylim(0.005, 20)
ax1.axhline(y=0.3, color='gray', ls=':', alpha=0.4, lw=0.8)
ax1.axhline(y=0.03, color='gray', ls=':', alpha=0.4, lw=0.8)
ax1.axhline(y=1.0, color='gray', ls=':', alpha=0.4, lw=0.8)
ax1.text(24*60 * 0.7, 0.32, '30 cm', fontsize=7, color='gray')
ax1.text(24*60 * 0.7, 0.032, '3 cm', fontsize=7, color='gray')
ax1.text(24*60 * 0.7, 1.08, '1 m', fontsize=7, color='gray')
ax1.grid(True, which='both', alpha=0.15)
ax1.legend(fontsize=7.5, loc='upper right')

# Mark NIST data points on survey-in cold
nist_times = np.array([6, 18, 24]) * 60  # minutes
nist_vert = np.array([2.0, 1.0, 0.5])    # meters vertical
ax1.scatter(nist_times, nist_vert, marker='o', s=30, color='#888888',
            zorder=5, edgecolors='black', linewidths=0.5)
ax1.annotate('NIST data', xy=(6*60, 2.0), xytext=(6*60*1.5, 3.5),
             fontsize=7, color='#888888',
             arrowprops=dict(arrowstyle='->', color='#888888', lw=0.8))

# Horizontal accuracy panel
ax2.set_xlabel('Survey time (minutes)')
ax2.set_ylabel('Horizontal accuracy (m, 95%)')
ax2.set_title('Horizontal Accuracy')
ax2.set_xlim(1/60, 24*60)
ax2.grid(True, which='both', alpha=0.15)

# Timing significance annotations on vertical panel
ax_t = ax1.twinx()
ax_t.set_yscale('log')
ax_t.set_ylim(np.array(ax1.get_ylim()) * 3.3)  # 1m vert = 3.3ns timing
ax_t.set_ylabel('Timing impact (ns)', fontsize=8, color='#9C27B0')
ax_t.tick_params(axis='y', labelcolor='#9C27B0', labelsize=7)

fig.suptitle('GNSS Position Convergence: Theoretical Curves by Method',
             fontsize=12, fontweight='bold', y=0.98)
fig.tight_layout(rect=[0, 0, 1, 0.95])

outdir = Path(__file__).parent.parent / 'docs'
outdir.mkdir(exist_ok=True)
outpath = outdir / 'convergence-curves.png'
fig.savefig(outpath, dpi=180, bbox_inches='tight')
print(f'Saved to {outpath}')

# ── Second figure: cold vs warm start comparison ──
fig2, ax3 = plt.subplots(figsize=(9, 5.5))

# F9T cold vs warm
ax3.loglog(t/60, to_vertical(survey_in_cold(t)), color='#888888', ls='-',
           lw=2, label='F9T survey-in (cold)')
ax3.loglog(t/60, to_vertical(survey_in_warm(t)), color='#888888', ls='--',
           lw=1.5, label='F9T survey-in (warm)')

# PPP with NTRIP eph vs F9T eph
ax3.loglog(t/60, to_vertical(ppp_broadcast_cold_ntrip_eph(t)), color='#2196F3',
           ls='-', lw=2, label='PPP broadcast + NTRIP eph')
ax3.loglog(t/60, to_vertical(ppp_broadcast_cold_f9t_eph(t)), color='#2196F3',
           ls='--', lw=1.5, label='PPP broadcast + F9T eph (45s delay)')

# PPP precise: warm (10cm seed) vs cold
ax3.loglog(t/60, to_vertical(ppp_precise(t)), color='#4CAF50',
           ls='-', lw=2, label='PPP precise (cold, no seed)')
# Warm seed: shift time by ~10 min equivalent
ppp_warm = ppp_precise(t + 600)  # 10 min head start
ax3.loglog(t/60, to_vertical(ppp_warm), color='#4CAF50',
           ls='--', lw=1.5, label='PPP precise (warm, 10cm seed)')

# PPP-AR
ax3.loglog(t/60, to_vertical(ppp_ar(t)), color='#FF9800',
           ls='-', lw=2.5, label='PPP-AR + SSR')

ax3.set_xlabel('Survey time (minutes)')
ax3.set_ylabel('Vertical accuracy (m, 95%)')
ax3.set_title('Cold vs Warm Start: Vertical Accuracy', fontweight='bold')
ax3.set_xlim(0.1, 24*60)
ax3.set_ylim(0.005, 15)
ax3.axhline(y=0.3, color='gray', ls=':', alpha=0.4, lw=0.8)
ax3.axhline(y=0.03, color='gray', ls=':', alpha=0.4, lw=0.8)
ax3.text(24*60 * 0.7, 0.32, '30 cm', fontsize=7, color='gray')
ax3.text(24*60 * 0.7, 0.032, '3 cm', fontsize=7, color='gray')
ax3.grid(True, which='both', alpha=0.15)
ax3.legend(fontsize=7.5, loc='upper right')
fig2.tight_layout()

outpath2 = outdir / 'convergence-cold-warm.png'
fig2.savefig(outpath2, dpi=180, bbox_inches='tight')
print(f'Saved to {outpath2}')
