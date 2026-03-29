# Platform Support Notes

This document summarizes what we learned while bringing `PePPAR-Fix` up on two different hardware platforms:

- `ocxo`: Intel E810-based host with onboard GNSS exposed as `/dev/gnss0`
- `timehat`: TimeHAT board with an Intel i226 PHC and GNSS over USB serial

The important conclusion is that these are not just two copies of the same platform. They differ in:

- PHC device naming and capabilities
- GNSS transport path
- F9T signal set
- PPS capture wiring model
- what the software can assume about buffering and correlation

Reference:

- TimeHAT project page: <https://github.com/Time-Appliances-Project/TimeHAT>
  - the `Testing PPS` section documents the post-power-cycle `testptp`
    commands that restore the expected SDP PPS input/output setup on the
    board

## Current support status

### `ocxo` / E810

Working:

- Host reachable at `10.168.60.37`
- PHC path identified and usable on `/dev/ptp1`
- E810 profile support added in [`scripts/peppar_fix_engine.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_engine.py) and [`config/receivers.toml`](/home/bob/git/PePPAR-Fix/config/receivers.toml)
- Explicit PHC timescale support added, with `e810` defaulting to `tai`
- F9T dual-frequency processing works with the `f9t` profile:
  - GPS `L1CA + L2CL`
  - Galileo `E1C + E5bQ`
  - BeiDou `B1I + B2I`
- E810 PPS timestamps are available from the PHC EXTS path

Known issue — E810 AQ I2C bandwidth limit:

- `/dev/gnss0` is a kernel GNSS char device backed by the `ice` driver's
  I2C polling thread.  Each I2C read goes through the E810 Admin Queue
  (AQ), which limits data payload to **15 bytes per command** (hardware
  constraint: `ICE_AQC_I2C_DATA_SIZE_M` = `GENMASK(3,0)`, a 4-bit
  field).  This cannot be changed without a hardware revision.
- Each AQ I2C command takes ~2.8 ms, giving a burst throughput of
  ~5.3 kB/s but an effective sustained rate of **~1.5-1.7 kB/s** after
  polling gaps and AQ overhead.
- The F9T generates ~1.5 kB/s for RAWX+TIM-TP alone, and ~2.2 kB/s with
  SFRBX+PVT+NAV-SAT.  With all messages enabled, the I2C bus is
  oversubscribed by ~2x.  The F9T's internal I2C buffer overflows and
  **~25-35% of RAWX epochs are lost**.
- The observation queue falls persistently behind PPS, with
  `epoch_offset` growing to 8-9 and `match_recv_dt` hitting the 11s
  correlation window limit.

Fix — minimal I2C message set:

- Disable SFRBX, NAV-PVT, and NAV-SAT on the I2C port (port 0).
  Keep only RXM-RAWX and TIM-TP.  This reduces F9T I2C output to
  ~1.0-1.4 kB/s, within the bus capacity.
- Broadcast ephemeris comes from NTRIP (BCEP00BKG0 mount), not SFRBX.
  See "SFRBX on E810" section below.
- With the minimal message set on the **stock in-kernel ice driver**:
  RAWX delivery is ~0.87 Hz (vs 0.60 Hz with all messages), epoch_offset
  stays at 0-1, correlation window is comfortable, writes succeed.
- **No custom driver patch is needed.**  The stock driver's page-batched
  delivery with 100 ms post-delivery delay is adequate when total I2C
  output fits within bus capacity.  A streaming delivery patch
  (`drivers/ice-gnss-streaming/`) is available for experimentation but
  is not required or recommended for production use.

SFRBX on E810:

- RXM-SFRBX provides broadcast ephemeris decoded locally from the GNSS
  signal.  On serial-connected F9T (TimeHat), SFRBX is enabled by
  default — USB serial has ample bandwidth.
- On E810 I2C, SFRBX consumes ~400 B/s (25% of bus capacity) and is
  **redundant** with NTRIP-sourced ephemeris.  It is disabled by default
  on the E810 platform via the `sfrbx_on_gnss_port` config option.
- SFRBX IS needed for the NTRIP caster use case (encoding local ephemeris
  as RTCM 1019/1042/1046 for peer bootstrap).  The caster runs on PiPuss
  (serial transport), not on E810.
- SFRBX is NOT needed for PPP-AR.  PPP-AR requires broadcast ephemeris
  (from NTRIP) plus SSR phase biases (not currently available from our
  SSR source).  SFRBX is just one source of broadcast ephemeris.

### `timehat` / i226

Working:

- GNSS path over `/dev/gnss-top -> /dev/ttyACM0`
- i226 PHC correctly identified on `/dev/ptp0`
- corrected PTP capability parsing now reports:
  - `n_extts=2`
  - `n_pins=4`
- receiver support added for a distinct `f9t-l5` profile
- live dual-frequency observations work with:
  - GPS `L1CA + L5Q`
  - Galileo `E1C + E5aQ`
  - BeiDou `B1I + B2aI`
- PPS input can be restored after a power cycle using the documented TimeHAT
  `testptp` SDP setup commands
- the unified path now runs end-to-end on `timehat` when:
  - `/dev/ttyACM0` is not already owned by `satpulse@ttyACM0.service`
  - the TimeHAT SDP pins have been restored after a power cycle
  - `--receiver f9t-l5` is used
  - the PHC timescale is treated as `tai`

Known issue — igc adjfine TX timestamp race:

- The igc driver has a race between `adjfine()` (PHC frequency adjustment)
  and hardware TX timestamping.  `igc_ptp_adjfine_i225()` writes the
  `IGC_TIMINCA` register without any lock, which can corrupt an in-flight
  TX timestamp, causing "Tx timestamp timeout" in dmesg and eventually
  breaking EXTTS (PPS capture).
- This is triggered by any PHC discipline software (peppar-fix, SatPulse)
  running concurrently with ptp4l using `time_stamping hardware`.
- At `logSyncInterval -7` (128 Hz), MTBF is ~30 minutes.
  At `logSyncInterval 0` (1 Hz), MTBF is ~64 hours.
- **Fix**: a patch in `drivers/igc-adjfine-fix/` serializes TIMINCA writes
  with pending TX timestamps.  Applied via DKMS on TimeHat.
  See `drivers/igc-adjfine-fix/README.md` for details.
- **Recovery** (without patch): `sudo rmmod igc && sudo modprobe igc`
  followed by SDP pin restore (`testptp -L 0,2 && testptp -L 1,1` or
  equivalent `set_pin_function()` calls).
- **Upstream**: bug is present in Linux master as of March 2026.
  Reproducer: `tools/igc_tx_timeout_repro.py`.

Current caveats:

- `satpulse@ttyACM0.service` will occupy `/dev/ttyACM0` and prevent
  `PePPAR-Fix` from opening the receiver unless it is stopped first
- the current `i226` profile should default to `tai` for PTP-GM-style use
  cases, and the live `timehat` testing supported that choice
- the strict sink gate still had to defer and drop some early epochs before
  settling:
  - `consumed_correlated = 34`
  - `deferred_waiting = 12`
  - `dropped_outside_window = 1`
  - `dropped_unmatched = 1`
- a later bug was found in the servo step path:
  - PPS history captured before a PHC step was being reused after the step
  - that caused `timehat` to keep matching against stale pre-step PPS events
    and hold the wrong whole-second offset
  - clearing PPS history after each PHC step fixed that failure mode

Latest validated result on `timehat` after the PPS-history purge:

- a patched 5-minute run stayed at `epoch_offset = 0` for all `83` logged
  epochs
- self-reported `pps_error_ns` TDEV improved from about `176 ms` at `τ = 1s`
  in the bad run to about `18.4 ms` at `τ = 1s`
- the path is still not “good,” but it is no longer obviously broken at the
  whole-second level

Later tuning result:

- the original `i226` tracking gains (`kp=0.3`, `ki=0.1`) were too aggressive
  for sustained `timehat` runs and could drive the servo into rail-hitting
  behavior
- lowering the default `i226` profile to `kp=0.05`, `ki=0.01` materially
  improved steady-state tracking
- in a 120-second low-gain run, the tracking-only rows held `epoch_offset = 0`
  and showed self-reported `pps_error_ns` TDEV of roughly:
  - `140 ns` at `τ = 1s`
  - `551 ns` at `τ = 2s`
  - `3.57 us` at `τ = 5s`

Wiring and board behavior:

- earlier, no PPS events were detected until after a full power cycle plus the
  documented SDP recovery commands
- wiring was later confirmed unchanged in the lab:
  - F9T PPS OUT goes to the TimeHAT v5 PPS IN SMA
  - the same PPS also reaches TICC #1 chB
  - TICC trigger indication is present there
  - TimeHAT PPS OUT to TICC #3 chA does not currently show activity

This means the earlier “no PPS on TimeHAT” conclusion is obsolete. The real
issue was board/driver state after power-up, not missing upstream PPS wiring.

Later lab finding:

- after a full power cycle, `timehat` may come back with the i226 SDP state in
  a bad configuration even when the PPS wiring is physically correct
- the TimeHAT project page points to the recovery sequence under `Testing PPS`
- the two documented commands are:
  - `sudo testptp -d /dev/ptp0 -L 0,2`
  - `sudo testptp -d /dev/ptp0 -L 1,1`

Interpretation:

- `pin 0` is configured for PPS output (`perout`)
- `pin 1` is configured for PPS input (`extts`)

This is important because the earlier conclusion that `timehat` had no PPS
input was made before this power-cycle-specific SDP recovery behavior was
known.

Additional operational note:

- on the tested `timehat` host, `satpulse@ttyACM0.service` also grabs the same
  GNSS serial device used by `PePPAR-Fix`
- for direct PePPAR-Fix testing, that service must be inactive so `/dev/gnss-top`
  can be opened exclusively

## TICC move status

As of `2026-03-22`, `TICC #3` has been moved to `ocxo`.

Current verified state on `ocxo`:

- udev naming works:
  - `/dev/ticc3 -> /dev/ttyACM0`
  - `ID_SERIAL_SHORT=44236313835351B0A091`
- `bob` has been added to the `dialout` group on `ocxo`
- the lab-wide udev rule has been installed at:
  - `/etc/udev/rules.d/99-timelab.rules`
- `TICC #3 chA` is wired to the E810 upper SMA on `ocxo`

Current unverified state after the move:

- live PPS timestamps on `TICC #3 chA/chB`
- `TICC #3 chB` cabling after the move

A boot-aware probe of `/dev/ticc3` on `ocxo` completed with zero timestamp
events, so the device is present and named correctly but the post-move PPS
wiring has not yet been confirmed by measurement.

Current state:

- E810 PPS input (EXTTS) works with the in-tree ice driver.  The GNSS pin
  captures the onboard F9T's 1PPS at channel 0.
- E810 PPS output (PEROUT) on SMA1 is enabled by the PHC bootstrap via
  sysfs pin programming.  The ice driver rejects `PTP_PIN_SETFUNC` ioctl
  but accepts writes to `/sys/class/ptp/ptpN/pins/SMA1`.
- **udev rule required**: the sysfs pin files must be writable by the
  `dialout` group.  Deploy `99-ptp-pins.rules`:
  ```
  SUBSYSTEM=="ptp", ACTION=="add", RUN+="/bin/chmod -R g+w /sys/class/ptp/%k/pins/"
  SUBSYSTEM=="ptp", ACTION=="add", RUN+="/bin/chgrp -R dialout /sys/class/ptp/%k/pins/"
  ```
  Without this rule, PEROUT enable succeeds but the signal doesn't reach
  the physical SMA connector (pin stays at function=NONE).
- TICC #3 chA is wired to SMA1 (upper bracket connector) and records
  disciplined PHC PPS timestamps at 1 Hz.
- The F9T PPS is internal to the E810 PCB and **not accessible externally**
  without soldering to a test point.  TICC can only observe the disciplined
  PHC PEROUT, not the raw F9T PPS.

## GNSS transport differences

### `ocxo`

GNSS arrives through the Linux kernel GNSS device:

- path: `/dev/gnss0`
- wrapper: [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py)

Important properties:

- not a conventional serial port
- host config for this path should omit `baud`
- host config should use `ubx_port = "..."` to mean the F9T logical output
  port being configured, not the Linux device type
- returns short reads and packet bursts
- may include queued startup data
- requires local buffering to avoid losing the remainder of a kernel packet while `pyubx2` is scanning one byte at a time

We added packet-level receive timestamps in the kernel wrapper because parse-time timestamps were not precise enough once bursts were involved.

Empirical F9T logical-port result on `2026-03-23`:

- `scripts/peppar_rx_config.py` was run directly against `/dev/gnss0` with
  `--port-type UART`, `UART2`, `USB`, and `SPI`
- all four configurations were accepted by the receiver
- after each configuration, the live `/dev/gnss0` verify stream still showed
  only:
  - `RXM-RAWX`
  - `NAV-PVT`
- `RXM-SFRBX` and `TIM-TP` did not reappear in the post-config verify stream
  for any tested logical port

Interpretation:

- this does not prove which F9T logical port the E810 kernel path corresponds
  to
- it does prove that “Linux sees a char device” and “the F9T logical message
  port is USB” are separate questions
- current evidence points to a platform-specific limitation or translation
  layer in the in-tree E810 GNSS path, rather than a simple wrong-port choice
- explicit `UART2` testing did not change the result, so this is not just a
  forgotten second UART case

### `timehat`

GNSS arrives through a standard USB serial device:

- path: `/dev/gnss-top`
- backing device: `/dev/ttyACM0`

Important properties:

- behaves much more like an ordinary serial stream
- did not show the same burst-delivery pathology as `/dev/gnss0`
- one early failure mode was simply another process holding the serial port open

## Receiver profile differences

One `F9TDriver` was not sufficient.

### `ocxo` profile

The `ocxo` receiver behaves like an L1/L2/E5b/B2I timing profile:

- GPS `L1CA + L2CL`
- Galileo `E1C + E5bQ`
- BeiDou `B1I + B2I`

This is represented by the `f9t` path in [`scripts/peppar_fix/receiver.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/receiver.py).

### `timehat` profile

The `timehat` receiver behaves like an L1/L5/E5a/B2a profile:

- GPS `L1CA + L5Q`
- Galileo `E1C + E5aQ`
- BeiDou `B1I + B2aI`

This is represented by the `f9t-l5` path in [`scripts/peppar_fix/receiver.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/receiver.py).

## PHC differences

### E810 on `ocxo`

Observed and code-relevant properties:

- PHC device: `/dev/ptp1`
- EXTS channels available
- pin programming is not required for the current working path
- current code uses the `e810` profile and implicit EXTS mapping
- physical connector notes from lab inspection:
  - two external SMA connectors, vertically stacked on the bracket when the
    card is mounted in a horizontal motherboard with slot openings facing up
  - upper connector is externally marked `A`
  - lower connector is externally marked `B`
  - on the PCB they are labeled `J13` and `J14`
  - nearby are two `u.FL` timing connectors marked `RX` and `TX` with
    `TIME PULSE` silk nearby

Interpretation of the nearby `u.FL` connectors:

- Intel documents the E810-XXVDA4T as exposing four external 1PPS timing
  connectors: `SMA1`, `SMA2`, `U.FL1`, and `U.FL2`
- Intel also documents the `u.FL` pair as dedicated send/receive timing ports,
  not generic RF connectors
- in practice, that matches the board silk:
  - `RX` is the 1PPS timing input side
  - `TX` is the 1PPS timing output side
- source: Intel E810-XXVDA4T user guide summary indexed by ManualsLib:
  <https://www.manualslib.com/manual/2991401/Intel-E810-Vda4t-Series.html>

Current practical behavior:

- PPS capture works
- PHC can be disciplined
- PHC should be treated as `tai` when used as a PTP-facing clock
- PPS output on the SMA connectors is not currently active through the in-tree
  `ice` path we are using

Still unverified from software:

- exact mapping between external bracket labels `A/B` and Intel logical names
  `SMA1/SMA2`
- exact role of nearby headers `J21`, `J20`, `J9`, and `J34`
  - one of these may expose a lower-latency serial/UART path to the onboard
    F9T, but that has not yet been confirmed from documentation or probing

### i226 on `timehat`

Observed and code-relevant properties:

- PHC device: `/dev/ptp0`
- explicit pin/channel programming matters
- SDP wiring must be known

Current practical behavior:

- software sees a valid PHC
- no PPS reaches the PHC yet

## Host-based timestamps available today

### What we do have

- PHC timestamp for each PPS edge from `PTP_EXTTS_EVENT`
  - exposed by [`scripts/peppar_fix/ptp_device.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/ptp_device.py)
  - fields: `sec`, `nsec`, `index`
- local monotonic receive timestamp when userspace reads that EXTS event
  - captured in [`scripts/peppar_fix_engine.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_engine.py) as `PpsEvent.recv_mono`
- local monotonic receive timestamp for each GNSS observation event
  - captured in [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py) as `ObservationEvent.recv_mono`

### What we do not have

We do not currently get a separate host-provided measurement of the phase relationship between:

- the host CPU reading the PPS event, and
- the PHC timestamp inside the EXTS event

The PHC timestamp is the authoritative PPS timestamp. The host monotonic timestamp only tells us when userspace observed the event, which is useful for cross-stream correlation and queue analysis but not as a replacement for the PHC measurement itself.

### GNSS quantization aid

From the F9T we also get `TIM-TP.qErr`:

- stored by [`realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)
- consumed through `QErrStore`

This is not a host timestamp. It is a receiver-originated PPS quantization/error term associated with the GNSS timing output.

## Practical support guidance

When adding a new platform, assume all of the following may vary independently:

- GNSS transport: kernel char device vs USB serial vs something else
- PHC device path
- PHC pin model: implicit EXTS mapping vs programmable pins
- receiver signal family: `L1/L2/E5b/B2I` vs `L1/L5/E5a/B2a`
- PPS routing
- burst/queue behavior of the GNSS source

The repo should treat these as platform configuration, not as incidental quirks.

## Recommended next steps

- keep `e810` and `i226` as explicit PTP profiles
- keep `f9t` and `f9t-l5` as explicit receiver profiles
- do not assume `/dev/gnss0` and `/dev/ttyACM0` have comparable buffering behavior
- treat `timehat` PPS routing as a hardware bring-up task
- treat `ocxo` `/dev/gnss0` burst delivery as a platform limitation that may require a different ingest path or more explicit timestamping upstream

## Bring-up checklist for the next platform

- [ ] Identify the PHC device path and verify reported capabilities
- [ ] Verify whether EXTS requires explicit pin programming or implicit channel mapping
- [ ] Verify PPS capture independently before attempting servo work
- [ ] Probe the GNSS stream directly for delivery cadence and startup backlog
- [ ] Identify the actual F9T signal family before reusing an existing receiver profile
- [ ] Verify whether the GNSS path is kernel-char-device based or ordinary serial
- [ ] Confirm which timescale the PHC should represent for the intended consumer
- [ ] Record whether host-side monotonic timestamps are sufficient for correlation or whether a deeper kernel timestamp is needed
