# Proposal: replace MW-residual rolling mean with a phase-only post-fix drift signal

**Author**: Charlie, 2026-04-28
**Status**: proposal — awaiting consensus
**Supersedes**: nothing yet; orthogonal to the in-flight
I-153334-main adaptive-threshold work, which can land first as a
short-term mitigation
**References**:

  - `project_wl_drift_vs_bnc_finding_20260428` — chance-corrected
    Z-test showing engine `WL_DRIFT` is statistically uncorrelated
    with BNC slips (Z = −0.17, p = 0.86)
  - `project_wl_drift_smooth_float_signal_20260428` — direct
    AMB-stream probe on three anti-correlated SVs (E29 / E21 /
    E19) showing BNC's filter state drifts smoothly through
    engine `wl_drift` trips with locked integer + stable σ
  - `docs/bnc-log-reference.md` — BNC `.ppp` log line types and
    AMB / RES / RESET semantics
  - `scripts/overlay/wl_drift_bnc_validate_v2.py` — chance-corrected
    validator (commit 057b6c3)
  - `docs/misnomers.md` — `WlDriftMonitor` entry (Dangerous)

## Problem statement

`WlDriftMonitor` watches the rolling mean of the per-SV
**Melbourne-Wübbena combination residual** post-fix.  In WL cycles:

```
MW residual = (φ_L1 − φ_L5) − ((f_L1 + f_L5) / (f_L1 c)) · (PR_L1 + PR_L5)
            ≈ phase_residual − pseudorange_residual_in_WL_cycles
```

The combination is dominated by pseudorange noise.  A non-zero
rolling mean of MW residual can originate on either side:

  - **Phase-side**: real cycle slip the cycle-slip-flush detector
    missed; wrong WL integer committed; sub-cycle phase drift.
    These are the events the monitor *should* fire on.
  - **PR-side**: code multipath, code-bias drift (SSR product
    latency, time-varying TGDs), receiver front-end PR shifts.
    These are **not** legitimate demotion grounds for carrier-phase
    tracking — phase tracking is unaffected by PR noise.

Empirical 2026-04-28 finding: PR-side is the dominant source.
WL_DRIFT events on the day0427night 3-host overnight correlated
with BNC's IF-phase-only slip detector at exactly chance level
(Z = −0.17).  Cycle-slip-flush, the engine's separate slip
detector, correlated at Z = +11.2.

For carrier-phase tracking, only the phase side matters.  The
right signal is one that is sensitive to phase events but
insensitive to PR noise.

## What BNC / RTKLIB does

BNC's `.ppp` log shows BNC's PPP filter operating on the
**ionosphere-free phase combination** (`lIF`) per SV per epoch.
The internal slip detection (cycle-slip-flush analog) uses the
**geometry-free combination** of phase observations:

```
GF = φ_L1 − φ_L5    (in metres or in cycles, sign convention varies)
```

GF is:

  - **Phase-only** — no pseudorange contamination
  - **Per-SV** — needs no inter-SV cross-checking
  - **Geometry-free** — insensitive to position errors, satellite
    motion, and most non-iono environment effects
  - **Slowly varying with iono** — TEC variation produces a
    smooth GF drift; a real cycle slip produces a step

A real cycle slip on either L1 or L5 produces a sharp GF jump
(λ_L1 ≈ 19 cm or λ_L5 ≈ 25.5 cm).  Slow iono drift produces a
ramp on the order of a few cm / minute under normal conditions,
and 10s of cm / minute under storm conditions.  The two
signatures are separable by step-vs-ramp detectors.

PRIDE `tedit` uses the LC + MW + LLI triple — same idea.

## Proposal — phase-only post-fix drift detector

Replace the MW-residual rolling-mean signal with one built on the
**geometry-free phase combination** (GF), tracked per SV.

### Signal definition

Per epoch, per fixed-WL SV:

```
gf_phase = φ_L1 − φ_L5    (cycles, expressed in WL cycles for
                           consistency with the existing WL float
                           bookkeeping)
```

Take the per-SV first difference Δgf_phase across adjacent epochs.
A real WL-relevant phase event produces a step in gf_phase, hence
a spike in Δgf_phase.  Slow iono produces small Δgf_phase (a ramp
in gf_phase, smooth in Δgf_phase).

Rolling-mean signal:

```
gf_drift_cyc = mean over the last N epochs of (Δgf_phase
              − model_iono_drift)
```

`model_iono_drift` is the expected iono drift per epoch (from the
broadcast Klobuchar coefficients or an SSR iono model).  Removes
the slow iono ramp; leaves true phase events.

If a model is unavailable, use the cohort median (Δgf_phase
median across all currently-fixed SVs) as a poor-substitute
common-mode iono estimate.

### Trip condition

```
|gf_drift_cyc| > threshold_cyc   for ≥ min_samples
                                 over rolling window of N epochs
```

Same shape as the existing `WlDriftMonitor` API; only the input
signal changes.

### Threshold calibration

