# Position Bootstrap Reliability — Implementation Plan

**Date**: 2026-04-18
**Status**: W1–W4 implemented; W5 partial (save/load exists, NAV2
startup cross-check outstanding); W6 deferred.
**Prerequisite**: None — parallel to post-bootstrap (steady-state) work
**Related**: `project_phase1_convergence_threshold` (memory),
`docs/position-convergence.md` (theory),
`docs/position-confidence.md` (NAV2 opinion framework).

## Problem

Phase 1 cold-start bootstrap is not yet reliable.  The symptom on
2026-04-17 was three hosts sharing one antenna finishing Phase 1
with `σ_3d < 0.1 m` but real position errors of 2 m, 17 m, and
40 m.  The filter declared victory on its own self-consistency and
stopped iterating while pseudorange residual RMS was 1–2 m against
a noise model of `SIGMA_P_IF = 3.0 m` — it had locally downweighted
observations that disagreed with its locked-in state and called the
result converged.

Because we cannot trust Phase 1 to produce the correct absolute
position, every lab test currently starts with `--known-pos` and
skips Phase 1 entirely.  That is a workaround.  The system cannot
cold-start at a new location until Phase 1 is trustworthy.

## What we have learned from running under `--known-pos`

The reason fixed-pos mode works well is illuminating:

- **AR converges cleanly at cm level across three hosts on the same
  antenna** when the starting position is correct.  Cross-host Δpos
  stays under a few metres once NL is fixed.
- **ZTD state is essential.**  Without it, NL fixing locks
  tropospheric bias into position and horizontal drifts by ~5 m
  over an hour.  With it (commit `e72e20d`), horizontal stays put.
- **The slow-mode PPP problem is real but manageable.**  The
  slip-prone SVs (E25 at 30–37°, E03, E11 at low elevation) cycle
  through WL and NL fixes repeatedly without breaking the filter as
  long as cycle-slip detection flushes phase-only state.
- **NAV2 is a faithful horizontal watchdog** after the
  2026-04-18 refactor (commit `98051d5`).  It is an independent
  single-epoch-code fix on the same F9T, which makes it useful as a
  sanity check that does not share any state with our PPP filter.

That last point is load-bearing: any Phase 1 convergence criterion
should be cross-checked against NAV2 before it exits.  Self-consistency
of the EKF alone is not enough.

## Proposed work (parallel to steady-state work, gated only on lab access)

### W1. Residual-consistency check in convergence gate — DONE

**Problem**: `σ_3d < 0.1 m` by itself is a state-covariance claim,
not an accuracy claim.  The filter can reach that while PR residuals
are 10× their modelled noise because down-weighted outliers have
been implicitly trusted.

**Change**: the CONVERGED test must also verify that observed
residuals are consistent with the assumed noise.  Concretely:

```
rms_pr_threshold = k * SIGMA_P_IF / sqrt(n_used)    # k ≈ 2
converged = (sigma_3d < sigma_target
             AND pos_stable_over_30_epochs < sigma_target
             AND rms_pr < rms_pr_threshold)
```

If the EKF's modelled noise and the observed residuals disagree by
more than 2×, the state is internally inconsistent and we must keep
iterating.

**Success measure**: on three-host shared-antenna cold starts, at
least 2 of 3 land within 2 m of truth (measured against LS fit from
the same epoch's observations, which is the independent reference
available in cold-start mode).

### W2. NAV2 cross-check at convergence gate — DONE

**Problem**: even with W1, it is possible for the filter to converge
to a locally-consistent minimum that is geometrically wrong (e.g.,
the 40 m bias seen on MadHat was self-consistent inside the filter).

**Change**: at the moment Phase 1 wants to exit, compare the PPP
position against NAV2 and require horizontal disagreement < N metres
(N = 5 initially) for k consecutive epochs.  If not, abort Phase 1
and re-enter convergence for another round with a harder state
scrub (see W3).

**Caveats**: NAV2 has a known systematic horizontal bias of up to
~4 m against PPP-AR on the same antenna.  The threshold must be
wider than that bias (5 m is a reasonable starting point).  This
check is NOT as tight as W1; it is a coarse-grained "are we at
roughly the right spot on Earth?" backstop.

**Success measure**: after W2, all three hosts land within 2 m on
cold start, i.e., the MadHat 40 m failure mode is eliminated.

### W3. Harder reset on gate abort — DONE

**Problem**: if W1 or W2 aborts a convergence attempt, simply
extending the iteration may not escape the wrong local minimum.  The
filter has committed to the bias.

**Change**: on gate abort,
- Inflate position covariance (`P[0:3, 0:3] = (100 m)^2`)
- Inflate ambiguity covariances (`P[N_BASE:, N_BASE:]` diagonal to
  `(50 m)^2`)
