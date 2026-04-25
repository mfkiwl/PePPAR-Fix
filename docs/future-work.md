# Future Work

Improvement candidates drawn from SatPulse comparison and operational
experience.  These are independent of each other and can be adopted
incrementally.

> **Architectural vision**: see `docs/architecture-vision.md` for the
> unified naming (AntPosEst / DOFreqEst), state machines, measurement
> fusion, bootstrap-as-seed-verification, and wrapper dissolution plan.
> Many of the entries below are stepping stones toward that vision.

## Cross-AC SSR diagnostic — engine flags + signal-map work

**See `docs/ssr-cross-ac-diagnostic-2026-04-25.md` for the full
investigation.**  As of 2026-04-25 our PPP+CNES SSR settles 6–9 m
west of Leica truth on UFO1 — *worse* than the bare F9T NAV2
autonomous fix.  CAS attempt to cross-check failed because the
engine's IGS-SSR signal map is missing Galileo `sig_id=2`, and CAS's
proprietary `4076_NNN` message IDs expose at least one more
compatibility gap (orbit/clock IOD matching) — CAS test diverged 280
m inter-host on a 30 m seed.

Three discrete pieces of engine work to enable cleaner diagnostics:

A. **`--no-primary-biases`** flag (~10 lines) — drop biases from the
   primary SSR mount while keeping its orbit/clock.  Required for the
   clean 4-cell 2x2 (CNES/CAS × orbit-clock/biases).
B. **IGS-SSR signal-map fix** for CAS / MADOCA — adds the missing
   bias-map entries under the IGS-SSR encoding.  Independent of (A).
C. **IOD-matching diagnostics** — log SVs whose orbit/clock SSR
   couldn't be matched to broadcast IODs, so we can spot silent
   fallback to broadcast-only orbits when SSR routing is broken.

If the 2x2 narrows the bias to "biases" but doesn't separate code-
from phase-bias, add `--no-ssr-code-bias` / `--no-ssr-phase-bias` —
~10 more lines in the bias router.

## Three-source position sanity + self-healing FixedPosFilter

