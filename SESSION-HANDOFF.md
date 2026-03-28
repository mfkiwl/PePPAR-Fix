# Session Handoff — 2026-03-28 (afternoon)

E810 I2C bandwidth investigation, SFRBX config, consumption sanity check.

## What was accomplished

### E810 AQ I2C bandwidth limit — root cause found
- The E810 Admin Queue limits I2C reads to **15 bytes per command**
  (hardware: `ICE_AQC_I2C_DATA_SIZE_M = GENMASK(3,0)`, 4-bit field).
- Each AQ command takes ~2.8ms, giving ~5.3 kB/s burst but ~1.6 kB/s
  sustained throughput after polling gaps.
- With all UBX messages enabled, F9T generates ~2.2 kB/s — 2x over-
  subscribed.  The F9T's I2C buffer overflows, shedding ~35% of RAWX.
- The overnight run's observation queue grew from `recv_dt=1.7` to 10+
  seconds in 14 epochs, then hit the 11s correlation window limit.

### Resolution: stock driver + SFRBX disabled + 0.5 Hz
- **No custom driver patch needed.** Stock ice driver is sufficient when
  I2C bandwidth is managed.  The streaming patch is retained for reference.
- `sfrbx_rate = 0` on E810 (disabled — ephemeris from NTRIP).  SFRBX
  consumed ~400 B/s (25% of bus), was redundant with NTRIP.
- `measurement_rate_ms = 2000` on E810 (0.5 Hz).  At 1 Hz, RAWX alone
  (~1.5 kB/s) still saturates the bus (~10% loss).  0.5 Hz is lossless.
- Every servo epoch gets the full PPS+qErr+PPP stack.  Intervening 1 Hz
  PPS events are pruned by the correlation gate.
- TimeHat unchanged: 1 Hz, sfrbx_rate=1, all messages.

### KernelGnssStream fixes (from overnight)
- Spin loop: `_fill_raw(1)` was no-op when buffer had lone `0xB5`.
  Fix: `_fill_raw(2)`.
- Over-buffering: `read(size)` forced `_fill_ubx(size)`, accumulating
  many frames.  Fix: return buffered data or one frame.

### Consumption rate sanity check
- Tracks `recv_dt_s` over 30 correlated epochs.  If growth > 3s, fires
  CONSUMPTION RATE ALARM and degrades clockClass to freerun.
- Tested: fires on E810 at 1 Hz (0.87 Hz delivery), does NOT fire on
  TimeHat or E810 at 0.5 Hz.

### Documentation
- `docs/platform-support.md` updated with AQ bandwidth findings, SFRBX
  consequences, stock driver recommendation, TICC/PPS routing notes.

## 4-hour stability runs in progress

Started ~17:03 UTC (12:03 CDT):

### TimeHat
- PID 6526, duration 14400s
- 1 Hz, sfrbx_rate=1, TICC #1
- Expected completion: ~21:03 UTC

### ocxo
- PID 23872, duration 14400s
- 0.5 Hz, sfrbx_rate=0, stock ice driver, TICC #3 (chA only)
- At epoch 70 (~2 min): PPS+PPP error +7.0 ns, adj +0.3 ppb (converged)
- Expected completion: ~21:03 UTC

## Commits (not pushed)

```
c39838e Add I2C write-yield to ice GNSS streaming patch, fix --baud for kernel GNSS
649e723 Fix KernelGnssStream spin loop and read over-buffering on E810
edfb80e Update session handoff for 2026-03-28 overnight
ebd94fa Make SFRBX optional on E810 I2C, document AQ bandwidth limit
3e7c32f Add consumption rate sanity check with clockClass degradation
18ccbda Add measurement_rate_ms to PTP profiles: 1 Hz i226, 0.5 Hz E810
215dded Replace minimal_messages with sfrbx_rate config knob
b7bb303 Set E810 measurement rate to 2000ms (0.5 Hz lossless)
```

## What to do next

### 1. Check 4-hour run results
Both runs should complete ~21:03 UTC.  Pull CSV/TICC logs, compute TDEV.
Compare TimeHat (1 Hz) vs ocxo (0.5 Hz).  ocxo showed +7 ns error at
epoch 70 with 0.3 ppb adj — should converge well.

### 2. Push commits
8 commits on main, not yet pushed to origin.

### 3. E810 PEROUT for TICC
TICC #3 chA needs a PPS signal.  Configure E810 PEROUT on SMA so TICC
can measure disciplined PHC PPS independently.

### 4. PPS-driven servo loop (future)
Currently the servo is observation-gated (runs only when RAWX arrives).
A PPS-driven loop would allow 1 Hz PPS+qErr steering with 0.5 Hz PPP
updates on E810.  Requires architecture change — the servo clock must
move from observation-driven to PPS-driven.
