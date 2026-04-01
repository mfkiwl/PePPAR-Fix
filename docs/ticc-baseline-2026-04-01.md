# TICC Baseline Characterization — 2026-04-01

Overnight captures on TimeHat TICC #1 measuring both the raw F9T PPS
(chB) and the free-running i226 PHC PEROUT (chA) at 60 ps resolution.

## Test setup

- Host: TimeHat (Raspberry Pi 5 + TimeHAT v5)
- TICC: #1 at `/dev/ticc1` (serial 95037323535351803130)
- chA: i226 PHC PEROUT (SDP0) — free-running at adjfine ≈ 101 ppb
- chB: F9T PPS (raw, from F9T-3RD on `/dev/gnss-top`)
- Both signals split from the same antenna/splitter chain
- PHC not being disciplined (post-freerun, adjfine held at bootstrap base)

## Runs

| Run | Duration | chA events | chB events | File |
|-----|----------|-----------|-----------|------|
| 30m #1 | 1800s | 1802 | 1802 | `ticc-baseline-30m-1.csv` |
| 30m #2 | 1800s | 1800 | 1800 | `ticc-baseline-30m-2.csv` |
| 2h #1 | 7200s | 7200 | 7199 | `ticc-baseline-2h-1.csv` |
| 2h #2 | 7200s | 7199 | 7200 | `ticc-baseline-2h-2.csv` |

Zero dropped events on all runs.

## F9T PPS baseline (chB)

The F9T PPS train exhibits a sawtooth phase modulation driven by the
receiver's internal TCXO beating against the GNSS clocks.  The sawtooth
has "smooth ramp" and "jumpy" periods of varying duration (typically
minutes), modulated by temperature.

### TDEV(1s) depends on observation length

| Duration | TDEV(1s) |
|----------|----------|
| 30 min #1 | 1.02 ns |
| 30 min #2 | 1.39 ns |
| 2 hour #1 | 2.23 ns |
| 2 hour #2 | 2.41 ns |

30-minute runs may catch mostly smooth or mostly jumpy periods,
yielding TDEV(1s) anywhere from 1.0–1.4 ns.  2-hour runs span enough
cycles to converge: **F9T PPS TDEV(1s) = 2.3 ±0.1 ns** (2-hour
baseline, 8% spread).

