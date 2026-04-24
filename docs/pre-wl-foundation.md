# Pre-WL Foundation

Before WL integer resolution has a chance of succeeding, the float
PPP solution feeding it must be *good enough* — and we must have an
*independent* way to judge "good enough" that isn't just the fleet
agreeing with itself.

This doc defines:

1. What "good enough" means concretely (three conjunct gates).
2. Why cohort self-agreement is necessary but NOT sufficient.
3. How we earn an independent position fix we can trust.
4. How the Foundation gate governs WL and downstream work.

Companion to `docs/wl-only-foundation.md` (WL on top of float) and
`docs/fleet-consensus-monitors.md` (runtime cohort self-agreement).

## Motivation

Observed on day0424b/c (2026-04-24):

- clkPoC3 and MadHat (both F9T-20B, same antenna via splitter)
  agree with each other *sub-μ° horizontally and within 20 cm
  vertically* — cohort consensus is perfect.
- Both are also ~2–4 m below the surveyed UFO1 altitude, and
  neither achieves WL fixing in this state.
- TimeHat (F9T-10, same antenna) lives in a different basin — ~1 m
  horizontal off the pair, different altitude — and intermittently
  trips `ztd_impossible`.

The fleet consensus monitor I built (2026-04-24, commit `cbb7126`)
catches "one host diverges from the cohort."  It *cannot* catch
"the whole cohort agrees on a wrong answer," which is the failure
mode we actually see.  An independent anchor — not another PPP
host — is the missing piece.

But (per Bob 2026-04-24) we cannot today treat any of our existing
ARP estimates as "surveyed truth."  We need at least one, preferably
two, *independent* position fixes before we can hold PePPAR-Fix to
an external standard.

## Three conjunct gates

The Foundation is considered stable when all three hold, sustained
for a configurable window (initial proposal: 5 min):

### Gate #1 — Cohort consensus

Already implemented at `cbb7126`.  The shared-ARP cohort's
position scatter must be tight:

- horizontal scatter (2σ across cohort members) < 5 cm
- vertical scatter (2σ) < 15 cm

When a single host diverges from the cohort median by more than
the existing `pos_consensus` threshold (20 cm / 30 epochs), the
Foundation is already stale.  This gate catches *individual-host*
drift.

### Gate #2 — Independent anchor agreement

The cohort median must agree, within an earned bound, with at
least one independent position fix.  *Earned* means: obtained
through a measurement path materially different from the fleet's
own float-PPP loop.  Candidates, in rough order of cost:

- **Fleet self-survey** (cheapest, available today): run a
  `diag_fleet_survey` tool that takes each F9T out of fixed-
  position mode into survey-in (UBX-CFG-TMODE3), collects
  NAV-SVIN convergence over ~24 h, consensus of the three
  independent survey estimates.  Error bar taken from NIST
  (Montare 2024) — 2 m vertical after 6 h, 1 m after 18 h,
  final floor ~50 cm vertical.  That's a 50 cm soft bound, which
  sets our Gate #2 threshold too.  See `diag-fleet-survey.md`
  (to be written).
- **Leica GRX 1200 GG Pro** (best available on-bench): the GRX
  is connected to the same splitter as the L5 fleet (Bob
  2026-04-24).  Its GPS+GLONASS L1/L2 solution runs through
  completely different firmware + different processing path,
  and the receiver itself is a known-good geodetic-grade unit.
  Agreement with the F9T survey consensus adds a strong
  independent data point.  See `docs/antenna-calibration-plan.md`
  Experiment 1 for the zero-baseline setup.
- **Local NTRIP caster (RTK)**: DuPage CORS / nearby base
  stations over NTRIP.  Short-baseline RTK solution on the F9T
  against a known base gives cm-level position in minutes.  Uses
  the same receiver but entirely different measurement chain
  (RTCM corrections vs. our SSR PPP).  Agreement is not as
  independent as the GRX but strongly diagnostic.

Once we have one or two of these, Gate #2 is "|cohort_median −
independent_anchor| < {survey bound, 50 cm vert / 20 cm horiz;
RTK bound, 5 cm vert / 2 cm horiz; GRX bound, 10 cm vert / 5 cm
horiz}."  Values are tunable — see the subsidiary tool docs.

### Gate #3 — Residual plausibility

The filter's PR residual must be consistent with the claimed
position.  Gate #3 guards against cases where the filter σ
*looks* tight but PR residuals tell a different story.  Example
from day0424c: clkPoC3 σ=0.025 m but IF-PR RMS=4 m.  The tight
σ is internal-only; the residuals show the filter has locked
onto a biased solution with high confidence.

Proposed threshold:

- IF-PR RMS across fix-set SVs < 2.0 m, sustained 60 epochs

This is a floor that every honest SSR-corrected PPP solution
should clear once converged.  A persistent RMS above this is a
signal that one or more observations (or corrections) disagree
with the geometry.

## Foundation states

Model as a small state machine, parallel to `AntPosEstState`:

```
   NOT_READY  →  CANDIDATE  →  STABLE
        ↑            |            |
        └────────────┴────────────┘
          (any gate fails for T sec)
```

- `NOT_READY`: filter hasn't converged enough to even evaluate.
  Any of positionσ > 50 cm, too few SVs, cold start.
