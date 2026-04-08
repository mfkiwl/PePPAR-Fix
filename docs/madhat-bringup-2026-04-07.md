# MadHat fresh-install bring-up — 2026-04-07

A second TimeHat-class host (Pi 5 + TimeHat v6 i226 NIC, hostname
`MadHat`).  Started from a fresh Raspberry Pi OS Trixie Lite SD card
image.  This document logs *every* stumble encountered during
bring-up so they can be folded into installation requirements,
`docs/lab-operations.md`, or a future `peppar-install` script.

**Rule for this session**: every issue gets logged here *before*
being fixed.  The point is to characterize the gap between "fresh
Raspbian" and "running peppar-fix", not to silently work around it.

## Hardware context

- Raspberry Pi 5 (no eMMC; SD card boot)
- TimeHat v6 i226 (PCIe-attached, but it's a HAT — appears as i226 NIC)
- TICC #3 (USB)
- F9T-BOT receiver (USB serial + PPS into TICC #3 chB)
- Trusted LAN at 10.168.60.46/24, PTP LAN at 10.168.13.46/24
  (eth1 cable not yet plugged in at start of bring-up)

## Process

Following the "First-time setup on a new host" procedure from
`docs/lab-operations.md` plus any preflight checks from the same doc.
Every step is run remotely from `gt`, the GT/storage server, via
`ssh MadHat.VanValzah.Com`.

## Stumbles

(Filled in as encountered.  Each entry: what I tried → what failed →
what was missing → what would prevent it next time.)

### #1 — `git` not installed on Trixie Lite

**Tried**: `git clone https://github.com/bobvan/PePPAR-Fix.git peppar-fix`

**Failed**: `bash: line 1: git: command not found`

**Missing**: `git` is not in the default Raspberry Pi OS Trixie Lite
package set.  The lab-operations checklist starts with "git clone"
but never says how `git` got there.

**Prevent next time**: add `git` to the apt prereq list.  Future
`peppar-install` script must install it as the very first step (or
document an `apt install git` precondition).  Alternatively, the
imager preset could be told to install it via the cloud-init-style
`firstboot` packages list.

### #2 — `numpy` / `scipy` / `plotly` not in the documented install list

**Tried**: surveyed Python package availability after the documented
`pip install pyubx2 pyserial` step.

**Failed**: `numpy`, `scipy`, `plotly` all `ModuleNotFoundError`.

**Missing**: `docs/lab-operations.md` "First-time setup on a new host"
section only lists `pyubx2 pyserial` (and `smbus2` for I2C hosts).
But the engine and the orchestration wrapper use additional packages:

- `numpy` — used by the engine for variance/state arithmetic and by
  every analysis tool
- `scipy.signal` — used by `tools/plot_psd.py` and
  `tools/build_do_characterization.py`.  The wrapper *automatically*
  runs a 30-minute freerun and feeds it to
  `build_do_characterization.py` on first startup of any host where
  `data/do_characterization.json` does not exist — so first-run
  bring-up unconditionally needs scipy even if the user never plans
  to run an analysis tool by hand.
- `plotly` — used by the analysis tools (lazy-imported in `plot_psd`,
  but `build_do_characterization` may pull it in transitively).

On a Pi/ARM host these may need to come from `apt install
python3-numpy python3-scipy python3-plotly` rather than from pip
(scipy wheels are big and slow to build from source on ARM, and the
Debian packages are usually fine).  That requires creating the venv
with `--system-site-packages` so it can see the apt-installed
modules — which the documented `python3 -m venv venv` does *not* do.

**Prevent next time**: lab-operations.md needs:
1. The full dep list (`numpy scipy plotly` in addition to
   `pyubx2 pyserial`)
2. An explicit "use `--system-site-packages` if you intend to satisfy
   numpy/scipy from apt" note, or pin to pip+wheels for
   reproducibility
3. Or: add `numpy scipy plotly` to the pip install list and accept
   the build time

### #3 — `99-timelab.rules` not deployed; no `/dev/ticc*` symlinks; PHC is root-only

**Tried**: `ls /dev/ticc* /dev/ptp*` after the documented setup steps.

**Failed**: no `/dev/ticc*` symlink at all.  `/dev/ptp0` (the i226
TimeHat PHC) was mode `0600 root:root` — unreadable to the wrapper's
non-sudo init paths.

**Missing**: nothing in lab-operations.md "First-time setup" tells
you to deploy the udev rules.  They live in `timelab/99-timelab.rules`
(outside this repo) and need to be `cp`'d to `/etc/udev/rules.d/`
followed by `udevadm control --reload-rules && udevadm trigger`.

Without this:
- TICC by stable name (`/dev/ticc3`) doesn't exist; the wrapper or
  config has to use `/dev/ttyACM1` directly, which is unstable across
  reboots if anything else enumerates first.
- `/dev/ptp0` is root-only, so any non-sudo path that opens the PHC
  fails.  The `igc` driver gets matched by `SUBSYSTEM=="ptp",
  DRIVERS=="igc", MODE="0664", GROUP="dialout"` in the rules file.

**Prevent next time**:
1. The rules file should be in this repo, not in a sibling repo, so
   `git clone` of peppar-fix gets you everything.
2. The first-time setup procedure should include `sudo cp
   config/99-timelab.rules /etc/udev/rules.d/ && sudo udevadm control
   --reload-rules && sudo udevadm trigger` as a numbered step.
3. The `peppar-install` script should detect missing rules and
   install them.

### #4 — F9T EVK has no unique USB serial → no stable device symlink possible

**Tried**: enumerate /dev/ttyACM* and look for the F9T-BOT.

**Found**: `/dev/ttyACM0` is the u-blox EVK (`ID_MODEL=u-blox_GNSS_receiver`),
but `udevadm` reports no `ID_SERIAL_SHORT` field at all.  CLAUDE.md
already documents this: "All F9T EVKs report the same VID:PID
(`1546:01a9`) with no serial number."

**Missing**: the udev rules can't create `/dev/gnss-bot` because there
is no per-unit attribute to match on.  On hosts with one F9T (TimeHat)
the F9T-3RD rule can match by USB ID_PATH, but the path depends on
which physical USB port you plug it into and breaks if the cable
moves.  On MadHat there is no rule for the F9T at all yet.

The host config has to point at `/dev/ttyACM0` directly, and the user
has to remember not to enumerate any other ACM device first.  This is
exactly the same trap CLAUDE.md warns about for PiPuss.

**Prevent next time**:
1. The first-time setup needs a step that says "identify your F9T's
   ttyACM device by `udevadm info` and put the path in your host
   config."
2. Future fix would be a `usb-modeswitch`-style serial assignment, or
   building per-host udev rules from the live USB topology, or
   programming a unique serial into each F9T EVK via its UBX
   interface (we have UBX access; the question is whether the EVK's
   FT232 or its onboard u-blox carries the USB descriptor).
3. `peppar-fix` could grow an `--auto-detect-f9t` mode that scans
   ttyACM* for u-blox VID:PID and probes UBX, but we'd want to error
   loudly if more than one is present.

### #5 — Unrelated F10T (`/dev/f10t`) auto-symlinked on MadHat

**Tried**: routine inventory after deploying udev rules.

**Found**: `/dev/f10t` was created, pointing at `/dev/ttyUSB0`.  The
device's `ID_SERIAL_SHORT=D30GD1PE` matches the F10T rule in
`99-timelab.rules`.

**Stumble character**: not a failure, but a *surprise*.  The F10T was
documented as living on Onocoy.  Either it was physically moved to
MadHat at some point, or there's a second board with the same FTDI
serial number, or someone left it plugged in.  This is the kind of
ambient state that bites you when you assume a host has only the
hardware you intended.

**Prevent next time**: a `peppar-preflight` step that prints every
recognized device on the host, with its serial, and warns about ones
that don't belong to the intended profile.

### #6 — No host config (`config/madhat.toml`) exists

**Tried**: `ls ~/peppar-fix/config/` looking for a host config.

**Found**: `ocxo.toml`, `otcbob1.toml`, `pipuss.toml`, `timehat.toml`,
plus `receivers.toml`.  No `madhat.toml`.

**Missing**: there is no template / generator / inheritance for host
configs.  Bringing up a new host requires hand-creating a TOML by
copying an existing one and editing serial port, PTP device, TICC
port, etc.  Documented in lab-operations.md step 5 ("Edit config/<hostname>.toml")
but with no concrete example or required-field list.

**Prevent next time**:
1. Add a `config/host-template.toml` with every required key marked
   `# REQUIRED` and a comment explaining each.
2. The wrapper should error with a useful message if the host config
   is missing.
3. A `peppar-config-init <hostname>` helper could generate a starting
   point from probed hardware (PTP devs, ttyACM/USB inventory).

### #7 — Host config `systems = "gps,gal"` silently ignored; wrapper hardcodes default

**Tried**: set `systems = "gps,gal"` in `config/madhat.toml` to keep
BDS disabled (per CLAUDE.md "BDS is broken with broadcast ephemeris").

**Observed**: engine started with `Systems: {'gal', 'bds', 'gps'}` —
BDS *included*, despite the host config.

**Root cause**: the `peppar-fix` wrapper has `SYSTEMS="${PEPPAR_SYSTEMS:-gps,gal,bds}"`
defaulting to all three systems, then passes `--systems "$SYSTEMS"`
to the engine.  It does not read `systems` from the host config TOML
(unlike `serial`, `ptp_dev`, etc.).  Setting `systems` in the host
config is effectively dead code — the user has to either set
`PEPPAR_SYSTEMS` in the environment or pass `--systems` on the
command line.

**Stumble character**: silent override is the worst kind.  The user
puts a value in their config, the wrapper ignores it, the engine
runs with a different value, and there is no warning.  On a host
where BDS is broken, this is a footgun.

**Prevent next time**:
1. The wrapper should read `systems` from the host config TOML and
   only fall back to the env/default if the TOML doesn't specify it.
2. Failing that, the wrapper should ignore unknown keys in the TOML
   loudly (warn) so dead config keys are visible.
3. Add a CLAUDE.md note: "BDS broken — set `PEPPAR_SYSTEMS=gps,gal`
   in the environment or pass `--systems gps,gal`."

### #8 — Notable non-stumble: it almost works on first try

**Observed**: after fixing #1–#6, the very first `peppar-fix` invocation:

- Bootstrapped the PHC (469 s step via `ADJ_SETOFFSET`)
- Computed and applied a glide slope from PPS frequency
- Programmed PEROUT pin for TICC chA
- Connected to NTRIP and pulled broadcast ephemeris
- Auto-detected the F9T as L1/L5 (despite `receiver = "f9t"` in config)
- Validated the position file against a live LS fix (56 m sep)
- Started the engine

The wrapper then proceeded to *the DO characterization step*, which
wants a 30-minute freerun.  My `--duration 20` killed it before it
could finish, but everything up to that point worked.

**Implication**: the existing `peppar-fix` wrapper is much closer to
"works on a fresh host" than the previous round of stumbles (ocxo /
otcBob1) suggested.  Most of the bring-up friction is now in the
*environment* (apt deps, udev rules, host config templating), not
in the engine code.  That's worth knowing.

### #9 — DO characterization auto-runs unconditionally on first start, blocks short test runs

**Tried**: `peppar-fix --duration 20` for a quick smoke test.

**Observed**: the wrapper inserted "Step 2c — One-time DO
characterization (1800s freerun)" between the PHC bootstrap and the
normal engine start.  This step runs a 30-minute freerun and writes
`data/do_characterization.json`.  It is skipped on subsequent runs
only if the file already exists.

There is no flag to skip the characterization step.  A short
`--duration` run on a brand-new host either has to wait 30 minutes
for the freerun to finish or has to be artificially short-circuited
by pre-creating a stub `do_characterization.json`.

**Stumble character**: makes the natural smoke-test workflow ("run
peppar-fix for 60 seconds and see if anything obvious is broken")
take 30+ minutes on a new host instead of 60 seconds.

**Prevent next time**:
1. Add `--skip-characterization` to the wrapper.
2. Or: have the wrapper print "DO characterization will take 30
   minutes; pass --skip-characterization to skip" so the user knows
   they can opt out.
3. Or: write the characterization data to a default file at install
   time (a generic per-NIC-type baseline) and only refresh on
   explicit request.

### #10 — Bootstrap leaves the i226 PHC ~100 ms off PPS, servo loops re-bootstrap forever

**Tried**: `PEPPAR_SYSTEMS=gps,gal sudo -E peppar-fix --duration 60`
with the DO characterization stub in place to skip Step 2c.

**Observed**: the engine made it to Phase 2 (steady state servo) and
then died, repeatedly:

```
PPS verify: phi_0 = -100001846 ns (epoch_offset=0)
Glide clamped: 2213635 → 100000 ppb (track_max=100000)
...
=== Phase 2: Steady state (FixedPosFilter) ===
Tracking clamp: adj=+3181457.6ppb limited to +100000.0ppb
Anti-windup: adj=+100000ppb at rail, integral reset
Tracking clamp: adj=+3178357.2ppb limited to +100000.0ppb
PHC error above 100000ns for 3 consecutive epochs — exiting for PHC re-bootstrap (exit code 5)
```

The wrapper restarts the engine, the bootstrap re-runs, the second
bootstrap step is much smaller (~1 ms), but the engine still sees
~100 ms of residual PHC error and dies again.  The cycle repeats
until the outer `timeout 90` kills the wrapper.

**Symptoms in numbers**:
- Initial bootstrap: stepped PHC by +468.5 s (TAI–UTC + cold-start
  delta).  *After* the step, `phi_0 = -100,001,846 ns` — i.e.,
  ~100 milliseconds of residual phase error, vs. the documented
  i226 ADJ_SETOFFSET precision of ~±2 µs.
- Glide computed `+2,213,635 ppb` to chase phi_0 over ~1000 s.
  Clamped to `track_max_ppb = 100,000`.
- In Phase 2, the servo's tracking term computed `+3,181,457 ppb`
  (~3 ms/s of phase change).  That implies the engine is observing
  the PHC drift by ~3 ms/s, which is ~3000 ppm — physically
  impossible for a normal TCXO.
- Subsequent bootstraps step the PHC by ~1 ms each round and the
  ~100 ms residual reappears every time.

**What this is *not***:
- Not a missing dep, missing rules, missing config, or missing
  credential — the wrapper got *all the way through* PHC bootstrap,
  receiver init, NTRIP connect, broadcast ephemeris load, PPP
  position validation, and *into* Phase 2.
- Not the documented `track_max_ppb = 100000` cap — that cap is
  merely revealing that the servo wants something insane.

**What it might be** (untested hypotheses, deferred):
1. **The TimeHat's i226 isn't being clocked by the TCXO**, so the
   PHC is running at some other rate (the bare 25 MHz reference?
   nothing at all?).  This would explain a huge "drift rate" in the
   engine's perception.  TimeHat boards have a clock-source
   mux/strap that may need configuration.
2. **The MadHat hardware is different from TimeHat-#1** in some way
   that affects either the TCXO or the SDP pin mapping.  v5 vs v6,
   defective unit, missing heatsink causing thermal runaway.
3. **`ADJ_SETOFFSET` is not honoring nanosecond precision on this
   kernel/driver combo** — Trixie's `igc` driver might have a
   regression vs the kernel TimeHat #1 was characterized on.
   Worth comparing kernel versions.
4. **`rounded_sec` zero-crossing bug** (memory note
   `project_rounded_sec_bug.md`) — known to drop ~11% of PPS
   observations on TimeHat at PPS-zero-crossing instants; could it
   also cause a 100 ms quantization in the wrong direction at
   bootstrap?  Worth checking.
5. **PEROUT enabled but PPS-IN unprogrammed** — pin 0 was claimed
   for PEROUT (PPS OUT), pin 1 for EXTTS.  If pin 1 isn't actually
   wired to F9T-BOT PPS *yet*, the engine would be reading
   garbage/no events from EXTTS and computing nonsensical PHC
   errors.  Worth physically verifying the wiring.

**Stumble character**: this is a *post*-deployment problem, not a
fresh-install gotcha — the install procedure landed me here cleanly.
But it's the kind of failure that requires hardware-level diagnosis
(scope on the SMA, PHC capability inspection, cross-check against
TimeHat #1 with the same code), not "follow more steps."  It blocks
all subsequent bring-up.

**Prevent next time**:
1. The wrapper should detect the `exit code 5` retry loop and bail
   out after N consecutive failures with a clear "this host's PHC
   is not converging — see ..." message instead of looping until
   the user kills it.
2. The bootstrap should refuse to declare success if `phi_0` is
   above some sanity threshold (e.g., > 1 ms — anything bigger
   means the step didn't land properly).
3. A `peppar-preflight` step should sanity-check the TimeHat by
   reading EXTTS for a few seconds and verifying it sees PPS edges
   ~1 s apart with reasonable jitter, before any bootstrap is
   attempted.

## Status

**As of 2026-04-07 evening**: bring-up reached Phase 2 servo on
first attempt after fixing #1–#7, then hit stumble #10 (PHC
bootstrap residual / runaway adjfine) which is hardware-level and
requires physical inspection.  Stumbles #1–#9 are real lessons
learned and should be folded into `docs/lab-operations.md` and a
future `peppar-install` script regardless of how #10 resolves.
