# Slip detector design notes — GF vs LC, rolling mean vs ramp model

Reference material informing slip-detector design choices in PePPAR-Fix.
Captured 2026-04-28 during the dayplan discussion of how PRIDE's and
BNC's GF-based detectors differ.  Live use today: `GfPhaseRollingMeanMonitor`
(commit `b9d40ab`, observe-only) per the redesign in
[`docs/wl-drift-redesign-proposal.md`](wl-drift-redesign-proposal.md)
(dayplan I-163535).  These notes exist so future iterations of that monitor
have the design space available without a fresh literature dive.

## Background: what GF measures

The geometry-free combination GF = φ_L1·λ_L1 − φ_L5·λ_L5 (in metres) per SV
cancels:

- Geometric range (same SV-RX path on both frequencies)
- Tropospheric delay (non-dispersive)
- Receiver clock, satellite clock (same on both frequencies)

What remains:

- **Carrier-phase ambiguity term** `λ₁·N₁ − λ₂·N₂` — constant per arc,
  until a real cycle slip
- **Ionospheric delay differential** `(1 − γ)·I_L1` where γ = (f₁/f₂)² ≈ 1.65 —
  varies with TEC; this is *the* "geometry-free" signal
- Per-SV hardware biases (small, mostly stable)

So GF over a stable arc is approximately `constant + iono_term(t)`.

A real cycle slip appears as a single-epoch step of λ₁ ≈ 19 cm or λ₂ ≈ 25 cm
on top of whatever the iono term is doing.  The detector's job: see the
step, ignore the iono drift.

## Axis 1: order of the iono model

This is the bigger of the two axes.

### Zeroth-order (BNC, our `GfPhaseRollingMeanMonitor`)

Maintain rolling mean of GF over the last N epochs.  At each new epoch,
compute `|GF(t) − rolling_mean|`; if it exceeds threshold, declare slip.

Implicit model: iono is *constant* within the window.  The mean lags
real iono drift by ~half-window × `dI/dt`, which becomes the noise floor
your slip threshold has to clear.

### First-order (PRIDE tedit, RTKLIB IONO test)

Fit `I(t) = I₀ + (dI/dt)·t` over the window.  Residual = GF − fit.
Threshold on residual size, not raw GF.

The noise floor now scales with iono *acceleration* × window² rather
than rate × window.  Same window length, much better SNR for slip-vs-iono.

### Higher-order

Quadratic + residual catches iono curvature.  Kalman-filtered iono delay
estimate gives a continuous estimate with uncertainty.  Diminishing returns
beyond first-order for typical conditions; complexity scales with model
order while marginal slip-detection sensitivity flattens.

### When the difference matters

At **mid-latitude under quiet conditions**, the iono term varies on
~10s-minute timescales.  At 1 Hz cadence with a 30-second rolling window,
zeroth-order has ~30:1 SNR on a slip-vs-iono comparison: rolling mean wins
on simplicity with no measurable cost.

Under **fast iono activity** the picture changes:

- **Sunrise / sunset TEC gradients** — rapid ionosphere flux over ~minutes
- **Solar storms** — tens of minutes of elevated TEC variability
- **Equatorial scintillation** — sub-minute rapid amplitude changes

In these regimes the rolling-mean noise floor inflates and either:

1. The detector's threshold has to widen to avoid false positives (small
   slips slip through)
2. The window has to shrink (less averaging, lower SNR for slips that
   ARE detected)

Both paths sacrifice slip-detection sensitivity.  First-order ramp
preserves both window length and threshold under faster iono activity.

The 04-22/23 sunrise TEC slip storm (memory:
`project_sunrise_slip_storm_lambda_fallback`) was exactly this regime.
That storm is what originally motivated `wl_drift`; it is the regime
where `GfPhaseRollingMeanMonitor` is most likely to expose its noise floor.

## Axis 2: GF + LC cross-check vs GF alone

GF catches per-band slips by exposing the iono-bearing component.  But
there's a slip pattern it misses entirely: a **same-direction integer
slip on both bands** — `N₁` and `N₂` both jump by the same number of
cycles — cancels in GF and shows nothing.

LC = ionosphere-free combination ≈ 2.55·φ_L1 − 1.55·φ_L5 catches that
case.  The two-band coefficients differ, so a same-direction slip leaks
through.

