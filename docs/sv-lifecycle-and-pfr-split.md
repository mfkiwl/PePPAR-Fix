# Per-SV Lifecycle and the PFR Split

*Design doc 2026-04-19 — introduces a per-SV state machine to
separate "is this SV's integer correct?" (Job A) from "is this SV
still useful as it sets?" (Job B).  Supplements, does not replace,
the host-level `AntPosEstState` machine documented in
`docs/state-machine-refactor-plan.md`.*

## Why this exists

Two clean observations from day0419c/d test runs drove this:

1. **Post-Fix-Residual monitor (PFR) is doing two structurally
   different jobs with one threshold.**  Both "did LAMBDA pick a
   wrong integer 5 min ago?" and "has this SV degraded as it
   descends through 20°?" share the same 3 m per-SV gate.  Neither
   is well-served: wrong-integer detection needs a time-of-flight
   probation that scales with geometry change, not wall-clock;
   retirement needs elevation-weighted slack.

2. **Nothing today distinguishes a new-fix ambiguity from a
   weeks-old validated one.**  The filter treats every NL-fixed
   SV as equally trustworthy.  That's why a single wrong-integer
   event propagates into host-wide RMS drift, trips PFR L1, fails
   to clear, escalates L2→L3, and loses ~10 min of convergence —
   **even when the other NL-fixed SVs have been correct and stable
   for 45 min.**

See `project_pfr_too_aggressive_20260419.md` for the observational
evidence and `project_bds_ar_first_success_20260419.md` for the
baseline multi-constellation AR performance this design targets.

## Two state machines, two scopes

The system has two orthogonal state machines.  They interact but
are not nested.

| Scope | Name | States | Where |
|---|---|---|---|
| **Host** | `AntPosEstState` | UNSURVEYED, VERIFYING, VERIFIED, CONVERGING, RESOLVED, MOVED | `docs/state-machine-refactor-plan.md` — unchanged |
| **Per-SV** | `SvAmbState` (new) | FLOAT, WL_FIXED, NL_PROVISIONAL, NL_VALIDATED, RETIRING, BLACKLISTED | **This document** |

**Host state answers**: is the receiver's *position* trustworthy
right now?  Does the DOFreqEst thread have permission to servo?
Does NAV2 agree we haven't moved?  One value for the whole host.

**Per-SV state answers**: is the ambiguity for *this specific
satellite* integer-fixed, and how confident are we?  One value per
(system, PRN).  The host has a fleet of these.

They interact via a simple rule: **the host is RESOLVED iff ≥ N
SVs are in NL_VALIDATED (where N is something like 4).**  SVs in
NL_PROVISIONAL count internally for position tightening but don't
contribute to the host-level RESOLVED declaration.  This prevents
a lucky-noise fix from pulling the host into RESOLVED prematurely.

## The per-SV state machine

```
                    ┌─────────────┐
                    │ BLACKLISTED │
                    └──────┬──────┘
                           │ cooldown expires
                           ▼
     ┌──────────┐   MW      ┌───────────┐   LAMBDA    ┌────────────────┐
     │          │  converge │           │  ratio > τ  │                │
     │  FLOAT   ├──────────►│ WL_FIXED  ├────────────►│ NL_PROVISIONAL │
     │          │           │           │             │                │
     └──────▲───┘           └──────▲────┘             └─────────┬──────┘
            │                      │                            │
            │    cycle slip        │       Δaz < 15°,           │ Δaz ≥ 15°
            │    or Job A reject   │       recent-fix residual  │ AND PR resid
            │                      │       exceeded             │ clean over
            │    ┌─────────────────┘                            │ validation
            └────┤                                              ▼ window
                 │                                    ┌──────────────────┐
                 │                                    │   NL_VALIDATED   │
                 │                                    │                  │
                 │                                    └────────┬─────────┘
                 │                                             │
                 │         elev-weighted PR resid              │
                 │         > retirement threshold              │
                 │                   OR                        │
                 │         elev < retirement mask              │
                 │                                             ▼
                 │                                    ┌─────────────────┐
                 │         elev < AR mask             │    RETIRING     │
                 └────────────────────────────────────┤                 │
                                                      └─────────────────┘
```

### States

**FLOAT** — Ambiguity is being estimated.  Contributes to filter
geometry but not to AR.  Default state for a newly-seen SV.

**WL_FIXED** — Melbourne-Wübbena wide-lane integer has converged.
NL float remains; SV still in "float geometry" for host purposes.

**NL_PROVISIONAL** — LAMBDA has declared an NL integer that passed
the ratio test.  The filter uses the integer internally (tighter
σ), but the host does not count this SV toward its RESOLVED
quorum.  This is the probation window.

**NL_VALIDATED** — The NL integer has survived sufficient geometry
change (e.g., ≥15° satellite azimuth motion) with post-fit
residuals staying within the elevation-weighted gate.  Now counts
toward host RESOLVED.

**RETIRING** — The SV is descending through the retirement
elevation band (say 20°-mask).  Its residuals are inflating as
expected from physics; the retirement logic releases the integer
gracefully.  Does not count toward host RESOLVED once in this
state.

