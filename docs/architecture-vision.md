# Architecture Vision — AntPosEst + DOFreqEst

*2026-04-09 design discussion.  This document describes where we're going,
not where we are today.  Current implementation details live in the
per-topic design docs; this doc provides the unifying frame.*

## Naming principle

Name things for their **value**, not their mechanism.  There are EKFs
inside these components, but "filter" describes how they work, not what
they deliver.  The right names describe what value each component adds
to the system.

## The two estimators

### AntPosEst — Antenna Position Estimator

**Value**: "Where is GPS time?" — refines the antenna phase center
position, our phase reference, pushing GPS time phase bias toward zero.

**Timescale**: minutes to hours.  Works over long timescales using
carrier-phase observations and corrections (float PPP → PPP-AR) to
converge the antenna position from meters to centimeters.

**Independence**: runs completely independently of any DO, PHC, or
servo.  Just needs observations and corrections.  This is the
`--no-do` mode: peppar-fix reduces to pure AntPosEst.

**Implementation**: the same `PPPFilter` class that currently runs in
Phase 1 (position bootstrap), kept alive into Phase 2 and fed
decimated observations from a background thread.  No new filter code.
Extended with AR when phase biases are available (see
`docs/ppp-ar-design.md`).

**Output**: ECEF position that gradually migrates into DOFreqEst's
phase reference via exponential blending.

### DOFreqEst — Disciplined Oscillator Frequency Estimator

**Value**: "How fast is the DO drifting from GPS time?" — tracks the
DO's frequency offset from GPS, given a fixed or slowly-changing
phase reference (position) over short timescales.

**Timescale**: sub-second to minutes.  Works epoch-by-epoch at 1 Hz,
producing a frequency correction (`adjfine_ppb`) applied to the DO.

**Components** (internal, not separate peers):

- **EKF** (currently `FixedPosFilter`): estimates `dt_rx` at each
  epoch given the position from AntPosEst.  Uses time-differenced
  carrier phase — no ambiguity states, no AR needed.
- **FrequencyIntegrator** (currently `CarrierPhaseTracker`):
  integrates adjfine corrections into a running phase estimate,
  providing continuous phase knowledge between PPS edges.
- **Measurement fusion** (currently `compute_error_sources` source
  competition): combines PPS edges, qErr refinement, TICC
  measurements, and the frequency integrator's prediction into a
  single phase/frequency estimate each epoch.
- **PI servo**: converts the phase/frequency estimate into
  `adjfine_ppb` applied to the DO.
- **InBandNoiseEstimator**: continuously estimates the DO's noise
  floor from discipline gaps (see below).

**Output**: `adjfine_ppb` written to the DO's PHC via `clock_adjtime`.

## How the two estimators interact

```
AntPosEst                          DOFreqEst
(position, long timescale)         (frequency, short timescale)

  observations ──┐                 ┌─────────────────────────┐
  corrections  ──┤                 │  EKF (dt_rx)            │
  AR module    ──┘                 │  PPS + qErr measurement │
       │                           │  FrequencyIntegrator    │
       │  position ──(gradual)──▶  │  Measurement fusion     │
       │                           │  PI servo → adjfine     │
  NAV2-PVT ──(consensus)────────▶ │  Consensus watchdog     │
                                   │  InBandNoiseEstimator   │
                                   └──────────┬──────────────┘
                                              │
                                              ▼
                                       DO frequency output
```

Position flows one-way from AntPosEst to DOFreqEst.  The consensus
watchdog uses both estimators plus the F9T secondary engine (NAV2-PVT)
to detect antenna movement or filter corruption (see
`docs/future-work.md` "Three-source position consensus").

## Measurement fusion replaces source competition

The current `compute_error_sources()` builds a sorted list of error
sources (PPS, PPS+qErr, Carrier, PPS+PPP, TICC) and picks the winner
by lowest `confidence_ns`.  This is a stepping stone.

The sources aren't actually competing for the same thing:

- **PPS, PPS+qErr, TICC** are **epoch measurements** — snapshots of
  the DO's current phase offset from GPS.  Independent noise each
  epoch.
- **Carrier (FrequencyIntegrator)** is a **prediction** — "given
  where we started and every adjfine correction since, the DO should
  be at this phase."  Smooth, but accumulates systematic errors.

