# WL-only foundation

A proposal to strip PePPAR Fix's ambiguity resolution back to
**wide-lane only** — no NL search, no ANCHORING, no ANCHORED.
Run that for days, measure stability, then layer NL back on top
with the confidence that the foundation is genuinely solid.

Motivated by day0422a-d: all four hosts converged to biased
equilibria (altitude 7-25 m off, ZTD -7 m to +3.4 m) despite
σ = 0.02-0.05 m.  Each LAMBDA attempt landed on wrong NL integers
conditioned on wrong WL integers, then the ZTD state absorbed the
bias silently.  The new `ztd_cycling` escalation is a defense
against that specific trap, but the deeper question is whether
our foundation was ever stable enough to build NL on top of.

## Why WL is the right foundation

Every textbook PPP/RTK treatment (Teunissen, Laurichesse, Geng)
resolves WL first because:

- λ_WL = 75 cm, λ_NL = 11 cm.  One cycle of tolerance is 7× bigger.
- The Melbourne-Wübbena combination is **iono-free AND
  geometry-free** — MW depends on nothing except the integer
  ambiguity and receiver/satellite biases.  No position, no ZTD,
  no clock.  None of the coupling paths that trapped us this week
  exist in MW-space.
- MW fractional σ averages as PR_noise / √N.  Sixty epochs gets
  most SVs to σ ≈ 0.1 cycle — well inside the ±0.15 rect gate for
  rounding.  Bootstrap P_correct > 0.99 on well-behaved sky.
- Once WL is fixed, the NL search is **constrained to a 1-D
  subspace** along (N1+N5) given (N1-N5) = N_WL.  NL's integer
  lattice only exists because WL's does first.  A wrong WL
  removes the correct NL integer from the lattice entirely.

Our bug was building NL on a WL foundation we never stress-tested.
This doc proposes living in the foundation for a while to learn
what its actual stability is.

## Expected stability in WL-only mode

| failure mode | affects WL-only? | why |
|---|---|---|
| wrong-WL integer on one SV | yes | MW rounding or LAMBDA can false-fix |
| wrong-NL integer | no | no NL search happens |
| ZTD absorbing position bias | reduced | no tight NL locks forcing ZTD to cover |
| biased equilibrium trap | **no** | needs the NL lattice to be trapped inside |
| `ztd_cycling` escalation firing | rare | the symptom it catches is NL-driven |
| anchor_collapse trip | n/a | no anchors to collapse |
| FalseFixMonitor rejections | n/a | no NL fixes to reject |
| cycle slips on fixed WL | yes | genuine physical event, same detection path |

Position accuracy target: WL-float IF ≈ **5-10 cm horizontal**,
**10-20 cm vertical**.  That maps to 0.3-0.7 ns of PPS error from
position ambiguity.  Our moonshot is sub-ns PPS agreement — WL-only
has enough headroom.

## Implementation

Four small changes plus a CLI flag.  Goal is to preserve all the
NL infrastructure so re-enabling is a one-flag flip.

### 1. `--wl-only` flag in engine

`scripts/peppar_fix_engine.py` argparse: add boolean flag.  Default
off (NL pursued as today).

### 2. NL resolver short-circuit

`scripts/peppar_fix/nl_resolver.py`: top of `resolve()` method,
early-return when flag is set.  No LAMBDA search, no rounding, no
commitment.  The resolver's per-SV prescreen (frac / σ) can still
run for `--nl-diag` telemetry without committing anything.

### 3. SV lifecycle clamp

`scripts/peppar_fix/sv_state.py`: in `transition()`, reject any
promotion beyond CONVERGING when the WL-only flag is set.  SVs
progress TRACKING → FLOATING → CONVERGING and terminate there.
All demotion paths (FLOATING, WAITING, SQUELCHED) remain intact —
the state machine is a strict subset in WL-only mode.

### 4. AntPosEst state-machine clamp

`scripts/peppar_fix/ant_pos_est.py`: same clamp at CONVERGING.
No ANCHORING, no ANCHORED.  `reached_anchoring` and
`reached_anchored` latches never set.

### 5. Monitor behavior (automatic)

Most monitors become inert by construction:

- **FalseFixMonitor**: no NL fixes → nothing to reject.
- **Setting-SV drop**: gated on NL-fixed members; empty set in WL-only.
- **anchor_collapse**: gated on `reached_anchored`; never latches.
- **ztd_impossible / ztd_cycling**: still fire if ZTD goes nuts,
  but the driver is much weaker without NL.  Leave them armed —
  free insurance.
- **window_rms**: armed on fix-set members; empty in WL-only.  The
  current implementation is a no-op when the fix-set is empty.

No code changes needed to any monitor — they already key off state
that never arrives.

### 6. Rename — deferred

`CONVERGING` → `CONVERGED` is the right name for a terminal state,
but the lifecycle rename just landed (2026-04-22).  Defer the name
change until we've validated that WL-only is the long-term
architecture.  Until then, documentation treats CONVERGING as the
terminal state in WL-only mode; logs still say "CONVERGING".

## Validation plan

Two independent yardsticks run in parallel: **lab stability**
(our own ARP, multi-night) and **PRIDE regression** (ITRF14 truth
on a published IGS dataset).  Passing both means the foundation
is both stable over time AND accurate against an external
reference.  Disagreement between them isolates cause: lab-only
failure = multipath / SSR / antenna; PRIDE-only failure = code
bug exposed by a geometry or correction profile we don't see in
the lab.

### PRIDE regression (absolute accuracy)

