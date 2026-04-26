# Cross-AC SSR diagnostic — 2026-04-25

*Investigating why our PPP+SSR solution lands 6–9 m west of Leica
truth on a clear lab day with matched-bias GAL-only signals.*

## Setup

- 3-host UFO1 fleet (TimeHat / clkPoC3 / MadHat) on shared SparkFun
  Spike antenna via splitter, plus ptpmon on PATCH3 (separate antenna,
  excluded from UFO1 truth scoring).
- Test instrument: `scripts/diag_seed_sensitivity.py` (committed
  2026-04-25).  Seeds the engine at known-pos ± offset E,N,U; engine
  pulls toward whatever attractor SSR + obs-model defines; we read the
  end position vs Leica GRX 1200 truth.
- Engine config: `--wl-only --systems gal --clock-model random_walk
  --sigma-phi-if 1.0 --phase-windup --gmf` plus per-test SSR overrides.
- Sensitivity floor: ~1 m, established by day0425c full no-SSR matrix.
  Anything ≥10 m offset is cleanly resolvable; we use ±30 m for
  diagnostic runs since the bias signal is ~9 m.

## Established baselines (day0425c, day0425d, day0425g)

| Configuration | UFO1 fleet attractor vs Leica E | inter-host spread |
|---|---|---|
| F9T NAV2 (autonomous, no PPP) | -0.5 to +0.2 m | 0.7 m |
| PPP no-SSR | +0.5 to +3 m (drifting null mode) | <0.5 m |
| PPP + CNES SSRA00CNE0 | **−6 to −8 m** (systematic) | 0.15 m |
| PPP + CNES orbit/clock + WHU biases overlaid | **−4.0 to −4.9 m** | 0.3 m |

## 2x2 result — day0425h (definitive)

After landing `--no-primary-biases` and the engine adjustment that
allows a secondary bias mount with `--no-ssr`, ran the 4-cell 2x2
back-to-back (~45 min total).  UFO1 attractor (mean of 4 host-runs
per cell), m east of Leica truth:

|                  | biases: CNES         | biases: WHU         |
|------------------|----------------------|----------------------|
| **O/C: CNES**    | -1.99 m, spread 1.55 m | **-0.65 m, spread 0.18 m** |
| **O/C: broadcast** | -1.74 m, spread 1.97 m | +0.10 m, spread 1.29 m |

**Marginal effects:**
- biases CNES → WHU: **+1.59 m east** (3.2× larger effect)
- O/C CNES → broadcast: +0.50 m east

**The bias source is the dominant contributor to the obs-model bias.**
CNES's published phase/code bias datum sits ~1.6 m west of WHU's at
this site/time.  CNES orbit/clock contributes a smaller ~0.5 m
westward bias.  Both are within metres of truth — neither is an
engine bug.

**Best PPP configuration found**: CNES orbit/clock + WHU biases
(`--ssr-conf ntrip-cnes.conf --ssr-bias-conf ntrip-whu.conf
--no-primary-biases`).  Sub-meter accuracy with 18 cm fleet
inter-host spread — a new production-candidate setup.

## Constellation sweep on the winning config — day0425i

Same back-to-back protocol, swapping `--systems` one constellation at
a time:

| cell | systems        | attractor median | inter-host spread | notes                                           |
|------|----------------|------------------|--------------------|-------------------------------------------------|
| A    | gal            | -1.17 m          | **0.39 m**         | winner — tightest cohort                        |
| B    | gps,gal        | +2.11 m          | 4.69 m             | TimeHat (F9T-10) under-admits GPS L5 vs siblings |
| C    | gal,bds        | -2.21 m          | 1.69 m             | BDS biases partially matched                    |
| D    | gps,gal,bds    | -0.01 m          | 1.27 m             | GPS east-pull + BDS west-pull happen to cancel  |

**Adding any constellation beyond GAL hurts inter-host agreement.**
GPS adds the most variance because TimeHat's older firmware
under-admits dual-frequency GPS SVs (memory
`project_timehat_f9t_10_under_admit_20260421`).  BDS adds modest
west-bias.  Cell D's accidental zero-attractor is not a real win — it's
a coincidental cancellation that opens the cohort spread by 3x vs
gal-only.

**For cross-host sub-ns PPS agreement, GAL-only is the right call.**
Multi-constellation may be worth revisiting if (a) we upgrade
TimeHat's firmware/EVK to match the F9T-20B siblings, OR (b) we have
to operate hosts at separate antennas where geometry diversity becomes
a bigger deal than firmware-induced cohort variance.

## Long-convergence test (day0425k) — 30-min runs on clkPoC3