These are the **predict** and **update** steps of a single estimator,
not competitors.  The FrequencyIntegrator predicts; the PPS/qErr/TICC
measurements update.  The source competition's winner-take-all
evolves into measurement fusion.

### The 4-state DOFreqEst filter (target architecture)

The fundamental insight (2026-04-10): the PHC phase — the thing we're
trying to control — is **never directly observed**.  We only observe
it indirectly through the difference (δ_phc − δ_tcxo) via PPS edges
timestamped on the PHC/TICC.  Meanwhile, carrier-phase measurements
give us δ_tcxo via the PPP filter.  Satellite clocks couple to our DO
**through** the receiver's TCXO.

The correct state space has four hidden variables:

| State | Meaning | Observed by |
|---|---|---|
| δ_tcxo | F9T TCXO phase vs GPS | PPP dt_rx (~0.1 ns) |
| f_tcxo | F9T TCXO frequency drift | d(dt_rx)/dt |
| δ_phc | DO/PHC phase vs GPS | only via (δ_phc − δ_tcxo) |
| f_phc | DO crystal drift rate | adjfine residuals |

Process model (linear, standard Kalman):

```
δ_tcxo[n+1] = δ_tcxo[n] + f_tcxo[n] · dt + w_tcxo
f_tcxo[n+1] = f_tcxo[n] + w_f_tcxo

δ_phc[n+1]  = δ_phc[n] + (f_phc[n] + adjfine[n]) · dt + w_phc
f_phc[n+1]  = f_phc[n] + w_f_phc
```

Measurement model:

```
z_ppp  = δ_tcxo + v_ppp              (PPP dt_rx, ~0.1 ns)
z_ticc = (δ_phc − δ_tcxo) + v_ticc  (TICC+qErr, ~0.178 ns)
```

The PPS edges serve as **ambiguity resolution**: every second, the
PPS says "the TCXO thinks it's the GPS second boundary" and the TICC
says "the PHC reads this."  That pins (δ_phc − δ_tcxo).  Between PPS
edges, carrier phase tracks δ_tcxo with 0.1 ns precision.  The filter
propagates that through the coupling to constrain δ_phc.

No separate drift rate D.  No cascade of estimators.  No
CarrierPhaseTracker accumulating adjfine with growing bias.  The
inter-oscillator coupling falls out of the filter states naturally.

adjfine is a **known input** to f_phc (we set it), not an estimated
state.  The filter only estimates the crystal drift component.

All noise parameters are lab-measured: TCXO TDEV (0.92 ns), PHC
TDEV, TICC floor (0.06 ns), PPP sigma (~0.1 ns), TCXO frequency
random walk (0.01 ppb/epoch), DO frequency random walk.

### Stepping stone: time-differenced dt_rx (implemented first)

The 4-state filter is the target, but a simpler stepping stone gives
most of the benefit: inject **time-differenced** Δdt_rx as a
frequency measurement into the existing 2-state Kalman servo.

Δdt_rx = dt_rx[n] − dt_rx[n−1] measures how much the TCXO-to-GPS
offset changed in one epoch.  This constrains the Kalman's frequency
state (how fast the PHC is drifting) without introducing any absolute
bias — the differencing cancels the unconverged drift rate D that
caused the absolute-carrier fusion to fail (2026-04-10 experiment:
Carrier's 0.1 ns sigma + 24 ns bias → Kalman trusted biased Carrier
30× more than truthful TICC → divergence).

Time-differencing throws away absolute phase knowledge (which the
TICC already provides) and keeps only the frequency information from
PPP.  This is suboptimal compared to the 4-state filter (which
extracts both phase and frequency from dt_rx), but robust to model
errors.  The 4-state filter subsumes the time-differenced approach:
its update-predict cycle implicitly time-differences while also
using the absolute information.

### Graceful degradation

In all fusion architectures, the sigma-inflation mechanism still
works.  When NTRIP corrections go stale:
- PPP dt_rx sigma inflates → its Kalman gain shrinks toward zero
- TICC+qErr (no NTRIP dependency) continues at full weight
- The filter naturally enters holdover: predict step dominates,
  last-known frequency sustains the DO

No winner-take-all.  No explicit mode switch.  Holdover falls out
of the math.

## State machines and seed verification

### AntPosEst states

