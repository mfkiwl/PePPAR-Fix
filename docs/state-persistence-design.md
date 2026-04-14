# State Persistence Design

How peppar-fix preserves state between invocations.

## Motivation

peppar-fix needs to remember things across runs: receiver identity,
oscillator characteristics, antenna position, frequency offsets.
Today this is scattered across `data/position.json`,
`data/drift.json`, and `data/do_characterization.json` with implicit
assumptions about single-receiver, single-DO hosts.  This design
replaces that with a structured `state/` directory indexed by hardware
unique IDs, supporting multiple receivers, DOs, and PHCs per host.

## Directory structure

```
state/
  receivers/
    <unique_id>.json        # one per physical GNSS receiver
  dos/
    <unique_id>.json        # one per physical disciplined oscillator
  phcs/
    <unique_id>.json        # one per PTP hardware clock
```

`data/` remains for experimental results, captured logs, and runtime
CSV output.  `state/` is explicitly for inter-invocation persistence.

## Entity model

### Receiver (`state/receivers/<unique_id>.json`)

Keyed by SEC-UNIQID (u-blox) or equivalent application-layer unique
identifier.  Receivers without a queryable unique ID (e.g., SkyTraq
PX1125T) cannot be auto-indexed and must be manually labeled.

```json
{
  "unique_id": 136395244089,
  "module": "ZED-F9T",
  "firmware": "TIM 2.20",
  "protver": "29.20",
  "capabilities": {
    "l2c": true,
    "l5": true,
    "l5_health_override": true,
    "glonass": false,
    "navic": false
  },
  "tcxo": {
    "last_known_freq_offset_ppb": -11.3,
    "last_known_temp_c": 42.1,
    "updated": "2026-04-14T01:22:00Z"
  },
  "last_known_position": {
    "lat": 41.8430626,
    "lon": -88.103719,
    "alt_m": 201.671,
    "ecef_m": [157469.988, -4756189.199, 4232768.421],
    "sigma_m": 0.020,
    "updated": "2026-04-14T01:22:00Z",
    "source": "ppp_bootstrap"
  },
  "last_known_port": "/dev/ttyACM0",
  "last_seen": "2026-04-14T01:22:00Z"
}
```

**last_known_position**: Each receiver carries its own antenna
position because different receivers may be connected to different
antennas.  "Last known" signals that this was true at `updated` but
the receiver may have moved since.  On startup, the engine validates
the stored position against a live LS fix — a mismatch triggers
re-bootstrap.  A receiver identity change (different unique_id on
the same port) amplifies this skepticism: the new receiver likely
has a different antenna, so the stored position is presumptively
wrong.

**tcxo**: Frequency offset of the receiver's internal TCXO, measured
relative to GPS time.  This is a property of the physical chip, not
the antenna or host.  Used to seed the PPP filter's clock state on
warm start and to compute the phc/tcxo frequency differential for
Carrier Phase servo correction.  Temperature is recorded alongside
to support future temperature/frequency modeling.  "Offset" rather
than "drift" — this is a static offset at a given temperature, not
a time-varying rate (though the offset changes as temperature
changes).

**capabilities**: Determined experimentally during `ensure_receiver_ready()`
by sending CFG-VALSET probes.  Cached here so subsequent runs skip
the probe if the firmware version hasn't changed.  A firmware version
change invalidates cached capabilities and triggers re-probing.

### Disciplined Oscillator (`state/dos/<unique_id>.json`)

Keyed by unique ID.  When the DO is bundled inside a PHC (i226, E810),
the PHC's unique ID serves as the DO's unique ID — the oscillator
and counter are physically inseparable.  When the DO is external
(VCOCXO via DAC, OCXO via ClockMatrix), the DO needs its own
identifier — either from the actuator hardware (if it has one) or a
manually assigned label.

```json
{
  "unique_id": "i226-54494d45006b",
  "label": "TimeHat i226 TCXO",
  "characterization": {
    "asd": { "tau": [1, 2, 5, 10], "values": [...] },
    "psd": { "freq_hz": [...], "values_db": [...] },
    "tdev_1s": 1.17,
    "noise_floor_ns": 1.17,
    "updated": "2026-04-13T12:00:00Z",
    "method": "TICC chA freerun 2h capture"
  },
  "adjustment": {
    "range_ppb": [-100000, 100000],
    "resolution_ppb": 0.001,
    "method": "adjfine"
  },
  "last_known_freq_offset_ppb": -11872.7,
  "last_known_temp_c": null,
  "updated": "2026-04-14T01:22:00Z"
}
```

**adjustment**: Range and resolution of the frequency actuator.
Moved here from PHC because it describes the oscillator's steerability,
not the counter.  A DAC-driven VCOCXO has an adjustment range
determined by the DAC voltage span and the varactor's tuning
sensitivity — this has nothing to do with a PHC.  When a PHC is
bundled with the DO, `adjfine` range still describes the crystal,
not the timestamping hardware.

**characterization**: ASD/PSD measurements from freerun captures.
These describe the physical crystal's noise floor — what the servo
cannot improve upon.  The measurement method and date are recorded
so stale characterizations can be identified.

### PTP Hardware Clock (`state/phcs/<unique_id>.json`)

Keyed by a stable unique identifier for the NIC.  MAC address is
the natural choice (stable across reboots, unique per NIC).  PCI
path is an alternative but changes if the card moves slots.

