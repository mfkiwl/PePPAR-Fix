# Per-SV trust: three-layer architecture

A design document for the "how much do we trust this SV?" question.
Covers three complementary layers, their relationship to each
other, and which piece is landing today.

## The question

Ambiguity resolution commits to an integer per SV.  That
commitment can be right or wrong.  When it's wrong, the biased
observation pulls the filter state.  We've been attacking this
with a single axis — binary fix/flush via the WL drift monitor —
but the day0423a and day0423b runs show binary reactions don't
dig us out of repeated-miscommit cycles.

A cleaner framing has three distinct answers to "how much trust?"
— all active at the same time, each answering a different
sub-question.

## The three layers

### Layer 1 — Proactive weighting (continuous, structural)

**Question**: structurally, given what we know about this SV and
current conditions, how much should its observation pull the
solution?

**Continuous**: every SV carries a weight `w_i ∈ [0, 1]`.  Weight
is a function of elevation, signal band, fix age, known stress
events.  A low-elev L5 SV joining during a TEC storm gets a small
`w_i` from the moment it joins; it never becomes dominant enough
to pull the solution even if its WL fix is wrong.

**Proactive**: acts before any misbehavior.  Reduces how often the
reactive layers have to fire.

**Status**: deferred.  Documented in `docs/future-work.md`
("Weighted position-fix strength metric") and
`docs/position-strength-metric.md` ("What's deferred: per-SV
weighting").  Empirical argument for its importance: the
ironic scaling where fewer-SV hosts (TimeHat F9T-10) out-perform
more-SV hosts (MadHat F9T-20B) during stress events.  Running
with uniform `w_i = 1` effectively treats a marginal low-elev
SV the same as a zenith high-elev one — that's wrong.

### Layer 2 — Reactive detection (binary, post-commitment)

**Question**: has this SV's committed integer turned out to be
wrong?

**Binary**: fire an event when post-fix MW residual drift
crosses threshold.

**Reactive**: runs after an SV is fixed, monitors for
inconsistency.

**Status**: **landed** as `WlDriftMonitor`
(`scripts/peppar_fix/wl_drift_monitor.py`) with warmup tuning
from b64b28c.  Catches wrong commitments with 30-epoch rolling
mean, 0.25 cyc threshold, 30-epoch warmup post-fix.  Current
rate ~3/min per host with F9T-20B susceptibility dominant.

### Layer 3 — Reactive admission gating (binary, post-flush)

**Question**: given this SV has shown it's unreliable, when do we
let it back in?

**Binary with a condition**: after flush, re-admission is gated
on something concrete — not just "time passed".  Options:

- **Elevation-gated** (today's pick): don't re-admit until the
  SV has moved ≥ 2° in elevation from its state at flush.
  Breaks the "re-fix to same wrong integer at same geometry"
  pattern.  A persistent multipath-induced bad integer re-fixes
  forever without this gate; with it, the SV only returns once
  geometry has meaningfully changed.
- **Frac-tightened on re-admit**: second fix requires
  `|frac| < 0.07` (vs normal 0.15).  Marginal MW that rounded
  wrong has to prove itself harder the second time.  Could
  layer on top of elevation-gated.
- **Skip-session**: drift twice in the same hour → blacklist
  until set.  Hard exit; preserves the rest of the fix set.

**Reactive**: acts on SVs that have already been flushed by
Layer 2.

**Status**: elevation-gated re-admission is **today's work**.

## How the layers interact

The three layers compose naturally:

```
SV observation arrives
  ↓
Layer 1: compute w_i  (how much does this SV pull?)
  ↓
Layer 2 monitors:     (has the fix been falsified?)
  ├── no  → continue at w_i
  └── yes → flush + hand to Layer 3
              ↓
        Layer 3 gate:  (when to re-admit?)
              ├── condition not met → drop observation
              └── condition met → return to Layer 1
```

Layer 1 reduces the rate Layer 2 needs to fire.  Layer 2 feeds
evidence into Layer 3.  Layer 3's binary decision is structurally
equivalent to Layer 1 saying `w_i = 0 for now` — they're the
same axis at different time-scales.

When Layer 1 lands, Layer 3's binary gate dissolves into a
continuous `w_i` that recovers from 0 as re-admission conditions
are met.  Today's elevation-gated Layer 3 is a stepping stone
toward that formulation, not an alternative.

## Today's implementation — Layer 3 elevation-gated

### Scope

- Record the SV's elevation at flush time.
- Block MW re-fixing that SV until it has moved ≥ 2° in elevation
  (either up or down).
- Unit tests cover: flush records elevation, re-admit blocked
  below threshold, re-admit allowed above threshold, no elevation
  information degrades gracefully.
- No change to the drift monitor itself; only adds gating on the
  re-fix path.

### Where it lives

New state in the existing `CycleSlipMonitor` or a sibling helper,
keyed on SV id with fields `(flush_elev_deg, flush_epoch)`.  When
the MW tracker would admit an SV (back to FLOATING after WAITING
or fresh `update`), consult the helper.

### Acceptance

- Test suite passes (adds ~5 tests).
- Live on lab hosts as `day0423c-elev-gate` in the afternoon run
  window.
- Observable: MadHat's drift event rate drops AND MadHat's WL
  fixed count recovers above 5.  If either fails, the gate is
  either too loose (did nothing) or too tight (starved the fix
  set).

### What this doesn't solve

- GPS-specific 12 m vertical bias on the precise-product path
  (Bravo's discovery, GPS observation-model gap — separate
  investigation).
- F9T-20B fundamental susceptibility to L5-band stress (may need
  Layer 1 weighting to really fix).
- Correlated-failure amplification during sunrise TEC storms
  (Layer 1 dynamic weighting would address).

Today's fix is one brick toward the fuller architecture.