Question: does the 1.4 m attractor gap between CNES (-2 m E) and WHU
(-0.65 m E) close given enough convergence time?  Single-host
sequential 30-min runs on clkPoC3, +30 m east seed.

**Final 5-min averages (min 25-29 of each run):**

| Configuration | east of Leica (m) | within-window stdev |
|---|---|---|
| CNES O/C + CNES biases       | -0.77 m | 0.09 m |
| CNES O/C + WHU biases (clean) | +0.45 m | 0.11 m |
| Δ (WHU - CNES)               | **+1.22 m** | — |

**Verdict: hypothesis partially refuted.**  The gap shrank from ~1.4 m
(5-min reading) to ~1.22 m (30-min reading), but did not close.  After
30 min of convergence, the two ACs anchor the receiver to genuinely
different absolute positions, not different transient paths.

Both ACs converge to **within ~1 m of truth** at this duration, with
WHU only ~0.3 m closer than CNES.  Inside each AC's own attractor the
position oscillates ±0.3-0.5 m driven by atmospheric drift, so the
1.22 m AC-to-AC gap is just barely larger than the within-AC noise
floor.

**Implications:**

1. The 5-min snapshot in the 2x2 over-stated the AC difference.  In
   steady state the gap is real but smaller (~1.2 m vs 1.4 m).
2. For cross-host PPS agreement (all hosts using same AC), either AC
   gives sub-meter absolute accuracy + tight cohort agreement.
3. The 1.2 m residual AC datum difference is a known PPP
   characteristic — different ACs choose slightly different reference
   frame realizations.  Closing this is not actionable at the engine
   level; it requires picking one AC as canonical.

## Latent bug surfaced — RTCM `disc_counter` ignored

`SSRState` parses the `disc_counter` field from RTCM 1265+ phase-bias
messages but the engine has no consumer for it.  The discontinuity
counter is the AC's signal to receivers that the integer-cycle
reference for a (SV, signal) has changed and the receiver should
reseed the integer ambiguity.  We don't.

Empirical: in the 30-min runs above, bias values were stable to
sub-mm — disc_counter didn't increment for any (SV, signal) during
either window.  So this latent bug isn't responsible for today's
1.2 m gap.  But it WILL bite multi-hour arcs where ACs typically
reseed integer references every 10-30 minutes.  See memory
`project_disc_counter_ignored_20260425` for the fix sketch (~30
lines).

## Earlier reading from day0425g (overlaid biases — superseded)

**Key reading from day0425g (CNES orbit/clock + WHU biases):**
overlaying WHU biases on CNES shifted the attractor ~3-4 m EAST
(closer to truth).  Two pieces:

1. **Bias datum matters.**  WHU's bias values disagree with CNES's by
   enough to shift the position 3-4 m.  Different ACs anchor their
   phase biases to different reference networks/epochs.
2. **Orbit/clock contributes too.**  Even with WHU biases overlaid,
   ~4 m of westward bias remains.  CNES orbit/clock (or our
   application of it) is responsible for the residual ~4 m.

Caveat: this is not pure "WHU biases" — both CNES's and WHU's biases
flow into the engine, and the per-(SV, signal) merge winner depends
on internal logic (most-recent? primary-wins? averaged?).  Clean
isolation needs the `--no-primary-biases` engine flag listed in the
"engine work" section below.

**Adding our PPP+SSR makes the position 6-9 m worse than the
autonomous code-only fix.**  Inter-host agreement under SSR is tight,
which rules out per-host noise — it's a systematic obs-model bias on
the SSR application path or in CNES products.

## CAS attempt — initial diagnosis was wrong; real bug found

Tried CAS (SSRA01CAS1 on the GA Australian mirror) as a second
provider: if CAS pulls in the same direction as CNES → bug is in our
SSR application code; opposite direction → CNES product reference
issue.  Result was unusable on first try:

- Filter divergence not convergence.  +30 m seed → ended -64 to -337
  m E of truth (UFO1 inter-host spread **279 m**).  -30 m seed →
  ended +370 to +384 m E of truth.  σ converged to ~1 m but position
  was wildly wrong.

**Initial diagnosis (wrong) — blamed signal-map gap.**  Engine warned
"unmapped signal E sig_id=2 (bias=-0.3377 m)" once at startup.  We
attributed the divergence to that, plus presumed IOD-matching gaps.
But the dropped phase bias is 0.34 m — can't possibly cause 280 m.

