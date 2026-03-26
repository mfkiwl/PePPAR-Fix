# Session Handoff — 2026-03-26

This document captures the state of work at the end of a long session.
Read it to understand what was done, what's working, what's broken, and
what to do next.  Delete or archive it once the next session has picked
up the threads.

## What was accomplished

### PPS+PPP error source (complete, merged)

The PPP filter's `dt_rx` can replace TIM-TP `qErr` as the PPS
correction, yielding 0.1 ns confidence vs qErr's 3 ns.

- **How it works**: The F9T's `rcvTow` fractional second is constant
  (994 ms, confirmed by experiment).  `dt_rx mod 1e9` gives the
  receiver clock position relative to the GNSS second.  Quantizing
  to the 125 MHz tick grid (8 ns) gives the PPS timing error.
- **Calibration**: A constant offset (~3 ns, from float ambiguity bias)
  is determined at startup by comparing against TIM-TP for ~10 epochs.
  `PPPCalibration` class handles this with a dt_rx stability gate.
- **Validation**: Mean agreement with PPS+qErr = 0.2 ns on both TimeHat
  and ocxo (48 and 40 steady-state epochs respectively).
- **Code**: `scripts/peppar_fix/error_sources.py` (`ppp_qerr()`,
  `PPPCalibration`, updated `compute_error_sources()`),
  `scripts/peppar_fix_engine.py` (calibration feeding, removed old
  `|dt_rx| < 100µs` gate).
- **Doc**: `docs/pps-ppp-error-source.md`

### Ice driver GNSS streaming patch (built, partially working)

The stock ice driver buffers GNSS I2C data into a 4 KB page before
delivering to userspace (~2100 ms latency).  A patch streams each
15-byte I2C chunk immediately (~20 ms latency).

- **In-tree patch works**: Built from `linux-source-6.8.0`, preserves
  EXTTS + DPLL + irdma.  Verified: 15-byte streaming AND PPS EXTTS
  capture working simultaneously on ocxo.
- **Out-of-tree patch breaks EXTTS**: The Intel out-of-tree driver
  doesn't support `PTP_EXTTS_REQUEST`.  Don't use it for servo.
- **Config ACK issue**: The streaming 15-byte delivery breaks UBX
  config handshake (ACK wait times out on partial frames).
  Observation flow works fine; only initial config is affected.
- **Code**: `drivers/ice-gnss-streaming/` (patch, build script, README)
- **Doc**: `drivers/ice-gnss-streaming/README.md`, `docs/platform-support.md`

### Pipeline stall fix (merged, partially validated)

The observation pipeline jammed when PPS correlation confidence dropped
below `min_correlation_confidence` (0.5).  The gate deferred indefinitely
hoping for better data that would never arrive.

- **Gate fix**: Drop observations with low-confidence PPS match instead
  of waiting.  A more confident PPS for that second will never arrive.
- **Gap recovery**: On dt > 30s gaps, clamp predict to 1s and process
  the observation instead of skipping.  Prevents cascade.
- **Validated on TimeHat**: 113s gap reduced to 33s.  Not yet tested
  on ocxo (blocked by receiver state issue).
- **Code**: `scripts/peppar_fix/correlation_gate.py`,
  `scripts/peppar_fix_engine.py`

### Repo reorganization (complete, merged)

- `scripts/` reduced from 44 to 16 files (engine runtime only)
- `tools/`, `tools/analysis/`, `tools/timebeat/`, `tests/`, `old/`
  with READMEs
- `pyproject.toml` stub with version 0.1.0
- `docs/packaging-plan.md` — phased plan for pip-installability

## Current host state

### TimeHat (healthy)

- Repo up to date at `main`
- PHC bootstrapped and close to GPS time
- USB transport works normally (~900 ms natural inter-epoch gap)
- Stall fix deployed and tested
- Ready for further servo testing

### ocxo (needs recovery)

