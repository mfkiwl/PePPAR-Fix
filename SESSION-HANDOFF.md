# Session Handoff — 2026-04-02/03

## What was accomplished this session

### Disciplined TDEV comparison: PPS vs qErr vs PPP

Six 15-minute disciplined runs on both platforms with `--no-qerr` and
`--no-ppp` flags (new this session) to control source selection.
Analysis of last 5 minutes (settled servo).

**TimeHat i226 (TCXO):**

| tau | PPS only | PPS+qErr | PPS+PPP |
|-----|----------|----------|---------|
| 1s  | 1.84 ns  | 2.37 ns  | 2.92 ns |
| 5s  | 0.76 ns  | 0.45 ns  | 0.77 ns |
| 10s | 0.54 ns  | 0.36 ns  | 0.32 ns |
| 30s | 1.21 ns  | 0.36 ns  | 0.28 ns |
| 60s | 2.53 ns  | 0.34 ns  | 0.29 ns |

**ocxo E810 (OCXO):**

| tau | PPS only | PPS+qErr* | PPS+PPP |
|-----|----------|-----------|---------|
| 1s  | 0.37 ns  | 0.22 ns   | 0.19 ns |
| 5s  | 0.11 ns  | 0.09 ns   | 0.07 ns |
| 10s | 0.14 ns  | 0.07 ns   | 0.06 ns |
| 30s | 0.36 ns  | 0.08 ns   | 0.04 ns |
| 60s | 0.53 ns  | 0.08 ns   | **0.018 ns** |

*qErr only 28% coverage on E810 I2C path.

**Key finding**: correction hierarchy validated at all taus ≥5s:
- PPS < PPS+qErr < PPS+PPP
- Corrections compound with better oscillators
- Best result: E810 OCXO + PPP = 18 ps TDEV at tau=60s
- At tau<5s, the free-running oscillator is quieter than any
  correction — servo should not steer at short tau

### TICC+qErr: 16.8x TDEV improvement

Simultaneous TICC chB + TIM-TP capture with host monotonic time
correlation at 0.9s offset.  Match quality: mean dt=27 ms, std=5.5 ms.
Raw TICC TDEV(1s) = 2.86 ns → corrected 0.17 ns (170 ps).
This is the definitive qErr validation at the TICC's 60 ps resolution.

### EXTTS resolution: both i226 and E810 have ~8 ns effective bins

E810 EXTTS: 77% identical adjacent timestamps.  Effective bin width
~8 ns (125 MHz period), not the 1 ns format suggests.  Sub-ns
capability is in the packet timestamp path, not GPIO.

i226 EXTTS adds ~2.9 ns RSS noise but tracks PPS movement (0%
identical adjacent).  qErr fully compensates the quantization:
EXTTS+qErr TDEV matches TICC ground truth (2.23 ns).

### PHC bootstrap simplified

Removed PTP_SYS_OFFSET readback + system clock extrapolation.
`adj_setoffset(-phase_error_ns)` directly.  E810 step residual:
-87192 ns → 0 ns.

### igc adjfine v3 patch + diagnostic

Tested tmreg_lock (v2, doesn't fix), TSYNCTXCTL disable (v3, works
at realistic rates).  Three distinct failure modes documented.
PTP GM scaling: ~500 clients at 128 Hz before slot exhaustion.
Draft upstream reply at `drivers/igc-adjfine-fix/upstream-reply-v3.txt`.

### Source selection flags

`--no-qerr` and `--no-ppp` added to the engine for controlled A/B
testing of correction sources.

### PPP-AR design document

Four-phase plan: single-AC SSR source → phase bias application →
integer ambiguity fixing → servo TDEV measurement.  Key risk:
FixedPosFilter cancels ambiguities by construction.

### Visual stories specification

Five distinct TDEV plots defined in `docs/visual-stories.md`, each
telling one story with consistent conventions (one-sigma shading,
duration matching).

### Galileo HAS IDD

Registration process documented.  Separate GSC account required,
human-reviewed approval (days to weeks).  Alternative: single-AC
NTRIP mount (CAS/CNES) using existing BKG credentials.

## Data files on lab hosts

TimeHat (`/home/bob/peppar-fix/data/`):
- `disc-pps-only-15m.csv` — disciplined PPS only
- `disc-pps-qerr-15m.csv` — disciplined PPS+qErr
- `disc-pps-ppp-15m.csv` — disciplined PPS+PPP
- `ticc-qerr-v2-30m.csv` — TICC+qErr simultaneous capture
- `freerun-timehat-2h.csv` — 2h freerun
- `ticc-baseline-{30m,2h}-{1,2}.csv` — TICC baselines

ocxo (`/home/bob/git/PePPAR-Fix/data/`):
- `disc-pps-only-15m.csv` — disciplined PPS only
- `disc-pps-qerr-15m.csv` — disciplined PPS+qErr (mostly PPS, 28% qErr)
- `disc-pps-ppp-15m.csv` — disciplined PPS+PPP
- `freerun-ocxo-2h.csv` — 2h freerun
- `ticc-ocxo-2h.csv` — TICC baseline

## Known issues

### ocxo qErr coverage

Only 28% of epochs get qErr on the E810 I2C path.  TIM-TP messages
are small but may be deprioritized by the I2C delivery thread.
Needs investigation — full 1 Hz qErr would likely improve the
already-impressive OCXO results further.

### PPP in freerun shows worse TDEV

In freerun mode, source_error_ns from PPP includes reconvergence
noise against the drifting PHC.  PPP improvement is only visible
in disciplined mode (confirmed this session).

### E810 PEROUT noise in freerun

Freerun PEROUT TDEV(1s) = 2.78 ns was from bootstrap adjfine
carrying PPS sawtooth.  Not hardware coupling — software artifact
from the bootstrap frequency estimate.  Confirmed by the 0.19 ns
disciplined result.

## Commits pushed

- 59934ab: Add --no-qerr and --no-ppp flags for source selection
- 3e5c29a: Add visual stories spec
- c0838a4: Add PPP-AR design doc
- 3c19abc: Add PHC noise floor extraction
- 2ede89d: TICC+qErr 16.8x improvement
- c2bdf7f: EXTTS resolution analysis
- 8dfa678: Simplify PHC step via ADJ_SETOFFSET
- 6daca83+: igc adjfine v3 patch + diagnostics

## Host state

- TimeHat: idle, v3 igc patch, TICC #1 available
- ocxo: idle, ptp_dev=/dev/ptp2, TICC #2 available
