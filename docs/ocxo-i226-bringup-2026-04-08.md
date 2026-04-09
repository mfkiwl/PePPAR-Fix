# ocxo i226 bring-up — 2026-04-08

Goal: get peppar-fix running on host `ocxo` against the freshly-installed
TimeHAT v6 i226 add-in card, alongside the existing E810 + onboard
PTP-slave NIC.  Stable interface and PHC names landed in commit `a83de14`
(MAC-keyed udev).  TICC #2 was recabled to ocxo with chA → i226 PEROUT
and chB → F9T PPS, matching the TimeHat / MadHat layout.

This doc captures every bump in the road encountered during bring-up,
in the same spirit as `madhat-bringup-2026-04-07.md`.

## What worked first try

- udev rules deployed cleanly: post-reboot `ip -br link` shows `e810p0..3`,
  `i226`, `mbeth` exactly as designed.  No naming surprises, no rename
  conflicts.
- The igc PHC for the new i226 inherited the dialout-group ownership
  from the existing `SUBSYSTEM=="ptp", DRIVERS=="igc"` rule (line 22 of
  `99-timelab.rules`).  Belt-and-suspenders worked.
- TICC #2 came up on `/dev/ticc2` (symlink to `ttyACM0`) and produces
  chB timestamps as soon as the F9T PPS cable is attached.  No surprises
  on the TICC side.

## Stumbles

### Stumble #1 — `/dev/ptp*` numbering shifted under us

**Symptom**: After adding the i226 PHC, the PCI enumeration order put
the new device ahead of both the motherboard NIC and the E810:

| `/dev/ptp*` | Before adding i226 | After adding i226 |
|---|---|---|
| `/dev/ptp0` | mbeth (e1000e) | **i226** (igc) |
| `/dev/ptp1` | E810 (ice) | mbeth (e1000e) |
| `/dev/ptp2` | — | **E810** (ice) |

Anything previously pinned to `/dev/ptp1` expecting the E810 now hits
the motherboard NIC.  `config/ocxo.toml` already had `ptp_dev = "/dev/ptp2"`
written in (unclear from when — possibly prior knowledge of an enumeration
hop), so the E810 path is coincidentally still correct, but this is
fragile.

**Why it bites**: Linux assigns `/dev/ptp*` minor numbers in PHC
registration order, which is roughly PCI enumeration order, which is
roughly bus/slot order — but adding/removing cards or BIOS changes
can shuffle it.  Same class of problem as `eth0`/`enp*` naming, with
the same fix: identify by clock_name (`/sys/class/ptp/ptpN/clock_name`)
or by walking from the network device.

**Mitigation worth doing**:

1. Document the current mapping in `config/ocxo.toml` with comments
   so the next reader knows which card each `/dev/ptpN` is.
2. Longer term: add a udev SYMLINK for PTP devices keyed off the
   parent's PCI address or MAC.  The kernel exposes
   `/sys/class/ptp/ptpN/device → /sys/devices/pci.../net/<iface>` so
   a rule like
   ```
   SUBSYSTEM=="ptp", ATTRS{address}=="f0:b2:b9:31:a7:86", SYMLINK+="ptp-i226"
   SUBSYSTEM=="ptp", ATTRS{address}=="50:7c:6f:53:d5:38", SYMLINK+="ptp-e810"
   ```
   would give us `/dev/ptp-i226` and `/dev/ptp-e810` that survive
   re-enumeration.  Filed as TODO.

### Stumble #2 — E810 PHC `/dev/ptp2` is `root:root`, not `dialout`

**Symptom**: Post-reboot:
```
crw-rw-r-- 1 root dialout 246, 0 ... /dev/ptp0   # i226 (igc rule matched)
crw------- 1 root root    246, 1 ... /dev/ptp1   # mbeth — also missed
crw-rw---- 1 root root    246, 2 ... /dev/ptp2   # E810 (ice not in rule)
```

The current `99-timelab.rules` line 22 only matches `DRIVERS=="igc"`.
The ice (E810) and e1000e (mbeth) PHCs miss the rule and stay at the
default permissions.  peppar-fix on i226 is unaffected today, but any
future ice-driver work on ocxo will need either `sudo` or a rule
extension.

**Mitigation worth doing**: Broaden the udev rule to cover ice and
e1000e too, e.g.
```
SUBSYSTEM=="ptp", KERNEL=="ptp[0-9]*", DRIVERS=="igc|ice|e1000e", MODE="0664", GROUP="dialout"
```
(udev's match-DRIVERS doesn't accept alternation directly — needs three
separate rules or a SUBSYSTEM-level match.)

### Stumble #3 — `~/peppar-fix` on ocxo isn't a git checkout

