# Servo Improvement Candidates

Ideas for servo quality improvements, drawn from SatPulse comparison
and operational experience.  These are independent of each other and
can be adopted incrementally.

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
