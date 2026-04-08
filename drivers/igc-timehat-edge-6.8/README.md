# Intel igc driver — TimeHAT PPS dual-edge fix (kernel 6.8 port)

This is a port of the vendored TimeHAT `intel-igc-ppsfix` driver to
Linux kernel 6.8, intended for x86-64 Debian hosts running a stock
`6.8.0-<N>-generic` kernel (specifically `ocxo`, which has an Intel
i226 add-in card in addition to its E810).

The original 6.12 version lives in `drivers/igc-timehat-edge/` and
targets Raspberry Pi 5 / Pi OS Trixie (MadHat).  Do not touch that
tree — it is still in use.

This document is a **delta** against `drivers/igc-timehat-edge/README.md`.
Read that one first for the full story of what the patch does and
why.  Only the differences are documented here.

## What was ported

All four logical hunks from the 6.12 TimeHAT patch transferred
essentially verbatim to the 6.8 igc source tree:

1. **Part A — EXTTS flag forcing** (`igc_ptp.c`): rewrites every
   EXTTS request to `PTP_ENABLE_FEATURE | PTP_RISING_EDGE`, stores
   `ts{0,1}_pin` and `ts{0,1}_flags`.
2. **Part B — GPIO-level edge filter** (`igc_main.c`, in
   `igc_tsync_interrupt`): `udelay(edge_check_delay_us)`, reads
   `igc_sdp_val[pin]` from `CTRL` / `CTRL_EXT`, drops mismatched
   events.  `edge_check_delay_us` and `edge_check_invert` module
   parameters, same semantics as 6.12.
3. **PEROUT 1 PPS fix** (`igc_ptp.c`): remove `ns == 500000000LL`
   from the frequency-mode special case so a 1 Hz PEROUT request
   uses Target Time mode.
4. **Per-channel state** (`igc.h`): `ts0_pin`, `ts0_flags`,
   `ts1_pin`, `ts1_flags` added to `struct igc_adapter`.
5. **SDP data-value register bits** (`igc_defines.h`):
   `IGC_CTRL_SDP0_VAL`, `IGC_CTRL_SDP1_VAL`, `IGC_CTRL_EXT_SDP2_VAL`,
   `IGC_CTRL_EXT_SDP3_VAL` added (not present in upstream 6.8
   headers).

## What changed between 6.12 and 6.8 in igc

The patch logic is byte-compatible with 6.8.  The only meaningful
differences between the trees that affected the port are:

- **`igc_leds.c` does not exist in 6.8.** LED classdev support was
  added to igc upstream after 6.8.  Consequences:
  - `intel-igc-ppsfix/src/` does **not** ship `igc_leds.c` or
    `igc_leds.h`.
  - The out-of-tree `Makefile` does **not** list `igc_leds.o`.
  - `struct igc_adapter` in `igc.h` does **not** have the
    `mutex led_mutex` or `struct igc_led_classdev *leds` fields.
- Small line-number drift in `igc_ptp.c` and `igc_main.c`, no
  semantic differences in the patch regions.

Kernel API symbols checked against 6.8:

| Symbol / struct member                | 6.8   | 6.12  | Notes |
|---------------------------------------|-------|-------|-------|
| `ptp_clock_event` signature           | same  | same  | 2-arg (clock, event) |
| `struct ptp_clock_event.type/index/timestamp` | same | same | |
| `PTP_RISING_EDGE`, `PTP_FALLING_EDGE`, `PTP_ENABLE_FEATURE`, `PTP_STRICT_FLAGS` | present | present | |
| `ptp_find_pin`                        | same  | same  | |
| `rq->extts.flags`, `rq->extts.index`  | same  | same  | |
| `udelay`                              | same  | same  | pulled transitively via `<linux/pm_runtime.h>` like upstream |
| `IGC_N_SDP`, `IGC_CTRL`, `IGC_CTRL_EXT`, `IGC_TSICR_AUTT{0,1}` | same | same | |
| `struct igc_adapter::sdp_config[]`    | same  | same  | |
| `igc_adapter::leds`, `led_mutex`      | absent | present | dropped in 6.8 build |

No PTP, PCI, or PHC infrastructure calls that the patch touches
changed between 6.8 and 6.12 in a way that required rework.

## What is NOT included

`igc_leds.c` / `igc_leds.h` are intentionally omitted and the
Makefile does not list `igc_leds.o`.  If you copy this tree onto a
kernel newer than ~6.11 it will fail to build — use
`drivers/igc-timehat-edge/` for 6.12 instead.

## DKMS package name / version

- `PACKAGE_NAME=igc`
- `PACKAGE_VERSION=6.8.0-ppsfix.1`

Distinct from the 6.12 version (`6.12.0-ppsfix.1`) so both can
coexist in `/var/lib/dkms/igc/` without collision.

## Installation on ocxo

```sh
# On the GT machine (this tree):
rsync -av drivers/igc-timehat-edge-6.8/ ocxo:peppar-fix/drivers/igc-timehat-edge-6.8/

# Or simply: git pull on ocxo if the repo is cloned there.

# On ocxo:
cd ~/peppar-fix/drivers/igc-timehat-edge-6.8
sudo ./build-and-install.sh --load
```

The script will install `dkms` and `linux-headers-$(uname -r)` if
missing, stage under `/usr/src/igc-6.8.0-ppsfix.1/`, run
`dkms add`/`build`/`install`, replace the stock `igc.ko{,.xz,.zst}`
under `/lib/modules/$(uname -r)/kernel/...` with a `.bak` backup,
run `depmod -a` and `update-initramfs -u`.

After install, verify with:

```sh
sudo testptp -d /dev/ptp0 -L1,1     # pin 1 -> EXTTS chan 1
sudo testptp -d /dev/ptp0 -e 5      # exactly 5 events, 1 s apart
cat /sys/module/igc/srcversion      # should differ from stock
cat /sys/module/igc/parameters/edge_check_delay_us  # should print 20
```

## Relationship to the runtime workaround (commit 8d9ab87)

`8d9ab87` adds a userspace workaround for the dual-edge bug in
`peppar_fix` itself so the servo behaves on `ocxo` even without a
patched kernel.  This DKMS package is the proper kernel-level fix;
once installed and loaded, the runtime workaround becomes redundant
(but harmless — same "defense in depth" arrangement as on MadHat
with `DualEdgeFilter`).

## Provenance

- Stock 6.8 igc source: [`v6.8.12` from kernel.org stable
  tree](https://git.kernel.org/pub/scm/linux/kernel/git/stable/linux.git/tree/drivers/net/ethernet/intel/igc?h=v6.8.12),
  fetched 2026-04-08.
- Patches: the four hunks from
  `drivers/igc-timehat-edge/intel-igc-ppsfix/src/` (commit ccdd8ae /
  earlier), re-applied by hand — each hunk lands on lines that are
  byte-identical between the 6.8 and 6.12 igc trees.
- License: GPL-2.0-only (inherited from upstream igc).