**Symptom**: `cd ~/peppar-fix && git pull` → `fatal: not a git repository`.
The directory exists, has scripts, configs, data, but no `.git`.  At some
point in the past it was deployed by `rsync` or `scp` instead of `git
clone`, so the standard "update via `git pull` on every lab host"
workflow from CLAUDE.md doesn't apply here.  `scp`-ing individual files
worked as a one-off but isn't how lab hosts are supposed to be kept in
sync.

**Mitigation**: Re-clone from `git@github.com:bobvan/PePPAR-Fix.git`
into `~/git/PePPAR-Fix`, then either symlink `~/peppar-fix` or update
the wrapper paths.  Hold off until current bring-up is done so we
don't move the rug under it.

### Stumble #4 — system python3 lacks `pyserial`, no venv exists

**Symptom**: `python3 -c 'import pyserial'` → `ModuleNotFoundError`.
Running `~/peppar-fix/venv/bin/python` → `No such file`.  No venv has
been set up on ocxo.  The previous overnight runs on this host (Carrier
Phase test, etc.) must have used some other path that's no longer
present, or the deps got removed by an apt upgrade.

Per CLAUDE.md: never use `--break-system-packages`.  The
`docs/lab-operations.md` standard procedure is:
```
python3 -m venv venv && venv/bin/pip install pyubx2 pyserial
```
plus `numpy`, `scipy` and `plotly` for the analysis side.

### Stumble #5 — ocxo.toml `serial = "/dev/ttyACM2"` doesn't exist

