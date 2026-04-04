# Session Handoff — 2026-04-03/04 (overnight)

## Major architecture change: TICC replaces EXTTS

When `--ticc-port` is present, the TICC now completely replaces
EXTTS as the PPS measurement source:

1. **Auto-enabled**: `--ticc-drive` activates automatically with
   `--ticc-port` (no explicit flag needed)
2. **TICC chB generates PpsEvent**: replaces EXTTS reader for the
   correlation pipeline. Correlation uses host monotonic time.
3. **EXTTS reader not started**: the EXTTS thread doesn't run in
   TICC-drive mode. No EXTTS ioctl calls.
4. **Servo feedback**: TICC differential (chA-chB) at 60 ps
   resolution, vs EXTTS at 8 ns. 133x improvement.

This enabled the E810 OCXO for the first time:
- Bootstrap on stock driver (EXTTS works) → step PHC
- Switch to out-of-tree driver (DPLL locks, OCXO active)
- Run TICC-driven servo (no EXTTS needed)

## E810 with external F9T operational

Host ocxo now has external F9T-BOT on USB (/dev/ttyACM2):
- SMA1 (top bracket, pin 1, ch 1) = PEROUT → TICC #3 chA
- SMA2 (bottom bracket, pin 2, ch 2) = EXTTS ← F9T-BOT PPS
- TICC #3 chB = F9T-BOT PPS (ground truth, also feeds DPLL)
- 1 Hz lossless via USB serial (no I2C limitations)

## DPLL/OCXO activated

Out-of-tree ice driver v2.4.5 (patched with GNSS pin +
EXTTS-in-locked-mode bypass):
- Both DPLLs lock to SMA2 (external F9T PPS)
- OCXO disciplined by F9T PPS via CGU
- adjfine works (timer stays in NANOSECONDS mode)
- EXTTS not available (DPLL consumes the PPS signal)
- TICC-driven servo bypasses EXTTS entirely

Driver files:
- Out-of-tree: `/tmp/ethernet-linux-ice/src/ice.ko`
- Stock: `/lib/modules/.../kernel/.../ice/ice.ko.zst`
- Switch: `rmmod ice; insmod /tmp/.../ice.ko` (out-of-tree)
         or `rmmod ice; modprobe ice` (stock)

## Overnight 8-hour runs in progress

**ocxo** (OCXO, TICC-driven, out-of-tree driver):
- Engine running directly (not through wrapper — bootstrap was
  done on stock driver, then switched)
- `data/ocxo-ocxo-8h.csv` + `data/ocxo-ocxo-8h-ticc.csv`
- Source: TICC, settled, adjfine ~4 ppb
- First ever run on the actual OCXO

**TimeHat** (TCXO, TICC-driven, stock igc driver):
- Running through wrapper (EXTTS available but not used)
- `data/timehat-8h-ticc-drive.csv` + `data/timehat-8h-ticc-drive-ticc.csv`
- Bootstrap in progress, servo should start within minutes

## PPP-AR status

- Phase bias application code committed and tested
- CAS (SSRA01CAS1) provides phase biases but signal codes don't
  match (C2W vs C2L for GPS, C5I vs C5Q for Galileo)
- Requested products.igs-ip.net access for CNES/GFZ
- Galileo HAS phase biases not until ~2028-2029
- Ambiguity integrality on ocxo: mean|frac| = 0.15-0.32 (partial match)

## Other session work

- E810 SMA pin mapping verified (SMA1=top=PEROUT, SMA2=bottom=EXTTS)
- Antenna calibration plan documented (docs/antenna-calibration-plan.md)
- F9T internal delay reference: ~28 ns (Piriz/GMV)
- Local NTRIP casters documented (DuPage/ISTHA CORS)
- Plot 2 renamed to phc-pps-in-time-error-tdev
- ADEV subplot added to TimeHat disciplined plot
- Ole's blog post reviewed for comparison (docs/ notes saved)

## Commits

- e88398e: TICC replaces EXTTS when --ticc-drive is active
- f088178: Fix cleanup crash when EXTTS was never enabled
- 5d34b4a: Fix crash when EXTTS unavailable: guard test_pps
- bab5b59: Make EXTTS optional when TICC-driven servo active
- d8671d9: Auto-enable TICC-driven servo with --ticc-port
- fafeeea: Fix E810 SMA mapping
- 94dbe0d: E810 profile for external F9T
- 1dbba47: Antenna calibration plan
- 444c547: PPP-AR signal code fix and L5 gap documentation

## Host state

- TimeHat: 8h TICC-driven run in progress (TCXO, stock igc)
- ocxo: 8h TICC-driven run in progress (OCXO active, out-of-tree ice)
- Both: TICC capturing in-process, 1 Hz, nohup
