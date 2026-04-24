# Clock-state modeling and time-to-position coupling

Where time-domain knowledge enters the position filter, and the
options for tightening the coupling.

Motivation: the 2026-04-23 PRIDE investigation arc identified
filter rank deficiency as the dominant remaining error source
(see `feedback_math_check_and_set_ceiling.md` and
`docs/obs-model-completion-plan.md`).  The null vector
`(δclk, δZTD·m_wet, δN_common)` has a clock axis.  If we
constrain that axis with physics we already know (the oscillators
are far more stable than white noise), we directly attack the
null mode.

But "the clock" is ambiguous — PePPAR Fix carries **three
distinct oscillators** in different states.  This doc maps where
each lives, which ones today's position filter can see, and the
path-chosen ordering for bringing time-domain information into
position estimation.

## The three oscillators

| oscillator | what it is | where it lives today | observed how |
|---|---|---|---|
| **rx TCXO** | F9T receiver's internal TCXO | Inside the F9T chip. Its phase-vs-GPS is the PPP filter's `dt_rx` state (`IDX_CLK`). | GNSS pseudorange / carrier-phase observations via the PPP filter. The filter's *only* clock observation today. |
| **DO** | Disciplined Oscillator — the crystal the servo steers (i226 TCXO on TimeHat, OCXO on ocxo host, etc.) | Not in any filter. Physically downstream of the servo. | TICC chA timestamps the DO PPS edge relative to the TICC's own RO timebase. The servo reads it; the position filter doesn't. |
| **RO** | Reference Oscillator — drives the TICC's 10 MHz timebase input (currently the Geppetto GPSDO's OCXO) | Not in any filter. Implicit in every TICC timestamp. | TICC chB timestamps the F9T PPS edge relative to the RO. Since F9T PPS is GPS-locked, chB = `f_gps − f_ro = −f_ro`. Not consumed by any filter today. |

Relationships:

```
     GNSS satellites
          │
          │ (carrier phase, pseudorange)
          ▼
     ┌─────────────────┐
     │  F9T rx TCXO    │─── PPS OUT + qErr (TIM-TP) ──────┐
     └────────┬────────┘                                   │
              │ observes via PR/phi                        │
              ▼                                            │
     ┌─────────────────┐                                   │
     │   PPP filter    │─── dt_rx ─── servo ─── DO ────────┤
     │  (AntPosEst)    │                                   │
     └─────────────────┘                                   │
                                                           │
                                                           ▼
     ┌─────────────────┐           ┌─────────────────────────┐
     │   Geppetto RO   │ → 10MHz → │      TICC               │
     │    (OCXO)       │           │  chA: DO_PPS in RO time │
     └─────────────────┘           │  chB: F9T_PPS in RO time│
                                   └─────────────────────────┘
                                              │
                                              │ (chA-chB, etc.)
                                              ▼
                                         DOFreqEst / servo
                                         (NOT back to PPP filter)
```

**Today the position filter has no path from TICC timestamps
back to its clock state.**  It only sees rx TCXO through GNSS
observations.  The DO and RO are not modeled in any filter at
all.

## The four levers

Four distinct approaches to feeding time-domain information
into the position filter, ordered by structural depth:

### (A) Stochastic rx-TCXO model in the PPP filter

Replace the default **random walk on `dt_rx`** with a
physically-calibrated 2- or 3-state process model matching the
F9T TCXO's ADEV curve.  [GPS Solutions 2023 "Stochastic
modeling of receiver clock in Galileo-only and multi-GNSS PPP
solutions"](https://link.springer.com/article/10.1007/s10291-023-01556-9)
quantifies the improvement.  For OCXO-grade (~1e-11) receivers
the model improves PPP convergence noticeably; for TCXO-grade
receivers the improvement is smaller but non-zero because even
an TCXO is more structured than uncorrelated white noise.

**Advantages:**
- No new measurements.  Only changes `PPPFilter.predict()`'s
  process-noise matrix for the `IDX_CLK` state.
- Directly attacks the null vector's clock axis: a tighter
  process model bounds how fast `δclk` can drift between
  observations.
- No circular-dependency concerns — we're only modeling the
  existing state more faithfully.

**Dependencies:**
- Requires characterization of the F9T's TCXO ADEV at 1, 10,
  100, 1000 s.  Can be measured via TICC chA (with the DO fully
  steered, residual chA motion reflects F9T rx TCXO noise) or
  taken from u-blox datasheet.

**Prior art:** clock constraint (CC) + adaptive clock constraint
(ACC) models; Malys 1992 NEPTUNE through current literature.

### (B) TICC + qErr as a pseudo-measurement of the rx TCXO state

