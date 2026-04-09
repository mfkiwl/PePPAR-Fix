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
evolves into measurement fusion:

```python
# Predict
phase += adjfine_ppb * dt_s
phase_sigma += process_noise  # DO instability

# Update from all available measurements, weighted by confidence
for measurement in [PPS, qErr_refined_PPS, TICC]:
    innovation = measurement.value - predicted_phase
    gain = phase_sigma² / (phase_sigma² + measurement.sigma²)
    phase += gain * innovation
    phase_sigma *= sqrt(1 - gain)
```

Every source contributes what it knows, weighted by its confidence.
No winner-take-all.  The graceful degradation cascade (sigma inflation
when NTRIP goes stale) still works — stale measurements get wider σ,
their gain shrinks toward zero, and the predict step dominates.
That's holdover, falling out naturally from the fusion math.

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

The `InBandNoiseEstimator` component of DOFreqEst:
- Watches for adjfine-write events
- Accumulates free-running phase samples from discipline gaps
- Skips the first epoch after each write (transient)
- Computes running ADEV/TDEV at τ = 1, 2, 4, ... seconds
- Exposes `current_adev(tau)` for the servo gain scheduler to read
- Continuously updates as temperature and conditions change

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
