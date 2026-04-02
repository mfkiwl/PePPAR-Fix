# Visual Stories — TDEV Plots for PePPAR Fix

Each plot tells one story.  No overloading.

## Plot 1: PePPAR Fix vs a Commercial GPSDO

**Story**: At long tau, all GPSDOs converge to the same GNSS-derived
stability.  Differentiation happens at short tau, where the local
oscillator and discipline algorithm matter.  PePPAR Fix should compete
with or beat a high-quality commercial GPSDO.

**Traces**:
- PePPAR Fix disciplined PPS OUT (TICC chA), warm start
- Published TDEV from a reference GPSDO (HP/Agilent 58503B or
  Symmetricom/Microsemi SA.45s — find a published plot with
  comparable tau range)
- Optional: raw F9T PPS (undisciplined) as a "without discipline"
  reference

**X-axis**: tau from 1s through 10,000s (showing the crossover to
GNSS).

**Y-axis**: TDEV (ns).

**Key visual**: both curves converge at long tau (GNSS limit).  At
short tau, PePPAR Fix should be comparable or better, proving the
servo + qErr + PPP corrections are competitive.

**Shading**: one-sigma confidence band on PePPAR Fix trace.  The
commercial GPSDO likely shows only a nominal curve from the datasheet.

**Data needed**:
- [ ] Disciplined PePPAR Fix run on TimeHat, ~4 hours, TICC chA
- [ ] Published TDEV data from HP 58503B, Symmetricom SA.45s, or
      similar (digitize from datasheet or find tabulated data)

**Reference GPSDO candidates**:
- HP/Agilent 58503B: classic telecom GPSDO, OCXO, widely published
  TDEV curves.  Typical TDEV(1s) ~1 ns.
- Symmetricom/Microsemi SA.45s CSAC: chip-scale atomic clock with
  GPS discipline.  Published ADEV curves available.
- Jackson Labs Fury: OCXO GPSDO, spec sheet has ADEV curves.
- SRS FS740: GPS-referenced frequency standard with published TDEV.

Any of these would make a credible comparison.  The HP 58503B is the
most widely recognized benchmark.


## Plot 2: EXTTS Cannot Accurately Measure Raw F9T TDEV

**Story**: neither the i226 nor the E810 EXTTS path faithfully
reproduces the true F9T PPS jitter.  The TICC is the ground truth.
The gap between TICC and EXTTS is measurement error, not signal.

**Traces**:
- F9T PPS TDEV measured on TICC (ground truth, solid black)
- F9T PPS TDEV measured on i226 EXTTS (orange)
- F9T PPS TDEV measured on E810 EXTTS (purple)

**Shaded regions**:
- Between TICC and E810 EXTTS (purple shading):
  "Actual F9T TDEV unreported by E810 — quantization flatness
  (77% identical adjacent timestamps, ~8 ns effective resolution)"
- Between TICC and i226 EXTTS (orange shading):
  "i226 EXTTS measurement noise — 8 ns tick quantization adds
  ~2.9 ns RSS to the measurement"

**No qErr on this plot.**  This plot is purely about measurement
fidelity.

**X-axis**: tau 1s to ~100s (short tau where EXTTS limitations
are most visible).

**Data needed**:
- [x] TICC chB 2h baseline (data/ticc-baseline-2h-1.csv)
- [x] i226 EXTTS freerun 2h (data/freerun-timehat-2h.csv)
- [x] E810 EXTTS freerun 2h (data/freerun-ocxo-2h.csv)

**Note**: the E810 EXTTS trace is BELOW the TICC (falsely low TDEV)
— shade downward from TICC to E810.  The i226 EXTTS trace is ABOVE
the TICC (added noise) — shade upward from TICC to i226.


## Plot 3: Free-Running Oscillator Comparison (TCXO vs OCXO)

**Story**: the E810's OCXO should beat the i226's TCXO at all
taus.  This sets the discipline floor — the servo can't make the
PHC quieter than its oscillator at short tau.

**Traces**:
- i226 TCXO free-running PEROUT (TICC chA, TimeHat)
- E810 OCXO free-running PEROUT (TICC chA, ocxo)

**Both measured by TICC** — no EXTTS in this plot.  Pure oscillator
comparison at 60 ps resolution.

**X-axis**: tau 1s to 2000s.

**Shading**: one-sigma confidence band on each trace.

**Data needed**:
- [x] TimeHat TICC chA 2h (data/ticc-baseline-2h-1.csv)
- [x] ocxo TICC chA 2h (data/ticc-ocxo-2h.csv)