> **Status 2026-04-22**: Most of this section *landed* as part of the
> architecture-vision work.  What remains is the explicit three-way
> vote at the watchdog (today it's 2-way: `known_ecef` vs NAV2), the
> Case-4 "bg-PPP itself is corrupted" recovery path, and a new idea —
> **cross-host ensemble NAV2** — that came up 2026-04-22 and isn't
> part of the original three-source design.  The "consensus"
> framing below has also been revised: the live PPP-AR solution is
> *the* best position estimate, not a peer-on-equal-footing with
> NAV2 or `known_ecef`.  NAV2 and `known_ecef` are **sanity checks /
> trip-wires**, not consensus inputs.  Over 12 h, the live solution
> is physically more accurate than either watchdog input (solid
> earth tides vertical motion of ±10–15 cm per semi-diurnal cycle
> at mid-latitudes is tracked by PPP phase measurements but
> averaged out of `known_ecef` by the exponential blend and
> unresolved by NAV2's coarse single-receiver filter).
>
> **Landed**:
> - Phase 1 PPPFilter kept alive past bootstrap as
>   `AntPosEstThread` (engine.py:1263) with its own PPPFilter
>   instance seeded from `known_ecef` (engine.py:1311).
> - NAV2 engine enabled, NAV2-PVT parsed, `Nav2PositionStore`
>   with `get_opinion()` returning ECEF + LLA + hAcc/vAcc + pDOP.
> - FixedPosFilter watchdog consults NAV2 before declaring
>   "antenna moved" — re-seeds from `known_ecef` if NAV2 agrees.
> - `known_ecef` is refined by AR via exponential blend
>   (α = 0.001, τ ≈ 1000 epochs) so transient wrong fixes only
>   shift it by ~3 mm/epoch.  **Warning**: 100 epochs of wrong
>   fix = 30 cm = 1 ns of timing error; the blend slows but
>   doesn't prevent contamination.  This is why the
>   CONVERGING → ANCHORING → ANCHORED state-transition gates
>   matter — catching wrong integers *before* commit is
>   cheaper than catching them after `known_ecef` absorbs the
>   damage.
> - Raw NAV2 position logged every 10 epochs alongside
>   AntPosEst (2026-04-22) — `[NAV2 N] lat=... lon=... alt=...
>   hAcc=... vAcc=... pDOP=... fix=... sv=... age=...`
>   — enables cross-host NAV2 analysis and future ensemble work.
>
> **Not yet**:
> - Explicit three-way comparison at the watchdog (AntPosEst
>   position vs NAV2 vs known_ecef, rather than just NAV2 vs
>   known_ecef).
> - Case 4: reset bg-PPP (AntPosEst) state when bg-PPP itself
>   is corrupted without touching FixedPos.
> - Cross-host ensemble NAV2 as an independent sanity reference
>   — shared antenna, three F9Ts, the ensemble-average NAV2 has
>   ~√3 less random noise than any single host's NAV2.
>
> Everything below is the original text, preserved for context
> but no longer prescriptive.  Rewrite pending.

**What** *(original)*: When the engine is in Phase 2 (FixedPosFilter,
position locked), maintain *three independent* estimates of the
antenna's position and use majority consensus to distinguish
"antenna physically moved" from "internal filter state corrupted".
When the FixedPos EKF blows up, reset it from the consensus position
and coast on PPS+qErr until it reconverges, instead of the current
behavior of exiting the engine and waiting for a manual restart.

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

1. **Live PPP background monitor** — implemented as **the same
   `PPPFilter` instance currently destroyed at the end of Phase 1**,
   simply kept alive and fed *decimated* observations during Phase 2
   from a background thread.  No new filter class is needed.  The
   existing PPPFilter is already proven (it's what bootstraps the
   position in the first place); all we need is:

   - In `run_steady_state`, *don't* `del filt` after Phase 1 — pass
     the converged PPPFilter into a `BackgroundPPPMonitor` thread.
   - The thread holds the filter, pulls one observation epoch every
     N (default 10–30) from a tee of the observation queue, calls
     `filt.predict()` + `filt.update()`, and exposes the latest
     position estimate via a thread-safe slot.
   - The main thread continues with FixedPosFilter exactly as today.

   The cost is one additional consumer on the observation queue and
   ~6% of one core for the PPPFilter step at 30 s cadence.  Memory is
   the existing PPPFilter state vector — no growth.

2. **F9T's secondary navigation engine** — the third opinion, and
   **purpose-built by u-blox for exactly this use case**.

   The F9T contains *two complete and independent navigation engines*.
   The primary engine is what we already use: it can run in
   position-fix, survey-in, fixed (TIME), or any of the standard
   modes, and emits messages on UBX class 0x01 (`NAV-*`).  The
   secondary engine is **always position-only** and runs continuously
   regardless of what mode the primary is in.  It emits messages on
   the parallel UBX class 0x29 (`NAV2-*`), with the same family of
   message IDs (`NAV2-PVT`, `NAV2-POSECEF`, `NAV2-POSLLH`,
   `NAV2-VELECEF`, etc.).

   The whole point of NAV2 is what the user originally asked for:
   when the primary is in TIME mode and forced to a fixed position,
   the secondary is *still computing a fresh fix* every epoch.  So
   you get your tight TIM-TP qErr (which depends on the primary's
   fixed-position timing-mode behavior) AND a continuously-fresh
   position estimate from a completely separate processing chain.
   It's the cleanest possible disambiguation: same antenna, same RF
   front end, same observations, but a different filter/engine.
   If the primary's fixed-position assumption ever becomes wrong
   (i.e. the antenna moved), NAV2 will see it within seconds.

   **Status of NAV2 in our config**:
   - pyubx2 fully supports the NAV2 family — verified on TimeHat:
     `NAV2-PVT`, `NAV2-POSECEF`, `NAV2-CLOCK`, `NAV2-DOP`, `NAV2-SAT`,
     `NAV2-SIG`, `NAV2-STATUS`, etc. all decode out of the box.
   - We do NOT currently enable the secondary engine.  The receiver
     config (`scripts/peppar_fix/receiver.py:99`) lists `NAV-PVT` as
     required but says nothing about NAV2.  By default the F9T ships
     with NAV2 *disabled* — it has its own enable key
     `CFG-NAV2-OUT_ENABLED` that turns the second engine on, after
     which individual `CFG-MSGOUT-UBX_NAV2_*` keys control message
     output.  Enabling NAV2 increases the F9T's CPU load slightly
     and adds a few hundred bytes/s to the UBX output stream — both
     well within budget.
   - We also do NOT parse NAV2-PVT in `realtime_ppp.py`, but the
     handler is the same shape as the existing NAV-PVT case — the
     parsed message has `lat`, `lon`, `height`, `hMSL`, `hAcc`,
     `vAcc`, `numSV`, `fixType`, etc.  Just stash it in an
     `F9TSecondaryPositionStore` (analogous to `QErrStore`) and the
     consensus monitor reads from it.

   Plumbing required (small):
   - Add `CFG-NAV2-OUT_ENABLED = 1` to the receiver config.
   - Add `CFG-MSGOUT-UBX_NAV2_PVT_<port> = 1` to enable the message.
   - Add `NAV2-PVT` to the message dispatcher in `serial_reader`.
   - New `F9TSecondaryPositionStore` class with `update()` and `get()`.

3. **The original `known_ecef`** — the position the engine bootstrapped
   to (or that came from `known_pos`/`position.json`).  This is the
   "ground truth as of the last bootstrap" that the FixedPosFilter
   trusts implicitly.

**Why this combination is robust**: the three sources have *completely
independent failure modes*.  Cycle slips that poison FixedPosFilter
won't affect the F9T secondary engine (different filter, possibly
different ambiguity resolution).  An NTRIP outage won't affect the
F9T secondary (it doesn't use SSR).  An F9T firmware glitch won't
affect our background PPPFilter (different code, different state).
And `known_ecef` is a fixed reference that doesn't change unless we
deliberately update it after a real antenna move.  Any single source
going wrong is caught by 2-of-3 vote against it.

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

- `run_bootstrap` already returns the converged PPPFilter; today it's
  destroyed.  Pass it through to `run_steady_state` instead.
- New `BackgroundPPPMonitor` thread in `peppar_fix_engine.py` that
  owns the inherited PPPFilter and steps it on a slow cadence.
- New `F9TSecondaryPositionStore` (analogous to `QErrStore`) in
  `realtime_ppp.py`.
- `serial_reader` adds a `NAV2-PVT` case in the message dispatcher
  and writes to `F9TSecondaryPositionStore`.
- Receiver config gains two new keys: `CFG-NAV2-OUT_ENABLED=1` and
  `CFG-MSGOUT-UBX_NAV2_PVT_<port>=1`.
- `FixedPosFilter` watchdog handler reads both consensus sources
  before deciding what to do.
- New CLI flag `--bg-ppp-cadence-s` (default: 30 s).
- New servo CSV column: `consensus_state` (one of `agree`, `f9t_only`,
  `ppp_only`, `disagreement`, `recovering`).

**CPU/memory cost**: PPP filter on a Pi 5 takes ~2 s of CPU per epoch
at full rate.  Subsampled to every 30 s, the background monitor
consumes ~6% of one core.  Memory is the existing PPPFilter state
vector — already in memory during Phase 1, just kept around past
the Phase 1 → Phase 2 transition instead of garbage-collected.
NAV2 enable adds a few hundred bytes/s on the UBX serial stream and
a small CPU bump on the F9T — both negligible.

**What this would have done last night**: when MadHat's FixedPos
watchdog tripped at 22:03, the consensus check would have:
- Read `bg_PPP_position` ≈ `known_ecef` (background PPPFilter is a
  different state from FixedPosFilter and was not corrupted by the
  cycle slip event)
- Read `F9T_NAV2_PVT_position` ≈ `known_ecef` (F9T's *secondary*
  navigation engine is a completely separate filter inside the F9T,
  immune to anything happening in our PPP code or in the F9T's
  primary timing-mode engine)
- Recognized case 1: both consensus sources agree antenna is fine
- Reset FixedPos from `known_ecef`, kept the engine running on PPS+qErr,
  written `consensus_state=recovering` to the servo CSV for the next
  60–90 s, then resumed Carrier-driven servo when FixedPos converged
- The 8-hour overnight would have completed with maybe 90 seconds of
  PPS+qErr fallback in the middle, instead of dying at 51 minutes

**Relationship to PPP-AR**: the background PPPFilter that serves as
the consensus watchdog is the *same* filter that PPP-AR extends.  Once
phase biases are available from a single-AC SSR source, the background
filter's float ambiguities cluster near integers, and the bootstrapping
AR module fixes them.  The AR-fixed position (cm-level) feeds gradually
back into `known_ecef` via exponential blending, removing the
decimeter-level phase bias that the FixedPosFilter would otherwise
carry.  See `docs/ppp-ar-design.md` "AR module: unified architecture
with background PPPFilter" for the full design including the blend
math and the interaction with the consensus truth table.

The NAV2-PVT secondary engine also serves as a safety net for the AR
fix itself: if the bootstrapping AR produces a wrong integer fix, the
position will jump, and the NAV2 consensus check will disagree —
allowing us to reject the bad fix before it migrates into `known_ecef`.

**Reference**: 2026-04-08 ocxo bring-up + MadHat overnight failure;
project memory `project_madhat_ekf_overconfidence`.

## Post-fix residual monitoring for wrong-integer detection

**What**: After NL ambiguities are fixed, continuously monitor the
pseudorange and carrier-phase residuals.  Wrong integers produce
residuals that grow as satellite geometry changes — correct integers
produce stable residuals regardless of geometry.

**Why**: The current AR validation gates (LAMBDA ratio test, bootstrap
P > 0.999, float-vs-fixed displacement check) prevent most wrong
integers, but a gap remains in the 2–10 m range.  The NAV2 sanity
check now uses a 10 m threshold for AR-fixed positions (commit
54d4cb2, 2026-04-16) — anything below 10 m is trusted.  A wrong
integer that shifts the position by 3–8 m would persist undetected
until the satellite sets and the ambiguity drops.

**How**: After each NL fix epoch, compute the post-fit pseudorange
residuals for all fixed satellites.  Track the RMS and per-satellite
residuals over a sliding window (e.g., 60 epochs).  If a fixed
satellite's residual grows monotonically (indicating the integer is
pulling against the geometry), unfix that satellite and re-attempt.

A simpler first step: compare the post-fix pseudorange RMS against
the float RMS.  A correct fix should reduce or maintain the RMS.
If the RMS increases after fixing, the integers are suspect.

**Discovered**: 2026-04-16 while investigating TimeHat AR loss.
The loss turned out to be NAV2 resets (not wrong integers), but the
investigation revealed the integrity gap for wrong integers in the
2–10 m range.

## Weighted position-fix strength metric

**What**: Extend the unweighted strength metric (`WL_fixed / σ_3d_m`,
see `docs/position-strength-metric.md`) to weight each SV's
contribution by its elevation, signal band, fix age, and susceptibility
to correlated failure modes.  Current first-cut treats every fixed WL
SV identically in the numerator; the weighted form would encode the
physical reality that not all fixed WL SVs contribute equally to
resistance against biased-integer pull.

**Why**: The overnight 2026-04-23 run showed hosts with **fewer** SVs
(TimeHat F9T-10, ptpmon) weathering pre-dawn TEC conditions better than
hosts with **more** SVs (clkPoC3, MadHat, F9T-20B's).  Per-SV
resistance `1/N_eff` scales with N, but per-SV failure probability
isn't uniform and correlated failures evaporate the sqrt-N
protection.  A weighted metric would reflect that a high-elev L1/L2
SV contributes more strength during a TEC storm than a low-elev L5
SV on the same receiver.

**How**: Replace the count in the numerator with `Σ_i w_i` where
`w_i` encodes:

- **Elevation**: monotone-increasing weight up to ~60°, flat above
  (e.g., `sin²(elev)` or RTKLIB-style elevation weighting).
- **Signal band / correlation cohort**: during known TEC-prone
  periods or flagged storm events, down-weight L5-band fixes; during
  multipath sweeps, down-weight low-elev fixes; etc.  May need a
  dynamic layer that responds to current fleet-level evidence of
  stress.
- **Fix age / MW averaging confidence**: SVs with longer post-fix
  averaging windows and tighter residual std get more weight.
- **SSR correction age / source reliability**: fixes tied to fresh,
  matched phase biases weight higher than those relying on stale or
  fallback-inferred biases.

**Dependencies**: first accumulate a week of runtime data with the
unweighted metric logged, to see which structural features correlate
most strongly with trap-susceptibility in our actual fleet.  The
weight axes chosen from that data will beat any a-priori design.

**Discovered**: 2026-04-23 discussion of the overnight WL-only run
(the ironic "fewer SVs, better survival" finding).

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


## Seed DOFreqEst x[2] with bootstrap phi_0

**What**: Pass the measured initial phase error (`phi_0`) directly
into DOFreqEst's initial state `x[2]` instead of letting the EKF
discover it from the first TICC measurement at 5% per epoch.

**Why**: After bootstrap, there's a known phase offset — ~2 µs of
cable delay + TADD alignment on clkPoC3, or the post-step residual
on PHC hosts.  The DOFreqEst's `_need_phc_seed` mechanism currently
waits for the first servo-epoch TICC measurement to set `x[2]`,
then the LQR's `L[2]=0.05` term converges the offset at 5%/epoch.
This works but takes ~200 epochs (~3 min) to settle.  Seeding
`x[2] = phi_0` from bootstrap gives the LQR full knowledge from
epoch 1 — it can compute the optimal frequency trajectory
immediately instead of rediscovering what the bootstrap already
measured.

**How**:
- PHC hosts: `phi_0` is already measured by EXTTS after the phase
  step (line ~2093 in `_do_bootstrap_phc`).  Pass it through to the
  DOFreqEst constructor.
- TICC-only hosts: `phi_0` = first TICC differential after TADD ARM
  (the `ticc_target_auto` value).  Measure it in bootstrap, before
  `_setup_servo`, and pass it through.
- DOFreqEst constructor: add `initial_phase_ns` parameter, set
  `x[2] = initial_phase_ns` and skip `_need_phc_seed`.

**Expected improvement**: Convergence from ~200 epochs to ~10 epochs.
The LQR already knows the optimal response — it just needs the
initial condition.


## In-band DO noise estimation from discipline gaps

> **Status 2026-04-22**: LANDED.  `scripts/peppar_fix/noise_estimator.py`
> implements the in-band noise estimator; used in the engine's
> servo path.  Verify the memory
> `project_session_handoff_20260414d` for landing date.  Section
> retained for design context only.

**What**: Continuously estimate the DO's noise floor (ADEV, TDEV,
dominant noise type) from free-running samples that occur naturally
during the adaptive discipline interval, instead of requiring a
dedicated 30-minute freerun characterization.

**Why**: The current `--freerun` characterization is a one-time
snapshot at one temperature.  It goes stale as conditions change.
But during normal servo operation, the adaptive discipline interval
creates natural measurement windows: when adjfine is held constant
for 10 seconds between corrections, epochs 2–9 are genuinely
free-running.  After removing the known linear drift (constant
adjfine × dt), the residual is pure DO phase noise.

**How**: An `InBandNoiseEstimator` component inside DOFreqEst:
1. Watches for adjfine-write events.
2. Accumulates phase samples from discipline gaps (skipping the
   first epoch after each write to avoid the transient).
3. Computes running ADEV/TDEV at τ = 1, 2, 4, ... seconds.
4. Exposes `current_adev(tau)` for the servo gain scheduler.
5. Continuously updates as temperature changes, oscillator ages, etc.

**Benefits over one-time freerun**:
- Continuously current (no stale characterization)
- No lost operational time
- Better statistics (thousands of gaps per overnight)
- Temperature correlation possible (if board temp is logged)

**Limitations**: Can't measure ADEV at τ longer than the maximum
discipline interval.  The one-time freerun still seeds the noise
model for the very first run; the in-band estimator verifies and
refines it.  Over time the initial characterization window can
shrink from 30 minutes to 5 minutes as the in-band estimator
matures.

**Reference**: `docs/architecture-vision.md` "In-band DO noise
estimation"; `docs/asd-psd-servo-tuning.md` and
`docs/freerun-characterization.md` for the current approach.


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

> **Status 2026-04-22**: LANDED.  Memory
> `project_adj_setoffset_experiment` confirms the experiment
> succeeded — E810 residual dropped from −87 µs to ~0 ns with
> `adj_setoffset(-phase_error_ns)` directly.  Section retained
> for the experimental record.

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


## Reference Oscillator (RO) characterization

> **2026-04-24**: the position-filter context for RO work lives
> in `docs/clock-state-modeling.md`.  This section focuses on
> the RO's operational role (TICC-side accounting, spoofing
> detection, single-channel calibration).  The companion
> discussion of how RO state flows into the PPP filter's clock
> dynamics is in the new doc.

