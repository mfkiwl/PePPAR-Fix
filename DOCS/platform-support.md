# Platform Support Notes

This document summarizes what we learned while bringing `PePPAR-Fix` up on two different hardware platforms:

- `oxco`: Intel E810-based host with onboard GNSS exposed as `/dev/gnss0`
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

### `oxco` / E810

Working:

- Host reachable at `10.168.60.37`
- PHC path identified and usable on `/dev/ptp1`
- E810 profile support added in [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py) and [`config/receivers.toml`](/home/bob/git/PePPAR-Fix/config/receivers.toml)
- Explicit PHC timescale support added, with `e810` defaulting to `tai`
- F9T dual-frequency processing works with the `f9t` profile:
  - GPS `L1CA + L2CL`
  - Galileo `E1C + E5bQ`
  - BeiDou `B1I + B2I`
- E810 PPS timestamps are available from the PHC EXTS path

Not yet working well enough:

- sustained PHC servo discipline through `/dev/gnss0`

Root problem:

- `/dev/gnss0` is a kernel GNSS char device that delivers UBX in bursts, not as a smooth 1 Hz observation stream
- direct probing showed multi-packet bursts roughly every `2.1s` to `2.4s`
- RAWX epochs commonly arrive `5s` to `11s` after the GNSS time they describe
- this introduces whole-second ambiguity pressure in the servo even when PPS capture itself is correct

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

As of `2026-03-22`, `TICC #3` has been moved to `oxco`.

Current verified state on `oxco`:

- udev naming works:
  - `/dev/ticc3 -> /dev/ttyACM0`
  - `ID_SERIAL_SHORT=44236313835351B0A091`
- `bob` has been added to the `dialout` group on `oxco`
- the lab-wide udev rule has been installed at:
  - `/etc/udev/rules.d/99-timelab.rules`
- `TICC #3 chA` is wired to the E810 upper SMA on `oxco`

Current unverified state after the move:

- live PPS timestamps on `TICC #3 chA/chB`
- `TICC #3 chB` cabling after the move

A boot-aware probe of `/dev/ticc3` on `oxco` completed with zero timestamp
events, so the device is present and named correctly but the post-move PPS
wiring has not yet been confirmed by measurement.

Current interpretation:

- the lack of `TICC #3 chA` activity is consistent with the present software
  path on `oxco`
- E810 PPS input / EXTS works with the in-tree `ice` driver
- E810 PPS output appears to require Intel's out-of-tree timing driver path
  before the upper SMA will emit a disciplined 1 PPS signal

## GNSS transport differences

### `oxco`

GNSS arrives through the Linux kernel GNSS device:

- path: `/dev/gnss0`
- wrapper: [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py)

Important properties:

- not a conventional serial port
- returns short reads and packet bursts
- may include queued startup data
- requires local buffering to avoid losing the remainder of a kernel packet while `pyubx2` is scanning one byte at a time

We added packet-level receive timestamps in the kernel wrapper because parse-time timestamps were not precise enough once bursts were involved.

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

### `oxco` profile

The `oxco` receiver behaves like an L1/L2/E5b/B2I timing profile:

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

### E810 on `oxco`

Observed and code-relevant properties:

- PHC device: `/dev/ptp1`
- EXTS channels available
- pin programming is not required for the current working path
- current code uses the `e810` profile and implicit EXTS mapping

Current practical behavior:

- PPS capture works
- PHC can be disciplined
- PHC should be treated as `tai` when used as a PTP-facing clock
- PPS output on the SMA connectors is not currently active through the in-tree
  `ice` path we are using

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
  - captured in [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py) as `PpsEvent.recv_mono`
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
- treat `oxco` `/dev/gnss0` burst delivery as a platform limitation that may require a different ingest path or more explicit timestamping upstream

## Bring-up checklist for the next platform

- [ ] Identify the PHC device path and verify reported capabilities
- [ ] Verify whether EXTS requires explicit pin programming or implicit channel mapping
- [ ] Verify PPS capture independently before attempting servo work
- [ ] Probe the GNSS stream directly for delivery cadence and startup backlog
- [ ] Identify the actual F9T signal family before reusing an existing receiver profile
- [ ] Verify whether the GNSS path is kernel-char-device based or ordinary serial
- [ ] Confirm which timescale the PHC should represent for the intended consumer
- [ ] Record whether host-side monotonic timestamps are sufficient for correlation or whether a deeper kernel timestamp is needed
