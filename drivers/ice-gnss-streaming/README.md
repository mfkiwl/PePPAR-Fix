# Ice GNSS Streaming Delivery Patch

## Problem

The Intel `ice` driver for E810-XXVDA4T NICs reads GNSS data from the
onboard u-blox ZED-F9T via I2C in 15-byte chunks.  The stock driver
(both the in-kernel version and Intel's out-of-tree release v2.4.5)
accumulates these chunks into a 4 KB page buffer and delivers the
entire batch to userspace via `/dev/gnss0` only after the read loop
completes.

With ~800 bytes per 1 Hz GNSS epoch, the driver needs ~54 I2C reads at
20 ms intervals (~1080 ms), then waits an additional 100 ms before
polling again.  Two epochs fill the page buffer, resulting in **~2100 ms
delivery latency** — observed as `read()` calls that block for over 2
seconds and return exactly 4096 bytes.

This makes real-time GNSS clock discipline impossible.  The servo
receives stale observation data seconds after the GNSS epoch it
describes, causing pipeline stalls, holdover events, and oscillating
corrections.

## Symptoms

When running `scripts/read_stall_probe.py /dev/gnss0` on an affected
system:

```
62 reads in 120s (0.5 reads/s)
max bytes in one read: 4096
max delta between reads: 2131 ms
```

Compare with a healthy system (USB transport on TimeHat, or this patch
applied):

```
14049 reads in 120s (112 reads/s)
max bytes in one read: 15
max delta between reads: 399 ms
```

## Fix

The patch changes the driver to stream each 15-byte I2C chunk to
userspace immediately via `gnss_insert_raw()` inside the read loop,
instead of batching into a page.  It also removes the 100 ms
post-delivery delay and uses the 20 ms polling interval uniformly.

The patch is adapted from a 3-patch series by Michal Schmidt
(Red Hat), submitted to `intel-wired-lan` in December 2024, reviewed
by Karol Kolacinski (Intel) and Simon Horman, but **not yet merged
into mainline Linux or the Intel out-of-tree release** as of March
2026.

## Affected systems

- Any E810-XXVDA4T (or E810-XXVDA2T) with onboard GNSS
- Ubuntu 24.04 kernels 6.8.x through at least 6.17 (HWE)
- Intel out-of-tree ice driver v2.4.5 (Dec 2025)
- All Linux kernels up to at least 6.17 (patches not merged upstream)

## Build and install

```bash
cd drivers/ice-gnss-streaming
./build-and-install.sh          # clone, patch, build, install
./build-and-install.sh --load   # also load the module immediately
```

Prerequisites (installed automatically by the script):
- `build-essential` (gcc, make)
- `linux-headers-$(uname -r)`
- Internet access (clones Intel's ice driver from GitHub)

The script:
1. Clones Intel's out-of-tree ice driver
2. Applies the streaming delivery patch
3. Builds the `ice.ko` module
4. Decompresses the DDP firmware package if needed
5. Installs to `/lib/modules/$(uname -r)/updates/` (takes priority
   over the stock module on next boot)

## Reverting

To revert to the stock driver:

```bash
sudo rm /lib/modules/$(uname -r)/updates/drivers/net/ethernet/intel/ice/ice.ko
sudo depmod -a
sudo rmmod irdma 2>/dev/null; sudo rmmod ice; sudo modprobe ice
```

## Upstream references

- [Patch 1/3: downgrade gnss_insert_raw warning](https://www.mail-archive.com/intel-wired-lan@osuosl.org/msg08351.html)
- [Patch 2/3: lower the latency of GNSS reads](https://www.mail-archive.com/intel-wired-lan@osuosl.org/msg08349.html)
- [Patch 3/3: remove special delay after processing a read batch](https://www.mail-archive.com/intel-wired-lan@osuosl.org/msg08350.html)
- [Cover letter: ice GNSS reading improvements](https://www.mail-archive.com/intel-wired-lan@osuosl.org/msg08352.html)