**What**: Treat the oscillator driving each TICC's 10 MHz reference
input as a first-class entity — the **Reference Oscillator** (RO) —
with its own characterization, bootstrap, runtime tracking, and
clean-shutdown state save.  Parallel to how we already treat the
**Disciplined Oscillator** (DO).

**The observations, written out**: All TICC timestamps are in RO
timescale.  With GPS as our truth reference (f_gps ≡ 0 by definition),

```
chA slope (DO PPS vs GPS in TICC time)  = f_do − f_ro
chB slope (F9T PPS vs GPS in TICC time) = f_gps − f_ro = −f_ro
```

The **differential** chA − chB = f_do removes f_ro algebraically, so
relative TDEV between two clocks on the same TICC only needs the RO
to be *stable*, not to be at a known frequency.  But for any
single-channel work — chA-only TDEV of a DO against truth, or chB
alone to monitor F9T PPS — the RO's actual offset from GPS matters.

**Why it matters**:

- Today our TICCs are driven by a Geppetto GPSDO OCXO, so f_ro ≈ 0
  within any reasonable window.  Next generation may use inexpensive
  OCXOs with meaningful nominal offset, temperature drift, ageing,
  and ASD/PSD noise.  All slow-moving; tracking them lets us:
  1. Hand every consumer of TICC timestamps a *calibrated*
     measurement (`chA − known_RO_ppb·τ` gives true DO phase, not
     DO phase contaminated by RO drift).
  2. **Detect F9T spoofing**: an unexplained offset between chB
     and our known-stable RO frequency flags F9T PPS manipulation —
     the RO is the trusted anchor, the F9T is what's being tested.
  3. Sanity-check RO replacement or ageing: a TICC that's always
     been driven by a 30 ppb OCXO with 1 ppb/year drift that
     suddenly reads 50 ppb offset needs investigation.

