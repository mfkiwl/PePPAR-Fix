# Session Handoff — 2026-03-28

Long session covering PHC bootstrap redesign, NTRIP caster testing,
PEROUT integration, TICC lock fix, ice driver I2C write investigation.

## What was accomplished

### PHC bootstrap: optimal stopping + glide slope
- Replaced 8-iteration PPS feedback loop with three-step approach:
  1. Optimal stopping phase step (parametric, p5 threshold)
  2. PPS ground truth measurement
  3. Glide slope frequency for smooth servo handoff
- Full-baseline PPS frequency measurement (first-to-last fractional
  second drift, sqrt(N) better than per-interval)
- Float64-safe arithmetic using elapsed seconds
- Servo Kp increased for both platforms to match glide damping:
  i226 0.01→0.03 (ζ=0.47), E810 0.005→0.015 (ζ=0.24)

### PEROUT integrated into PHC bootstrap
- Discovered igc driver adds period/2 to PEROUT start time
- Fixed by setting start = next_PHC_second - period/2
- PEROUT now owned by phc_bootstrap.py, not systemd service
- Verified chA-chB alignment within 54 ns on TICC
- Disabled systemd phc-pps-out.service (no longer needed)

### TICC lock leak fixed
- Ticc.__enter__ leaked flock when wait_for_boot failed mid-boot
- Wrapped entire __enter__ in try/except for guaranteed lock release
- Added dsrdtr=False to primary serial open path

### NTRIP caster tested on PiPuss
- Standalone caster works: MSM4 + 1005, clients connect and parse
- Architecture gap: caster sends observations, not ephemeris
- Spec written for SFRBX-to-RTCM encoding (docs/caster-ephemeris.md)
- mDNS discovery spec written (docs/ntrip-mdns-discovery.md)
- NtripStream.close() bug fixed

### Cold boot wrapper fix
- peppar-fix wrapper now creates position.json from known_pos when
  the file doesn't exist (was failing PHC bootstrap)

### E810 ice driver I2C write investigation
- **Root cause found**: streaming patch polls I2C every 20ms, starving
  the write path. Stock driver (100ms gap) has working writes.
- Stock driver restored on ocxo to enable receiver configuration
- Patch needs write-yield logic: skip a read cycle when write is pending

### Other fixes
- Serial exclusive lock fallback (TIOCEXCL → non-exclusive)
- 100% CPU spin fix: raw byte ACK scan instead of pyubx2 deserialization
- KernelGnssStream: blocking I/O (removed O_NONBLOCK)
- Hostname oxco → ocxo everywhere (including the actual host)
- Renamed phc-initialization.md → phc-bootstrap.md
- Private timelab repo created (github.com/bobvan/timelab)
- First sync of timelab/ public slice to PePPAR-Fix

## TDEV results (TimeHat, 1-hour run)

| tau | TICC TDEV | PHC TDEV |
|-----|-----------|----------|
| 1s  | 3.2 ns    | 3.9 ns   |
| 10s | 215 ns    | 220 ns   |
| 100s| 6.4 µs   | 8.6 µs   |
| 300s| 3.0 µs   | 3.5 µs   |

TICC converged state (last 1000 samples): mean=24 ns, stdev=54 ns.
Disciplined PHC tracks F9T PPS within ±12.5 ns (1σ) as measured by TICC.

## Current host state

### TimeHat
- Last 1-hour run completed cleanly (3599 epochs, TICC data collected)
- PEROUT working via bootstrap (igc period/2 compensation)
- TICC #1 at /dev/ticc1, both chA and chB recording
- ModemManager disabled (was locking serial port after reboot)
- phc-pps-out.service disabled (PEROUT now in bootstrap)

### ocxo
- Rebooting with stock ice driver (I2C writes restored)
- Hostname changed from oxco to ocxo
- TICC #3 at /dev/ticc3, only chA wired (no signal — needs PEROUT or PPS routing)
- Patched driver backed up at /home/bob/ice-intree-patch/

### PiPuss
- Running latest code from git pull
- NTRIP caster may still be running (port 2102)
- No PHC (no discipline, caster-only role)

## What to do next (priority order)

### 1. Fix ice-gnss streaming patch for I2C write coexistence
The streaming patch needs to yield the I2C bus when a write is pending.
Check in the read work function: if gnss_serial has pending write data,
increase the delay or skip one read cycle.  This restores both streaming
reads AND working writes.

### 2. E810 2-minute test run
Once writes work with the patched driver (or with stock driver after
reboot), run the 2-minute test to verify I2C delivery rate and TICC
on ocxo.  The TICC chA needs a PPS source — either configure E810
PEROUT or verify the F9T PPS routing to the SMA connector.

### 3. Longer stability runs
The 1-hour TimeHat TDEV data is good but the servo oscillation
(tau=10-100s peak) could improve with further gain tuning.  A 4-hour
run would give meaningful TDEV at tau=1000s+.

### 4. SFRBX-to-RTCM ephemeris encoding
Spec is written (docs/caster-ephemeris.md).  Phase 1 (GPS 1019,
~200 lines) would make the PiPuss caster self-sufficient for peer
bootstrap without the Australian NTRIP service.

## Key files changed

| File | Changes |
|------|---------|
| scripts/phc_bootstrap.py | Optimal stopping, glide slope, PPS freq measurement, PEROUT |
| scripts/peppar_fix/ptp_device.py | enable_perout/disable_perout, optimal_stop mode, igc period/2 fix |
| scripts/peppar_fix/gnss_stream.py | Blocking I/O, read_raw() for ACK scan |
| scripts/peppar_fix/receiver.py | Raw byte ACK scan, no-sleep loops |
| scripts/peppar_fix/servo.py | (unchanged) |
| scripts/peppar-fix | known_pos→position.json, PEROUT restart removed |
| scripts/ticc.py | Lock leak fix, dsrdtr=False |
| scripts/ntrip_client.py | close() alias |
| config/receivers.toml | Kp tuning, glide_zeta, search_time_s, pps_out_pin, track_max_ppb |
| docs/phc-bootstrap.md | Full rewrite documenting implemented design |
| docs/caster-ephemeris.md | NEW: SFRBX-to-RTCM spec |
| docs/ntrip-mdns-discovery.md | NEW: mDNS discovery spec |
| docs/platform-support.md | initramfs requirement, oxco→ocxo |
| drivers/ice-gnss-streaming/README.md | initramfs documentation |
