# Session Handoff — 2026-04-01

## What was accomplished this session

### White Rabbit GM architecture review

Reviewed WR softpll codebase. Key finding: the GM uses PPS only for
one-shot alignment (then ignores it), and distributes frequency/phase
from 10 MHz.  peppar-fix's continuous PPS+PPP discipline is strictly
better than the GM's alignment approach.  Two integration paths
documented: PHC PEROUT as 10 MHz reference, or OCXO+ClockMatrix with
software steering (preferred).  See `docs/wr-gm-research.md`.

### Solarflare SFN8522 evaluated — not viable with upstream driver

Installed on ocxo.  PTP license is active (PHC appears, HW timestamping
works) but upstream `sfc` driver reports `n_ext_ts=0` — PPS hardware
exists but is invisible.  Out-of-tree `sfc-dkms` driver required.
PEROUT never supported on any Solarflare generation.  TICC #3 wired
to Solarflare PPS IN/OUT but shows no data.  See updated
`docs/nic-survey.md` and `docs/platform-support.md`.

**PTP device numbering changed**: Solarflare pushed E810 from ptp1 to
ptp2.  Updated `config/ocxo.toml`.

### TICC serial open — HUPCL fix

`dsrdtr=False` was insufficient to prevent Arduino reboot across
process boundaries.  Root cause: `cdc_acm` driver toggles DTR on
open/close regardless.  Fix: clear HUPCL termios flag so DTR stays
asserted after close.  Tested cross-process (3 independent opens) on
both TimeHat (kernel 6.12) and ocxo (kernel 6.8) — no reboots.

Also: shared port singleton (`_SharedTiccPort`) for within-process
reuse, `TimeoutError` on boot wait failure, CR sent during menu phase,
flock replaced with TIOCEXCL.

### Freerun mode implemented

`--freerun` runs the full pipeline without steering the PHC.  Logs what
the servo would do (adjfine, error sources, qErr litmus) but never calls
adjfine.  For characterizing EXTTS precision and oscillator stability.

- `--freerun-max-error-ns` auto-stops when PPS error exceeds threshold
- `--no-glide` in bootstrap (automatic with freerun): sets adjfine to
  base oscillator frequency only, no glide slope
- `phc_gettime_ns` column added to servo CSV
- clockClass held at 248 throughout

### flock replaced with TIOCEXCL in PtpDevice

Stale flock files from crashed processes blocked re-runs.  TIOCEXCL is
kernel-enforced and auto-released on fd close, even on SIGKILL.

### qErr sign fixed in plot_deviation.py

Was using `pps - qerr` (wrong), fixed to `pps + qerr` (correct).
Now shows 1.4–2.0x improvement, consistent with qerr_offset_sweep.

### TICC baseline characterization (overnight)

Four captures on TimeHat TICC #1 (2×30m + 2×2h):

**F9T PPS TDEV(1s) = 2.3 ±0.1 ns** (2-hour baseline).  30-minute runs
give 1.0–1.4 ns depending on sawtooth phase — short observations are
unreliable.  The 2-hour value is the authoritative F9T PPS baseline.

**i226 TCXO PEROUT TDEV(1s) = 1.170 ±0.002 ns** (0.2% spread across
all 4 runs).  The free-running TCXO is quieter than the F9T PPS at
tau=1–5s.  The servo cannot improve the PHC at short tau.

**E810 EXTTS TDEV(1s) = 0.34 ns** — artifact of quantization flatness
(77% identical adjacent timestamps).  The E810 sub-ns resolution can't
distinguish the ~2.3 ns PPS jitter.  Not a real measurement of
timing precision.

See `docs/ticc-baseline-2026-04-01.md`.

## Known issues

### E810 ADJ_SETOFFSET residual

`step_relative` reports +3281 ns but PPS truth shows -87192 ns.
The readback from `clock_adjtime` doesn't reflect actual E810 PHC
state.  Pre-existing but not previously characterized.

### ocxo E810 I2C qErr coverage

Only 133/436 epochs (30%) had qErr in the ocxo freerun run.
TIM-TP should arrive at 1 Hz even when RAWX is at 0.5 Hz.  May need
to check TIM-TP configuration on the I2C port.

### TimeHat "missing epochs" — not missing

96 epochs in "300s" duration was actually 96 epochs in 95s of servo
time — bootstrap consumed most of the 300s budget.  Use `--duration 600`
for ~5 min of servo data.

### Position watchdog on ocxo

Still trips after ~2.4 hours (from previous sessions).

## Commits pushed

- 79a6f59: Fix TICC serial open: use HUPCL to prevent cross-process reboots
- c088f2c: Add White Rabbit GM architecture research doc
- 3a7f48c: Update Solarflare platform docs
- 24e49b5: Add --freerun mode for PHC stability characterization
- 340f838: Fix freerun issues: no-glide, drop flock, fix qErr sign
- 0d825db: Add TICC baseline characterization: F9T PPS and i226 TCXO

All on main, pushed to origin.

## Host state

- TimeHat: idle, adjfine at ~101 ppb (base freq from bootstrap),
  EXTTS enabled, PEROUT running, TICC #1 available
- ocxo: idle, ptp_dev updated to /dev/ptp2 (Solarflare shifted E810),
  Solarflare at ptp0 (upstream driver, no PPS), TICC #2/#3 available
- PiPuss: not touched this session