```
UNSURVEYED  → no seed, no observations.
               DOFreqEst: blocked.  clockClass: 248.

VERIFYING   → seed exists (position.json / known_pos).
               Live LS fix being computed to check seed.
               DOFreqEst: blocked.  clockClass: 248.

VERIFIED    → live LS fix agrees with seed within threshold.
               DOFreqEst: may initialize.  clockClass: 52.

CONVERGING  → float PPP running, position improving.
               DOFreqEst: running.  clockClass: 6 (once locked).

RESOLVED    → AR fixed, position at cm level.
               Phase bias < 100 ps.  clockClass: 6.

MOVED       → consensus detected antenna displacement.
               DOFreqEst: holdover.  clockClass: 7.
               AntPosEst: re-enters CONVERGING.
```

### DOFreqEst states

```
UNINITIALIZED   → waiting for AntPosEst.  clockClass: 248.

PHASE_SETTING   → stepping DO phase to GPS second.  clockClass: 52.

FREQ_VERIFYING  → checking drift file against PPS measurement.
                   clockClass: 52.

TRACKING        → servo running.  clockClass: 6.

HOLDOVER        → no usable measurements.  Coast on last adjfine.
                   clockClass: 7.
```

### Seed verification principle

No estimator trusts its seed.  Seeds are initial conditions that
accelerate convergence.  Verification is always the first step:

- **Position seed** (position.json): verified by live LS fix within
  threshold (~10 m).  If rejected, fall back to PPP from scratch.
- **Frequency seed** (drift.json): verified by PPS interval
  measurement within threshold (~50 ppb).  If rejected, use the
  measured value.
- **DO characterization seed** (do_characterization.json): verified
  by the InBandNoiseEstimator as discipline gaps accumulate.  If the
  live noise profile diverges significantly (temperature change, aging),
  the live estimate supersedes the file.

## In-band DO noise estimation

The current model requires a one-time 30-minute `--freerun`
characterization run that produces `do_characterization.json`.  This
is a snapshot at one temperature, one time, one set of conditions.
It goes stale.

**The adaptive discipline interval creates natural measurement
windows.**  When the scheduler extends the discipline interval (because
the servo has converged and the oscillator is stable), epochs between
adjfine writes are genuinely free-running.  During a 10-second
discipline interval, epochs 2–9 see the DO drifting with no correction
applied.  After removing the known linear drift (constant adjfine × dt),
the residual is pure DO phase noise.

The `InBandNoiseEstimator` component of DOFreqEst runs two channels:

