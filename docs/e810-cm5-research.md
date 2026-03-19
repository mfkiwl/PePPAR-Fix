# E810-XXVDA4T on Raspberry Pi CM5: Feasibility Research

## Summary

**The E810-XXVDA4T will NOT work on a Raspberry Pi CM5 IO board.** The
ice driver does not compile on aarch64. Use an x86 PC as the host.

## Question 1: PCIe lane negotiation

**Will the E810 negotiate down to x1?** Yes.

The E810 controller supports PCIe 4.0 x8 natively but will negotiate
down to x1 if that's all the slot provides. An [Intel Community
thread](https://community.intel.com/t5/Ethernet-Products/E810-XXVDA2-running-at-PCIe-x4/td-p/1451852)
confirms the E810 negotiates to x4 or x1 when the slot is limited.

The CM5 IO board's M.2 M-key connector provides PCIe Gen 2 x1 (5 Gb/s).
For PTP timestamping, bandwidth is irrelevant — we're sending/receiving
PTP packets, not bulk data. x1 Gen 2 is more than sufficient.

**Verdict: PCIe lanes are not a blocker.**

## Question 2: Power delivery

**Can the M.2 connector power the E810?** Probably not.

The E810-XXVDA4T idles at ~15W and can draw 20W+ with optics. The M.2
M-key specification provides 3.3V at up to 3A (~10W). An M.2-to-PCIe
adapter would need external 12V power injection (PCIe auxiliary or
Molex connector on the adapter board).

This is solvable with the right adapter, but adds complexity and
another power supply.

**Verdict: Solvable with external power, but not plug-and-play.**

## Question 3: ice driver on aarch64

**This is the showstopper.**

Intel's out-of-tree ice driver [does not support aarch64](https://community.intel.com/t5/Ethernet-Products/intel-ice-drivers-1-9-11-does-not-support-aarch64/td-p/1428330).
Build errors include missing x86-specific functions (`convert_art_ns_to_tsc`,
`boot_cpu_has`) in `ice_ptp.c`. These are not trivial to stub out —
they're part of the PTP hardware timestamping path, which is exactly
what we need.

The [mainline kernel ice driver](https://docs.kernel.org/networking/device_drivers/ethernet/intel/ice.html)
is in the upstream kernel tree and should compile on aarch64, but it's
significantly older than Intel's out-of-tree version and may lack
PTP pin configuration features. A user [reported ice 1.3.2 working on
aarch64](https://community.intel.com/t5/Ethernet-Products/Update-ice-iavf-driver-on-Ubuntu-2204-aarch64/m-p/1573093),
but that's a very old version.

The Raspberry Pi kernel (6.12.x-rpi) is custom and may not include the
ice module at all. Building it would require kernel headers and likely
patching the x86-specific PTP code.

**Verdict: Showstopper. The PTP timestamping features we need are in
the x86-only code paths.**

## Question 4: PTP hardware timestamping on ARM

Moot given Question 3. If the ice driver compiled, PTP timestamping
itself is architecture-independent (it's kernel infrastructure, not
CPU-specific). The problem is that Intel's ice PTP implementation
uses x86-specific ART (Always Running Timer) integration for
cross-timestamping, which doesn't exist on ARM.

## Recommendation

**Use an x86 PC** as the E810 host. Options:

1. **Old PC** — known to work, physically large but functional.
   Any Intel/AMD x86_64 with a PCIe x8 or x16 slot and Ubuntu/Debian.

2. **Mini PC** — small form factor x86 with PCIe slot (e.g., ASRock
   DeskMeet, Intel NUC with Thunderbolt-to-PCIe). Smaller than a
   tower but still bigger than a Pi.

3. **Timebeat OTC SBC** — you already have otcBob1 with an i226 and
   OCXO. The E810 is a different class of hardware (100G, OCXO, 4-port)
   and doesn't fit the OTC form factor.

The E810 host would connect to the PTP LAN via one of its four SFP28
ports (using a 1G/10G SFP module). It becomes another PTP GM on the
lab network, with significantly better timestamping precision than the
i226.

## References

- [E810-XXVDA4T Product Brief](https://cdrdv2-public.intel.com/641626/Intel%20Ethernet%20Network%20Adapter%20E810-XXVDA4T%20Product%20Brief.pdf)
- [E810-XXVDA4T User Guide](https://cdrdv2-public.intel.com/646265/646265_E810-XXVDA4T%20User%20Guide_Rev1.2.pdf)
- [ice driver aarch64 issue](https://community.intel.com/t5/Ethernet-Products/intel-ice-drivers-1-9-11-does-not-support-aarch64/td-p/1428330)
- [ice driver aarch64 build errors](https://community.intel.com/t5/Ethernet-Products/Update-ice-iavf-driver-on-Ubuntu-2204-aarch64/m-p/1573093)
- [Compile ice driver with DKMS for PPS (Timebeat)](https://support.timebeat.app/hc/en-gb/articles/13199965947026)
- [CM5 IO Board Datasheet](https://pip-assets.raspberrypi.com/categories/1097-raspberry-pi-compute-module-5-io-board/documents/RP-008182-DS-2-cm5io-datasheet.pdf)
- [Linux ice driver documentation](https://docs.kernel.org/networking/device_drivers/ethernet/intel/ice.html)