**Note from initial data**: surprisingly, the E810 OCXO PEROUT
showed 2.78 ns TDEV(1s) vs the i226 TCXO at 1.17 ns.  The E810
PEROUT appears coupled to the F9T PPS sawtooth internally.  If
this holds, the story becomes: "the E810's OCXO is excellent, but
its PEROUT output doesn't reflect the OCXO's true stability."
Investigate whether the E810 PEROUT is phase-locked to the PPS
rather than free-running from the OCXO.


## Plot 4: Disciplined PPS OUT — PPS vs PPS+qErr vs PPS+PPP

**Story**: each correction layer improves the disciplined output.
PPS+qErr should beat raw PPS.  PPS+PPP should beat PPS+qErr.

**One plot per host** (TimeHat and ocxo, if both have TICC on chA).

**Traces** (all measured on TICC chA = disciplined PEROUT):
- Disciplined with raw PPS only (servo source = PPS)
- Disciplined with PPS + qErr (servo source = PPS+qErr)
- Disciplined with PPS + PPP (servo source = PPS+PPP)

**All warm start** — after servo has settled (skip first 5-10 minutes
of convergence).

**X-axis**: tau 1s to crossover point with GNSS (~1000s), or a
little beyond.

**Shading**: one-sigma confidence band on each trace.  Shade the
improvement region between raw PPS and the best correction.

**Data needed**:
- [ ] TimeHat disciplined run, PPS-only, ~2 hours, TICC chA
- [ ] TimeHat disciplined run, PPS+qErr, ~2 hours, TICC chA
- [ ] TimeHat disciplined run, PPS+PPP, ~2 hours, TICC chA
- [ ] Same three on ocxo (if TICC #2 chA carries PEROUT)

**Note**: these are disciplined runs, not freerun.  The servo must
be actively applying corrections.  The TICC chA measures the result
on the PEROUT.


## Plot 5: Our Measurements Are Valid

**Story**: our best measurement (TICC+qErr corrected F9T PPS) is
well above the noise floor of the measurement setup.  We are
measuring real signal, not instrument noise.

**Traces**:
- TICC+qErr corrected F9T PPS (our best result, 170 ps at τ=1s)
- TICC+GPSDO OCXO measurement noise floor (gray shaded)
  - Component: TICC 60 ps white phase noise (τ⁻¹)
  - Component: Geppetto GPSDO OCXO stability (rises at long τ)

**Shading**:
- Gray: measurement floor region (everything below this is
  instrument noise)
- One-sigma confidence band on the TICC+qErr trace

**X-axis**: tau 1s to 600s.

**Key visual**: clear daylight between the green trace and the gray
floor at all taus.  At τ=1s, the measurement (170 ps) is 5× above
the floor (35 ps).

**Data needed**:
- [x] TICC+qErr v2 30m (data/ticc-qerr-v2-30m.csv)
- [x] GPSDO OCXO ADEV estimate (5×10⁻¹²/√τ, 5×10⁻¹⁴ flicker)

**Note**: the floor at long τ is estimated from the GPSDO spec, not
measured independently.  The uncommissioned rubidium oscillators
would provide a tighter floor characterization.


## Common conventions across all plots

### One-sigma shading

Every TDEV trace should show a one-sigma confidence band.  For
overlapping Hadamard (ohdev) estimators, the confidence interval
is approximately:

    σ_TDEV ≈ TDEV / sqrt(N_effective)

where N_effective ≈ (total_samples - 3*tau) / tau for non-overlapping,
or the EDF (equivalent degrees of freedom) for overlapping.  A
simpler approximation for plotting:

    upper = TDEV * (1 + 1/sqrt(N_eff))
    lower = TDEV * (1 - 1/sqrt(N_eff))

Use translucent fill matching the trace color.

### Duration matching

When comparing two traces on the same plot, use the same observation
duration.  The F9T PPS sawtooth has observation-length-dependent TDEV
(2h gives 2.3 ns, 30 min gives 1.0-1.4 ns).  Mismatched durations
produce misleading comparisons.

### Tau range

Most plots should focus on tau 1s to 1000s.  Beyond 1000s, all
GPS-disciplined sources converge to the same GNSS limit and the
differentiation story ends.  Exception: Plot 1 (commercial GPSDO
comparison) should extend to 10,000s to show the convergence.

### Detrending

Freerun data (Plots 2, 3) must be linearly detrended to remove the
oscillator's frequency offset before computing TDEV.  Disciplined
data (Plots 1, 4) should not be detrended — the servo's residual
frequency error is part of what we're measuring.