**BLACKLISTED** — Temporarily excluded from AR.  Entered via cycle
slip detection, repeated LAMBDA failures, or Job A rejection.
Cooldown (currently `blacklist_epochs = 60`) then FLOAT re-entry.

### Transitions

| From | To | Trigger |
|---|---|---|
| FLOAT | WL_FIXED | MW tracker converges (frac < 0.1, n ≥ 60 epochs) |
| WL_FIXED | NL_PROVISIONAL | LAMBDA ratio > τ_ratio, σ_N1 < threshold |
| NL_PROVISIONAL | NL_VALIDATED | Δaz ≥ 15° AND PR residual stayed in elev-weighted gate for full window |
| NL_PROVISIONAL | FLOAT | Job A: PR residual exceeded gate during probation (likely wrong integer) |
| NL_VALIDATED | RETIRING | elev < retirement_mask (e.g., 20°) OR elev-weighted PR gate exceeded |
| RETIRING | FLOAT | elev < AR mask (25°); WL/MW state preserved |
| any NL state | FLOAT | CycleSlipMonitor fires (slip flushes phase only; WL may survive) |
| any state | BLACKLISTED | Job A rejection repeatedly on same SV, or cycle slip in recent window |
| BLACKLISTED | FLOAT | `blacklist_epochs` elapsed without signal degradation |

## PFR Split: Job A and Job B

PFR today evaluates one condition: `worst_sv_mean > 3m OR rms > 3m`.
Both conditions feed the same escalation ladder.  We split this
into two independent monitors.

### Job A — Wrong-integer detection (acts on NL_PROVISIONAL)

**Purpose**: catch statistically-unlucky or systematically-biased
fixes before they corrupt the host position.

**What it measures**: PR post-fit residuals **on SVs currently in
NL_PROVISIONAL state**, tracked individually.

**Trigger**: per-SV mean |PR resid| over a geometry-change window
(not wall-clock).  A SV moving 5° in az has seen all the geometry
dependence exposed; if the integer is wrong, the position-shift
signature is fully revealed.  Threshold ~2 m per-SV — tighter than
today's 3 m because the target is NEW fixes, which should show
essentially zero residual if correct.

**Action**: demote the SV to FLOAT.  Do **not** touch other SVs,
do not touch the filter covariance broadly.  Re-enable AR after
the SV accumulates new MW/WL evidence.

**Cadence**: every 10 epochs.  Applies only to SVs whose
provisional age is shorter than `validation_window`.

### Job B — Setting-SV retirement (acts on NL_VALIDATED)

**Purpose**: release setting SVs gracefully before their multipath-
inflated residuals pollute the filter.

**What it measures**: PR post-fit residuals on SVs currently in
NL_VALIDATED state, with threshold scaled by 1/sin(elev).

**Trigger**: per-SV mean |PR resid| > `base_threshold / sin(elev)`.
A SV at 25° tolerates ~2.4× the zenith noise; at 10° ~5.8×.  Also
absolute floor when elev < `retirement_mask` (e.g., 20°) —
triggers retirement regardless of residual magnitude.

**Action**: transition SV to RETIRING.  Preserve WL/MW state in
case the SV rises again at a later arc.  Remove integer from
filter with gentle covariance growth (not a slam-to-FLOAT).

**Cadence**: every 10 epochs.

### Host-level monitor (replaces today's L3)

**Purpose**: catch the rare case where *many* SVs misbehave at
once — genuine systemic failure (bad SSR correction, clock datum
change, etc.).

**What it measures**: host PR RMS across all NL_VALIDATED SVs.

**Trigger**: RMS > much larger threshold (say 5 m) sustained over
multiple evals AND Job A hasn't fired on individual SVs.

**Action**: full filter re-init at current known_ecef — similar
to today's L3 but only as last resort.  Expected rate: < 1/day in
good conditions.  If firing more than ~1/hr, something is broken
at the correction-source level, not at the AR level.

### What happens to today's L1/L2/L3

| Today | In new design |
|---|---|
| PFR L1 (surgical unfix worst SV) | Job B retirement for descending SVs; Job A for mid-probation SVs.  Per-SV threshold, two different gates by state. |
| PFR L2 (partial soft-unfix all NL) | **Deleted.**  No structural role: if many SVs are bad simultaneously, that's a host-level event (go to L3-equivalent); if only one is bad, handle it per-SV. |
| PFR L3 (full re-bootstrap) | Host-level monitor, much rarer.  Expected < 1/day. |

## Elevation-weighted thresholds

All per-SV PR residual thresholds scale with `1/sin(elev)`:

```
threshold(elev) = threshold_zenith * max(1.0, csc_elev / csc_45°)
                = threshold_zenith * max(1.0, sqrt(2) / sin(elev))
```

So zenith (90°) uses `threshold_zenith`; 45° uses the same; 30°
gets 1.41×; 25° gets 1.67×; 15° gets 2.73×.  The clamp at 45°
prevents unrealistically-tight thresholds on near-zenith SVs where
multipath is actually lower than the "nominal" model.