```json
{
  "unique_id": "54:49:4d:45:00:6b",
  "device": "/dev/ptp0",
  "driver": "igc",
  "model": "i226-LM",
  "extts": {
    "resolution_ns": 8.0,
    "measurement_noise_ns": 2.9,
    "n_channels": 2,
    "updated": "2026-04-13T12:00:00Z"
  },
  "perout": {
    "quantization_ns": 8.0,
    "half_period_latch_bug": true,
    "n_channels": 1,
    "updated": "2026-04-13T12:00:00Z"
  },
  "do_unique_id": "i226-54494d45006b",
  "last_known_device": "/dev/ptp0",
  "last_seen": "2026-04-14T01:22:00Z"
}
```

**do_unique_id**: Links to the DO state file.  For bundled PHC+DO
(i226, E810), this is the same as the PHC's unique_id.  For
ClockMatrix or external VCOCXO, this points to a separate DO file.

**extts/perout**: Measurement characteristics of the timestamping
hardware, independent of the oscillator.  EXTTS resolution determines
the noise floor of PPS measurements.  PEROUT quantization affects
PPS output alignment.  These are properties of the counter/comparator
silicon, not the crystal.

## Identity discovery

### What the software can auto-discover

| Entity | Unique ID source | Discovery method |
|--------|-----------------|------------------|
| u-blox receiver | SEC-UNIQID | UBX query on serial open |
| PHC / NIC | MAC address | `ethtool -P <dev>` or sysfs |
| DO (bundled) | Same as PHC | Inherited |
| TICC | Arduino serial | USB descriptor (udev) |
| F10T on ArduSimple | FTDI serial + SEC-UNIQID | USB descriptor + UBX |

### What requires manual labeling

| Entity | Why no auto-ID | Labeling approach |
|--------|---------------|-------------------|
| SkyTraq PX1125T | No unique ID at any layer | Config file: `receiver_label = "px1125t-lab1"` |
| I2C DAC (VCOCXO) | I2C DACs have pin-strapped addresses, not unique IDs | Config file: `do_label = "vcocxo-clkpoc3"` |
| Antenna / ARP | No electronic identity | Not labeled in state; position is the proxy |

## Lifecycle

### First run (cold start)

1. `ensure_receiver_ready()` queries SEC-UNIQID + MON-VER
2. No matching `state/receivers/<id>.json` found → new receiver
3. Capabilities probed via CFG-VALSET
4. Position bootstrap runs from scratch (no last_known_position)
5. State files created on convergence

### Warm start (same receiver)

1. SEC-UNIQID matches stored state → known receiver
2. Firmware version matches → capabilities still valid, skip probe
3. last_known_position loaded → LS validation, skip bootstrap if OK
4. last_known_freq_offset_ppb seeds the PPP filter and servo glide

### Receiver change (different unique_id on same port)

1. SEC-UNIQID doesn't match any stored state → new receiver, OR
   matches a receiver last seen on a different port → receiver moved
2. **Position skepticism**: last_known_position from the old receiver
   is NOT inherited.  The new receiver may be on a different antenna.
   Bootstrap from scratch unless the operator provides `--known-pos`.
3. **Capability re-probe**: firmware may differ
4. **Drift invalidation**: old receiver's tcxo offset doesn't apply

### Firmware change (same unique_id, different firmware)

1. SEC-UNIQID matches, firmware version differs → upgraded/downgraded
2. Capabilities re-probed (L2/L5/health-override may have changed)
3. Position still valid (same receiver, same antenna)
4. TCXO offset still valid (same physical chip)

### DO/PHC discovery

1. PHC opened by `--servo /dev/ptp0`
2. MAC address read from sysfs → unique_id for PHC
3. Load `state/phcs/<mac>.json` if it exists
4. Load linked `state/dos/<do_id>.json` for oscillator characterization
5. If no DO state: freerun characterization hasn't been done yet

## Migration from current state files

| Current file | Migrates to | Notes |
|---|---|---|
| `data/position.json` | `state/receivers/<id>.json` `.last_known_position` | Position moves into the receiver that measured it |
| `data/drift.json` | `state/dos/<id>.json` `.last_known_freq_offset_ppb` + `state/receivers/<id>.json` `.tcxo` | Frequency offsets split: DO's adjfine vs receiver's TCXO offset |
| `data/do_characterization.json` | `state/dos/<id>.json` `.characterization` | Noise floor moves to the specific DO |

During migration, existing files are read once, split into the
appropriate state files, and the originals are left in place (not
deleted) as a safety net.  New code reads from `state/` first, falls
back to `data/` for backwards compatibility.

## Implementation plan

### Phase 1: Receiver identity (smallest useful increment)

1. Add `query_receiver_identity(port, baud)` → `{unique_id, module, firmware}`
2. Add `state/receivers/` load/save functions
3. Call from `ensure_receiver_ready()`: query identity, compare with stored state,
   log warnings on mismatch
4. Store last_known_position in receiver state instead of `data/position.json`
5. On receiver change: invalidate position, force re-bootstrap

### Phase 2: DO/PHC state separation

1. Add PHC unique ID discovery (MAC from sysfs)
2. Add `state/phcs/` and `state/dos/` load/save
3. Move `do_characterization.json` into `state/dos/<id>.json`
4. Move DO frequency offset from `data/drift.json` into DO state
5. Move TCXO frequency offset into receiver state
6. `data/drift.json` becomes a backwards-compatibility shim that reads from both

### Phase 3: Multi-receiver support

1. `--serial auto` discovers receivers on tagged ports, matches by unique_id
2. Engine supports `--receiver-id <unique_id>` to select among multiple
3. Each receiver carries its own position, so multi-receiver hosts
   can have per-antenna positions
4. Wrapper discovers available receivers and presents a menu (or uses
   config file to assign roles)

### Phase 4: External DO support (VCOCXO, ClockMatrix)

1. DO state for non-PHC oscillators with manual labels
2. DAC actuator interface alongside adjfine and ClockMatrix FCW
3. Characterization workflow for external DOs (TICC-based, no EXTTS)