PRIDE runs both detectors and OR's the trip.  BNC mostly relies on GF
alone — the rationale being that same-direction integer slips are rare
in typical receiver behavior (a slip on one band is much more common
than a coordinated slip on both).

For us: GF alone is fine 95% of the time.  Adding LC cross-check would
catch the remaining 5% but doubles the per-epoch work and the
maintenance surface.  Worth keeping as an option for the redesign's
second iteration if silent slips appear in cross-host integer
comparisons.

## Tradeoff: model order vs start-of-arc latency

More sophisticated models have more parameters to estimate per arc.
That means more samples per arc before the model is "ready" to detect
slips.

- Rolling mean: ready after N samples (N = window length); simple,
  robust
- Linear ramp: needs slightly more samples before the slope estimate
  converges; better noise floor under iono activity
- Higher-order: still more; marginal gains

For first-of-arc slips (a slip in the first ~30 seconds of an SV's
visibility), the simpler detector wins by being ready sooner.  After
that, the more sophisticated detector wins on noise floor.

Phased deployment can have it both ways: a simple detector for the
first N samples, then transition to the modeled detector once it's
ready.  Not in scope for I-163535's first iteration; flagged here for
v2.

## When this matters for our deployment

Lab context:

- Mid-latitude (DuPage County, IL, ~42° N)
- Suburban — modest multipath, no equatorial scintillation
- 1 Hz GNSS cadence
- Slips are integer-cycle (large; far above the noise floor under
  quiet conditions)

Conclusion: rolling-mean is appropriate as the **default**.  The iono
drift in our normal weather is well within the noise floor, and we get
the simplicity advantage.

The two scenarios where v1 (rolling mean) is most likely to expose its
limits:

1. **Sunrise TEC storms** at the lab (re-occurring, predictable hours)
2. **Solar flares / geomagnetic storms** (rare, but real)

In both cases the storm symptom would be: the GF detector's
chance-corrected excess (versus BNC AMB integer-jump ground truth — the
methodology Charlie established in `wl_drift_bnc_validate_v2.py`)
*decreases* during the storm hours, because real slips are getting
buried in iono ramps that the zeroth-order model can't subtract.  If
that signature shows up in storm-window analysis, v2 = first-order ramp.

## Future iteration triggers

The "backlog" — concrete signals that should promote a v2 iteration of
the GF detector.

### Trigger 1: storm windows show degraded GF-vs-truth excess

After v1 ships and we have a few weeks of data, run the BNC-AMB-validator
on storm-window subsets vs quiet-window subsets.  If the chance-corrected
excess drops materially (say > 5 percentage points) during storms, the
rolling-mean noise floor is the bottleneck.  Promote to first-order ramp.

### Trigger 2: silent slips appear in cross-host integer comparison

If MadHat and clkPoC3 (same UFO1 antenna) ever fix to genuinely different
NL integers on the same SV at the same epoch, that's a strong indicator
of a missed slip on at least one host.  If those mismatches correlate
with quiet GF detector output, it's the same-direction-on-both-bands
case GF is blind to.  Add LC cross-check.

### Trigger 3: first-of-arc slip prevalence

If post-fix integer mismatches concentrate in the first ~30 seconds of
arcs, the rolling-mean's start-of-arc readiness gap is the issue.
Add a simple-detector / modeled-detector phase transition.

## References

- [`docs/wl-drift-redesign-proposal.md`](wl-drift-redesign-proposal.md) — the proposal that introduced the GF detector (commit `e2ab81a`)
- `scripts/peppar_fix/gf_phase_monitor.py` — the live observe-only detector (commit `b9d40ab`)
- `scripts/overlay/wl_drift_bnc_validate_v2.py` — the BNC AMB integer-jump validator that established the chance-corrected statistics methodology (Charlie, dayplan I-150009)
- PRIDE tedit handling of GF + LC: see memory `project_to_main_pride_discontinuity_finding_20260427` (file:line pointers into `src/tedit/`)
- 04-22/23 sunrise TEC storm context: memory `project_sunrise_slip_storm_lambda_fallback`
- BNC empirical validation (the chance-corrected -0.2% vs +12.4% finding):
  Charlie's `project_wl_drift_vs_bnc_finding_20260428`
