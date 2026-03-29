# igc adjfine TX Timestamp Race Fix

## Bug

The igc driver (Intel i225/i226) has a race between `clock_adjtime
(ADJ_FREQUENCY)` and hardware TX timestamping.  `igc_ptp_adjfine_i225()`
writes `IGC_TIMINCA` without holding any lock, while the TX timestamp
hardware is asynchronously capturing timestamps using the increment rate
from that same register.  When the rate changes mid-capture, the
timestamp is corrupt and the driver times out waiting for a valid value.

After `IGC_PTP_TX_TIMEOUT` (15 seconds), the driver logs:
```
igc 0001:01:00.0 eth1: Tx timestamp timeout
```

Repeated timeouts wedge the EXTTS subsystem, breaking PPS capture.

## Trigger

Any combination of PHC frequency discipline + PTP hardware timestamping:
- peppar-fix (or SatPulse, or any GPSDO) calling `adjfine()` at 1 Hz
- ptp4l sending sync packets with `time_stamping hardware`

Higher sync rates increase collision probability:
- `logSyncInterval -7` (128 Hz): ~30 minute MTBF
- `logSyncInterval 0` (1 Hz): ~64 hour MTBF (estimated)

## Reproducer

```bash
sudo python3 tools/igc_tx_timeout_repro.py eth1 /dev/ptp0 30
# Watch: dmesg -w | grep "Tx timestamp"
```

Triggers in ~17 seconds on unpatched driver.

## Fix

Patch: `0001-igc-serialize-adjfine-with-tx-timestamps.patch`

Hold `ptp_tx_lock` and skip the TIMINCA write if any TX timestamps are
pending (`tx_tstamp[i].skb != NULL`).  Return `-EBUSY` so the PTP
subsystem retries.

This doesn't fully close the race (a new TX timestamp can start between
the check and the write), but under realistic rates the residual
probability gives ~25 year MTBF.

## Applying to Intel's out-of-tree igc driver (DKMS)

```bash
cd /usr/src/igc-*/src
# Apply patch manually to igc_ptp.c (see patch file)
sudo dkms build igc/<version> --force
sudo dkms install igc/<version> --force
sudo rmmod igc && sudo modprobe igc
```

## Affected systems

- Intel i225/i226 NICs (igc driver)
- Any Linux kernel through at least 6.12 (bug present in upstream master)
- Triggered by any PTP stack + PHC discipline running concurrently

## Upstream status

Not yet submitted.  Upstream `igc_ptp_adjfine_i225` in
`drivers/net/ethernet/intel/igc/igc_ptp.c` has no locking as of
Linux 6.12+.