Add TICC chB + qErr as a rank-1 EKF pseudo-measurement of the
rx TCXO state.  The F9T PPS edge arriving at the TICC chB input
is a **physical observation** of where the rx TCXO thought it
was at that instant.

**Advantages:**
- Adds independent information beyond what the stochastic model
  captures — the actual edge timestamp, not just the predicted
  physics.
- Noise floor of 60 ps (TICC) + ~0.1 ns (qErr residual) is
  better than the PPP filter's internal clock-state noise
  (~0.3 ns) at 1 s integration.
- Closes the filter ↔ servo loop symmetrically: we already have
  `dt_rx → servo`; this gives us `TICC PPS → dt_rx`.

**Dependencies:**
- RO characterization (so the TICC timebase has a known noise
  contribution to subtract).  See "(0) RO characterization"
  below.
- Care with circular-dependency: the OCXO (DO) is disciplined
  by `dt_rx`, so TICC-measured DO is downstream of the filter.
  But TICC chB measures the **F9T PPS**, not the DO PPS — and
  the F9T PPS is NOT downstream of the filter's own output.
  Clean independent measurement.

### (C) Full three-oscillator co-estimation

Add DO and RO as additional states in the PPP filter (or a
coupled filter), with cross-state correlations capturing the
servo loop and the TICC measurement path.

**Advantages:**
- Captures all information the physical system provides.
- DMTD-style two-oscillator intercomparison becomes natural.
- Spoofing detection, holdover, and cross-consistency all fall
  out of the same framework.

**Disadvantages:**
- Large architectural change.  Adds multiple states; requires
  careful handling of correlations.
- Marginal improvement over (A) + (B) for typical operation.
- Deferred until (A) and (B) are validated.

### (0) RO characterization — not a time→position coupling itself

Characterize the Geppetto RO's ASD/PSD using TICC chB.  Reuse
the existing DO-characterization pipeline; point it at
qErr-corrected chB timestamps instead of chA.

**This by itself does NOT constrain the position filter.**  The
RO's state doesn't live in any filter today.  RO characterization
is required input for:

- Option (B): interpreting TICC measurements with calibrated
  RO-timebase noise.
- Spoofing detection: unexplained chB drift against a
  characterized-stable RO flags F9T PPS manipulation.
- Clean single-channel chA analysis (subtract RO contribution
  to isolate pure DO-vs-GPS).

See `docs/future-work.md` — "Reference Oscillator (RO)
characterization" section for the state-persistence schema and
operational motivation.  Sooner-or-later work regardless of
options (A)/(B)/(C).

## Status (2026-04-24)

### Part 1 — opt-in scaffold (landed)

Kwarg-gated calibrated-white Q is in `PPPFilter.__init__`:

- `clock_model='random_walk'` (default, bit-exact legacy behavior).
- `clock_model='calibrated_white'` uses
  `Q_clk = C² · rx_tcxo_adev_1s² · dt`.
- Default `rx_tcxo_adev_1s = 1e-8` is intentionally pessimistic
  (~100× looser than our measured TCXO); real F9T characterization is
  a prerequisite before flipping the engine default.
- 10 unit tests in `scripts/peppar_fix/test_clock_model.py` cover the
  formula, mode switching, default-preservation, and state-layout
  invariance.

State layout **unchanged** — still 7 base states, no `IDX_CLK_RATE`.
The scaffold is a process-noise tightening only.  Two-state (phase +
frequency) adds `IDX_CLK_RATE` and shifts `N_BASE` from 7 to 8,
which propagates to `ppp_ar.py` + every test that imports `N_BASE`.
Deferred to a later session.

### Part 2 — F9T rx TCXO characterization (pending)

Before flipping the engine default to `calibrated_white`, measure the
F9T's rx TCXO ADEV at τ = 1, 10, 100, 1000 s.  Two paths:

- **Datasheet lookup** — u-blox ZED-F9T hardware manual.  Fast, coarse.
- **Lab measurement** — qErr-detrended F9T PPS via TICC (chB against
  Geppetto RO) reveals the rx TCXO's instability once the RO
  contribution is subtracted.  Requires (0) RO characterization first.

Once we have a defensible σ_y(1s), wire it through the engine CLI as
`--rx-tcxo-adev-1s` and a matching `--clock-model` flag, and run
an A/B night against a host still on `random_walk`.

### Part 3 — two-state refactor (deferred)

Proper 2-state (phase + frequency) adds a clock-rate state and a
2×2 process-noise block.  Refactor touches:

- `solve_ppp.py`: add `IDX_CLK_RATE`, bump `N_BASE` to 8.
- `ppp_ar.py`: all `N_BASE + amb_idx` arithmetic auto-updates
  (uses module-level constant).