This is a property of all F9T PPS trains (the sawtooth comes from the
receiver's clock architecture, not the antenna or host).  Any EXTTS
measurement of the same PPS that reports TDEV(1s) < 2.3 ns is
underreporting due to PHC timestamp quantization.

### F9T PPS TDEV at longer tau

TDEV drops steeply — the sawtooth averages out.

| tau | TDEV (2h avg) |
|-----|--------------|
| 1s | 2.3 ns |
| 2s | 1.2 ns |
| 5s | 0.45 ns |
| 10s | 0.23 ns |
| 30s | 0.075 ns |
| 60s | 0.038 ns |
| 100s | 0.024 ns |
| 300s | 0.009 ns |
| 1000s | 0.003 ns |

By tau=10s, TDEV is sub-250 ps.  This confirms that the F9T PPS is an
excellent long-term reference — the sawtooth is a purely short-tau
phenomenon.

## i226 PHC PEROUT stability (chA)

The PHC PEROUT reflects the free-running i226 TCXO, steered to a fixed
adjfine (~101 ppb) by bootstrap but not actively disciplined.

### TDEV(1s) is constant across all runs

| Run | TDEV(1s) |
|-----|----------|
| 30m #1 | 1.169 ns |
| 30m #2 | 1.173 ns |
| 2h #1 | 1.171 ns |
| 2h #2 | 1.168 ns |

**1.170 ±0.002 ns** — 0.2% spread.  The TCXO's short-term jitter is
highly reproducible and is dominated by a stationary noise process
(not the F9T sawtooth modulation).

### TDEV at longer tau

| tau | TDEV (2h avg) | Notes |
|-----|--------------|-------|
| 1s | 1.17 ns | TCXO short-term jitter |
| 2s | 0.46 ns | |
| 5s | 0.25 ns | |
| 10s | 0.25 ns | Flattens — noise type transition |
| 30s | 0.19 ns | |
| 60s | 0.14 ns | |
| 100s | 0.12 ns | |
| 300s | 0.085 ns | |
| 600s | 0.061 ns | |
| 1000s | 0.052 ns | |
| 2000s | 0.074 ns | Uptick — temperature drift emerging |

The TCXO TDEV is below the F9T PPS TDEV at all taus from 1s through
~5s.  This means:

- At tau=1s, the PHC PEROUT (1.17 ns) is quieter than the PPS it's
  being disciplined to (2.3 ns).  The servo cannot improve the PHC at
  short tau — it can only make it worse.
- The crossover where PPS becomes better than the free-running TCXO is
  around tau=5s (both ~0.25 ns).  Below this tau, the servo should not
  be steering.

### Drift rate

The TCXO drifts at -2.6 to -3.2 ppb relative to the TICC reference
(which is clocked by the F9T's internal oscillator).  This is the
residual adjfine error: bootstrap set 101 ppb but temperature changed
overnight.

## EXTTS resolution analysis

### The F9T 125 MHz clock sets the test signal range

The F9T generates PPS at the nearest tick of its 125 MHz internal
clock (8 ns period).  As the F9T's TCXO beats against the GNSS clocks,
the PPS edge visits different positions within this 8 ns range.  The
TICC confirms this — the full sawtooth has ~2.3 ns TDEV(1s) and sweeps
across the 8 ns tick grid over the beat period.

This means any timestamper observing the F9T PPS will see the edge
move over an 8 ns range.  The question is whether a given EXTTS path
can resolve that movement.

### E810 EXTTS: 77% identical timestamps → ~8 ns effective resolution

The E810 EXTTS reported TDEV(1s) = 0.34 ns for a signal with 2.3 ns
true jitter.  77% of adjacent timestamps were identical.  If the E810
truly had 1 ns resolution, the probability of identical adjacent
timestamps would be ~12% (for a Gaussian with σ_diff ≈ 3.8 ns in 1 ns
bins).  Working backward from P(same) = 0.77:

    P(same bin) = 0.77, σ_diff = 3.8 ns → effective bin width ≈ 9 ns

The E810 EXTTS for GPIO/SMA events has **~8 ns effective resolution**,
despite reporting 1 ns in the timestamp format.  The sub-ns capability
advertised for the E810 applies to the MAC's packet timestamp path
(which uses the 812.5 MHz PLL), not the external pin capture path.

The E810 EXTTS adds very little noise within each bin (timestamps are
clean) but cannot resolve changes smaller than ~8 ns.  Most of the
F9T sawtooth is invisible to it.

### i226 EXTTS: noisy but resolving

The i226 EXTTS reported TDEV(1s) = 1.99 ns with 0% identical adjacent
timestamps.  Every pair differs.  The i226's 125 MHz clock gives 8 ns
tick quantization, and its capture path adds ~1.7 ns RSS noise on top.
This noise is a nuisance, but it means the i226 is always reporting
a different value — it tracks the PPS movement, albeit noisily.

The i226's dithering noise may actually provide better effective
resolution than the E810's clean-but-flat timestamps when averaged
over multiple epochs.  Stochastic resonance: noise that spans the
quantization boundary lets averaging interpolate between bins.

### Both platforms: ~8 ns EXTTS resolution

Despite very different noise characteristics, both the i226 and E810
have similar EXTTS resolution (~8 ns, the 125 MHz period).  They
differ in how they behave within that quantization:

| Property | i226 | E810 |
|----------|------|------|
| EXTTS resolution | ~8 ns | ~8 ns |
| Within-bin noise | ~1.7 ns (noisy) | <1 ns (flat) |
| Adjacent identical | 0% | 77% |
| Tracks PPS movement | Yes (noisily) | Rarely (bin crossings only) |
| Averaging benefit | Yes (noise dithers bins) | Limited (flat within bins) |

## Timestamper comparison: EXTTS vs TICC

The PPS edge can be timestamped by EXTTS (8 ns bins) or by TICC
(60 ps resolution).  These are independent measurement paths with
different resolution, noise, and correlation requirements.

### TICC advantage

The TICC resolves the full F9T sawtooth at 60 ps.  With qErr
correction (which removes the ~2.3 ns sawtooth), TICC+qErr should
approach the TICC's own noise floor.  This is far below any EXTTS
path.

qErr's value is **independent of the timestamper**.  qErr tells you
where the true GNSS second falls within the F9T's 8 ns tick.  Whether
the PPS is timestamped by EXTTS or TICC, subtracting the sawtooth
removes the same ~2.3 ns of jitter.  The improvement is:

| Timestamper | Raw TDEV(1s) | With qErr | Improvement |
|-------------|-------------|-----------|-------------|
| TICC (60 ps) | 2.3 ns | ~60 ps floor | ~38x potential |
| i226 EXTTS | 1.99 ns | 1.72 ns | 1.4x |
| E810 EXTTS | 0.34 ns (flat) | worse | n/a (already below qErr) |

The EXTTS+qErr improvement on i226 is modest (1.4x) because the 8 ns
quantization noise dominates.  TICC+qErr removes the sawtooth entirely,
limited only by the TICC's own 60 ps single-shot noise.

### Correlation challenges differ

Each timestamper has different correlation requirements:

- **EXTTS**: captured in PHC time, correlated with GNSS observations
  via the strict correlation gate (monotonic time matching, confidence
  scoring).  Well-understood path.
- **TICC**: captured in TICC time (arbitrary epoch), must be mapped to
  host monotonic time via the `TimebaseRelationEstimator`, then
  correlated with GNSS observations.  Additional uncertainty from the
  serial transport delay (~1 ms jitter).

Both paths must be correlated with TIM-TP for qErr.  The TIM-TP
correlation was fixed in commit c15d3b4 (monotonic matching at 0.9s
offset).

## Implications for peppar-fix

### Servo bandwidth

The free-running TCXO at 1.17 ns TDEV(1s) is quieter than the raw PPS
it's being disciplined to (2.3 ns).  At tau=1s, the servo cannot
improve the PHC — it can only inject PPS noise.  The crossover where
PPS correction becomes beneficial is around tau=5s.

The current gain schedule (Kp=0.03, Ki=0.001) is appropriate: it
converges at tau ~10-30s, above the crossover point.

### qErr on EXTTS vs TICC

On the EXTTS path, qErr provides 1.4x improvement at tau=1s on i226.
This is modest because the 8 ns quantization noise dominates over the
~2.3 ns sawtooth being removed.

On the TICC path, qErr should provide dramatically larger improvement
because the TICC resolves the full sawtooth.  TICC-driven servo with
qErr correction is the path to sub-nanosecond discipline — the
combination removes the F9T's 2.3 ns sawtooth while preserving the
TICC's 60 ps measurement resolution.

### Platform choice

For EXTTS-only operation (no TICC), the i226 and E810 are roughly
equivalent in EXTTS resolution (~8 ns).  The i226's noisy-but-resolving
timestamps may be slightly better for averaging.  The E810's advantage
is its OCXO (much better holdover and long-tau stability), not its
EXTTS resolution.

For TICC-driven servo, the platform choice is less important — the
TICC bypasses EXTTS entirely.  The PHC still matters for PEROUT
(distributing the disciplined clock), but the measurement path is
independent of PHC resolution.