Initial proposal: rolling mean threshold of 0.05 cyc on Δgf_phase
(roughly 1 cm at L1 wavelength), window = 30 epochs, minimum 15
samples.  These are starting points; tune empirically against
BNC's slip events using the v2 validator.  The success criterion
is that the chance-corrected excess vs BNC matches
cycle-slip-flush's +12 % rather than WL_DRIFT's −0.2 %.

### What this catches that the existing detector doesn't

Cases where:

  - The committed WL integer is wrong by ≥ 1 cycle (rare; original
    use case)
  - A small phase event slipped past the cycle-slip-flush detector
    (sub-threshold MW jump, sub-threshold GF jump, but persistent
    drift over multiple epochs)

### What this *correctly* doesn't catch

  - PR multipath bursts
  - Code-bias step events (SSR product latency)
  - Receiver front-end PR shifts (temperature-driven autocorrelator
    drift)

These are the events the current detector fires on, and exactly
the events BNC's filter ignores.  Eliminating them from the
demotion stream is the goal.

### Migration path

Phased to keep current behaviour during validation:

  1. **Implement alongside, log only** (~1 day).  New
     `GfPhaseRollingMeanMonitor` class; engine logs both
     `[WL_DRIFT]` and `[GF_DRIFT]` events.  Demotion still driven
     by old `WlDriftMonitor`.
  2. **Validate on overnight data** (1–2 nights).  Run v2
     validator on `[GF_DRIFT]` events; compare chance-corrected
     excess vs BNC.  Success: GF excess > 10 % above chance.
  3. **Switch demotion to GF detector** with old monitor still
     running for one more night as a comparison signal.  Watch
     for regressions (slip-storm catching).
  4. **Remove old WL drift monitor** if GF monitor performed
     correctly through the transition.

### Cost estimate

  - GF combination computation: already in the codebase for
    cycle-slip-flush (`gf_jump` reason).  Reuse.
  - New monitor class: ~150 lines (mirrors `WlDriftMonitor`
    structure, different input).
  - Iono-model integration: ~30 lines wiring + cohort-median
    fallback.
  - Engine integration: ~30 lines (parallel call site to the
    existing one).
  - Tests: ~100 lines (unit tests + a regression scenario from
    the day0427night log).

Total: ~half-day implement + 1–2 nights validate + half-day
migrate.  Comparable to I-153334-main's adaptive-threshold work.

## Relationship to I-153334-main (adaptive threshold by integer
history)

These are complementary, not competitive:

  - **I-153334-main is a short-term mitigation** that turns the
    noise floor down on Pop A SVs.  It still operates on the same
    PR-contaminated MW residual; it just demotes less often when
    the integer history says the SV is stable.  Lands quickly.
  - **This proposal is the structural fix.**  Replacing the input
    signal addresses the root cause; once the GF detector is in
    place, the adaptive threshold can be revisited (likely
    simplified or removed, since the underlying signal is now
    honest).

Land I-153334-main first for the immediate FP relief.  Validate
the GF detector against BNC over 1–2 nights afterwards.  Switch
the demotion driver when validation lands.

## Risks and open questions

  1. **Iono-model accuracy under storm conditions.**  A poor iono
     model would leak ramp into Δgf_phase and produce false
     positives at sunrise / sunset.  Cohort-median fallback should
     be robust under shared-antenna conditions but degrades on
     single-receiver setups.  Worth testing under sunrise.
  2. **Single-frequency receivers.**  A receiver tracking only L1
     has no GF combination available.  PePPAR-Fix targets L1+L5
     so this isn't a current concern, but the new monitor should
     fail gracefully on SVs that lack dual-frequency observations.
  3. **Computation cost.**  GF differences are O(N_SV) per epoch;
     well within the existing per-epoch budget.
  4. **What if GF detector also performs at chance vs BNC?**
     Possible but unlikely — RTKLIB inside BNC uses GF for slip
     detection, so we'd be implementing the same thing.  If this
     happens, the conclusion is that BNC's slip detector and ours
     are looking at differently-processed phase data
     (post-correction vs pre-correction, e.g.) and we need to
     align further upstream.  This is a recoverable failure mode
     — the v2 validator will tell us.

## Decision needed

Three options for the consensus:

  - **(a)** Land I-153334-main now; defer GF redesign as a future
    item.  Ship adaptive thresholds as the operational fix.
  - **(b)** Land I-153334-main now; concurrently build the GF
    detector as a logging-only signal (phase 1 of migration); use
    the next 1–2 nights of overlapping logs to validate; decide
    on full migration after data.
  - **(c)** Skip I-153334-main, go directly to the GF redesign.
    Risks delay if validation surfaces issues.

Recommendation: **(b)**.  The adaptive-threshold lands the
immediate operational improvement; the GF detector lands as a
parallel observation channel that we can validate cheaply against
BNC.  If validation succeeds, we have a clean structural fix; if
it doesn't, we've lost only the build cost and learned something.