**Storage**: extend `state/timestampers/<unique_id>.json` (already
designed for noise parameters) with an `ro` block parallel to the
receiver's `tcxo` block:

```json
"reference_oscillator": {
  "label": "Geppetto GPSDO",
  "type": "gpsdo-ocxo",
  "nominal_offset_ppb": 0.0,
  "last_known_offset_ppb": -0.05,
  "last_known_adev_1s": 1e-13,
  "tempco_ppb_per_C": null,
  "ageing_ppb_per_day": null,
  "samples": 86400,
  "updated": "2026-04-17T12:00:00Z",
  "method": "chB vs F9T PPS over 24 h"
}
```

**Lifecycle** (mirrors DO exactly):

1. **Bootstrap**: load last-known RO offset.  Measure chB−nominal
   over the first N epochs, compare against stored offset.  If
   within `freq_tolerance_ppb`, bless; otherwise log a warning
   and keep using the measured value.
2. **Runtime tracking**: an `ROFreqEst` (light EKF or just running
   mean + variance) accumulates offset estimates epoch-by-epoch,
   writes periodic snapshots to state.
3. **Clean shutdown**: save the latest estimate and sample count.

**Shared code with DO**: the only fundamental difference is that
the DO is steerable and the RO is observable-only.  Filter math,
bootstrap sanity check, state save/load, conventions
(`last_known_freq_offset_ppb`, `updated`) are identical.  Concretely,
both `DOFreqEst` and any future `RxTcxoEst` should accept an
optional `ro_freq_ppb` parameter (or read it from the timestamper
state file); every TICC-timed measurement has that known offset
subtracted before the innovation step.  That's the useful code
sharing, and it neatly enforces the "don't silently conflate
oscillators" invariant.

