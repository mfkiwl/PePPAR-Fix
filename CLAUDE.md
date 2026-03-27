# PePPAR Fix — Polecat Operating Manual

You are working on PePPAR Fix, a GNSS-disciplined precision clock system.
This file contains hard-won operational knowledge. Read it before writing
code or touching lab hardware.

## Lab Hosts and Access

All lab hosts are Raspberry Pis or similar SBCs. SSH access is
passwordless for user `bob`.

| Host | Access | Role | GNSS | Notes |
|---|---|---|---|---|
| TimeHat | `ssh TimeHat` | Primary peppar-fix dev + PHC discipline | F9T-3RD on `/dev/gnss-top` | Has i226 PHC, TICC #1, heatsink on TCXO |
| PiPuss | `ssh PiPuss.local` | Dual-F9T, caster/client testing | F9T-TOP `/dev/gnss-top`, F9T-BOT `/dev/gnss-bot` | Zero-baseline (both on Patch3 via GUS #2) |
| Onocoy | `ssh Onocoy.local` | F10T, PX1125T, TICC #2 | F10T on `/dev/f10t`, PX1125T on `/dev/ttyUSB0` | F10T uses ArduSimple FTDI, not CDC ACM |
| otcBob1 | `ssh otcBob1` | Timebeat OTC SBC, OCXO, Renesas ClockMatrix | F9T on `/dev/ttyAMA0` at 460800 | Stop `timebeat` before accessing I2C or GNSS |
| ptBoat | `ssh ptBoat` | Timebeat OTC Mini PT, weatherproof, PoE | F9T on `/dev/ttyAMA0` at 115200 | Same Renesas ClockMatrix as otcBob1 |
| ocxo | `ssh ocxo` | E810-XXVDA4T x86 host, OCXO, DPLL | F9T on `/dev/gnss0` (kernel, I2C) | PHC at `/dev/ptp1`, trusted net + PTP net |
| bbb | `ssh bbb` | BeagleBone, GPS L1 only | `/dev/gps0` at 9600 | Legacy NTP/PTP GM |

**Hostname resolution**: Try `<host>` first (DNS search domain VanValzah.Com).
If that fails, try `<host>.local` (mDNS). Some hosts (PiPuss, Onocoy)
only resolve via `.local`. Never use the PTP LAN (10.168.13.x) for
SSH — keep that clean for timing traffic.

## Serial Port Gotchas

### TICC resets on serial open

TAPR TICCs use Arduino Mega 2560. **Opening the serial port toggles DTR,
which reboots the Arduino.** The TICC goes silent for ~10 seconds during
boot, then outputs its config header before starting measurements.

```python
# WRONG — resets the TICC:
ser = serial.Serial("/dev/ticc1", 115200)

# RIGHT — prevents DTR toggle:
ser = serial.Serial("/dev/ticc1", 115200, dsrdtr=False, rtscts=False)
```

If you WANT to reset a TICC intentionally:
```python
ser = serial.Serial("/dev/ticc1", 115200, dsrdtr=True)
ser.close()
# Wait 15 seconds for boot
```

### F9T EVKs have no USB serial number

All F9T EVKs report the same VID:PID (`1546:01a9`) with no serial number.
You cannot distinguish them by USB descriptor. On PiPuss (two F9Ts),
udev uses USB path matching which breaks if cables move. On single-F9T
hosts (TimeHat), VID:PID matching is fine.

Each F9T does have a unique `SEC-UNIQID` queryable via UBX protocol,
but this is not visible to udev.

### Stable device names

Devices with unique serial numbers get stable names everywhere via
`99-timelab.rules`:

| Device | Name | Basis |
|---|---|---|
| `/dev/ticc1` | TICC #1 | Arduino serial `95037323535351803130` |
| `/dev/ticc2` | TICC #2 | Arduino serial `44236313835351B02001` |
| `/dev/ticc3` | TICC #3 | Arduino serial `44236313835351B0A091` |
| `/dev/f10t` | NEO-F10T (ArduSimple) | FTDI serial `D30GD1PE` |

Devices without unique serials (PX1125T, F9T EVKs) do NOT get udev
symlinks. Your code must identify them at runtime.

## NTRIP Credentials and Configuration

NTRIP config file: `/home/bob/peppar-fix/ntrip.conf` on TimeHat.

```ini
[ntrip]
caster = ntrip.data.gnss.ga.gov.au
port = 443
mount = SSRA00BKG0         # SSR corrections
user = bobvan
password = maHxyc!gebweb6
tls = true
```

Broadcast ephemeris mount: `BCEP00BKG0` (same caster, pass via `--eph-mount`).

**SSR status**: Orbit + clock + code bias available. **Phase bias = 0**
(not provided by this stream). This means PPP-AR is not possible with
this SSR source alone.

## Known Broken Things

### BDS is broken with broadcast ephemeris

Default is `--systems gps,gal`. BDS produces ISBs of 1500+ ns (should
be <200 ns). Root cause: BDS time system (BDT vs GPST, 14-second offset)
handling in `broadcast_eph.py` is still wrong despite multiple fix
attempts. Do NOT enable BDS until this is fixed (see bead `pf-luu`).

### F10T on Onocoy doesn't respond to UBX

The NEO-F10T is on an ArduSimple board using FTDI at `/dev/f10t`.
It doesn't respond at any standard baud rate. May need USB-C power
on the ArduSimple's second connector. Investigation ongoing.

## peppar-fix Python Environment

| Host | Venv | Activation |
|---|---|---|
| TimeHat | `/home/bob/peppar-fix/venv` | `source ../venv/bin/activate` |
| PiPuss | `/home/bob/pygpsclient` | `source ~/pygpsclient/bin/activate` |
| Onocoy | (system python3) | pyubx2 may not be installed |
| otcBob1 | (system python3) | May need `pip install smbus2` for I2C |

Scripts live in `/home/bob/peppar-fix/scripts/` on TimeHat. Other hosts
may not have the full peppar-fix repo — deploy scripts via `scp` as
needed.

## Lab Storage Warning

**Lab hosts use eMMC or SD cards.** These can fail without warning.
After any significant capture or test, pull results back to the GT
server (`/home/bob/gt/`) which has RAIDZ-3 + offsite backup. Large
datasets can stay on lab hosts; everything else should be pulled.

## Resource Allocation

Lab hardware is shared. Before using a host or device, check that no
other work is running on it:

```bash
# Check for running processes using a serial port
ssh <host> "fuser /dev/gnss-top 2>/dev/null"

# Check for running peppar-fix or analysis processes
ssh <host> "ps aux | grep -E 'peppar|servo|ticc|analyze' | grep -v grep"
```

Beads that need hardware carry `hw:` labels (e.g. `hw:TimeHat`,
`hw:F9T-3RD`). The Mayor checks these before assigning work. If your
bead has a hardware label, you are the exclusive user of that hardware
for the duration of your work.

## Key Technical Context

### Position finding

The PPP position finder (`peppar_find_position.py` or Phase 1 of
`peppar_fix_cmd.py`) converges in ~90 seconds at sigma 0.5m with
GPS+GAL and NTRIP broadcast ephemeris. The convergence requires:
- Per-system ephemeris warmup (all configured systems must have ≥8 SVs)
- Satellite health filtering (excludes unhealthy Galileo E14/E18)
- Satellite clock sanity check (|sat_clk| < 2ms)
- LS outlier rejection (>50m residuals excluded)

### Unified CLI

`peppar_fix_cmd.py` is the unified entry point:
- Phase 1: PPPFilter bootstrap (estimates position from scratch)
- Phase 2: FixedPosFilter (clock estimation with optional servo)
- `--servo /dev/ptp0 --pps-pin 1` enables PHC discipline
- `--position-file` skips Phase 1 if the file exists and is valid
- Position file is validated against live LS fix before Phase 2

### Free-running TCXO baseline

TimeHAT v5 TCXO with heatsink: TDEV(1s) = 100-130 ps free-running.
This is the discipline floor — the PHC cannot be quieter at tau=1s.
The F9T PPS sawtooth is 1.6-1.7 ns TDEV(1s) during "jumpy" periods,
0.7 ns during "smooth ramp" periods.

## Design Documentation

The `docs/` directory contains design documents and research notes. Start
here before changing anything in the areas they cover.

| File | Summary |
|---|---|
| [stream-timescale-correlation.md](docs/stream-timescale-correlation.md) | **Read this first.** How to correctly correlate events from independent timescales (GNSS, PPS, TICC, NTRIP). Covers why queue-order matching fails, the strict correlation gate design, confidence scoring, and fault injection testing. |
| [full-data-flow.md](docs/full-data-flow.md) | Complete inventory of live data sources, their timescales, sink policies (freshest-only vs loss-free vs correlated-window), freshness requirements, and decimation effects. |
| [platform-support.md](docs/platform-support.md) | Per-platform status for TimeHat (i226) and ocxo (E810). Documents device paths, PHC behavior, GNSS transport differences, and bring-up checklists. |
| [time-and-platform-todo.md](docs/time-and-platform-todo.md) | Concrete work breakdown: remaining tasks for E810 GNSS, TimeHat PPS, correlation model, legacy cleanup, diagnostics. |
| [timebeat-otc-research.md](docs/timebeat-otc-research.md) | Renesas 8A34002 ClockMatrix research: I2C access, DPLL modes, TDC phase measurement, clock tree configuration. Essential for Timebeat OTC work. |
| [timebeat-otc-signal-routing.md](docs/timebeat-otc-signal-routing.md) | Signal flow architecture: which DPLLs drive which outputs, DPLL mode mapping, how to open the loop for software steering. |
| [data-flow.md](docs/data-flow.md) | Original data flow sketch (superseded by full-data-flow.md for sink policy details). |
| [position-convergence.md](docs/position-convergence.md) | PPP position bootstrap convergence analysis and tuning. |
| [nic-survey.md](docs/nic-survey.md) | Survey of NICs with PTP hardware timestamping support. |
| [e810-cm5-research.md](docs/e810-cm5-research.md) | E810 on Raspberry Pi CM5: showstopper (ice driver x86-only). |
| [phc-bootstrap.md](docs/phc-bootstrap.md) | PHC bootstrap design: cold/warm start, optimal stopping, glide slope, characterization method, drift file, how the servo starts with bounded error. |
| [pps-ppp-error-source.md](docs/pps-ppp-error-source.md) | PPS+PPP servo error source: using carrier-phase dt_rx to replace TIM-TP qErr via 125 MHz tick model. Experiment results, calibration procedure, formula. |
| [correction-sources.md](docs/correction-sources.md) | How to get SSR corrections: registration, caster options, which streams for float PPP vs PPP-AR, why AR requires a single analysis center. |
| [galileo-has-research.md](docs/galileo-has-research.md) | Galileo HAS: free PPP-AR corrections via E6-B signal. |
| [peer-bootstrap-sketch.md](docs/peer-bootstrap-sketch.md) | NTRIP caster mode for peer-to-peer bootstrap. |
| [ntrip-mdns-discovery.md](docs/ntrip-mdns-discovery.md) | Spec: mDNS service advertisement for NTRIP peer discovery. Caster announces `_ntrip._tcp`, client discovers and selects by accuracy/proximity. |
| [ticc-calibration-2026-03-19.md](docs/ticc-calibration-2026-03-19.md) | TICC calibration procedure and results. |
| [hw-labels.md](docs/hw-labels.md) | Hardware labeling conventions for beads. |
| [draft-dupage-inquiry.md](docs/draft-dupage-inquiry.md) | Draft inquiry to DuPage County about GNSS antenna siting. |
| [packaging-plan.md](docs/packaging-plan.md) | Plan for making peppar-fix pip-installable from GitHub Releases. Phased: pyproject.toml stub (done), flatten imports, versioned releases. |
| [ptp4l-supervision.md](docs/ptp4l-supervision.md) | Layered ptp4l clockClass supervision via systemd. Three layers: engine (Python UDS), wrapper (pmc command), systemd ExecStopPost. Covers clock-class mapping, ptp4l config, privilege model, and example unit file in `deploy/`. |
| [extts-lifecycle.md](docs/extts-lifecycle.md) | EXTTS (PPS IN/OUT) initialization lifecycle. Bootstrap owns pin programming; engine inherits and verifies. Covers PTP profile extension for IN+OUT pins, PEROUT for TICC, fd persistence, platform matrix (i226/E810), and phased migration path. |

## Lab Documentation Pointers

The `timelab/` directory at the town root has authoritative lab state:

| File | Contents |
|---|---|
| `timelab/topology.md` | Current wiring: antenna→splitter→receiver→host→TICC, PTP domains, NTP |
| `timelab/gear.md` | Hardware inventory: every host, receiver, TICC, antenna, with specs |
| `timelab/usb-identification.md` | USB serial numbers, udev policy, device identification |
| `timelab/status.md` | Active experiments and recent results (may be stale) |
| `timelab/calibration.md` | TICC calibration procedures |
| `timelab/99-timelab.rules` | Universal udev rules (deployed to all Pis) |
| `timelab/scripts/` | Lab utility scripts (ticc_read.py, calibration_capture.py, etc.) |

If you need to know what's physically connected where, start with
`topology.md`. If you need device specs, start with `gear.md`.

## Acceptance Criteria for Beads

Your bead is NOT done until:
- Code changes are committed to your feature branch
- A test plan is documented in a bead comment
- If the bead has `hw:` labels: code stays on branch (`--no-merge`),
  lab validation happens separately
- If pure software: include unit tests or a verification script
- Research tasks MUST produce a markdown document in `docs/`

Do not close a bead with "Completed with no code changes" unless you
can explain in a comment exactly what you verified and why no changes
were needed.