`threshold_zenith` is a new host TOML parameter, default ~1.5 m
(vs today's flat 3 m).  Justification: at zenith a healthy PR
post-fit residual should be well under a meter; 1.5 m is
per-satellite multipath + receiver noise budget.

## Relationship to host state

The host's `AntPosEstState` machine is untouched by this doc.
The per-SV states feed into it via the RESOLVED transition:

```
Host CONVERGING → RESOLVED: ≥ N SVs in NL_VALIDATED
                             (N = 4 default, configurable)

Host RESOLVED → CONVERGING: NL_VALIDATED count drops below N
                             (many SVs retired, few new validations)

Host RESOLVED → (stays RESOLVED): individual SVs cycling through
                                  RETIRING → FLOAT → WL_FIXED →
                                  NL_PROVISIONAL → NL_VALIDATED in
                                  the background is routine
```

This means **SV retirement does not flip host state**.  A setting
SV retiring is not a reason to re-bootstrap; it's normal sky
motion.  The host stays RESOLVED throughout.  That's a structural
change — today any PFR L3 event drags the host from RESOLVED →
CONVERGING, and we measured that costs ~10 min/event.

## Expected rates (steady state, GAL+BDS, 25° mask)

- SVs rising through AR mask: ~5/hr
- SVs setting through retirement mask: ~5/hr
- NL_VALIDATED count: 8-14 steady
- Job A rejections (wrong integers): target < 1/hr
- Job B retirements: ~5/hr (one per descending SV)
- Host-level monitor fires: < 1/day

If Job B is firing much above 5/hr, the per-SV thresholds are too
tight.  If Job A is firing above 1/hr, either the LAMBDA ratio
test is too loose or there's a correction-source bias.

## Validation targets

This design is validated when a 24 h run shows:

- Host state stays RESOLVED for ≥ 95% of the run (excluding first
  30 min convergence)
- Per-SV retirements happen at the expected sky-motion rate,
  quietly, without host state flipping
- Wrong-integer events (Job A rejections) < 1/hr, each resolved
  surgically without cascading
- Host-level monitor doesn't fire at all, or fires < 1/day on a
  specific debuggable cause (captured in logs)

Compare against today's ~2 PFR L3/hr with ~35% time resolved (see
day0419d 2 h data, 2026-04-19 ~11:30-13:30).

## Implementation plan (bead-sized)

### Bead 1 — SvAmbState class and plumbing
- `scripts/peppar_fix/sv_state.py`: `SvAmbState` enum, `SvStateTracker` class
- Hook into `ppp_ar.py` to transition on MW convergence, LAMBDA fix
- Emit `[SV_STATE]` structured log events on every transition
- Unit tests

### Bead 2 — Job A / Job B split in PFR
- Replace `PostFixResidualMonitor` with two monitors: `ProvisionalFixValidator`
  (Job A) and `RetirementGate` (Job B)
- Both consult `SvStateTracker`; neither has an escalation ladder
- Host-level monitor becomes a separate `HostRmsAlarm` with a much
  higher threshold

### Bead 3 — Elevation-weighted thresholds
- Add `pr_threshold_zenith_m` to host TOML
- Implement `threshold(elev)` helper
- Update both Job A and Job B to use it

### Bead 4 — Provisional → Validated promotion
- Track first-fix epoch per SV, starting az
- Check az delta each eval, promote when ≥15° accumulated
- Host RESOLVED recomputed from NL_VALIDATED count

### Bead 5 — Migration and backwards-compat
- Default parameters chosen so existing host configs work unchanged
- Log format change: `[STATE] AntPosEst` lines unchanged; new
  `[SV_STATE]` lines added
- `scripts/peppar_fix/pfr_monitor.py` symlink or shim until all
  callers migrate

Each bead is independent and testable — no big-bang rewrite.  If
we want to stop partway (e.g., after Bead 3 we may already see
L3 rates drop enough to declare victory), beads 4 and 5 can be
deferred.

## Open questions

1. **Validation window units**: wall-clock minutes vs azimuth
   degrees vs "residuals stayed in gate for N evals" — which is
   the cleanest trigger for NL_PROVISIONAL → NL_VALIDATED?  This
   doc proposes azimuth degrees because it's the direct signal
   (different geometry can catch wrong integers that wall-clock
   can't); subject to revision once we measure.

2. **WL_FIXED tenure cap**: should a stale WL_FIXED (no NL attempt
   for 30 min despite passing the ratio) be demoted to FLOAT as
   a health check?  Probably yes; open on threshold.

3. **Cross-constellation coupling in Job A**: if BDS and GAL are
   simultaneously tripping Job A, is that a Constellation-level
   problem (ISB drift) rather than per-SV?  Could warrant a new
   Job-A-Bravo for constellation-wide patterns.

4. **ptpmon integration**: this design doesn't address ptpmon's
   current NL-never-lands problem (see
   `project_ptpmon_cnes_only_result_20260419.md`).  That's a
   separate debugging track — likely ZTD-ambiguity coupling on
   single-constellation L2-profile — and beads above should not
   block on it.
