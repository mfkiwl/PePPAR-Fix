# EXTTS Lifecycle — PPS IN/OUT Initialization and Ownership

## Problem

EXTTS (external timestamp) initialization is currently split between
PHC bootstrap and the engine, with neither owning the full lifecycle.
Bootstrap enables/disables EXTTS around each measurement.  The engine
re-enables it independently at Phase 2 entry.  This creates gaps and
fragile state:

- If bootstrap leaves EXTTS in a bad state (pin corruption, driver
  reload), the engine's enable may silently fail.
- The engine has no way to verify EXTTS is actually producing events
  before entering the servo loop — it just starts the reader thread
  and discovers the problem 11+ seconds later via correlation gate
  timeout.
- PPS OUT (PEROUT) is not initialized anywhere, though the PtpDevice
  API supports it.

## Proposed Model

### Ownership

**Bootstrap owns EXTTS initialization.**  If a PHC is configured:

1. Bootstrap programs PPS IN pin (EXTTS) and PPS OUT pin (PEROUT)
   per the PTP profile.
2. Bootstrap verifies at least one PPS IN event is received.
3. Bootstrap leaves both enabled when it exits.
4. The engine inherits working EXTTS — it verifies events are
   arriving but does not re-program pins or re-enable EXTTS.

When no PHC is configured (NTRIP caster, logging-only mode), no
EXTTS initialization occurs.  The engine runs without a servo.

### Lifecycle

```
peppar-fix wrapper
  │
  ├─ Step 2b: PHC bootstrap
  │    ├─ Open PTP device
  │    ├─ Read PTP profile (i226 / e810 / host config)
  │    ├─ Program PPS IN pin  → EXTTS function, channel from profile
  │    ├─ Program PPS OUT pin → PEROUT function, channel from profile
  │    ├─ Enable EXTTS IN
  │    ├─ Verify: read one PPS event (fail fast if none in 3s)
  │    ├─ Measure frequency from PPS intervals
  │    ├─ Run clock estimate, evaluate phase, step if needed
  │    ├─ Enable PEROUT (1 PPS, aligned to PHC second)
  │    └─ Exit — leave EXTTS IN and PEROUT enabled, device OPEN
  │         (device fd is NOT inherited across exec; engine re-opens)
  │
  ├─ Step 3: Engine
  │    ├─ Open PTP device (new fd)
  │    ├─ Do NOT program pins or enable EXTTS (bootstrap did it)
  │    ├─ Verify: read one PPS event within 3s
  │    │    └─ If none: exit code 3 (no PPS), wrapper retries
  │    ├─ Start extts_reader thread
  │    └─ Servo loop (EXTTS events flow from kernel → pps_history)
  │
  └─ Cleanup
       ├─ Engine disables EXTTS IN on exit
       └─ Engine disables PEROUT on exit (optional — may want to
            leave PPS OUT running during re-bootstrap)
```

### Problem: fd not inherited

The bootstrap process opens `/dev/ptpN`, programs pins, enables
EXTTS, then exits.  The engine is a separate process invocation that
opens `/dev/ptpN` again with a new fd.

**Question**: Does EXTTS state persist across fd close/reopen on
the same PTP device?  This is kernel-driver-specific:

- **igc (i226)**: Pin function programming persists (hardware
  register).  EXTTS enable state persists until the device is reset
  or the driver is unloaded.  Verified empirically.
- **ice (E810)**: Pin function state is managed by the driver.
  Behavior on fd close needs testing — the ice driver may disable
  EXTTS when the last fd closes (refcounted).

If EXTTS does not persist across fd close, the alternatives are:
1. Bootstrap and engine share the same process (bootstrap becomes
   a function call, not a separate script).
2. The engine re-enables EXTTS (but does not re-program pins).
3. A helper daemon holds the fd open.

Option 2 is the simplest and most portable.  The engine would call
`enable_extts()` but skip `set_pin_function()` (pins already
programmed by bootstrap).  This is a smaller change than the ideal
model but avoids the fd-persistence question.

## PTP Profiles

The profile system (`config/receivers.toml`) already defines per-
platform pin mappings.  It needs to be extended with PPS OUT config:

```toml
[ptp.i226]
device = "/dev/ptp0"
pps_in_pin = 1          # SDP1 receives PPS from GNSS
pps_in_channel = 0      # EXTTS channel 0
pps_out_pin = 0          # SDP0 outputs disciplined PPS
pps_out_channel = 0      # PEROUT channel 0
program_pin = true
timescale = "tai"
# ... servo params ...

[ptp.e810]
device = "/dev/ptp1"
pps_in_pin = 0           # U.FL PPS IN
pps_in_channel = 0
pps_out_pin = -1          # no user-accessible PPS OUT pin
pps_out_channel = -1
program_pin = false       # ice driver doesn't support PTP_PIN_SETFUNC
timescale = "tai"
```

The `-1` convention means "not available on this platform."

## PPS OUT (PEROUT)

PPS OUT serves two purposes:

1. **TICC measurement**: The TICC compares PHC PPS OUT (disciplined)
   against GNSS PPS (raw) to measure discipline quality at sub-ns
   resolution.
2. **External distribution**: Other equipment can lock to the
   disciplined PPS output.

PEROUT programming requires:
- Pin function: `set_pin_function(pin, PTP_PF_PEROUT, channel)`
- Period: 1 second (1,000,000,000 ns)
- Phase: aligned to the PHC second boundary

The `PtpDevice` class does not currently have `enable_perout()` /
`disable_perout()` methods.  These need to be added using
`PTP_PEROUT_REQUEST2` (ioctl 12) with the `ptp_perout_request`
structure.

## Verification at Engine Startup

The engine should verify EXTTS before entering the servo loop:

```python
# After opening PTP device, before starting extts_reader
ptp.enable_extts(extts_channel, rising_edge=True)  # re-enable (safe if already enabled)
test_event = ptp.read_extts(timeout_ms=3000)
if test_event is None:
    log.error("No PPS event within 3s — check EXTTS wiring and bootstrap")
    return 3  # exit code 3 = no PPS, wrapper retries
```

This catches:
- Bootstrap failure that wasn't detected
- Cable disconnected between bootstrap and engine start
- Driver reload that cleared EXTTS state
- Pin corruption from external tools

## Migration Path

### Phase 1 (minimal, do now)
- Engine re-enables EXTTS (doesn't assume bootstrap left it on)
- Engine verifies one PPS event before starting servo
- Exit code 3 if no PPS (wrapper retries with backoff)

### Phase 2 (profile extension)
- Add `pps_in_pin`, `pps_out_pin` to profiles (rename from `pps_pin`)
- Bootstrap programs both IN and OUT per profile
- Add `enable_perout()` / `disable_perout()` to PtpDevice
- Bootstrap enables PEROUT after stepping PHC

### Phase 3 (engine simplification)
- Engine stops calling `set_pin_function()` entirely
- Engine only calls `enable_extts()` + verify
- Profile is the single source of truth for pin assignments

## Platform Matrix

| Platform | PPS IN | PPS OUT | Pin Programming | EXTTS Persist? | Notes |
|----------|--------|---------|-----------------|----------------|-------|
| i226 (igc) | SDP1 → ch0 | SDP0 → ch0 | Yes (`PTP_PIN_SETFUNC`) | Yes (hw register) | TimeHat default |
| E810 (ice) | U.FL → ch0 | N/A | No (driver limitation) | Needs testing | GNSS PPS via ice DPLL |
| Generic | Profile-defined | Profile-defined | Profile-defined | Assume no | Safe default |