- `CANDIDATE`: all three gates currently pass, but haven't
  sustained 5 min.  WL admission still blocked.
- `STABLE`: all three gates passed for 5 min.  WL admission
  allowed.  Fall back to CANDIDATE on any gate miss; fall to
  NOT_READY on severe regression (e.g., σ > 50 cm again).

### Visibility

A new `[FOUNDATION]` log line per status cadence with the current
state + per-gate pass/fail breakdown.  peppar-mon surfaces it as
a new column.  When stuck in CANDIDATE or NOT_READY for > 30 min,
the engine escalates the log to WARNING so it's impossible to
miss.

### Effect on WL

`NarrowLaneResolver` (despite the name it also does WL admission
in wl-only mode) consults the Foundation state before admitting
any candidate.  Until `STABLE`, the MW tracker continues to
observe but never commits a fix.  Observations still flow into
the filter at float; diagnostics still stream.

## What this does NOT do

- **It does not define "truth."**  Gate #2 is the best
  independent check we can muster, not truth.  If our survey + GRX
  + RTK all agree to 10 cm, we treat the cohort-vs-anchor
  agreement threshold at ~20-30 cm.  If they disagree among
  themselves, we have a separate problem and the Foundation
  concept itself needs to be revisited.
- **It does not prevent the basin trap we saw.**  A filter can
  sit in `CANDIDATE` indefinitely.  The escape mechanism (basin-
  escape action) is a future item — `docs/pre-wl-foundation.md`
  documents the gate; the escape path is its own design.  First,
  we need Foundation visibility to see what the failure looks
  like; then we design the escape based on what we see.

## Implementation plan

Ship in three commits (not necessarily one session):

### Commit A — Foundation gate and visibility (no action yet)

- `scripts/peppar_fix/foundation_gate.py`: new module with
  `FoundationGate` class.  Evaluates the three gates per epoch,
  emits `[FOUNDATION]` log lines, reports state via attribute.
- Engine integration: evaluate gate right after the AntPosEst
  log line; status emitted alongside.
- peppar-mon column: plumb the Foundation state through the
  bus and show it as a fleet-row column.
- **No effect on filter or WL** yet.  Pure observability.

### Commit B — WL admission gated on `STABLE`

- `NarrowLaneResolver.attempt()` + MW admission path consult the
  `FoundationGate`.  Don't admit candidates or commit WL fixes
  until gate says STABLE.
- This will, in the current state of the F9T-20B fleet, leave
  them in `float-only debug mode` instead of attempting WL — a
  feature, not a bug.  The failure becomes *visible and named*.

### Commit C — Independent-anchor plumbing

- Load the independent anchor from a new `state/independent_anchor.json`
  file (gitignored, placed by lab procedures).  Fields: `ecef_m`,
  `sigma_m`, `source` ("fleet_survey" | "grx_1200" | "ntrip_rtk"),
  `captured_utc`.
- Foundation Gate #2 reads this file at startup + watches for
  updates; evaluates |cohort − anchor| every epoch.
- Until an independent anchor exists, Gate #2 is "skipped" and
  logged as `INCONCLUSIVE`.  The Foundation state can't reach
  `STABLE` in that case — which is the correct interpretation:
  we genuinely don't know if the fleet is right.

## Interaction with other subsystems

- **Fleet consensus monitors** (`cbb7126`): already implement
  Gate #1.  Foundation Gate wraps them.
- **FixSetIntegrityMonitor**: unchanged.  Its trips (anchor_collapse,
  ztd_impossible, ztd_cycling, pos_consensus, ztd_consensus) run
  in parallel to the Foundation; any trip forces Foundation back
  to `NOT_READY`.
- **Clock-state option (A)** (`calibrated_white`, commit `cca7623`):
  independent axis.  Foundation gate observes filter behaviour
  regardless of Q_clk choice.
- **Slip instrumentation** (`8e52eb9`): independent axis.  Slip
  records tell us which SVs are unreliable; Foundation tells us
  if the whole solution is trustworthy.

## Open questions

- **Threshold values.**  Initial proposals (5 cm H / 15 cm V for
  Gate #1; residual < 2 m for Gate #3) are guesses.  First
  lab-validation pass should inform tuning.
- **Gate #2 without an independent anchor.**  Do we launch WL
  without Gate #2 (effectively `STABLE` becomes `1-of-2-and-#3`)?
  Bob's explicit ask — "one or two independent position fixes
  before holding PePPAR-Fix to any standard other than self
  agreement" — points toward *not* shipping WL into production
  without Gate #2.  A `--foundation-strict=[on|off]` flag could
  preserve today's behaviour while the independent anchor is
  being earned.
- **Sustained window.**  5 min is a guess.  Too short and we
  ping-pong; too long and we lose information.  Lab data will
  tell us.

## Cross-references

- `docs/wl-only-foundation.md` — the decision to defer NL
  fixing until WL works reliably.
- `docs/fleet-consensus-monitors.md` — Gate #1 implementation.
- `docs/antenna-calibration-plan.md` — GRX 1200 zero-baseline
  setup.
- `docs/position-convergence.md` — NIST Montare 2024 survey-in
  characterisation numbers.
- `docs/diag-fleet-survey.md` — (to write) the fleet-survey
  diagnostic tool's design and procedure.
