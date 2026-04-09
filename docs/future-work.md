# Future Work

Improvement candidates drawn from SatPulse comparison and operational
experience.  These are independent of each other and can be adopted
incrementally.

## Three-source position consensus + self-healing FixedPosFilter

**What**: When the engine is in Phase 2 (FixedPosFilter, position
locked), maintain *three independent* estimates of the antenna's
position and use majority consensus to distinguish "antenna physically
moved" from "internal filter state corrupted".  When the FixedPos
EKF blows up, reset it from the consensus position and coast on
PPS+qErr until it reconverges, instead of the current behavior of
exiting the engine and waiting for a manual restart.

**Why**: 2026-04-08 evening, MadHat's overnight died at 51 minutes
because the FixedPosFilter's residual-RMS watchdog tripped:

```
[3030] dt_rx=+8.18 ms ±0.11 ns  rms=  28 m  ← normal
[3035] dt_rx=−12.04 ms ±0.11 ns rms=1323 m  ← BLOWN UP
```

The dt_rx jumped 20 ms in one epoch while the EKF's own σ stayed
at 0.1 ns — the textbook "misplaced confidence" failure.  Most likely
cause: simultaneous undetected cycle slips on multiple satellites,
which the EKF "absorbed" into its position+clock states while
reporting unchanged covariance.  The watchdog correctly detected
something was wrong, but its only diagnostic message was "antenna may
have moved!" — which was *false*.  TimeHat (different F9T on the same
physical antenna via splitter) ran fine through the same instant, so
the antenna obviously hadn't moved.  The engine had no way to tell.

The current architecture has only **one** position-aware EKF in
steady state (FixedPosFilter), and its watchdog can't disambiguate
"antenna moved" from "filter corrupted".  Adding two independent
position references gives us a 2-of-3 vote.

**The three sources**:

1. **Live PPP background monitor** — a second `PPPFilter` instance
   running in its own thread on the same observation stream (or a
   subsampled copy, e.g. one epoch every 10–30 s to keep CPU low).
   Independent state vector, independent ambiguity tracking.  When
   it converges, its position estimate is an outer-loop sanity check
   on `known_ecef`.  If it agrees with `known_ecef`, the antenna is
   where we think it is.  If it disagrees, the antenna actually moved.

2. **F9T's own onboard position fix** — the third opinion.  The F9T
   runs its own internal position-fixing engine continuously and
   reports it via `NAV-PVT` (and `NAV-HPPOSECEF` for high precision).
   We **already enable NAV-PVT** in the receiver config
   (`scripts/peppar_fix/receiver.py:99`) but currently drop it on the
   floor in `realtime_ppp.py`.  The plumbing addition is small: stash
   the latest `NAV-PVT` lat/lon/h into a thread-safe slot and let the
   monitor read it.  In TIME mode the F9T forces its position, so we
   need to either run the F9T in NAV mode (defeats the purpose of
   TIME mode) or use NAV-PVT only as a sanity check that the F9T's
   *internal* fixed-position state hasn't been disturbed.  Ideally
   we toggle the F9T out of TIME mode briefly each minute to take a
   fresh fix — but the cleaner answer is to just leave the F9T in NAV
   mode and rely on our own EKFs for the precise clock estimation.

3. **The original `known_ecef`** — the position the engine bootstrapped
   to (or that came from `known_pos`/`position.json`).  This is the
   "ground truth as of the last bootstrap" that the FixedPosFilter
   trusts implicitly.

**Consensus logic** (all distances in metres, computed in ECEF):

```
Δ_PPP    = | bg_PPP_position - known_ecef |
Δ_F9T    = | F9T_NAV_PVT_position - known_ecef |
Δ_F9T_v_PPP = | F9T_NAV_PVT_position - bg_PPP_position |

case 1: Δ_PPP ≤ ε   AND  Δ_F9T ≤ ε
        → all three agree.  If FixedPos watchdog trips here,
          known_ecef and bg_PPP_position both confirm the antenna
          is fine.  → RESET FixedPos state from known_ecef, coast
          on PPS+qErr until FixedPos reconverges.  Self-healing.

case 2: Δ_PPP > ε   AND  Δ_F9T > ε
        → both independent sources say the antenna moved.
          known_ecef is stale.  → trigger a real bootstrap, save the
          new position, restart FixedPos from the new known_ecef.

case 3: Δ_PPP ≤ ε   AND  Δ_F9T > ε  (or vice versa)
        → disagreement between background sources.  Don't act on
          either alone.  Hold the alarm and log loud — operator
          investigation needed.

case 4: Δ_F9T ≤ ε   BUT bg_PPP itself blows up
        → the background PPP is poisoned (cycle slips, bad SSR).
          F9T position confirms antenna is fine.  → reset bg_PPP
          state without touching FixedPos.

Threshold ε: ~5 m for "agreement".  Loose enough that PPP filter
noise during convergence doesn't trip it; tight enough that real
antenna moves of >10 m alarm immediately.
```

