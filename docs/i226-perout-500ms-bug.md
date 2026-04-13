# i226 PEROUT 500ms Phase Bug

## Summary

The Intel i226 (igc driver) PEROUT can fire at 500ms into the PHC
second instead of at the top of the second.  This is a **hardware-level
issue** — confirmed with both peppar-fix and SatPulse (jclark/satpulse)
on the same boards.  Some i226 boards consistently land on the wrong
half-period regardless of:

- `start_nsec = 0` in the PEROUT request
- Double-programming (disable → enable → sleep 2s → disable → enable)
- Kernel version (tested 6.12.62 and 6.12.75 with identical DKMS)
- Full power cycle of the host

## Affected and unaffected hosts (2026-04-11)

| Host | Board | Kernel | PEROUT phase | Notes |
|---|---|---|---|---|
| TimeHat | TimeHAT v5 | 6.12.62+rpt-rpi-2712 | Correct (0 ms) | Reference |
| MadHat | TimeHAT v5 | 6.12.62+rpt-rpi-2712 | Wrong (500 ms) | Same kernel, same DKMS binary |
| MadHat | TimeHAT v5 | 6.12.75+rpt-rpi-2712 | Wrong (500 ms) | Also tested |
| ocxo | i226 PCIe | 6.8.0-107-generic (x86) | Wrong (500 ms) | Different arch, different DKMS |

TimeHat and MadHat have the same TimeHAT v5 boards (purchased months
apart), the same DKMS source (`igc-6.12.0-ppsfix.1`), and on 6.12.62
the same igc module binary (identical md5sum).  MadHat still gets
500ms.  The difference is in the hardware.

## Root causes

There are two independent issues that can produce the 500ms offset:

### 1. Stock igc frequency mode (kernel-level, fixable)

The stock igc driver intercepts 1 Hz PEROUT requests and routes them
into **frequency mode** (`IGC_TSAUXC_EN_CLK0`), which produces a
free-running square wave with no relationship to the PHC second
boundary.  The `start_nsec` field is ignored.

**Fix**: The TimeHAT DKMS patch (`igc-6.12.0-ppsfix.1` from
Time-Appliances-Project/TimeHAT) removes `500000000` from the
`use_freq` condition, forcing 1 Hz into **Target Time mode**
(`IGC_TSAUXC_EN_TT0`).  In TT mode, the output aligns to the
programmed start time.

**Alternative**: Set `period_ns = 999_999_999` (1 ns off) to dodge
the exact-match condition.  Our `enable_perout()` does this
automatically on stock igc (detected via `_stock_igc_freq_mode_
workaround_needed()`).

### 2. Hardware Target Time half-period latch (hardware, no known fix)

Even in Target Time mode with the DKMS patch, some i226 boards
consistently latch PEROUT onto the 500ms half-period.  This survives:

- All start_nsec values (0, 500_000_000, arbitrary)
- Double-programming with 2-3 second waits
- Full host power cycle
- Kernel version changes
- SatPulse's programming (same `Round(time.Second).Add(2s)` approach)

This appears to be a per-board hardware difference — possibly related
to the i226's internal PLL or Target Time comparator.  The same
TimeHAT v5 design works correctly on one Pi 5 (TimeHat) and fails
on another (MadHat).

## SatPulse comparison (2026-04-11)

SatPulse (github.com/jclark/satpulse) was built and tested on both
MadHat and ocxo.  Its `PeroutEnable` function uses:

```go
startTime := now.Round(time.Second).Add(2 * time.Second)
// start.nsec is always 0
```

This is functionally identical to our approach.  SatPulse showed the
same 500ms offset on both hosts, confirming the issue is below
userspace.

## Detection

Check the TICC serial output.  In correct operation, chA (DO PPS)
and chB (GNSS PPS) timestamps are within microseconds:

```
183188.023798216560 chB
183188.023798229839 chA   ← 13 ns apart (correct)
```

At 500ms offset:

```
265824.482265513607 chB
265824.982263544955 chA   ← 500 ms apart (wrong)
```

Also checkable via `phc_ctl`:

```bash
sudo phc_ctl /dev/ptp0 cmp
```

Expected offset from CLOCK_REALTIME is ~37 billion ns (TAI-UTC = 37s).
If the PHC itself is 500ms off, the offset will be ~36.5B or ~37.5B.
But the PHC can be correct while PEROUT is still 500ms off (the bug
is in the PEROUT hardware, not the PHC clock).

## Root cause identified (2026-04-12)

The igc hardware always uses 50% duty cycle.  The `start` field in
`PTP_PEROUT_REQUEST` specifies the **falling edge** (start of LOW),
not the rising edge.  Setting `start_nsec = 0` puts the falling edge
at the top of the second and the rising edge at 500ms — exactly the
offset we observed on every igc host.

SatPulse (jclark/satpulse) handles this correctly in
`internal/sdpcmd/perout.go`:

```go
case "igb", "igc":
    // igb and igc always use 50% duty cycle
    // The start in PTP_PEROUT_ENABLE determines the start of the
    // *low* part i.e. the falling edge.
    // But we want the rising edge to be aligned with the start of
    // the second.
    startOff = cfg.Perout.Period / 2
```

Testing confirmed: SatPulse's PEROUT produced correct alignment on
MadHat (23 µs offset vs 500ms before).

## Fix

Set `start_nsec = period_ns // 2` (500_000_000 for 1Hz) on igc so
the falling edge starts at 500ms and the rising edge aligns with the
top of the PHC second.  This affects only the PEROUT output timing,
not the PHC clock value — PTP slaves still read correct TAI.

Implemented in `scripts/peppar_fix/ptp_device.py:enable_perout()` via
`_is_igc_driver()` detection.  Verified on MadHat: TICC errors
dropped from 500ms to sub-4ns immediately after the fix.

This explains **all** observed 500ms offsets across all igc hosts.
The "hardware Target Time half-period latch" hypothesis (root cause
#2 above) was wrong — the issue was simply that `start_nsec = 0`
means falling edge at 0, rising edge at 500ms on igc.

TimeHat previously appeared immune because its DKMS patch may have
altered the polarity behavior, or because a prior SatPulse test left
the correct PEROUT state.  With the fix applied, all igc hosts should
behave consistently.

## Pre-flight check

Added to `docs/lab-operations.md`:

1. Verify system time daemon is locked (NTP/chrony)
2. Check PHC vs system time: `sudo phc_ctl /dev/ptp0 cmp`
3. After bootstrap: verify TICC chA-chB spacing (read 10 lines
   from TICC serial port — the spacing should be microseconds,
   not 500ms)

## References

- u-blox F9 TIM 2.20 Interface Description (UBX-21048598 R01), §3.19.3
- Time-Appliances-Project/TimeHAT: DKMS patch for igc
- SatPulse (jclark/satpulse): `time/phc/phc.go:PeroutEnable()`
- Linux kernel `igc_ptp.c`: `igc_ptp_feature_enable_i225()`