- `cycle_slip.py`, `fix_set_integrity_monitor.py`: same.
- `scripts/peppar_fix_engine.py`: filter-state readers
  (`filt.IDX_CLK`, etc.) — CLK is still at index 3, unchanged.
- Tests: `test_join_test.py`, `test_reached_resolved_regimes.py`,
  `test_phase_bias_gate.py` — regenerate fake filter shapes.

Lab validation required (regression the null-mode behavior against
`calibrated_white` 1-state result).

## Recommended ordering

```
      (0) RO characterization           (A) rx TCXO stochastic model
            │                                  │
            │                                  │
            ▼                                  ▼
      (1) RO state persisted             (2) Null-mode clock-leg
         in timestampers/                     constraint active in filter
            │                                  │
            └──────────────┬───────────────────┘
                           ▼
                  (B) TICC + qErr pseudo-measurement
                  of rx TCXO state in PPP filter
                           │
                           ▼
                  (C) Full three-oscillator
                       co-estimation
                       (if needed)
```

Step-by-step:

1. **(0) RO characterization** — code reuse from
   DO-characterization pipeline pointed at qErr-corrected chB.
   Gives us a calibrated RO model and spoofing-detection hook.
   Low effort (~1 session).  Already anticipated in
   `docs/future-work.md`.
2. **(A) rx TCXO stochastic model** — replace `IDX_CLK`'s
   process noise with a 2-state (or 3-state) calibrated model.
   No new measurements.  Directly attacks the null vector's
   clock axis.  ~30-50 LOC in `PPPFilter.predict()` plus a
   lab-local ADEV measurement for the F9T TCXO.  Low-medium
   effort (~1-2 sessions).
3. **(B) TICC + qErr as pseudo-measurement** — rank-1 EKF
   update injected after the regular observation update.
   Depends on both (0) and (A) landing first.  Medium effort
   (~2 sessions).
4. **(C) Full co-estimation** — larger architectural change;
   defer until (A) and (B) are validated and there's a concrete
   regime they don't serve.

## Prior art worth knowing

The literature covers several related threads:

- **Stochastic receiver-clock modeling** (option A): Malys 1992
  NEPTUNE; refined through current GPS Solutions papers.  The
  improvement scales with oscillator grade (bigger gain on
  OCXO/CSAC/Rb, smaller on TCXO/XO).
- **Real-Time Precise Point Timing (RT-PPT)** with time
  holdover: receivers like Meinberg / Microchip GPSDOs.
  Combines stochastic clock modeling with servo discipline.
  Achieves <1 ns / hour (Rb) and <1 μs / day (OCXO) holdover.
- **Chip-Scale Atomic Clock (CSAC) + GNSS deep integration**:
  closest architectural analog to option (C).  Standard in
  military timing receivers.
- **Dual-Mixer Time Difference (DMTD)** measurements: the
  metrology technique for two-oscillator intercomparison
  mentioned in our future-OCXO-second-host architecture
  discussion.

## What this doc does NOT cover

- Servo design.  The feedback from `dt_rx` to the servo is a
  different optimization problem (loop bandwidth tuning via
  PSD crossover; see `docs/asd-psd-servo-tuning.md` and
  `docs/architecture-vision.md`).  Clock-state modeling in the
  position filter is orthogonal.
- PPS physical-edge measurement details.  Quantization, sawtooth,
  qErr correction — all characterized in their own docs
  (`docs/qerr-correlation.md`, `docs/stream-timescale-correlation.md`).
- DMTD as an instrument.  Two-OCXO with deliberate frequency
  offset discussed in the 2026-04-24 session notes; queued as
  a future metrology technique when a regime surfaces that PPS-
  based TICC comparison can't serve.

## Cross-references

- `docs/future-work.md` — RO characterization section; now
  points back to this doc for the position-filter context.
- `docs/obs-model-completion-plan.md` — engine roll-up;
  clock-state work is an independent axis from the six-phase
  obs-model plan but compose cleanly.
- `feedback_math_check_and_set_ceiling.md` — the investigation
  arc that identified null-mode excitation as the dominant
  error source.  The "null mode is geometric, not tunable"
  bonus section specifically calls out that filter tuning
  changes how the filter *responds* to residuals; the
  stochastic clock model (option A) is one of those responses,
  tightened.
- `docs/glossary.md` — definitions for null mode, rank-
  deficient, null vector, OU process, DO, RO, rx TCXO.
- `docs/qerr-correlation.md` — the qErr matching story that's
  load-bearing for option (B).
- `docs/asd-psd-servo-tuning.md` — noise-spectrum
  characterization machinery we'll reuse to characterize the
  rx TCXO for option (A).
