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

Triggers in ~17–22 seconds on unpatched driver (at extreme rates).

**Note**: The stress reproducer (200k adjfine/s + 100k TX/s) also
reveals a **separate TX-only timeout** at extreme TX rates (~166k/s),
even without adjfine.  This is a different bug (TX timestamp hardware
overload, not the adjfine race).  See "TX-only timeout" below.

## Patches

### v1: ptp_tx_lock + EBUSY (`0001-igc-serialize-adjfine-with-tx-timestamps.patch`)

Hold `ptp_tx_lock` and skip the TIMINCA write if any TX timestamps are
pending (`tx_tstamp[i].skb != NULL`).  Return `-EBUSY` so the PTP
subsystem retries.

**Stress test**: fails (EBUSY starves adjfine when TX rate is extreme).
**Realistic rates** (1 Hz adjfine + 128 Hz TX): **passes** — zero EBUSY
in 5 minutes, all 301 adjfine calls succeed.  At 128 Hz TX, the
probability of a pending TX timestamp during the 1 Hz adjfine is
negligible.

### v2: tmreg_lock only (`0002-igc-use-tmreg_lock-for-adjfine.patch`)

Hold `tmreg_lock` around the TIMINCA write, consistent with all other
timing register accesses in igc_ptp.c.  No TX-pending check.

**Result**: does NOT fix the bug.  Fails in 17 seconds under stress,
same as stock.  `tmreg_lock` serializes software register accesses but
cannot prevent the hardware's asynchronous TX timestamp capture from
reading TIMINCA at the instant software writes it.

This was tested per kernel maintainer feedback suggesting `tmreg_lock`
instead of `ptp_tx_lock`.  The experiment confirms the race is between
software and hardware, not between two software threads.

## Experimental results (2026-04-01)

| Variant | Stress (200k+100k/s) | Realistic (1+128 Hz) |
|---------|---------------------|----------------------|
| Stock (no lock) | **FAIL 22s** | FAIL ~30 min |
| v1 (ptp_tx_lock + EBUSY) | FAIL 16s (EBUSY) | **PASS 300s** |
| v2 (tmreg_lock only) | **FAIL 17s** | Not tested (same as stock) |
| TX-only (no adjfine) | **FAIL 30s** | Expected OK |

### Key findings

1. **tmreg_lock alone doesn't help** — the race is between the
   software TIMINCA write and the hardware's asynchronous TX timestamp
   capture.  No software lock can prevent the hardware from reading
   TIMINCA at the same instant software writes it.

2. **v1's EBUSY is fine at realistic rates** — at 1 Hz adjfine + 128 Hz
   TX, there's never a pending TX timestamp when adjfine runs (zero
   EBUSY in 5 minutes).  The maintainer's concern about adjfine
   starvation is valid in theory but doesn't manifest in practice.

3. **TX-only timeout exists** — at extreme TX rates (~166k/s), the TX
   timestamp hardware times out even without adjfine.  This is a
   separate bug (resource exhaustion, not the TIMINCA race).

### Possible v3 approaches

A complete fix would need to prevent the hardware from starting a new
TX timestamp capture during the TIMINCA write:

- **Disable TX timestamping** via TSYNCTXCTL around the TIMINCA write
  (hold tmreg_lock, clear TSYNCTXCTL.TXTS_EN, write TIMINCA, restore
  TSYNCTXCTL).  This is the cleanest hardware-level fix but may drop
  one TX timestamp.

- **Combined lock + EBUSY** — hold tmreg_lock (for register
  consistency) AND check TX pending under ptp_tx_lock (for hardware
  race).  Same EBUSY limitation as v1 at extreme rates.

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

Submitted to intel-wired-lan list.  Maintainer feedback: use
`tmreg_lock` instead of `ptp_tx_lock`.  Testing shows tmreg_lock alone
is insufficient (see experiments above).  Discussion ongoing.
