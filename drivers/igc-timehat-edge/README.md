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

The stock Linux igc driver has two related problems for the way
PePPAR Fix uses i226 timing hardware:

1. **EXTTS dual-edge timestamping.** The i226 hardware unconditionally
   captures both rising and falling PPS edges and the stock driver
   passes them all to userspace.  With a typical GPS receiver emitting
   a 100 ms wide PPS pulse at 1 Hz, the engine sees 2 events/sec
   100 ms apart and the servo demands +3 Mppb to chase the spurious
   error.  See `docs/madhat-bringup-2026-04-07.md` stumble #10 for
   the full debugging trail.

   **PePPAR Fix already filters this in userspace** via
   `peppar_fix.ptp_device.DualEdgeFilter` (commit `aa20423`).  The
   kernel patch is defense in depth and lets non-peppar-fix tools
   (`testptp`, `ts2phc`, custom scripts) see clean rising edges.

2. **PEROUT 1 PPS quirk.** Stock `igc` produces a 1 Hz square wave
   instead of a proper 1 PPS pulse when `testptp -p 1000000000` is
   used.  The TimeHAT patch fixes the periodic-output configuration
   path to honor the requested period correctly.

The TimeHAT patch also adds per-channel EXTTS pin and flags tracking,
which is groundwork the `igc-adjfine-fix` patch series (in this repo
at `drivers/igc-adjfine-fix/`) builds on.  See "Composing with
igc-adjfine-fix" below.

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