**Behavior when FixedPos resets without operator intervention**:
the engine should stay in steady-state servo on PPS+qErr (whose error
source confidence is 3 ns) for the ~60–90 seconds it takes the new
FixedPosFilter to reconverge from `known_ecef`.  PPS+qErr doesn't
care about `dt_rx` so the corrupted-EKF event doesn't disturb it.
Once FixedPos's `dt_rx_sigma_ns` drops back below the carrier_max
threshold and the source competition picks Carrier again, the engine
is fully back.  No restart, no missed minutes of data, no operator
woken at 3 am.

**Implementation sketch**:

- New `BackgroundPPPMonitor` thread in `peppar_fix_engine.py`
- New `F9TPositionStore` (analogous to `QErrStore`) in `realtime_ppp.py`
- `serial_reader` parses `NAV-PVT` (already enabled) and writes to
  `F9TPositionStore`
- `FixedPosFilter` watchdog handler reads both consensus sources
  before deciding what to do
- New CLI flag `--bg-ppp-cadence-s` (default: 30 s)
- New servo CSV column: `consensus_state` (one of `agree`, `f9t_only`,
  `ppp_only`, `disagreement`, `recovering`)

**CPU/memory cost**: PPP filter on a Pi 5 takes ~2 s of CPU per epoch
at full rate.  Subsampled to every 30 s, the background monitor
consumes ~6% of one core.  Memory is one extra EKF state vector
(dozens of floats per satellite) — negligible.  NAV-PVT adds zero
cost — we already enable it on the receiver.

