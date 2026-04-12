# qErr Correlation: Principles, Measurement, and Implementation

## What qErr is

TIM-TP qErr is a hardware measurement from the F9T receiver.  It
reports the timing error of the **next** PPS edge — the signed
distance between the true GPS second and the nearest 125 MHz tick
where the PPS will fire.  It is a quantization correction: the PPS
edge snaps to a discrete tick grid, and qErr tells us exactly where
within that grid the GPS second actually falls.

See `docs/glossary.md` for term definitions.

## The GPS-to-DO chain and where qErr fits

```
GPS satellite clocks
    │
    │  carrier-phase observations → PPP → dt_rx
    ▼
rx TCXO (F9T 125 MHz crystal)
    │
    │  PPS fires at nearest tick → qErr = GPS_second - PPS_time
    ▼
gnss_pps edge (physical voltage transition)
    │
    │  Timestamped by TICC (60 ps) and EXTTS (8 ns on i226)
    ▼
pps_err = gnss_pps - do_pps → servo → adjfine → DO
```

qErr corrects the tick quantization in the gnss_pps edge.  Applying
it to a gnss_pps timestamp recovers the true GPS second position on
whatever timescale the timestamp was taken:

```
corrected = gnss_pps_timestamp + qErr
```

The sign convention: positive qErr means the GPS second is AFTER the
PPS edge (PPS fired early).  Adding qErr advances the timestamp to
the true GPS second.

## Why TICC qVIR is the definitive correlation check

### What we're checking

qErr must be matched to the correct PPS edge.  "Correct" means: the
TIM-TP message that predicted this specific PPS edge gets applied to
the TICC timestamp of that same edge.  If the match is wrong (off by
one PPS epoch), the correction is from a different point in the
sawtooth — typically ~8 ns wrong, making the corrected timestamp
WORSE than uncorrected.

qVIR (qErr Variance Improvement Ratio) measures whether the
correction is actually reducing timestamp variance:

```
qVIR = detrended_var(gnss_pps_ticc) / detrended_var(gnss_pps_ticc + qErr)
```

- **qVIR >> 1**: qErr is correctly matched and removing the PPS
  sawtooth.  Observed: 80-165x on TimeHat.
- **qVIR ≈ 1**: qErr is uncorrelated — wrong epoch match.
- **qVIR < 1**: qErr is anticorrelated — wrong sign or consistently
  wrong epoch.

### Why we measure on TICC chB alone

TICC chB timestamps gnss_pps on the TICC timescale.  This is a
**pure measurement** of the F9T PPS edge — no DO, no servo, no PHC.
The TICC timescale doesn't change when the servo adjusts adjfine.
The sawtooth in these timestamps comes entirely from the rx TCXO
beating against GPS time.

qErr should remove exactly this sawtooth.  If it does, the corrected
timestamps have only TICC measurement noise (~60 ps single-shot,
~178 ps TDEV at 1s).  qVIR directly measures how well the
correction works.

### Why we do NOT compute qVIR on chA-chB diff

The chA-chB diff (pps_err_ticc_ns) includes:
- The PPS sawtooth (from chB) — what qErr corrects
- The DO noise (from chA) — what the servo controls
- The servo's adjfine corrections — leaking into chA

Computing qVIR on this diff mixes qErr quality with DO noise and
servo dynamics.  A "good" qVIR could mean good qErr OR a quiet DO.
A "bad" qVIR could mean bad qErr OR a noisy DO.  On MadHat (noisy
TCXO, no heatsink), the DO noise dominates and qVIR ≈ 1.3 even
with perfect qErr matching — because the qerr variance (2.97 ns)
is small compared to the DO variance (5.33 ns).

The chB-only measurement eliminates this confusion.  If qVIR is
80-165x on chB alone, the qErr matching is correct — period.  The
DO's noise level is a separate concern that doesn't affect this
diagnostic.

### Why we don't compute qVIR on EXTTS timestamps

EXTTS timestamps gnss_pps on the PHC timescale.  Two problems:

1. **PHC quantization**: on i226, EXTTS has ~8 ns effective
   resolution — the same scale as the 8 ns tick period.  The EXTTS
   measurement is quantized at the same granularity as the thing
   qErr corrects.  qVIR ≈ 1.0 regardless of qErr quality.

2. **Servo contamination**: the servo changes adjfine, which changes
   the PHC clock rate, which changes how EXTTS timestamps the next
   PPS edge.  The servo's actions leak into the measurement.  EXTTS
   qVIR tracks servo performance, not qErr quality.

EXTTS qVIR is logged for completeness but is not a reliable
indicator of qErr correlation quality on i226.

## How matching works

### TIM-TP-initiated window matching

The two streams — TIM-TP (from F9T serial) and TICC chB (from TICC
serial) — run on independent USB ports.  They can't be matched by
index (drops happen) or by FIFO ordering (USB jitter causes bursts
that break 1:1 alignment).

The matching uses CLOCK_MONOTONIC with a freshness filter:

1. **TIM-TP arrives** at host_time A (CLOCK_MONOTONIC).  It
   predicts qErr for the PPS edge that will fire ~900 ms later.
   The QErrStore records (A, qErr) as `pending_for_chb`.

2. **TICC chB arrives** at recv_mono B (CLOCK_MONOTONIC).  Only
   processed if `queue_remains=False` — the serial buffer was empty
   after this read, so B is maximally fresh.

3. **Window check**: if `0.8 ≤ (B - A) ≤ 1.1` seconds, this chB
   event matches this TIM-TP.  The pending is consumed (cleared).

4. **No match**: if B - A > 1.1, the pending expired (PPS dropped
   or chB was queued).  If B - A < 0.8, timing is wrong (shouldn't
   happen with fresh events).

This is deterministic and robust:
- Each TIM-TP is consumed at most once (clear_pending on match)
- Only fresh chB events participate (no queuing artifacts)
- The 800-1100 ms window is tight enough to be unambiguous (TIM-TP
  samples are 1000 ms apart)
- No offset calibration needed — the window is wide enough to
  absorb USB jitter while remaining unambiguous

### The queue_remains principle

After a read() from a serial port, check if there's more data
buffered.  If the buffer is empty (`queue_remains=False`):
- The message is fresh — it arrived close to the physical event
- The CLOCK_MONOTONIC timestamp is a solid correlation anchor
- Use this for timescale relationship calibration

If the buffer is not empty (`queue_remains=True`):
- The message was queued — it arrived earlier but we just now read it
- The CLOCK_MONOTONIC timestamp includes queuing latency
- Do not use for correlation — timing is unreliable

This principle applies to ALL streams, not just qErr and TICC.
See `docs/stream-timescale-correlation.md` for the full model.

## Verified results

| Metric | Value | Notes |
|---|---|---|
| TICC qVIR | 80-165x | TimeHat, freerun and discipline modes |
| Raw gnss_pps std | 2.3 ns | PPS sawtooth (detrended) |
| Corrected gnss_pps std | 0.17-0.25 ns | TICC noise + TIM-TP noise |
| Match window | 800-1100 ms | TIM-TP to chB delay |
| Typical delay | ~910 ms | Stable to ±20 ms |
| Match rate | 97% of fresh chB events | 3% missed = TIM-TP not yet arrived |