The regression harness at `scripts/regression/` runs float-PPP
against PRIDE's bundled ABMF 2020 DOY 001 dataset (ground truth =
IGS weekly SINEX coordinate).  Current result: **2.66 m 3D @ 16 h**
with broadcast NAV + float PPP — meets gate 1 (< 5 m) but not
cm-class.  Critically, **no AR (WL or NL) is wired into the
harness yet** — the runner header documents `MW + LAMBDA + state
machine` as explicit TODOs.

WL-only is therefore the **first** AR tier to add to the harness,
not just a `--wl-only` flag flipping through existing code.  Work
breakdown:

1. Wire `MelbourneWubbenaTracker` into the per-epoch loop so WL
   ambiguities get estimated.  ~30 LOC.
2. Feed fixed WL integers back as equality constraints on the
   filter's IF ambiguity state.  ~50 LOC.
3. Add `--wl-only` flag (at this point it's a no-op because
   NL is never attempted, but keeps CLI shape parallel to the
   engine).
4. Gate on WUM OSB biases — already loadable via
   `bias_sinex_reader`, just not applied in the runner today.

Target once wired: **WL-only IF position on ABMF 2020 DOY 001
(24 h static) ≤ 20 cm 3D vs ITRF14** with broadcast NAV + WUM
OSB.  10× better than the current float-only number.

**CI gate** once passing: any PR that regresses the WL-only ABMF
number by more than 50% fails.

This is a follow-up to the engine-side `--wl-only` flag, not
part of the same PR.  Lab-stability data is the first yardstick;
PRIDE accuracy comes online once the harness gets the AR wiring
it currently lacks.

### Lab stability (over time)

- **Night 1**: 1 host (clkPoC3), shared antenna, `--wl-only
  --systems gps,gal,bds`.  Monitor: how many SVs stay in CONVERGING
  through the night?  Any lock drops?  Position stability on the
  known ARP?
- **Night 2** (if night 1 clean): 2 hosts (clkPoC3 + MadHat), same
  antenna via splitter.  Measure cross-host position agreement.
  Hypothesis: sub-10cm horizontal at all times without any
  convergence drama.
- **Night 3+**: full L5 fleet.  48-hour run.  Measure daily cycle
  effects, SSR outage recovery, cycle slip handling.

- **Night 1**: 1 host (clkPoC3), shared antenna, `--wl-only
  --systems gps,gal,bds`.  Monitor: how many SVs stay in CONVERGING
  through the night?  Any lock drops?  Position stability on the
  known ARP?
- **Night 2** (if night 1 clean): 2 hosts (clkPoC3 + MadHat), same
  antenna via splitter.  Measure cross-host position agreement.
  Hypothesis: sub-10cm horizontal at all times without any
  convergence drama.
- **Night 3+**: full L5 fleet.  48-hour run.  Measure daily cycle
  effects, SSR outage recovery, cycle slip handling.

### Success criteria

- **Admission rate**: ≥ 90% of WL-eligible SVs reach CONVERGING
  within their first 60 MW-tracker epochs.
- **Hold rate**: once in CONVERGING, < 5% false drop rate per pass
  (excluding legitimate setting below elev mask).
- **Cross-host agreement (shared antenna)**: ≤ 10 cm horizontal,
  ≤ 20 cm vertical, at any epoch after 10 min of startup.
- **ZTD behavior**: stays within ±500 mm without any intervention.
  No ztd_impossible trips firing in a 24h run.
- **No biased equilibria**: position does not drift by more than
  30 cm over the run (excluding natural ARP placement).

### Measurement caveat

We have no direct observable for "a WL fix is wrong."  The best
proxies:

- **MW post-fix residual drift**: if WL is wrong, MW drifts off
  zero slowly as more data averages in.  Currently surfaces as
  `slip:mw_jump` — reclassified from "cycle slip" to "wrong-fix
  re-decide" depending on whether the new integer matches the old.
- **WL-float PR residual per SV**: if one SV has a wrong WL, its
  PR residual in the IF solution will stand out.  Chi-squared on
  the residual vector catches it.
- **Cross-constellation consistency**: GPS-only vs GAL-only WL
  positions should agree to cm.

A future iteration adds an explicit WL-level post-fix monitor
(the WL analog of FalseFixMonitor) but it's **not required** for
the experiment — the validation criteria above catch the same
failure modes via position and ZTD behavior.

## Re-enabling NL later

Once we have confidence the foundation is solid, NL adds on top
cleanly:

- Flip `--wl-only` off.
- SV lifecycle clamp goes away — promotions past CONVERGING
  resume.
- NL resolver's early-return goes away.
- FalseFixMonitor, setting-SV drop, anchor_collapse all re-arm
  automatically once their gating conditions (NL fixes,
  `reached_anchored`) start happening again.
- The WL post-fix monitor we built during WL-only stays armed —
  now it's the "WL foundation health check" for the NL layer.

The experiment is non-destructive: we never delete NL code, just
gate it.

## Open questions

- **What "WL fix" means with LAMBDA vs rounding**.  Current code
  attempts rounding first, falls through to LAMBDA if rounding
  fails.  WL-only mode uses the same path; no change needed.
- **Should we disable the WL LAMBDA fallback too?**  Arguably
  rounding-only is even simpler.  TBD — try LAMBDA-fallback first,
  downgrade if it looks like LAMBDA is over-committing on
  borderline MW distributions.
- **Servo behavior under WL-float position**.  The PPS discipline
  loop uses the filter's position estimate to compute the receiver
  clock's range contribution.  5-10 cm position means 0.3-0.7 ns
  of ambiguity in the clock estimate — fine for the servo's loop
  bandwidth but worth watching.