- Keep ZTD, ISBs, clock state (those don't carry position bias)
- Optionally: re-seed position from NAV2 or LS fit if W2 aborted.

This is less destructive than a full `initialize()` but decisive
enough to break out of a locked-in wrong state.

**Success measure**: abort-and-retry converges on the second attempt
≥ 90% of the time in the lab (easy to measure by forcing aborts).

### W4. Tighten default `σ_target` to 0.02 m — DONE

**Problem**: the current default `--sigma 0.1 m` gates convergence
at exactly the range where self-consistency reports tightly but
accuracy is poor.  The Phase-1 2026-04-17 snapshot had clkPoC3
continuing past 0.1 m all the way to 0.035 m — which happened to
be where the correct position lived.

**Change**: default `σ_target = 0.02 m`.  This requires more filter
iterations on cold start (minutes), but it pushes the gate into the
regime where the carrier-phase ambiguities are forcing the filter
onto the right geometry rather than drifting along a ridge of the
position-clock correlation.

**Caveat**: must be wrapped by W1 and W2 — dropping the σ threshold
alone could simply delay the wrong-answer failure instead of
preventing it.

**Success measure**: Phase-1 TTFF on cold start stays under 20
minutes (from the current ~10) and accuracy improves to ≤ 1 m.

### W5. State persistence for warm re-start — PARTIAL

Receiver-state persistence is already in place via
`peppar_fix.receiver_state.save_position_to_receiver` /
`load_position_detail_from_receiver`, keyed by F9T unique ID.
With W1+W2 now gating what can be saved, the persisted position is
now a trusted one.

**Outstanding**: NAV2-signature cross-check on warm restart — if live
NAV2 at startup disagrees with the cached position by > 5 m
horizontal, force a Phase-1 re-run rather than trusting the cache.
This is a follow-up commit.

**Problem**: if Phase 1 completes successfully once, throwing away
the converged state on every start is wasteful.  Every subsequent
`peppar-fix` start at the same antenna has to rebuild the position
from scratch.

**Change**: after Phase 1 CONVERGED, persist `(lat, lon, alt)`,
PR-residual RMS, σ_3d, NAV2 agreement margin, and an ARP signature
(the known-good antenna position and a sanity-check range) to a
state file (e.g., `data/position.json`).  On the next start, if
the file exists and the signature matches, skip Phase 1 and warm-
start from the cached position.

**Signature**: NAV2 at startup time compared to the cached position
— if they disagree by > 5 m horizontal, treat as "antenna may have
moved" and re-run Phase 1 anyway.

**Success measure**: warm restart after a converged run goes from
~10 min Phase 1 to ~5 s no-Phase-1, and still triggers Phase 1
when NAV2 suggests the antenna has moved.

### W6. Multi-start validation (optional, low priority)

**Problem**: a single convergence run can land in a bad local
minimum even with W1–W4 in place.  Two independent starts that
converge to within a metre of each other is much stronger evidence
of the right position than one run that looks good by itself.

**Change** (only if W1–W4 prove insufficient): run Phase 1 twice with
different initial seeds (e.g., one from LS fit, one from NAV2, one
from the cached state file).  Require the two results to agree
within 2 m before declaring the position trusted.

**Success measure**: used only as a fallback when W1–W4 cannot
explain a convergence failure in the lab.

## Sequencing

```
W4 (tighten σ) ──┐
                 ├──> W1 (residual check) ──> W2 (NAV2 check)
                 │                                    │
                 │                                    v
                 └───────────────────────────> W3 (harder abort)

W5 (state persistence) ──> depends on W1+W2 passing ("trusted result" must be defined)

W6 (multi-start) ──> only if lab data after W1–W4 still shows failures
```

Each of W1–W4 is a small, isolated code change.  They can be
implemented in any order individually, but W1 should land before
W4 because W4 without W1 just takes longer to fail.

W5 is independent in code but depends on W1+W2 in semantics: the
persisted position must be a trusted one.  If W1+W2 aren't there
yet, the persistence step would cache a possibly-bad position and
reuse it forever — worse than no cache.

## Testing strategy

### Per-item unit tests
Each work item gets a unit test against synthetic observations:
- W1: inject outlier observations, verify convergence gate blocks.
- W2: inject a known-bad position, verify NAV2 check aborts.
- W3: force abort from W1, verify covariance inflation produces
  expected state.
- W5: write state file, restart, verify warm-start skips Phase 1.

### Lab validation (requires hardware access)
- Three-host shared-antenna cold start, `--systems gal`.
- All three must land within 2 m of LS-fit truth (W1 measure).
- MadHat specifically must not land 40 m off (W2 measure).
- Warm-restart after converged run must skip Phase 1 in seconds (W5).

### Regression tests to retain
The current `--known-pos` path must remain functional as a manual
override.  When `--known-pos` is passed, Phase 1 is still skipped.
This is operationally valuable for controlled experiments.

## Out of scope for this plan

- **BDS re-enable** — BDS is independently broken (see CLAUDE.md
  "Known Broken Things").  Fixing BDS is a separate plan.
- **Peer-to-peer bootstrap** — future work, see
  `docs/peer-bootstrap-sketch.md`.
- **PHC bootstrap** — already covered by `docs/phc-bootstrap.md`
  and the DO state machine.
- **Multi-constellation weighting** — outside the Phase-1 gate
  fix; would be covered by a separate AR tuning plan.

## What "reliable" means for this plan

Done when:
- Three hosts on a shared antenna cold-start with `--systems gal`
  and all land within 2 m of truth without `--known-pos`, at least
  9 out of 10 trials.
- Warm restart after a converged run skips Phase 1 in ≤ 5 s.
- No lab runs need `--known-pos` as a workaround for Phase 1 failure.

Eventually this also unblocks:
- Deployment at any antenna location without pre-surveying via
  external means.
- Dropping the bootstrap-validator role of NAV2 (already done in
  principle with commit `98051d5`; this plan removes the underlying
  reason we ever needed it).
