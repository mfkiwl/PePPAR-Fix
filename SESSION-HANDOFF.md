# Session Handoff — 2026-04-02/03 (continued)

## Headline result — TICC-measured disciplined TDEV

All TDEV numbers below are from TICC chA (disciplined PEROUT),
measured in-process alongside the servo via `--ticc-port --ticc-log`.
Last 5 minutes of 15-minute runs (warm, settled servo).

**TimeHat i226 TCXO:**

| tau | PPS only | PPS+qErr | PPS+PPP |
|-----|----------|----------|---------|
| 1s  | 0.93 ns  | 1.00 ns  | 1.05 ns |
| 5s  | 0.33 ns  | 0.25 ns  | 0.28 ns |
| 10s | 0.41 ns  | 0.24 ns  | 0.25 ns |
| 30s | 1.07 ns  | 0.20 ns  | 0.17 ns |
| 60s | 2.24 ns  | 0.19 ns  | **0.15 ns** |

**ocxo E810 OCXO:**

| tau | PPS only | PPS+PPP |
|-----|----------|---------|
| 1s  | 2.71 ns  | 2.64 ns |
| 10s | 0.59 ns  | 0.50 ns |
| 30s | 0.39 ns  | 0.25 ns |
| 60s | 0.23 ns  | **0.13 ns** |

Correction hierarchy validated:
- tau<5s: oscillator wins (don't steer)
- tau=5-10s: qErr wins on TimeHat (1.3-1.7x)
- tau≥20s: PPP wins (up to 14.7x on TimeHat)
- Best: 133-153 ps TDEV at tau=60s (genuine sub-200 ps)

## EXTTS TDEV is unreliable — documented restriction

Both i226 and E810 EXTTS have ~8 ns effective resolution. EXTTS-only
TDEV measurements are unreliable:
- E810 EXTTS reported 18 ps at tau=60s; TICC truth = 133 ps (7x understatement)
- i226 EXTTS reported 288 ps; TICC truth = 153 ps (1.9x overstatement)

Restriction added to CLAUDE.md: never report TDEV from EXTTS alone.
TICC required for all characterization.  Warnings added to engine
(--freerun without --ticc-port) and plot_deviation.py.

## Stale locks fixed

exclusive_io.py: PID-validated flock — checks if owning PID is alive
before blocking.  Stale locks from crashed processes are automatically
reclaimed.  GNSS serial open uses TIOCEXCL directly (no flock wrapper).
PtpDevice uses TIOCEXCL.  TICC uses TIOCEXCL + HUPCL.

## E810 I2C gap root cause

Multi-second gaps (up to 33s) on ocxo traced to AQ contention: the
E810 Admin Queue is shared between PTP operations (~22 misc interrupts/s
from adjfine, EXTTS, PEROUT) and GNSS I2C reads.  When PTP commands
pile up, I2C polls get starved.  This is a hardware/driver architecture
limitation, not I2C bandwidth oversubscription.

## In-process TICC capture

All TICC logging now uses the engine's built-in `--ticc-port --ticc-log`
rather than separate capture processes.  Both channels logged with host
monotonic timestamps, shared lifecycle with the servo.

## Epoch delivery

- TimeHat: 100% delivery confirmed (736/736, 900/900). No missing
  epochs with current code. March 28 issue (1911/3600) is resolved.
- ocxo: ~100% of configured 0.5 Hz rate.  Large gaps from AQ
  contention, not I2C oversubscription.

## Other changes this session

- `--no-qerr` and `--no-ppp` flags for controlled source selection
- PPP-AR design doc (`docs/ppp-ar-design.md`)
- Visual stories spec (`docs/visual-stories.md`)
- Galileo HAS IDD registration process documented
- Lab timezones: all hosts set to America/Chicago
- Empty `scripts/phc_servo.py` removed

## Data files

TimeHat (`/home/bob/peppar-fix/data/`):
- `disc-pps-{only,qerr,ppp}.csv` — disciplined servo CSVs (in-process TICC)
- `ticc-pps-{only,qerr,ppp}.csv` — TICC chA+chB logs (in-process)

ocxo (`/home/bob/git/PePPAR-Fix/data/`):
- `disc-pps-{only,ppp}.csv` — disciplined servo CSVs
- `ticc-pps-ppp.csv` — TICC chA+chB log (in-process, PPP run only)

## Host state

- TimeHat: idle, v3 igc patch, TICC #1 available, 100% epoch delivery
- ocxo: idle, ptp_dev=/dev/ptp2, TICC #2 available, AQ gap issue noted