- **Receiver stuck in single-freq mode**.  Multiple driver swaps and
  kill signals during this session left the F9T with no dual-freq
  signals configured.  The config code can't get past ACK wait to
  reconfigure.
- **Fix**: Power-cycle the F9T (physically unplug USB or cycle the host)
  or flash the config to EEPROM from a working session.  The receiver
  will boot with factory defaults and `ensure_receiver_ready()` should
  be able to reconfigure it.
- **Stock ice driver is loaded** (the patched in-tree module was removed
  from `/lib/modules/.../updates/` and `depmod -a` run).
- The patched module can be rebuilt from source already extracted at
  `/usr/src/linux-headers-.../drivers/net/ethernet/intel/ice/`.

## What to do next (priority order)

### 1. Recover ocxo receiver

Power-cycle the F9T on ocxo (reboot the host, or if accessible, cycle
the E810's GNSS I2C bus).  Verify dual-freq observations resume:

```bash
ssh ocxo "cd /home/bob/PePPAR-Fix/scripts && source ../venv/bin/activate && \
  timeout 30 python3 -c '
from peppar_fix.receiver import ensure_receiver_ready
d = ensure_receiver_ready(\"/dev/gnss0\", 9600, port_type=\"I2C\", systems={\"gps\",\"gal\"})
print(d.name if d else \"FAILED\")
'"
```

### 2. Harden I2C config path

The UBX config handshake (CFG-VALSET → ACK/NAK) fails on I2C because:
- Stock driver: 2-second batched delivery mixes ACK with observation data
- Patched driver: 15-byte fragments split ACK across multiple reads

The config code in `scripts/peppar_fix/receiver.py` needs:
- A read loop that reassembles partial UBX frames (pyubx2's `UBXReader`
  does this on a stream, but the config code may be doing single reads)
- A longer timeout for I2C (the current timeout may be tuned for USB)
- Possibly: disable observation output temporarily during config, send
  CFG-VALSET, wait for ACK, then re-enable

### 3. Lower min_correlation_confidence

The remaining 33s gap on TimeHat is from confidence dipping just below
0.5 during a servo transient.  The PPS correlation confidence reflects
userspace receive timing, not PPS validity — a reading with 0.4
confidence is still a real PPS edge, just with slightly uncertain
host-receive timing.

Consider lowering from 0.5 to 0.2 or 0.1, or eliminating the
confidence gate entirely for PPS (keep it for observation correlation
where freshness matters more).

### 4. Sustained TDEV comparison

Once stalls are eliminated, run a long (30+ minute) servo session and
compare TDEV at tau = 1s, 10s, 100s between PPS+qErr and PPS+PPP.
The expected result: PPS+PPP should show lower TDEV at short tau due
to its 0.1 ns confidence vs qErr's 3 ns noise.

To force PPS+qErr for comparison, temporarily set
`carrier_max_sigma=0` in the engine args (disables PPS+PPP).

### 5. Ice driver: fix config + streaming coexistence

The streaming patch is the right fix for I2C delivery latency, but
receiver config must work with it.  Options:
- Fix the config code to handle 15-byte fragments (preferred)
- Temporarily load stock driver for config, then swap to patched
  (fragile, not recommended)
- Send config before loading the streaming module (boot-order hack)

## Key files to read

| File | Why |
|------|-----|
| `docs/pps-ppp-error-source.md` | PPS+PPP formula, experiment, calibration |
| `docs/platform-support.md` | E810 I2C delivery issue and fix status |
| `docs/packaging-plan.md` | Future pip-installability plan |
| `drivers/ice-gnss-streaming/README.md` | Driver patch, build, known issues |
| `scripts/peppar_fix/error_sources.py` | `ppp_qerr()`, `PPPCalibration` |
| `scripts/peppar_fix/correlation_gate.py` | Pipeline stall fix |
| `scripts/peppar_fix_engine.py` | Gap recovery fix, calibration integration |