**Does this help DOFreqEst reject a wrong-ridge lock?**  Yes,
*structurally* — this is more than a cross-check.

A ridge exists in DOFreqEst because every TICC chA observation is
of the form `f_do − f_ro`; filter states `f_do` and `f_ro` are
only constrained jointly.  Priors on f_ro keep the covariance
non-singular, but the gradient of information perpendicular to the
ridge is zero.  Small biases (or numerical drift) slide the state
along the ridge to a self-consistent but wrong point, and the
filter reports tight σ because any point on the ridge *is*
self-consistent.  This is exactly analogous to the ISB/clock
degeneracy that hit the position-side PPPFilter 2026-04-16, fixed
by pinning the ISB when its reference system is absent (see
`PPPFilter.initialize(systems=…)` in `scripts/solve_ppp.py`).

Two ways to use the known RO in the DO filter, with different
strengths:

1. **Pin (breaks the ridge structurally)**.  Inject a pseudo-
   measurement `f_ro = stored_value ± σ_ro` with small σ — or,
   equivalently, treat `f_ro` as a known parameter rather than a
   state, shrinking the filter by one dimension.  The degenerate
   ridge collapses and `f_do` becomes observable on its own.
   This is the primary value of RO knowledge.
2. **Cross-check (doesn't change filter math)**.  After the filter
   reports its state, verify that `filter's implied f_do`
   ≈ `(chA − chB slope) + stored f_ro`.  Disagreement flags a
   biased fit even when the ridge has been pinned.  This implements
   the "filter σ ≠ correctness" invariant in
   `docs/architecture-vision.md`.

**Does this help a future rx_tcxo filter reject wrong-ridge locks?**
*Less directly.*  The primary input to rx_tcxo estimation is PPP's
`dt_rx`, which is GPS-referenced and doesn't involve the RO at all
— no RO-shaped ridge to break.  But if the rx_tcxo filter also
consumes chB slope (as a short-tau frequency cross-check), that
measurement is `(f_rxtcxo_after_F9T_discipline − f_ro)`.  Without
knowing f_ro, we'd silently conflate RO and rx_tcxo drift.  With
f_ro known, chB gives an independent, clean estimate of f_rxtcxo
— a legitimate cross-check rather than a contaminated one.  Same
decontamination applies to using chB as a sanity check on F9T
spoofing, or on rx_tcxo temperature sensitivity.

**Implementation order**:

1. Add `reference_oscillator` block to timestampers schema
   (`docs/state-persistence-design.md` + the TICC writer).
2. Add an initial frequency measurement to bootstrap (chB
   interval trend over N epochs, report ppb).
3. Trust-but-verify: if a stored offset exists and the new
   measurement disagrees by more than `freq_tolerance_ppb`, log
   both and use the new one (don't block bootstrap).
4. Runtime tracker: maintain running mean + variance, snapshot
   state periodically (not every epoch).
5. Clean-shutdown save — matches the pattern already in place for
   DO state.
6. Later: characterize the inexpensive-OCXO variants against GPS
   over days/weeks, populate `tempco_ppb_per_C` and
   `ageing_ppb_per_day`, feed those into the consistency check.
