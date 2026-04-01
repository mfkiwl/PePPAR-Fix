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

## Implications for peppar-fix

### Servo bandwidth

The free-running TCXO at 1.17 ns TDEV(1s) is quieter than any
correction source at tau=1s:
- F9T PPS: 2.3 ns
- PPS + qErr: ~1.7 ns (from freerun data)
- PPS + PPP: ~0.1 ns confidence, but applied at 1 Hz

The servo should have low gain at short tau to avoid injecting PPS
sawtooth noise into the PHC.  The current gain schedule (Kp=0.03,
Ki=0.001) converges at tau ~10-30s, which is appropriate.

### qErr value

qErr reduces the PPS correction noise from 2.3 ns to ~1.7 ns (1.4x at
tau=1s, up to 2x at tau=5s).  Since the TCXO is 1.17 ns at tau=1s,
even qErr-corrected PPS (1.7 ns) is noisier than the free-running
TCXO.  qErr helps most in the tau=2-10s range where the PPS and TCXO
TDEV are comparable.

### EXTTS comparison

When EXTTS TDEV(1s) < 2.3 ns (the F9T baseline), the PHC timestamp
quantization is masking real PPS jitter.  The E810 EXTTS reported
0.34 ns — far below the 2.3 ns ground truth — confirming that 77% of
its timestamps are quantization-flat.  The i226 EXTTS reported 1.99 ns,
which is below the 2.3 ns baseline but close enough to be useful (the
8 ns tick grid adds ~1.7 ns RSS noise to the measurement).
