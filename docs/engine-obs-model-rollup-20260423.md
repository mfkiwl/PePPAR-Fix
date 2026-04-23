# Engine roll-up: lessons from the 2026-04-23 PRIDE investigation arc

A single reference for what today's harness-side investigation
means for the engine.  Maps Bravo's findings to engine action
items.  Companion to `docs/obs-model-completion-plan.md` (the
implementation spec) and `feedback_math_check_and_set_ceiling.md`
(the lessons captured in memory).

## Arc summary

Bravo ran five hypotheses to ground on `pride-harness-ar`.
Cached ABMF 2020/001 + PRIDE-PPPAR as external yardstick.

| # | Hypothesis | Result |
|---|---|---|
| 1 | Per-constellation profiles fix GPS+GAL residual | Falsified (L2 profile worse than L5) |
| 2 | ZTD pseudo-measurement bounds null-mode drift | Falsified (bad σ math + fights clean geometry) |
| 3 | Q_pos / σ_PR / σ_phi tuning closes overconfidence | σ_phi=0.30 honest on ABMF; did NOT port to lab |
| 4 | OU-process ZTD replaces random-walk | Falsified (fights real tropo, every (τ, σ)) |
| 5 | Solid Earth tide addition | Landed on harness, −39 mm on clean geometry |
| 6 | WUH2 slip-storm handling | Filter handles cleanly; MW not load-bearing |

Quantitative ceiling set:

- **PRIDE ABMF 2020/001 GPS+GAL static float: 3.3 mm 3D.**
- **PRIDE WUH2 2023/002 GPS+GAL static float: sub-mm.**
- Our harness on same data: 5.7 m and 0.58 m respectively.

## Root-cause diagnosis

The 5.7 m ABMF gap is not a single broken piece.  It is
**obs-model gaps × null-mode coupling** — cm-scale residuals
from missing corrections (solid tide 42 mm, PCVs 5-15 mm,
wind-up 1-5 mm, GMF 1-5 mm, attitude 1-5 mm) get amplified by
~100× through the filter's near-rank-deficient geometry
((δclk, δZTD·m_wet, δN_common)) over a 24 h run.

Three approaches ruled out by today's data:

- **Particle filter** — scales exponentially with state
  dimension; PPP's ~20-30 states put it out of reach.  Not
  used by any mainstream PPP implementation.
- **Blunt structural constraint** on null direction
  (ztd-tie, OU-ZTD) — fights real physics on clean geometry
  with no mechanism to selectively disable.
- **Adaptive structural constraint** — plausible but large
  investigation (particle filter / mixture model
  territory).  Deferred.

One approach validated:

- **Complete the obs-model**.  Each correction reduces
  residuals at the source; null mode stops being excited;
  PRIDE's 3.3 mm ceiling proves this works.

## Engine action items — ordered by value

### Definite wins, land when the prerequisite is met

#### 1. Solid Earth tide port

Largest single correction (42 mm on ABMF per PRIDE ablation,
−39 mm measured on our harness).  Bravo's
`solid_tide.py` module on `pride-harness-ar` ports directly.
`PPPFilter.update()` already has a `receiver_offset_ecef`
kwarg.  ~1 session.  **Phase 1 of
`docs/obs-model-completion-plan.md`.**

Expected: clean-geometry lab hosts gain ~40 mm; trapped hosts
largely unchanged until enough obs-model is in to starve the
null mode.

#### 2. Satellite + receiver PCV port (after harness lands Phase 2)

Second-biggest magnitude (~10-25 mm combined).  Needs ANTEX
parser + per-SV-per-epoch nadir/elevation lookup + IGS14.atx
distribution.  **Phase 2 of obs-model plan.**

Order: PCVs must follow solid tide because together they cover
~70 mm of the ~100 mm total obs-model gap.  Either one alone
is incremental; both together is the break-even checkpoint.

#### 3. Phase wind-up / GMF / attitude in sequence

Each ~1-5 mm.  **Phases 3-5 of obs-model plan.**  Ship after
the break-even checkpoint reveals whether they're worth the
effort.

### Contingent wins — need more data before shipping

#### 4. σ_phi calibration (lab-local)

Today's attempt to port σ_phi=0.03 → 0.30 regressed the lab
by 25 m altitude on two of three L5 fleet hosts.  Bravo's
ABMF matrix said "no regression in any geometry tested";
the claim didn't transfer because ABMF's post-processed data
and our real-time SSR differ in PR noise characteristics.

The finding persists: **our lab filter is 10-250×
overconfident on Q2 ratio.**  σ_phi is the right lever.  The
value isn't 0.30 for our lab.

