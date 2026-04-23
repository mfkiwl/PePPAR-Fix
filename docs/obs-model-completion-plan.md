# Observation-model completion plan

A phased work spec for closing the observation-model gap between
our PPP filter and PRIDE-PPPAR, which reached 3.3 mm 3D on ABMF
2020/001 where we're at 5.7 m.  Path forward chosen because the
two alternatives — particle filter replacement and adaptive
structural constraints — are either not viable at PPP state
dimensionality (PF) or unproven to selectively disable on clean
geometry (constraint falsifications on 2026-04-23).

## Context

The 2026-04-23 PRIDE investigation arc (see
`feedback_math_check_and_set_ceiling.md`) concluded:

1. The null mode is geometric — tuning can't eliminate it.
2. Blunt structural constraints (ZTD-tie, OU-ZTD) fight real
   physics on clean geometry — falsified on their critical
   control runs.
3. Completing the observation model starves the null mode at
   the residual source.  Residuals at truth get small enough
   that the null direction has nothing to drive it.

Mainstream alternative estimators don't help us:

- **Particle filter**: scales exponentially with state
  dimension.  PPP has ~20-30 states; PF would need millions of
  particles.  Not used by any mainstream PPP implementation.
- **Factor graph optimization**: batch-like, 2024-era research
  direction.  Different tool with different tradeoffs.
  Deferred as a future architectural question.

The EKF we have is the industry-standard PPP estimator.  The
gap is observation model, not estimator.

## Measured magnitudes

Bravo's 2026-04-23 PRIDE ablation (see
`project_to_main_pride_ablation_20260423.md`) directly measured
each correction's contribution on ABMF 2020/001 GPS+GAL static
float:

| Correction | Contribution (mm) | Mechanism |
|---|---|---|
| **Solid Earth tides** | **42** | Gravity-driven site displacement, ±10 cm vertical |
| **Satellite PCVs** | **~5-15** (inferred) | Apparent transmit-point motion with nadir angle |
| **Receiver PCVs** | **~5-10** (inferred) | Antenna phase-center offset with elevation |
| **Phase wind-up** | **~1-5** (inferred) | Satellite-receiver antenna orientation |
| **GMF/Niell mapping** | **~1-5** (inferred) | Replaces `1/sin(elev)` with higher-order atmos model |
| **Satellite attitude** | **~1-5** (inferred) | Yaw-steering during eclipse |
| **Tropo gradients** | **~sub-mm at ABMF** | Horizontal variation in atmospheric delay |
| Ocean tidal loading | < 0.4 | Coastal / high-latitude only |
| Pole tides | < 0.8 | Polar-motion-driven |

Totals: directly measured = 42 mm (solid tide) + < 1 mm
(ocean + pole).  Remaining ~100 mm comes from the other
pieces collectively.  Each of those is plausible 1-15 mm
based on literature.

## Scope

**In scope**: implement the top six corrections — solid tide,
satellite PCVs, receiver PCVs, phase wind-up, GMF mapping,
satellite attitude.  Each component lands as a separate commit
with its own unit tests and its own verification against PRIDE's
same-config run where possible.

**Explicitly out of scope (now)**:

- Ocean tidal loading (< 0.4 mm at ABMF; irrelevant until
  everything else is sub-cm).  Coastal / polar sites would
  need it eventually.
- Pole tides (< 0.8 mm; same reasoning).
- Tropospheric horizontal gradients (adds 2 filter states;
  sub-mm at ABMF).  Revisit only if the six corrections get
  us to < 1 cm and we need more.
- Re-architecting the estimator (EKF → factor graph or
  similar).  Separate question; not this spec.

## Priority order

Fixed by magnitude + dependency structure.  Bravo already did
the first one on the harness.

### Phase 1 — Solid Earth tides (largest single contributor)

**Status**: landed on `pride-harness-ar` at `44a7e49`, measured
−39 mm on clean GAL+BDS (matches PRIDE's 42 mm ablation within
noise), −4 mm on null-mode GPS+GAL (noise).  Engine port
pending.

**Scope**:
- Port `scripts/regression/solid_tide.py` (IERS 2010 Step-1)
  from `pride-harness-ar` into a module accessible to the
  engine's `PPPFilter.update()`.
- Plumb receiver ECEF position into the tide module; compute
  ECEF displacement per epoch.
- Add displacement to the observation model (subtract from
  truth position when computing predicted range).
- Unit tests verifying ~150 mm vertical peak amplitude at
  reasonable latitudes.

**Effort**: ~1 session (code exists; porting + integration).

**Verification**: re-run lab with port enabled; measure Q2
ratio improvement.  Expect ~40 mm improvement on clean-
geometry hosts; no change on trapped hosts (null mode eats
the gain).

### Phase 2 — Satellite PCVs + Receiver PCVs (shared infrastructure)

**Status**: not started.

**Scope**:
- Add `ANTEXParser` class to `scripts/ppp_corrections.py`
  (or a sibling module).  IGS14.atx format is standard;
  ~200 lines for parser plus per-SV/per-receiver lookup.
- Per-epoch per-SV nadir-angle calculation (satellite frame
  direction to receiver) for satellite PCVs.
- Per-epoch per-SV elevation + azimuth for receiver PCVs.
- Bilinear interpolation on the ANTEX grid.
- Apply correction at observation ingest: subtract PCV from
  observed phase.
- Unit tests against known PRIDE-matching values for a
  sampled (SV, elev, az) point.

