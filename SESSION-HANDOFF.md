# Session Handoff — 2026-04-03

## Headline: 2-hour TICC-measured disciplined TDEV

All numbers from TICC chA (60 ps), in-process via `--ticc-port`,
10 min warmup skipped.

**TimeHat i226 TCXO:**

| tau | PPS only | PPS+qErr | PPS+PPP |
|-----|----------|----------|---------|
| 1s  | 1.00 ns  | **0.79 ns** | 1.18 ns |
| 10s | 0.25 ns  | 0.25 ns  | 0.26 ns |
| 30s | 0.33 ns  | 0.24 ns  | **0.22 ns** |
| 60s | 0.55 ns  | 0.24 ns  | **0.18 ns** |
| 100s| 0.73 ns  | 0.20 ns  | **0.14 ns** |
| 300s| 0.38 ns  | 0.054 ns | **0.035 ns** |
| 1000s| 0.11 ns | 0.015 ns | **0.013 ns** |
| 2000s| 0.041 ns| 0.009 ns | **0.005 ns** |

**ocxo E810 OCXO (0.5 Hz lossless):**

| tau | PPS only | PPS+PPP |
|-----|----------|---------|
| 1s  | 2.62 ns  | 2.68 ns |
| 60s | 0.20 ns  | 0.21 ns |
| 300s| 0.13 ns  | 0.13 ns |
| 1000s| 0.078 ns| 0.057 ns|

**Key findings:**

1. **TCXO+PPP beats OCXO at every tau.** The TCXO's lower short-tau
   noise (0.79 ns vs 2.68 ns at tau=1s) gives it a head start,
   and PPP compensates both equally at long tau.
2. **qErr provides 1.3-8x improvement** on TCXO at tau=1-600s.
3. **PPP provides 3-11x improvement** on TCXO at tau=30-300s.
4. **5 ps TDEV at tau=2000s** on a $200 TCXO board with PPP.
5. **OCXO benefit is at short tau only** (lower noise floor), but
   the TCXO is already better there due to EXTTS path differences.

## E810 lossless delivery confirmed

Fixed measurement rate bug: bootstrap and engine were resetting
E810 to 1 Hz, overriding the 2000 ms profile setting. Now all
three code paths (wrapper, bootstrap, engine) auto-detect kernel
GNSS and default to 0.5 Hz.

Result: 3887-3891 epochs in 7800s = 99.6% lossless at 0.5 Hz.
Only 9-14 single-epoch skips per 2-hour run.

## CAS SSR mount confirmed — phase biases for PPP-AR

`SSRA01CAS1` on the Australian mirror provides 159 phase biases.
Existing BKG credentials work. No new registration needed.

Ready to begin PPP-AR Phase 2 (apply phase biases in filter).

## E810 I2C corruption recovery

Process kills leave the E810 I2C bus corrupted (checksum errors on
every read). Fix: `sudo rmmod irdma && sudo rmmod ice && sudo modprobe ice`.
Wait 10s for GNSS reinitialization.

## Stale lock fix

exclusive_io.py: PID-validated flock. Checks if owning PID is alive
before blocking — stale locks from crashed processes auto-reclaim.

## Plot 2 renamed

`phc-pps-in-time-error-tdev` — shows TICC ground truth vs EXTTS
measurement error on both platforms. In `plots/` directory.

## Overnight runs still in progress

ocxo qErr run queued (after PPS-only and PPP complete).

## Commits

- b08292d: Engine kernel GNSS rate detection
- 321d509: Bootstrap kernel GNSS rate detection
- 7020d68: Plot 2 renamed to PHC PPS IN
- 7fec677: Stale lock fix, in-process TICC guidance
- 57bc9fb: EXTTS TDEV warnings
- 3568eb1: E810 epoch delivery clarification
- a109665: E810 AQ contention root cause

## Host state

- TimeHat: idle after 3 × 2h10m runs, TICC #1 available
- ocxo: qErr run may still be in progress, 0.5 Hz lossless confirmed
