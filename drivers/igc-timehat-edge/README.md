# Intel igc driver — TimeHAT PPS dual-edge fix

## What this is

A vendored copy of the [Time Appliances Project's TimeHAT](https://github.com/Time-Appliances-Project/TimeHAT)
patched `igc` kernel driver for Linux 6.12, packaged as a DKMS module.
Installs on top of the stock Raspberry Pi OS Trixie igc driver and
fixes the i226's PPS input/output behavior.

Source: `intel-igc-ppsfix_rpi5_6.12.62.zip` from the TimeHAT GitHub
release, vendored into `intel-igc-ppsfix/` so we have it under version
control and can layer our own patches on top deterministically.

## Why we want it

The stock Linux igc driver has two unrelated problems for the way
PePPAR Fix uses i226 timing hardware.  This patch fixes both, plus
adds groundwork the `igc-adjfine-fix` series may build on.

### 1. EXTTS dual-edge timestamping (the falling-edge bug)

The i226 hardware unconditionally captures both rising and falling
PPS edges and the stock driver passes them all to userspace.  With
a typical GPS receiver emitting a 100 ms wide PPS pulse at 1 Hz,
EXTTS reports 2 events/sec 100 ms apart, the engine arbitrarily
picks the falling edge half the time, and the servo demands ~3 Mppb
to chase a spurious 100 ms phase error.  See
`docs/madhat-bringup-2026-04-07.md` stumble #10 for the full
debugging trail.

The TimeHAT patch fixes this at the kernel level using **two
mechanisms working together**:

**Part A — flag forcing** (`intel-igc-ppsfix/src/igc_ptp.c:265`):

```c
case PTP_CLK_REQ_EXTTS:
    /* PPS fix: force rising edge only ... */
    if (rq->extts.flags & PTP_ENABLE_FEATURE) {
        rq->extts.flags = PTP_ENABLE_FEATURE | PTP_RISING_EDGE;
    }
```

This rewrites every EXTTS request to ask for rising edges only,
overriding whatever userspace passed in.  By itself it would not be
enough, because the i226 hardware ignores the request flag and
captures both edges anyway.

**Part B — interrupt-handler GPIO filter**
(`intel-igc-ppsfix/src/igc_main.c:5466–5547`):

When an EXTTS interrupt fires, the handler

1. Waits `edge_check_delay_us` (default 20 µs) for the line to settle.
2. Reads the actual GPIO pin level via `gpio_get_value()`.
3. Compares the level to the saved per-channel `ts{0,1}_flags` to
   determine whether the captured event was a rising or falling edge.
4. **Drops the event before delivering it to userspace** if the polarity
   doesn't match what was requested.

Two new module parameters control the filter:

- `edge_check_delay_us` — settling delay before pin read (default 20 µs)
- `edge_check_invert` — flip the expected pin level for hardware that
  inverts the signal somewhere (default 0)

This is what *actually* fixes the dual-edge problem — Part A is
bookkeeping, Part B is the filter.

**Relationship to PePPAR Fix's userspace filter**: PePPAR Fix
already ships its own filter at
`peppar_fix.ptp_device.DualEdgeFilter` (commit `aa20423`).  That
filter is purely temporal — it drops any EXTTS event closer than
0.4 s to the previous accepted event — and works on a stock kernel.
The TimeHAT kernel patch is **defense in depth**: it lets
non-peppar-fix tools (`testptp`, `ts2phc`, custom scripts, future
peppar-fix variants) see clean rising edges without each having to
implement their own filter, and it inspects the physical pin state
rather than relying on temporal heuristics.  Both can run together
safely; the kernel filter just makes the userspace filter a no-op.

### 2. PEROUT 1 PPS frequency-mode bug

Stock `igc` has a special case that, when asked for a 1 Hz
periodic output, drops into "frequency mode" which produces a 50%
duty-cycle square wave (500 ms HIGH / 500 ms LOW) instead of a
proper PPS pulse.  The TimeHAT patch removes the `500000000` special
case so 1 Hz uses Target Time mode like every other rate, producing
a clean short pulse.

**Overlap with PePPAR Fix's userspace PEROUT fix**: commit `01a401c`
in this repo sets the `PTP_PEROUT_DUTY_CYCLE` flag with a 1 ms ON
time so the kernel produces a clean 1 ms pulse instead of the
default ~500 ms wide one.  The TimeHAT patch reaches the same goal
through a different code path (forcing TT mode unconditionally).
Both fixes target the same symptom — `peppar-fix` works on stock
kernels with `01a401c`, and `testptp -p 1000000000` works on
patched kernels with the TimeHAT fix.  After installing this patch
the userspace `PTP_PEROUT_DUTY_CYCLE` request should still be
honored (TT mode honors per-pulse ON time), but this is **untested**
and worth verifying with a TICC measurement after the first patched
boot.

### 3. Per-channel pin/flags tracking (groundwork)

Adds `ts0_pin`, `ts0_flags`, `ts1_pin`, `ts1_flags` fields to
`struct igc_adapter` (`intel-igc-ppsfix/src/igc.h:331-334`),
populated by the EXTTS request handler in Part A and consumed by
the GPIO edge filter in Part B.  Our `igc-adjfine-fix` series
(`drivers/igc-adjfine-fix/`) does not currently use these fields,
but they're useful future infrastructure for any code that needs
per-channel polarity awareness in the interrupt path.

## What's included

```
drivers/igc-timehat-edge/
├── README.md                                ← this file
├── build-and-install.sh                     ← DKMS build/install wrapper
└── intel-igc-ppsfix/                        ← vendored TimeHAT source
    ├── dkms.conf                            ← PACKAGE_VERSION=6.12.0-ppsfix.1
    ├── README.md                            ← upstream TimeHAT readme
    └── src/
        ├── igc_ptp.c                        ← contains the PPS fix at line ~260
        ├── igc_main.c
        └── ... (full igc source tree, ~28 files)
```

## Installation

```sh
cd drivers/igc-timehat-edge
sudo ./build-and-install.sh          # build and install via DKMS
sudo ./build-and-install.sh --load   # build, install, and load now (no reboot)
```

The script:

1. Installs `dkms` and the kernel headers package if missing.
2. Copies `intel-igc-ppsfix/` to `/usr/src/igc-6.12.0-ppsfix.1/`.
3. Runs `dkms add` / `dkms build --force` / `dkms install --force`.
4. Replaces the stock `igc.ko.xz` in
   `/lib/modules/$(uname -r)/kernel/drivers/net/ethernet/intel/igc/`
   with the patched version (backing up the original to `igc.ko.xz.bak`).
5. Runs `depmod -a` and `update-initramfs -u -k $(uname -r)`.
6. Optionally reloads the module immediately if `--load` is passed.
   Otherwise the patched module loads on next reboot.

After installation, verify with:

```sh
sudo testptp -d /dev/ptp0 -L1,1          # pin 1 → EXTTS chan 1
sudo testptp -d /dev/ptp0 -e 5           # read 5 PPS edge timestamps
# ↑ should report exactly 5 events 1 s apart, no falling-edge "extras"

sudo testptp -d /dev/ptp0 -L0,2          # pin 0 → PEROUT
sudo testptp -d /dev/ptp0 -p 1000000000  # request 1 Hz PEROUT
# ↑ measure with a TICC; should be a clean ~1 ms PPS pulse, not a
#   square wave
```

## Composing with `igc-adjfine-fix`

`drivers/igc-adjfine-fix/` contains a separate three-patch series
(`0001` / `0002` / `0003`) that fixes a race in the stock `igc` driver
between `clock_adjtime(ADJ_FREQUENCY)` and hardware TX timestamping.
That race causes `Tx timestamp timeout` errors when peppar-fix
disciplines the PHC at 1 Hz while ptp4l is also using hardware
timestamping.

Both patch sets target the same file (`igc_ptp.c`) and the same kernel
generation (6.12).  They are *complementary* and should both be
applied for production use.  The intended layering is:

```
   Stock Raspberry Pi OS igc        (ships with kernel)
   ├── TimeHAT PPS fixes            (this directory; replaces igc.ko.xz)
   └── igc-adjfine-fix              (drivers/igc-adjfine-fix; layered on top)
```

The TimeHAT vendored source is already a complete, self-contained
copy of the 6.12 igc tree — so the easiest way to combine is to
re-apply the `igc-adjfine-fix` patches against the
`intel-igc-ppsfix/src/` tree, rebuild, and reinstall via DKMS with a
new `PACKAGE_VERSION` (e.g. `6.12.0-ppsfix-adjfine.1`).

A future update to this directory will:

1. Rebase the `igc-adjfine-fix` patches against `intel-igc-ppsfix/src/`.
2. Bump `dkms.conf` `PACKAGE_VERSION` so DKMS treats it as a new build.
3. Document the combined module in this README.

For now, only the TimeHAT PPS fix is installed.  Apply the
`igc-adjfine-fix` patches separately when needed (see
`drivers/igc-adjfine-fix/README.md`).

## Provenance

- Source: <https://github.com/Time-Appliances-Project/TimeHAT>
- File: `intel-igc-ppsfix_rpi5_6.12.62.zip`
- Vendored: 2026-04-07 (during MadHat fresh-host bring-up; see
  `docs/madhat-bringup-2026-04-07.md` stumble #10)
- License: see `intel-igc-ppsfix/LICENSE` (GPL-2.0-only, inherited
  from the upstream Linux igc driver)
