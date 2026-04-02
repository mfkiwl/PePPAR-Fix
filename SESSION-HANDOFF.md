# Session Handoff — 2026-04-01/02 (overnight)

## What was accomplished this session

### igc adjfine TX timestamp race — full investigation

Tested three patches per kernel maintainer feedback:
- v1 (ptp_tx_lock + EBUSY): works at realistic rates, starves adjfine
  under extreme TX load
- v2 (tmreg_lock only): does NOT fix bug — race is hardware, not
  software register contention
- v3 (tmreg_lock + TSYNCTXCTL disable/enable): works at realistic
  rates, strands in-progress captures at extreme rates

Diagnostic testing with driver reload between each test revealed three
distinct failure modes: TIMINCA corruption (the original bug), TX
timestamp slot exhaustion (4-slot hardware limit at ~65k TX/s), and
TSYNCTXCTL stranding (v3 side effect).

PTP GM scaling: i226 handles ~500 PTP clients (65k TX timestamps/s)
before slot exhaustion.  The adjfine race is negligible at 1 Hz.

Draft upstream reply with v3 patch at
`drivers/igc-adjfine-fix/upstream-reply-v3.txt`.

### PHC bootstrap simplified — ADJ_SETOFFSET

Removed the vestigial PTP_SYS_OFFSET readback and system clock
extrapolation from the step path.  The PPS measurement already gives
the exact phase error; `adj_setoffset(-phase_error_ns)` applies it
directly.

Results: E810 step residual went from -87192 ns to 0 ns.  TimeHat
i226: -1968 ns (±2 µs).

### Overnight TICC + freerun characterization (2h captures)

Four parallel captures: TICC on TimeHat (existing), TICC on ocxo,
freerun on TimeHat, freerun on ocxo.

**F9T PPS baseline** (TICC chB, 2h):
- TimeHat: TDEV(1s) = 2.23 ns
- ocxo: TDEV(1s) = 3.01 ns
- Both converge at tau>10s (same underlying F9T behavior)

**PHC PEROUT stability** (TICC chA, 2h, free-running):
- TimeHat i226 TCXO: TDEV(1s) = 1.17 ns (0.2% reproducible)
- ocxo E810 OCXO: TDEV(1s) = 2.78 ns (surprisingly noisier)
- E810 PEROUT noise tracks the F9T PPS (3.01 ns), suggesting internal
  coupling between PPS and PEROUT paths

**PHC noise extraction** (EXTTS vs TICC, duration-matched 2h):
- i226 EXTTS TDEV(1s) = 3.64 ns
- i226 PHC capture noise = 2.88 ns RSS (36% of 8 ns tick)
- i226 EXTTS + qErr TDEV(1s) = 2.23 ns = TICC ground truth
- **qErr fully compensates the 8 ns quantization** at tau=1s (1.63x)

**E810 EXTTS** quantization analysis:
- Both i226 and E810 have ~8 ns effective EXTTS resolution
- i226 adds noise (resolves PPS movement), E810 is flat (77% identical)
- E810's sub-ns capability is in the packet timestamp path, not GPIO

### EXTTS resolution analysis documented

Updated `docs/ticc-baseline-2026-04-01.md` with the 8 ns quantization
analysis, PHC noise extraction method, qErr value assessment for each
timestamper, and TICC vs EXTTS comparison.

### PPP-AR design document

`docs/ppp-ar-design.md`: four-phase plan from float PPP to PPP-AR.
Phase bias sources (Galileo HAS IDD, single-AC NTRIP), filter changes,
bootstrapping AR algorithm, five validation tests.  Key risk: the
FixedPosFilter cancels ambiguities by construction — AR requires an
undifferenced clock filter.

### Visualization tools

- `tools/plot_oscillator_floor.py` — oscillator noise floors + EXTTS
  measurement noise with shaded PHC noise region
- `tools/plot_pps_corrections.py` — PPS→PPS+qErr→PPS+PPP TDEV
  improvement plot
- Updated `tools/plot_deviation.py` — fixed qErr sign, added MDEV
  and detrending

### Other fixes

- Lab timezones: all hosts set to America/Chicago
- ocxo ptp_dev updated to /dev/ptp2 (Solarflare shifted E810)
- Removed empty `scripts/phc_servo.py`

## Known issues

### E810 PEROUT noise = F9T PPS noise

E810 PEROUT TDEV(1s) = 2.78 ns via TICC, very close to the F9T PPS
on the same host (3.01 ns).  The OCXO should be sub-ns.  Suggests
the PEROUT path is coupled to the F9T PPS sawtooth internally —
the PEROUT may be phase-locked to the PPS rather than running from
the OCXO directly.

### PPS+PPP appears worse than raw PPS in freerun

In freerun, `source_error_ns` from PPS+PPP has higher TDEV than raw
PPS.  This is because the PPP filter sees a drifting PHC and its
correction includes reconvergence artifacts.  PPP improvement requires
disciplined mode.

### ocxo E810 I2C qErr coverage

Only 30% of epochs had qErr on ocxo.  TIM-TP should arrive at 1 Hz
even when RAWX is at 0.5 Hz.

### Position watchdog on ocxo

Still trips after ~2.4 hours.

## Commits pushed

- 3a7f48c: Update Solarflare platform docs
- 24e49b5: Add --freerun mode for PHC stability characterization
- 340f838: Fix freerun issues: no-glide, drop flock, fix qErr sign
- 0d825db: Add TICC baseline characterization: F9T PPS and i226 TCXO
- c2bdf7f: Rewrite EXTTS resolution analysis: both i226 and E810 ~8 ns
- 93a8da0: igc adjfine: test tmreg_lock patch (v2) per maintainer
- 098a790: igc adjfine v3: disable TX timestamping around TIMINCA write
- 6daca83: igc adjfine: clean-state diagnostic reveals true failure modes
- ac1e604: Draft upstream reply with v3 patch and diagnostic findings
- 187bcad: igc: add PTP GM scaling analysis to upstream reply
- 8dfa678: Simplify PHC step: apply PPS error directly via ADJ_SETOFFSET
- 1dcdf3e: Add oscillator noise floor and EXTTS measurement noise plots
- 3a70be5: Show both hosts on oscillator noise floor plot
- 3c19abc: Add PHC noise floor extraction to PPS measurement plot
- 1cb3355: Add PPS corrections TDEV improvement plot
- c0838a4: Add PPP-AR design: from float PPP to integer AR

All on main, pushed to origin.

## Host state

- TimeHat: idle, v3 igc patch deployed, adjfine at ~97 ppb,
  TICC #1 available
- ocxo: idle, ptp_dev=/dev/ptp2, Solarflare at ptp0 (no PPS),
  TICC #2/#3 available
- Data files: 2h TICC + freerun CSVs on both hosts and in repo data/
