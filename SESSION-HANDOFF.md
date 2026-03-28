# Session Handoff — 2026-03-28 (overnight)

Driver write-yield fix, gnss_stream bug fix, 4-hour TDEV runs started.

## What was accomplished

### Ice GNSS streaming patch: I2C write yield
- Added `usleep_range(1000, 2000)` every 8 I2C read chunks in the read
  work function to yield the admin queue to writes
- Without this, the ~54 back-to-back reads per epoch starved
  `ice_gnss_write()`, making UBX CFG-VALSET fail on E810
- Tested: 10/10 writes succeed, streaming reads unaffected (~7ms added
  latency per epoch)
- Patch rebuilt, installed, initramfs updated on ocxo

### KernelGnssStream spin loop + over-buffering fix
- **Root cause found**: `_fill_ubx()` sync search had a spin loop when
  the raw buffer contained a lone `0xB5` byte.  `_fill_raw(1)` was a
  no-op (buffer already had 1 byte), so the loop never progressed.
  Fix: `_fill_raw(2)` to force reading at least one more byte.
- **Secondary bug**: `read(size)` called `_fill_ubx(size)`, forcing
  accumulation of `size` bytes across multiple UBX frames.  For RAWX
  payloads (~1500 bytes), this meant reading ~30 small frames on 15-byte
  I2C delivery, stalling for seconds.  Fix: return buffered data or one
  frame, matching socket `read()` semantics pyubx2 expects.
- Tested: 11 RAWX/15s continuous on ocxo; 59 epochs/60s on TimeHat.

### peppar-fix wrapper: --baud '' fix
- Wrapper passed `--baud ""` to phc_bootstrap.py on kernel GNSS devices
  (where BAUD is intentionally empty).  Fixed both bootstrap invocations.

## 4-hour TDEV runs in progress

Started ~04:55 UTC (23:55 CDT):

### TimeHat
- PID 5342, duration 14400s (~4h)
- Servo log: `data/tdev-4h-20260328-0053.csv`
- TICC log: `data/ticc-4h-20260328-0053.csv`
- Engine log: `data/peppar-4h-20260328-0053.log`
- At 360 epochs (~6 min): PPS+PPP error -5.3 µs, gain 0.50x
- Expected completion: ~05:00 CDT

### ocxo
- PID 15493, duration 14400s (~4h)
- Servo log: `data/tdev-4h-20260328-0455.csv`
- TICC log: `data/ticc-4h-20260328-0455.csv`
- Engine log: `data/peppar-4h-20260328-0455.log`
- At 60 epochs (~4 min): TICC reader started, servo active
- Expected completion: ~05:00 CDT
- TICC #3 on /dev/ticc3, only chA wired

## Commits (not yet pushed)

```
c39838e Add I2C write-yield to ice GNSS streaming patch, fix --baud for kernel GNSS
649e723 Fix KernelGnssStream spin loop and read over-buffering on E810
```

## Current host state

### TimeHat
- Running 4-hour TDEV collection
- TICC #1 at /dev/ticc1, both channels
- Serial F9T on /dev/gnss-top (USB)

### ocxo
- Running 4-hour TDEV collection
- Patched ice driver with write-yield (initramfs updated)
- TICC #3 at /dev/ticc3, only chA wired
- Kernel GNSS on /dev/gnss0 (I2C, streaming patch)

## What to do next

### 1. Check 4-hour run results
Pull servo and TICC CSV logs from both hosts.  Compute TDEV at
tau=1s, 10s, 100s, 300s, 1000s.  Compare TimeHat (i226) vs ocxo (E810).
The E810 is the first full servo run — expect worse TDEV at short tau
due to the bimodal clock_settime latency (±2 ms step error vs ±10 µs
on i226).

### 2. Push commits
Two commits on main, not yet pushed to origin.

### 3. E810 servo tuning
The 2-minute test showed the servo converging slowly on E810
(655 µs error at epoch 30).  May need more aggressive Kp during the
initial convergence period, or the glide slope from PHC bootstrap
isn't landing close enough.

### 4. ocxo TICC #3 signal routing
Only chA is wired.  Need to route either PEROUT (E810 PHC PPS OUT)
or F9T raw PPS to SMA for TICC chA, and the other to chB for
a reference.  Currently no PEROUT configured on E810 (unlike TimeHat).