**Symptom**: The current `config/ocxo.toml` profile points the engine
at an external F9T-BOT on USB CDC ACM at `/dev/ttyACM2`, but only
`/dev/ttyACM0` exists (and that's TICC #2).  Either the F9T-BOT was
removed, the cable is in a different port, or the config is stale from
a previous experiment.  The only F9T currently visible is on `/dev/gnss0`
(I2C kernel driver) which has the known broken write issue
(`project_e810_i2c_write` in memory).

**Implication for the i226 bring-up**: We can't use this profile as-is.
Need a separate `ocxo-i226.toml` (or override flags) that points the
engine at the i226 PHC (`/dev/ptp0`), uses `ptp_profile = "i226"`, and
sources the F9T from `/dev/gnss0` with the I2C-write workaround in mind
(F9T must already be configured — peppar-fix can't write config to it).

### Stumble #6 — F9T on `/dev/gnss0` rejects CFG-VALSET (known I2C write hazard)

**Symptom**: With a fresh `ocxo-i226.toml` pointing the engine at the
i226 PHC and the I2C-driven F9T on `/dev/gnss0`, `peppar-fix` enters
the receiver-config phase, sends CFG-VALSET to enable the L5/L2C signal
mix, and gets back a NAK every time:
```
INFO   Signals: GPS GPS-L1CA+GPS-L5Q, GAL GAL-E1C+GAL-E5aQ, BDS ...
WARNING NAK received for CFG-VALSET
WARNING   Signals: ... TIMEOUT (no ACK)
INFO L5 signal config NAK'd — falling back to L2C
INFO   Signals: GPS GPS-L1CA+GPS-L2CL, GAL GAL-E1C+GAL-E5bQ, BDS ...
INFO   Signals: ... OK
... but later ...
ERROR Receiver still not producing dual-freq observations after reconfiguration (0/0 SVs)
```
Three retry attempts, all the same outcome.  Engine exits with
`Receiver configuration failed after 3 attempts`.

**Root cause**: Already documented in project memory
(`project_e810_i2c_write`): `os.write()` against `/dev/gnss0` (the
kernel `gnss` char device backed by I2C) is unreliable.  Some writes
(the L2C fallback) appear to take, but the verification readback shows
0/0 SVs producing dual-frequency observations, so something is going
wrong even on the writes that didn't NAK.  The reads work fine —
RAWX/TIM-TP/SFRBX flow into peppar-fix as expected — it's only the
config-write path that's broken.

**Why this didn't bite before**: The previous overnight Carrier Phase
runs on ocxo (project task #14) used a separate **external F9T-BOT on
USB CDC ACM at `/dev/ttyACM2`**.  That device path is now gone — the
F9T-BOT has been disconnected, removed, or its USB cable is in a
different port.  The current `config/ocxo.toml` still references it.
The I2C-driven F9T behind `/dev/gnss0` is the only F9T currently
visible, and it triggers the I2C-write hazard.

**Implications for the user's "factory-fresh F9T must work" rule**:
The current code path is correct in spirit — peppar-fix tries to set
all the F9T's required configuration explicitly.  The hazard is the
*transport*, not the requirements: the kernel `gnss` char device path
silently mishandles UBX writes.  Two real fixes:

1. **Use `/dev/i2c-N` directly with the i2c-dev raw register protocol**
   instead of the `gnss` char device.  Bypasses the kernel driver's
   write quirks at the cost of becoming a write-only path that can't
   coexist with the kernel driver's read side.
2. **Add a `--skip-receiver-config` flag** that lets peppar-fix assume
   the F9T was pre-configured externally (BBR or flash) and skip the
   CFG-VALSET phase entirely.  Easy escape hatch but loses the
   "factory-fresh works automatically" guarantee.

**Operational workaround for *today's* bring-up**: connect the F9T-BOT
back via USB CDC ACM so the I2C path isn't on the critical path.  That
gets us a working ocxo i226 servo run; the I2C-write fix can land
separately.

**Status**: blocked here pending either a USB F9T being reconnected to
ocxo, or guidance on which longer-term fix to take.

### Stumble #7 — Position file from old antenna confused validation after antenna swap

**Symptom**: After the F9T-TOP was recabled from Patch3 to the UFO antenna,
the saved `data/position.json` (from the Patch3 era) disagreed with live
LS by 103–134 m, beyond the engine's 100 m hard threshold.  Engine fell
back to Phase 1 PPP bootstrap on every retry, taking ~5 minutes per cycle.

**Mitigation**: Delete `data/position.json` after physical antenna moves,
or update `known_pos` in the host config to a value within 100 m of the
new antenna's actual location.  The engine saves a fresh `position.json`
once Phase 1 converges.

### Stumble #8 — Phantom duplicate engine on TimeHat

**Symptom**: After kernel patch + reboot, when I launched the TimeHat
smoke test, `pgrep` showed **two** `peppar_fix_engine` processes
running concurrently, started by two different sudo/bash chains
(parents 1939 and 2031) about two minutes apart.  Both had identical
command lines.  TIOCEXCL on `/dev/ptp0` should have prevented this.

The competing engines made the TICC chA–chB measurement read 500 ms
offset (each engine was re-programming PEROUT and the captured edges
were a mix of two phases).  Killing both and relaunching one engine
showed chA–chB ≈ 974 ns (correct).

**Root cause**: never determined.  No systemd auto-start, no cron, no
crontab.  Suspect a residual ssh from an earlier rejected tool call
that quietly ran on the host even though the local Bash tool reported
"rejected".  Worth investigating tomorrow.

### Stumble #9 — TimeHAT igc 6.8-port loaded but PEROUT still drifting on ocxo

**Symptom**: Installed `drivers/igc-timehat-edge-6.8/` (the kernel-port
agent's first build, never tested before today).  DKMS build succeeded,
module loaded with srcversion `DF1857673C3E208F0D58A25` and
`/sys/module/igc/parameters/edge_check_delay_us=20`.  But:

- TICC chA (i226 PEROUT) drifted at ~3.5 ns/s relative to chB (F9T PPS),
  signature of frequency-mode operation despite the patched module
  supposedly removing the `period_ns == 1_000_000_000` special case.
- chA–chB widened at ~3.5 ms/s (3500 ppm).  No TCXO drifts that fast.
- The engine exited with `PHC error above 100000ns for 3 consecutive
  epochs — exiting for PHC re-bootstrap (exit code 5)` every ~30s of
  steady state.  The wrapper retry-loops indefinitely.
- I verified by reading the 6.8 source: the special-case removal IS
  present (`use_freq` only sets when `ns <= 70000000 || ns == 125000000
  || ns == 250000000`), so `period_ns=1_000_000_000` (which gives
  `ns=500000000`) should fall through to TT mode.  The patch text is
  correct.  Something else is causing this.

**Note**: The same symptoms appeared after reverting to stock 6.8 igc
+ the userspace workaround (`period_ns=999_999_999`).  So this is **not**
specific to the kernel patch port — it's specific to ocxo's hardware
configuration.  ocxo runs a bare Intel-branded i226 retail card with
hand-soldered pin headers, a Timebeat u.FL adapter, and SMA connectors.
Possible suspects:

1. **Signal integrity on the hand-wired SDP path** — the PEROUT pin's
   rising edge may be slow or noisy, causing the i226's internal PHC
   retrigger logic to fire at unexpected times.
2. **Timebeat adapter electrical mismatch** — possibly an impedance
   discontinuity or hidden termination on the SDP0 (PEROUT OUT) path.
3. **Different SDP register defaults on this i226 silicon stepping** —
   Intel may have shipped different firmware versions of the i226 chip
   over time; the bare retail card might be a different stepping than
   the TimeHAT v6 (MadHat) and v5 (TimeHat) cards which both work fine.
4. **GPIO direction set wrong** — the patched module reads SDP pin
   level for the dual-edge filter; if the pin was left in input mode
   instead of output mode for SDP0, the read voltage would be
   meaningless.

**Status**: dropped ocxo from tonight's overnight 3-way reference run.
Will revisit tomorrow with proper debugging time.

**Mitigation for tonight**: 2-way overnight on TimeHat + MadHat only,
both of which work cleanly with the patched igc module on kernel 6.12.
