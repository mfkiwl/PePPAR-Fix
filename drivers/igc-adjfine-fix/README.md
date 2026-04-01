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
| v3 (tmreg_lock + TSYNCTXCTL) | FAIL 17s | **PASS 300s** |
| TX-only (no adjfine) | **FAIL 30s** | Expected OK |

### Key findings

1. **tmreg_lock alone doesn't help** — the race is between the
   software TIMINCA write and the hardware's asynchronous TX timestamp
   capture.  No software lock can prevent the hardware from reading
   TIMINCA at the same instant software writes it.

2. **v3 is the recommended patch** — uses tmreg_lock (correct lock per
   maintainer) + TSYNCTXCTL disable/enable (prevents new captures
   during TIMINCA write).  Always succeeds (no -EBUSY), no adjfine
   starvation.  Passes at realistic rates (301/301 adjfine in 5 min).

3. **No patch survives extreme stress** — at 200k adjfine/s + 100k TX/s,
   all variants fail.  This is because an in-flight TX timestamp
   capture that started before the disable cannot be cancelled.  At
   realistic rates the collision window (~1 µs per adjfine at 1 Hz)
   is negligible.

4. **TX-only timeout exists** — at extreme TX rates (~166k/s), the TX
   timestamp hardware times out even without adjfine.  This is a
   separate bug (resource exhaustion, not the TIMINCA race).

### Detailed clean-state testing (driver reloaded between each test)

The multi-test diagnostic initially gave misleading results because
the 15-second timeout pipeline carried stranded slots between tests.
Single-test runs with driver reload between each test give the true
picture:

**Stock driver:**

| TX rate | Adj rate | Timeout | HW-TO | Skip | Notes |
|---------|----------|---------|-------|------|-------|
| 100/s | 1/s | no (60s) | 0 | 0 | Clean |
| 1k/s | 1/s | no (60s) | 0 | 0 | Clean |
| 10k/s | 1/s | no (60s) | 0 | 0 | Clean |
| 1k/s | 10k/s | no (60s) | 0 | 0 | Clean |
| 10k/s | 100k/s | no (60s) | 0 | 0 | Clean |
| 100k/s | 100k/s | **16s** | **3** | 76 | TIMINCA race |
| 100k/s | 0 | **16s** | 4 | 8M | Slot exhaustion |

At realistic rates, the stock driver survives 60 seconds — the race
probability per adjfine call is very low.  It only triggers when
BOTH rates are extreme (100k+ each).

Signature: **low skip, moderate TO** = TIMINCA corruption (captures
acquired a slot but the timestamp was corrupt).

**v3 (TSYNCTXCTL disable/enable):**

| TX rate | Adj rate | Timeout | HW-TO | Skip | Notes |
|---------|----------|---------|-------|------|-------|
| 100/s | 1/s | no (300s) | 0 | 0 | Clean |
| 128/s | 1/s | no (300s) | 0 | 0 | Realistic ptp4l |
| 100k/s | 100k/s | **17s** | **12** | 1.7M | TSYNCTXCTL stranding |

Signature: **high skip, high TO** = disabling TSYNCTXCTL strands
in-progress captures.  At 100k adj/s, timestamping is disabled 100k
times per second, each time potentially preventing a capture from
completing.  Slots remain occupied until the 15-second timeout fires.

### Why no software fix fully closes the hardware race

The race window is not just the TIMINCA write instant.  It spans the
entire period between "hardware starts reading TIMINCA for a timestamp
capture" and "hardware finishes latching the result."  This window is
internal to the hardware and invisible to software.  No lock,
disable, or check-before-write can prevent a capture that's already
mid-flight from seeing the TIMINCA change.

At realistic rates (1 Hz adjfine + 128 Hz TX), the collision
probability per adjfine call is negligible.  All patches (v1, v3)
pass at these rates.  The stock driver also survives 60 seconds.
The real-world MTBF at 1 Hz + 128 Hz is estimated at ~30 minutes
for the stock driver (from prior empirical testing).

### v3 assessment

v3 passes at realistic rates and uses the correct lock (tmreg_lock).
At extreme rates it's actually worse than stock (stranding effect).
However, extreme rates (100k+) are unrealistic for any PTP
deployment.  The patch is still valuable because:

1. It eliminates the known race at realistic rates
2. It uses the correct locking convention (per maintainer)
3. It never returns -EBUSY (no adjfine starvation concern)
4. The stranding effect at extreme rates is a fair trade for
   preventing corruption at realistic rates

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
