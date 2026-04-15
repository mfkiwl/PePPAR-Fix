# igc Kernel Patches — Inventory and Deployment Status

The Intel i225/i226 igc driver ships with two bugs that affect PePPAR
Fix.  We carry two independent patch sets in `drivers/` to fix them.
Both must be applied together for correct operation on any i226 host
that runs PHC discipline + ptp4l.

## Patch inventory

### Patch 1: TimeHAT PPS dual-edge fix ("ppsfix")

**Source directory**: `drivers/igc-timehat-edge/` (kernel 6.12, aarch64)
and `drivers/igc-timehat-edge-6.8/` (kernel 6.8, x86-64).

**What it fixes**:

1. **EXTTS dual-edge timestamping**: Stock igc delivers both rising
   and falling PPS edges to userspace.  With a 100 ms wide GPS PPS
   pulse, this gives 2 events/sec 100 ms apart, and the engine
   randomly picks the wrong edge half the time.  The patch adds a
   GPIO-level filter in the interrupt handler that drops falling edges
   before they reach userspace.

2. **PEROUT 1 Hz frequency-mode bug**: Stock igc has a special case
   that, when asked for a 1 Hz periodic output, switches to "frequency
   mode" producing a 50% duty-cycle square wave (500 ms HIGH / 500 ms
   LOW) instead of a proper PPS pulse.  The patch removes the special
   case so 1 Hz uses Target Time mode.

**Detection**: The patched driver exposes module parameters
`edge_check_delay_us` and `edge_check_invert` that the stock driver
does not have.  Check with:

```bash
cat /sys/module/igc/parameters/edge_check_delay_us  # → "20" if patched
```

**DKMS package version**: `igc/6.12.0-ppsfix.1` (aarch64) or
`igc/6.8.0-ppsfix.1` (x86-64).

### Patch 2: adjfine TX timestamp race fix

**Source directory**: `drivers/igc-adjfine-fix/`

**What it fixes**: `igc_ptp_adjfine_i225()` writes the `IGC_TIMINCA`
register without synchronization.  When the hardware captures a TX
timestamp while TIMINCA is changing, the captured value is corrupt.
After 15 seconds the driver logs `Tx timestamp timeout` and the skb
is freed.  Repeated timeouts wedge the EXTTS subsystem, breaking PPS
capture and causing ptp4l to cycle through MASTER → FAULTY endlessly.

**Trigger**: Any combination of PHC frequency discipline (peppar-fix
calling `adjfine()` at 1 Hz) and PTP hardware timestamping (ptp4l
sending Sync packets).  At `logSyncInterval -7` (128 Hz): ~30 min
MTBF.  At `logSyncInterval 0` (1 Hz): ~64 hour MTBF.

**The fix** (v3): Hold `tmreg_lock`, temporarily disable TX
timestamping via `TSYNCTXCTL`, write TIMINCA, re-enable.  The disable
window is ~1 µs — any TX packet in that window simply gets no hardware
timestamp (no corruption, no timeout, no wedge).

**Patch file**: `drivers/igc-adjfine-fix/0003-igc-disable-tx-tstamp-around-timinca.patch`

**Detection**: The patched `igc_ptp_adjfine_i225()` contains
`TSYNCTXCTL` references.  The simplest runtime check: if the driver is
patched with the ppsfix AND has adjfine fix, `igc_ptp.c` has 12+
`TSYNCTXCTL` references in the DKMS source (vs 9 for ppsfix-only).
At runtime, the definitive test is absence of `Tx timestamp timeout`
in `dmesg` during PHC discipline + ptp4l operation.

**Upstream status**: Submitted to intel-wired-lan list.  Maintainer
suggested `tmreg_lock` (incorporated in v3).  Discussion ongoing.  See
`drivers/igc-adjfine-fix/upstream-submission.txt` and
`upstream-reply-v3.txt`.

## How patches are composed

Both patches target `igc_ptp.c` in the same kernel generation (6.12).
The ppsfix is a complete vendored igc source tree.  The adjfine fix is
a patch applied on top.  The intended layering:

```
Stock Raspberry Pi OS igc        (ships with kernel)
└── TimeHAT PPS fix              (drivers/igc-timehat-edge/)
    └── adjfine TX tstamp fix    (drivers/igc-adjfine-fix/0003-*)
```

The adjfine patch must be applied to the ppsfix vendored source tree in
`drivers/igc-timehat-edge/intel-igc-ppsfix/src/igc_ptp.c`, then the
combined tree is built and installed via DKMS as a single module.

## Host applicability

| Host | Has i226? | Needs igc patches? | Notes |
|------|-----------|---------------------|-------|
| TimeHat | Yes (TimeHAT board, PCIe) | **Yes** | Runs ptp4l as GM + PHC discipline |
| MadHat | Yes (TimeHAT board, PCIe) | **Yes** | Runs PHC discipline; ptp4l planned |
| clkPoC3 | No (Pi 4, bcmgenet only) | No | DKMS tree installed but igc never loads |
| ocxo | Yes (E810-XXVDA4T) | No — uses `ice` driver | Different driver, different bugs |

## Deployment verification checklist

Run on each i226 host after any driver install or kernel update:

```bash
# 1. Confirm DKMS-installed igc is loaded (not stock)
cat /sys/module/igc/srcversion
# Should match the DKMS-built srcversion, not the stock kernel module.

# 2. Confirm ppsfix patch (edge filter)
cat /sys/module/igc/parameters/edge_check_delay_us
# Expected: "20" — if missing, ppsfix is NOT installed.

# 3. Confirm adjfine patch (TSYNCTXCTL in adjfine)
sudo grep -c TSYNCTXCTL /usr/src/igc-6.12.0-ppsfix.1/src/igc_ptp.c
# Expected: 12+ — if 9, adjfine patch is NOT applied.
# (This checks the DKMS source; it implies the built module has it,
# but only if DKMS was rebuilt after patching.)

# 4. Runtime confirmation: no TX timestamp timeouts
sudo dmesg | grep -c "Tx timestamp timeout"
# Expected: 0 after reboot with both patches.

# 5. ptp4l is not cycling through FAULTY (if running)
sudo journalctl -u ptp4l --since "1 hour ago" | grep -c FAULTY
# Expected: 0 (or only at startup).
```

## Incident history

| Date | Host | Symptom | Root cause | Resolution |
|------|------|---------|------------|------------|
| 2026-04-01 | TimeHat | Tx timestamp timeout every ~30 min | adjfine race (Patch 2 missing) | Discovered bug, wrote patches |
| 2026-04-07 | MadHat | Dual-edge EXTTS, servo demands 3 Mppb | Missing ppsfix (Patch 1) | Installed ppsfix DKMS |
| 2026-04-14 | TimeHat | 1,266 Tx timestamp timeouts over 18h; ptp4l MASTER→FAULTY loop every 24s; AntPosEstThread correlation gate stall | adjfine race — ppsfix installed but adjfine patch never applied to ppsfix source tree | Applied adjfine patch to ppsfix tree (this fix) |

## Old / stale DKMS trees

The Intel out-of-tree driver `igc/5.4.0-7642.46` was used for early
adjfine patch development.  It has the adjfine fix but NOT the ppsfix.
It should be removed from any host where it exists — it's half-patched
and the 5.4.0 source base predates the ppsfix changes, making it
unsuitable as a production driver.  The patch files in
`drivers/igc-adjfine-fix/` preserve the work for posterity.