**Effort**: ~1-2 sessions (ANTEX parser is the long pole;
rest is mechanical).

**Verification**: re-run lab with sat+rx PCVs enabled; expect
~10-25 mm additional improvement on clean-geometry hosts.

**Dependencies**: IGS14.atx (or newer) file cached in the
repo or pulled on demand.  ~2 MB.

### Phase 3 — Phase wind-up

**Status**: not started.

**Scope**:
- Well-known formula (Wu et al. 1993).  ~30 lines.
- Needs satellite orientation (can approximate yaw = 0 for
  non-eclipse SVs; phase 4 handles eclipse SVs properly).
- Tracks cumulative wind-up per (SV, receiver) pair across
  epochs.  Small state addition.

**Effort**: < 1 session.

**Verification**: expect ~1-5 mm improvement on long
observation arcs.

### Phase 4 — GMF/Niell tropo mapping

**Status**: not started.

**Scope**:
- Replace `1/sin(elev)` with GMF (Böhm et al. 2006) or
  Niell mapping function.
- Both have published implementations.  GMF is newer and
  matches modern IGS products.  ~50 lines for the function
  itself; small change to the filter's mapping application.
- Unit tests against known values at low elevations.

**Effort**: < 1 session.

**Verification**: improvement mostly at low elevations
(below ~15°).  May be more visible in slip-storm recovery
or at steep-multipath sites than in static baseline.

### Phase 5 — Satellite attitude model

**Status**: not started.

**Scope**:
- WUM ATT.OBX file format parser (or equivalent from other
  analysis centers).
- Per-epoch per-SV yaw angle lookup.
- Feeds Phase 3 (wind-up) during eclipse periods.
- Only matters for satellites currently in eclipse; otherwise
  yaw = heliocentric yaw-steering default.

**Effort**: ~1 session (file format + integration).

**Verification**: expect ~1-5 mm improvement, mostly during
eclipses.

## Verification strategy

After each phase lands:

1. **Harness regression** on ABMF 2020/001 — component's
   measured delta should match PRIDE ablation for that
   component (within ~20%).
2. **Lab re-run** on L5 fleet — measure Q2 ratio change.
   Expected: clean-geometry (TimeHat-style healthy runs)
   improves by component's magnitude; trapped-geometry
   doesn't improve noticeably (null mode masks gains until
   enough obs-model is in to starve it).
3. **PRIDE comparison** — Bravo's pdp3 baseline on ABMF
   stays our external ceiling.  Our number should
   monotonically approach 3.3 mm as phases land.

## Break-even checkpoint

After Phase 1 (solid tide) + Phase 2 (PCVs) land — the two
largest magnitudes — expect:

- ABMF harness: 5.7 m (today) → plausibly 100-500 mm on
  GPS+GAL (1-2 orders of magnitude).
- Lab Q2 ratio: current 10-250× → plausibly 3-10× (honest
  filter within one order of magnitude).

That checkpoint is the decision point for whether Phase 3-5
are worth chasing or whether we've closed enough of the gap
to reprioritize other work (Q3 slip-storms, cross-host
agreement characterization, engine-side σ re-tuning with
completed obs-model).

## Engine-side sequencing

All six corrections live in the **observation ingest path**,
not the filter update math.  That means:

- Engine and harness share the same correction implementations
  (both call into the shared observation-prep code).
- No filter-state changes.  No process-noise tuning.  No
  observability changes to the estimator.
- Porting cost per correction is low once the harness-side
  implementation exists — just plumbing the engine's obs path
  to the shared code.

Lab-lock-in risk is low per phase because:

- Each correction reduces residuals (never inflates them).
- A filter that was running without the correction is seeing
  a biased observation; adding the correction un-biases it.
- The lab-specific failure mode of the σ_phi port
  (over-weighting PR on marginal low-elev SVs) doesn't recur
  here — we're changing the predicted observation, not the
  noise model.

## What this doesn't fix

Even with complete obs-model, two things remain:

1. **Q2 overconfidence** on our lab's real-time SSR stream.
   σ_phi=0.03 is likely still too tight; a lab-local sweep
   with completed obs-model will find the right value.
   Queue for after Phase 2.
2. **Null-mode drift in the strictest geometry** (GPS-only
   SP3).  PRIDE's 6.4 mm on that config proves it's
   *tunable*, not absent.  If we ever run single-constellation
   in production, need a second tuning pass.

Neither is blocking.  Neither requires the alternatives
(adaptive constraints, factor graph, PF) that we've ruled out.

## Open questions

- **ANTEX file versioning**.  IGS14.atx is current.  Future
  IGS20 is coming.  Pin a specific version or accept whatever
  the repo has.  Probably pin + document.
- **Per-SV attitude data source**.  WUM is what PRIDE uses.
  Other ACs (CNES, CODE) publish their own.  Do we need
  AC-specific attitude files?  Or does the WUM data match
  CNES/CODE orbits closely enough to reuse?
- **ANTEX path for engine deployment**.  Lab hosts need the
  ~2 MB file.  Check into repo, fetch at install, or
  download at first-start?

These are operational details, not blockers.  Answer during
Phase 2.

## Who does what

- **Bravo** stays on Q3 (slip-storm handling on WUH2
  2023/002) until it lands.  Obs-model work is strictly
  queued behind.
- **Main** writes the engine-side port when each phase's
  harness implementation lands, or picks up components
  directly if Bravo stays on Q3 longer.
- **Engine** continues on `--wl-only` with current tuning
  through all phases.  No servo impact.  No PPS impact.