**Real cause (found 2026-04-25 evening) — IGS-SSR orbit/clock 1000×
unit-scaling bug.**  pyrtcm decodes both standard RTCM SSR
(`DF365`-`DF378`) and IGS SSR (`IDF013`-`IDF021`) orbit/clock
fields with their spec's per-LSB scale factor — both return values
in mm and mm/s.  The engine correctly /1000-converted the DF* path
but used IDF* values raw, treating mm as already-metres.  Every
IGS-SSR-format AC's orbit and clock corrections were 1000× too
large.  CAS is the only IGS-SSR AC we use, so the bug only
manifested there.

Verified via pyrtcm field defs:
```
"DF365":  (INT, 22, 0.1, "Delta Radial"),
"IDF013": (INT, 22, 0.1, "Delta Orbit Radial"),
```
Identical scale factors, both return mm.

**Fix landed in commit `485612d`** — divides IDF013-021 by 1000 in
the IGS-SSR branches of `_parse_orbit` and `_parse_clock`.
5 regression tests in `tests/test_ssr_igs_unit_scaling.py` all pass.

**Post-fix CAS validation on clkPoC3 (5-min single host):**

| | east of Leica truth | σ |
|---|---|---|
| CAS pre-fix (5-min) | -64 m to -337 m | 1.0 m (filter "thinks" it's solved) |
| **CAS post-fix (5-min)** | **-1.85 m mean ± 0.38 m** | 0.87 m |

Reduction ~150× on absolute error.  CAS now produces PPP solutions
in the same regime as CNES (-0.77 m) and WHU (+0.45 m).  The
remaining ~1.85 m offset is consistent with normal AC datum
differences.

The signal-map gap (sig_id=2 unmapped) is a real but minor remaining
item — CAS works without it, fixing it improves coverage by one
signal per SV.  The disc_counter bug remains separately tracked.

## Path forward

### Near-term: cleaner provider comparisons that work today

1. **WHU OSBC00WHU1 paired with CNES orbit/clock** *(in flight as
   day0425g)* — WHU publishes biases only.  Engine has
   `--ssr-bias-ntrip-conf` for exactly this case.  Caveat: CNES still
   provides its own biases on the primary mount, so the test is
   "CNES orbit/clock + (CNES + WHU) biases merged" not pure
   substitution.  Useful as long as WHU's biases differ enough from
   CNES's to shift the result.
2. **Galileo HAS via E6-B** — would be the ideal independent
   diagnostic, but our F9T's don't track E6.  Out of scope without
   different hardware.
3. **MADOCA-PPP** — Japanese AC, different format again.  Same
   compatibility risks as CAS.

### Engine work to enable cleaner diagnostics

A. **`--no-primary-biases` flag** *(landed 2026-04-25)* — drops bias
   messages from `--ssr-ntrip-conf` while still consuming its
   orbit/clock.  Enables the clean 4-cell 2x2:

   | orbit/clock | biases | tells us |
   |---|---|---|
   | CNES | CNES | baseline (today: −6 to −8 m E) |
   | CNES | WHU  | does WHU's bias datum shift it toward truth? |
   | broadcast | CNES | does CNES orbit/clock contribute the bias? |
   | broadcast | WHU | clean WHU-only test |

B. **IGS-SSR signal-map fix for CAS / MADOCA** — extends the bias
   table with Galileo `sig_id=2` (likely E1B) under the IGS-SSR
   encoding (vs the RTCM 3.3 encoding we have).  Requires looking at
   the IGS-SSR signal-ID convention table and the CAS source-table
   metadata to confirm.  Likely 5–10 lines of map + a regression
   test.  Independent of (A); can land separately.

C. **IOD matching diagnostics** — add per-epoch logging of "SVs with
   no SSR orbit correction matched" so we can tell when the engine
   is using broadcast orbits unexpectedly because of IOD mismatch.
   Helps diagnose the second part of the CAS divergence.

### Per-correction-class binary search (if 2x2 doesn't isolate)

`--no-ssr-code-bias` and `--no-ssr-phase-bias` *(landed 2026-04-25)*
— drop the matching bias class from BOTH primary and secondary
SSR streams.  Use to isolate which class drives the bias once the
2x2 narrows it to "biases not orbit/clock".  Toggling one keeps the
other intact.

## References

- `scripts/diag_seed_sensitivity.py` — test harness
- `/tmp/seed_sensitivity-day0425c-noSSR.csv` — no-SSR baseline
- `/tmp/seed_sensitivity-day0425d-CNES.csv` — CNES full matrix
- `/tmp/seed_sensitivity-day0425e-CAS-broken.csv` — CAS divergence
- `docs/ssr-mount-survey.md` — strategic ranking of SSR providers
- `docs/ssr-requirements-by-receiver.md` — per-F9T-variant signal
  coverage requirements
- Memory `project_seed_sensitivity_test_floor_20260425.md` — full
  matrix calibration result