**What this would have done last night**: when MadHat's FixedPos
watchdog tripped at 22:03, the consensus check would have:
- Read `bg_PPP_position` ≈ `known_ecef` (background filter unaffected by
  the cycle slip in FixedPos's state)
- Read `F9T_NAV_PVT_position` ≈ `known_ecef` (F9T's onboard fix is its
  own engine, immune to our EKF's state)
- Recognized case 1: both consensus sources agree antenna is fine
- Reset FixedPos from `known_ecef`, kept the engine running on PPS+qErr,
  written `consensus_state=recovering` to the servo CSV for the next
  60–90 s, then resumed Carrier-driven servo when FixedPos converged
- The 8-hour overnight would have completed with maybe 90 seconds of
  PPS+qErr fallback in the middle, instead of dying at 51 minutes

**Reference**: 2026-04-08 ocxo bring-up + MadHat overnight failure;
project memory `project_madhat_ekf_overconfidence`.

## MAD-based outlier rejection

**What**: Reject individual PPS error samples that are statistical
outliers, using Median Absolute Deviation (MAD) rather than a fixed
threshold.

**Why**: The current outlier rejection uses a fixed `track_outlier_ns`
threshold.  A fixed threshold must be set conservatively (large) to
avoid rejecting valid samples during convergence, which means it
misses outliers during steady-state tracking when the error
distribution is tight.  MAD adapts to the actual noise level.

**How**: Maintain a sliding window of the last N PPS error samples
(SatPulse uses N=20).  Compute the median and MAD
(`median(|x_i - median(x)|)`).  Reject any sample where
`|x - median| > K * MAD` (SatPulse uses K=25, with a hard ceiling
at 500 ns).  MAD is robust to the outliers it's trying to detect —
unlike standard deviation, a single wild sample doesn't inflate the
threshold.

**Trade-off**: Adds a 20-sample warmup period where no rejection
occurs.  During convergence the error distribution is non-stationary,
so MAD may over-reject.  SatPulse handles this by using MAD only in
Tracking mode (after convergence), not during Converging.  We could
do the same — apply MAD only after the servo has settled.

**Reference**: SatPulse `time/internal/phcsync/tracking.go`, MAD
window size 20, threshold 25, hard reject 500 ns.


## Median-based convergence detection

**What**: Detect when the servo has finished converging by monitoring
whether the median of |offset| has stopped decreasing, rather than
testing against a fixed sigma threshold.

**Why**: The current bootstrap-to-servo handoff uses the glide slope
to converge smoothly, but the servo has no explicit convergence
detection — it starts PI tracking immediately.  A convergence
detector would allow:
- Reporting convergence time in logs
- Switching from convergence gains to tracking gains
- Enabling MAD outlier rejection only after convergence
- Promoting ptp4l clockClass from "initialized" to "locked"

**How**: Track the running median of |PPS error| in a short sliding
window (SatPulse uses 5 samples).  Maintain `min_median` — the lowest
median seen so far.  When the current median fails to improve on
`min_median` for N consecutive samples (SatPulse uses N=3), AND all
recent samples are within an absolute limit (SatPulse uses 1 µs),
declare convergence.

This is a plateau detector: it doesn't require the error to reach a
specific target, just to stop improving.  This is arguably better than
a fixed sigma threshold because it adapts to the PHC's achievable
accuracy without prior characterization.

**Trade-off**: Minimum convergence time is window_size + N samples
(~8 seconds with SatPulse defaults).  On a TCXO with high short-tau
noise, the median may oscillate and delay convergence detection.
Tune window size and N for the platform's noise profile.

**Reference**: SatPulse `time/internal/phcsync/converging.go`,
window 5, stable count 3, offset limit 1000 ns.


## Holdover with frequency blending

**What**: When PPS or observations disappear, maintain clock accuracy
by holding the last-known frequency with gradual decay toward a
long-term average.

**Why**: Currently we preserve the last adjfine and hope for the best.
SatPulse blends two exponential moving averages (30s short + 300s
long time constants) to get a frequency estimate that's responsive
to recent drift but stable over long gaps.  A proper holdover design
would:
- Degrade clockClass to holdover (not freerun)
- Use the blended frequency estimate
- Set a holdover time limit (default 60s)
- Phase recovery on PPS return (relaxed outlier detection → normal)

**Reference**: SatPulse `plan/phc-holdover.md`, dual-EMA with 30/300s
time constants, 60s max holdover, three-phase recovery.

**Longer term**: Build temperature/frequency curves from TICC data for
temperature-compensated holdover (noted in project memory).


## Clock simulator for servo regression testing

**What**: A discrete-event simulator that models PHC behavior (Allan
variance profile, frequency drift, step latency) for testing servo
algorithms without hardware.

**Why**: Currently all servo testing requires lab hardware.  A
simulator would allow:
- Regression testing of gain changes
- A/B comparison of outlier rejection strategies
- Holdover testing without physically removing the antenna
- CI integration

**Reference**: SatPulse `time/internal/clocksim/` package.


## TOML-based unified configuration

**What**: Replace the current CLI-args-plus-INI mix with structured
TOML configuration for all aspects of peppar-fix operation.

**Why**: Configuration is currently spread across:
- `config/receivers.toml` (PTP profiles, servo gains, platform params)
- `config/ocxo.toml`, `config/timehat.toml` (host-specific peppar settings)
- `ntrip.conf` (INI format, NTRIP credentials)
- CLI arguments (everything else, dozens of `--flags`)
- Environment variables (`PEPPAR_*`)

This is fragile: CLI args have no schema validation, the INI/TOML
split is arbitrary, and there's no single file that fully describes
a deployment.  SatPulse uses a single TOML config with JSON schema
validation covering all aspects (PHC, serial, GPS, servo, PTP, NTP,
logging, HTTP).

**How**: Migrate incrementally:
1. Merge `ntrip.conf` into the host TOML (already has `[peppar]`)
2. Move servo/bootstrap params from CLI-only to TOML with CLI override
3. Add JSON schema for validation (catch typos at startup, not at
   the servo loop)
4. Support layered configs (`-f base.toml -f override.toml`) for
   separating platform defaults from site-specific overrides

The `receivers.toml` pattern already works well.  The host config
files (`ocxo.toml`, `timehat.toml`) are the natural place to
consolidate everything.

**Trade-off**: CLI args remain useful for development and one-off
experiments.  Keep them as overrides, not the primary config path.

**Reference**: SatPulse `configs/satpulse.toml` + `config-schema.json`.


## ADJ_SETOFFSET for PHC stepping

**What**: Use `clock_adjtime(ADJ_SETOFFSET)` instead of
`clock_settime` for the PHC phase step in bootstrap.

**Why**: `ADJ_SETOFFSET` applies a relative offset rather than
setting an absolute time.  Since it's relative, systematic read
latency may cancel — the PHC ticks forward between the kernel's
internal read and write, but the offset is applied correctly
regardless of where the clock happens to be.

The E810 shows bimodal `clock_settime` latency (1.6 ms typical,
16 ms ~30% of calls).  This bimodality might come from the
absolute-time computation path rather than the PHC register write.
If so, `ADJ_SETOFFSET` could have a tighter, unimodal distribution,
dramatically improving step accuracy (potentially ±10 µs instead
of ±2 ms).

**Experiment**: Run optimal stopping with `ADJ_SETOFFSET` on ocxo,
collect the |residual| distribution, compare against `clock_settime`.
Same search budget, same PHC, same host load.

We already have an accurate PPS-measured phase error (`phi_0`),
which is exactly the relative offset `ADJ_SETOFFSET` wants.