- **Gap channel**: phase noise from epochs with no adjfine change
  (pure DO noise — the oscillator's free-running floor).
- **Correction channel**: residual noise from epochs where adjfine
  changed.  The expected phase change is subtracted
  (`residual = measured - expected`, where `expected = Δfreq × Δt`).
  If adjfine() calls are truly free (as measured 2026-04-09:
  `docs/adjfine-noise-characterization-2026-04-09.md`), both channels
  should agree.  Divergence indicates DO stress or write-latency issues.

Both channels compute running ADEV/TDEV at τ = 1, 2, 4, ... seconds.
The gap channel gives the DO's noise floor; the correction channel
validates that the servo's corrections aren't adding hidden noise.

Dynamic uses of the two-channel comparison:
- **Adaptive correction clamping**: reduce max correction if the
  correction channel exceeds the gap channel (DO can't absorb it)
- **DO health monitoring**: aging crystals show increasing correction
  noise before catastrophic failure
- **Per-epoch confidence weighting**: epochs with large corrections
  get lower confidence for downstream consumers (PTP clients, TICC)
- **Correction scheduling**: skip corrections whose noise cost
  exceeds their benefit (Δfreq × Δt < DO noise floor → don't bother)

**Benefits over one-time freerun**:
- Continuously current (tracks temperature-dependent noise changes)
- No lost time (happens during normal operation)
- Better statistics (thousands of gaps per overnight)
- Temperature correlation possible (if board temp is logged)

**What it can't measure**: ADEV at τ longer than the maximum
discipline interval (the servo is steering within that window).
For initial deployment, the one-time freerun characterization still
seeds the noise model — the in-band estimator verifies and refines it.
See `docs/asd-psd-servo-tuning.md` and `docs/freerun-characterization.md`
for the current (pre-in-band) approach.

## `--no-do` mode

AntPosEst runs alone.  No PHC, no servo, no adjfine.  Doesn't need
root.  Produces `position.json` (converged position) and optionally
a convergence log.  Useful for:

- Pre-surveying a deployment site before DO hardware is installed
- Validating the GNSS chain independently of timing hardware
- The outdoor weatherproof unit scenario: run position survey first,
  then start frequency tracking once position has converged

## `--characterize-do` mode (replaces `--freerun`)

There IS a DO, but we deliberately don't steer it.  Measures the
free-running DO noise for initial servo tuning.  This is different
from `--no-do`: it opens the PHC, reads EXTTS, reads PPS, computes
phase error — everything except writing adjfine.  Produces
`do_characterization.json`.

As the in-band noise estimator matures, the initial characterization
window can shrink (from 30 minutes to 5 minutes) because the in-band
estimator will refine the profile from real operational data within
the first hour.

## Wrapper dissolution

The current `scripts/peppar-fix` shell wrapper provides seven pieces
of value.  In the integrated model:

| Wrapper value today | Future home |
|---|---|
| Sequencing (steps 1→2→2b→2c→3) | Internal state machine (AntPosEst / DOFreqEst states) |
| Retry dispatch (exit codes 0/2/3/5) | Internal state transitions (MOVED, HOLDOVER, self-heal) |
| PHC bootstrap (phc_bootstrap.py) | DOFreqEst initialization (PHASE_SETTING, FREQ_VERIFYING) |
| Receiver config | First thing in `main()` or `ExecStartPre` in systemd |
| DO characterization | `--characterize-do` mode or in-band estimation |
| clockClass management | Driven by estimator state transitions, not exit codes |
| Config resolution | Python argparse + TOML loader in `main()` |

The wrapper becomes a one-liner:

```sh
exec "$PYTHON" peppar_fix.py "$@"
```

Systemd's `Restart=on-failure` handles crash recovery.  The supervisor
cron job handles exponential backoff for repeated crashes.  All
intelligence lives in Python where it can be tested and debugged.

## Migration path

No big-bang rewrite.  Each step makes the wrapper simpler and the
engine more self-contained:

1. Merge `phc_bootstrap.py` into the engine as DOFreqEst initialization.
   Wrapper's Step 2b disappears.
2. Keep PPPFilter alive past Phase 1 as AntPosEst background thread.
   Wrapper's Step 2 disappears.
3. Move exit-code retry logic into internal state transitions.
   Wrapper's `while true` loop disappears.
4. Move clockClass management to estimator state transitions.
   Wrapper's `degrade_clock_class`/`promote_clock_class` disappear.
5. Add InBandNoiseEstimator.  DO characterization step shrinks or
   becomes optional.

Each step is independently testable and deployable.

## Naming concordance

| Current name | Future name | Notes |
|---|---|---|
| `PPPFilter` | `AntPosEst` | Antenna Position Estimator |
| `FixedPosFilter` | `DOFreqEst` (internal EKF) | DO Frequency Estimator |
| `CarrierPhaseTracker` | `FrequencyIntegrator` (inside DOFreqEst) | Integrates adjfine into running phase |
| `compute_error_sources()` | `fuse_measurements()` (inside DOFreqEst) | Predict/update fusion, not competition |
| `phc_bootstrap.py` | DOFreqEst `PHASE_SETTING` + `FREQ_VERIFYING` states | Absorbed into engine |
| `--freerun` (for DO characterization) | `--characterize-do` | Distinct from `--no-do` |
| `--freerun` (no PHC at all) | `--no-do` | AntPosEst only |
| `peppar-fix` wrapper | thin launcher or `exec peppar_fix.py` | Intelligence moves to Python |

## Cross-references

- **Three-source consensus + self-healing**: `docs/future-work.md`
- **PPP-AR + gradual position migration**: `docs/ppp-ar-design.md`
- **clockClass state mapping**: `docs/ptp4l-supervision.md`
- **Current PHC bootstrap (to be absorbed)**: `docs/phc-bootstrap.md`
- **Current freerun characterization (to be superseded by in-band)**: `docs/freerun-characterization.md`
- **Current source competition (to evolve to fusion)**: `docs/pps-ppp-error-source.md`, `docs/carrier-phase-servo.md`
- **Current data flow (to be updated)**: `docs/full-data-flow.md`
- **Servo noise tuning (impacted by in-band estimation)**: `docs/asd-psd-servo-tuning.md`
- **MadHat EKF overconfidence incident (motivating the consensus design)**: project memory `project_madhat_ekf_overconfidence`