Revised plan: **after Phase 1 + 2 of obs-model lands** (which
will reduce residual magnitudes at the source), re-run the
Q2 analysis and do a lab-local σ_phi sweep with completed
obs-model.  The right value likely changes because the
effective phase variance changes.

Don't attempt this before obs-model is in.  That was today's
mistake.

#### 5. ≥ 6× safety factor on σ-gate decisions

Any downstream code that reads the filter's reported σ and
compares it against a threshold (anchor_collapse,
bootstrap-gate, false-fix) should multiply σ by ≥ 6× before
trusting.  6× is Bravo's measured ABMF baseline overconfidence
with σ_phi=0.30; real lab overconfidence is worse but the 6×
is a defensible floor.

Concrete engine changes:

- `FixSetIntegrityMonitor`'s window_rms and anchor_collapse
  gates — currently threshold in meters, not σ-relative.  If
  they become σ-relative in the future, apply the factor.
- `NarrowLaneResolver` bootstrap gate on LAMBDA — uses
  filter σ in its ratio test chain.  Audit and apply.

Low urgency (we're in WL-only, NL not active), but worth a
pass when the engine re-enters NL territory.

### Diagnostics — land when convenient

#### 6. Null-mode monitor

Compute smallest eigenvalue of P's position+clk+ZTD block
per epoch.  Log a warning when it drops below threshold.
Diagnostic-only, no action.  Tells operators when the filter
is in a rank-degenerate regime.  ~30 lines.

#### 7. Q2 ratio as standing metric

Fold `reported σ / empirical cross-host spread` into the
engine's continuous monitoring (peppar-mon dashboard if
feasible, log line at minimum).  Standing signal for
strong-but-wrong trap formation.  Analysis script already
exists at `/tmp/q2-analysis/analyze.py` from today.

## Not engine-portable from this arc

- **Per-constellation profiles** (Bravo's Part B): the
  infrastructure landed on harness but the hypothesis was
  falsified.  Keep the code as a diagnostic tool, do not
  promote to engine.
- **ZTD pseudo-measurement tie** / **OU-process ZTD**:
  both falsified.  Flags remain on harness as diagnostic
  tools with negative results documented in commit messages.
  Engine gets nothing from these.
- **MW slip detection defaults**: Bravo's WUH2 ablation
  showed MW detection isn't load-bearing on that dataset
  (−11 cm improvement when disabled).  Our engine has a
  multi-layer slip-detection stack (MW jump + WlDriftMonitor
  + Layer 3 re-admission) that catches traps we've actually
  seen in the lab.  Don't change defaults based on a single
  dataset's ablation, but note the signal: aggressive early
  flushing has costs.  Could justify an engine-side ablation
  study when priorities allow.

## Sequencing

1. Port solid tide (engine, ~1 session) — after or in
   parallel with Bravo's Phase 2 PCV harness work.
2. PCV port — after Bravo lands harness-side Phase 2.
3. Break-even checkpoint: measure lab Q2 + lab 3D vs
   surveyed on clean-geometry hosts.  Decide whether
   Phases 3-5 are worth chasing.
4. Lab-local σ_phi sweep AFTER solid tide + PCVs are in.
   This order matters — measurement noise model depends on
   residual magnitudes, which depend on obs-model
   completeness.
5. σ-gate safety factors when/if NL re-enters.
6. Null-mode + Q2 diagnostics as background enhancements.

## What's NOT in this memo

- The lessons on reviewing filter-math proposals (σ-math
  sanity check), setting external ceilings before chasing
  residuals, structural-constraint pathology class, and
  tuning-transferability.  All captured in
  `feedback_math_check_and_set_ceiling.md`.
- The implementation plan for each obs-model phase.  In
  `docs/obs-model-completion-plan.md`.
- The glossary entries for rank-deficient, null vector, null
  mode, OU process.  In `docs/glossary.md`.

This memo's job is to be the single engine-side reference:
"given everything learned today, what does the engine do,
in what order, and why?"

## Status of supporting work

- Engine at `bb01b78` on `main`.  Lab L5 fleet restarted onto
  it as `day0423f-revert` after the σ_phi regression.
  ptpmon continues on `day0423c-elev-gate` (older cb44f4e,
  own antenna, continuous baseline).
- Bravo at `b28e66a` on `pride-harness-ar` — 13 commits
  ahead of main.  All harness-side.  Moving to Phase 2 PCV
  work next.
- No engine commits from this arc have produced regressions
  that require rollback beyond what's already been handled.
