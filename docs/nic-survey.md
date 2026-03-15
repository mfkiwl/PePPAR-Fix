# NIC and Timing Hardware Survey for PHC Discipline

All devices must have **both PPS input and PPS output** from a disciplined PHC.
Devices without both are disqualified.

## Qualified Hardware

### Consumer / Hobbyist Tier

| Device | PHC Res | adjfine (ppb/LSB) | max_adj | PPS I/O | Oscillator | Interface | Price | Notes |
|---|---|---|---|---|---|---|---|---|
| Intel i210 | 1 ns | ~0.12 | ±62.5 ppm | 4 SDP (pin header) | 25 MHz XO | PCIe 2.1 x1, 1G | ~$50 | Best community docs, proven |
| Intel i225 | 1 ns | ~0.12 | ±62.5 ppm | 4 SDP | 25 MHz XO | PCIe 3.1 x1, 2.5G | ~$50 | Adds PTM; early rev bugs |
| Intel i226 | 1 ns | ~0.12 | ±62.5 ppm | 4 SDP | 25 MHz XO | PCIe 3.1 x1, 2.5G | ~$55 | Current best consumer; SDP3 is strapping pin |
| TimeNIC | 1 ns | ~0.12 | ±62.5 ppm | 2 SMA | TCXO ±280 ppb | PCIe 3.1 x1, 2.5G | $200 | i226 + TCXO + SMA, turnkey |
| TimeHAT | 1 ns | ~0.12 | ±62.5 ppm | 2 SMA + 2 U.FL | TCXO ±280 ppb | Pi 5 HAT, 2.5G | $200 | i226 + TCXO, Pi 5 only |

### Enterprise Tier

| Device | PHC Res | adjfine | max_adj | PPS I/O | Oscillator | Interface | Price | Notes |
|---|---|---|---|---|---|---|---|---|
| **Intel E810-XXVDA4T** | **Sub-ns (7-bit)** | Very fine | ±1000 ppm | **2 SMA** + U.FL | **OCXO** (4h holdover) | PCIe 4.0 x16, 4×25G | ~$1,100 | **Best-in-class: 6 ns RMS measured** |
| OCP Time Card | FPGA-dep | FPGA | FPGA | 4 SMA | Rb/OCXO (modular) | PCIe full | $3,200-10k | Reference grandmaster, not a NIC |
| Timecard Mini 2.0 | FPGA-dep | FPGA | FPGA | SMA | TCXO-OCXO | CM4/CM5 | $290-1,500 | Compact grandmaster |
| Oregano syn1588 | Sub-ns (2⁻⁴⁵) | FPGA | FPGA | 2 SMA + 2 int | TCXO/OCXO opt | PCIe 2.0 x1, 1G | $2k-5k+ | Enterprise FPGA NIC (Meinberg) |
| Meinberg GNS183PEX | 5 ns | Proprietary | Proprietary | D-Sub | TCXO/OCXO + GNSS | PCIe LP | $2k-5k+ | Professional, GNSS built-in |
| Safran TSync | 5 ns | Proprietary | Proprietary | Multiple | TCXO/OCXO + GNSS | PCIe | $3k-8k+ | Military/telecom grade |

## Key Findings

### The E810-XXVDA4T stands out

The Intel E810-XXVDA4T ("T" = timing variant) is the only NIC with:
- **Sub-nanosecond timestamping** (7-bit sub-ns field in hardware)
- **Onboard OCXO** with 4-hour holdover (<±1.5 µs)
- **SMA connectors** on the bracket (no soldering, no breakout boards)
- **Measured 6 ns RMS** PTP sync accuracy (Scott Laird oscilloscope tests)
- Optional GNSS mezzanine module (u-blox)

At $1,100 it's 5× the cost of a TimeHAT but offers fundamentally better
timestamping resolution. For a PPP-AR project aiming at sub-nanosecond clock
estimation, the servo output precision shouldn't be limited by 1 ns
timestamping granularity.

**Caveat**: The mainline Linux `ice` driver does NOT support PPS I/O. Must use
Intel's out-of-tree driver compiled via DKMS.

### Intel i210/i225/i226 share the same timing core

All three use the same 31-bit INCVALUE register architecture:
- 1 ns SYSTIM resolution
- ~0.12 ppb per LSB adjfine granularity
- ±62.5 ppm max_adj range
- **Dual-edge quirk**: timestamps both rising AND falling PPS edges

The i210 actually measures better than the i226 for timing (76 ns vs 439 ns
mean offset) because the i226's 2.5G DSP adds latency. For pure PPS
timestamping (no packet timestamps), they should be equivalent.

### TCXO matters for holdover

The bare i226's commodity 25 MHz crystal drifts 20-50 ppm over temperature.
The TimeHAT/TimeNIC's TCXO (±280 ppb) is ~100× better. For a GPSDO that
needs to maintain accuracy during brief GNSS outages, the TCXO is essential.

## Disqualified

| Device | Reason |
|---|---|
| Broadcom BCM54210PE (CM4/CM5) | Single pin for PPS — can't do simultaneous IN + OUT |
| Solarflare SFN6122F | Base 10G NIC — no PTP timestamping, no PPS I/O. The PTP variant is SFN**6322**F (different hardware with precision oscillator + SMA bracket). Bob has this card. |
| Solarflare SFN7122F | Base 10G NIC — has PHC via `sfc` driver but no PPS connectors. PPS bracket kit (SOLR-PPS-DP10G) was a special-order retrofit for SFN7000 series but is unobtainable (Solarflare → Xilinx → AMD). The PTP variant SFN**7322**F has SMA PPS I/O built-in. Bob has this card. |
| Marvell/Aquantia AQR | No PPS I/O pins |
| Microchip LAN743x | No PPS input (n_ext_ts=0) |
| Microchip LAN937x | No PPS I/O |
| Trimble Thunderbolt | Standalone GPSDO, not a NIC/PCIe card |
| SiTime eval boards | Oscillator test platforms, no PHC |
| Calnex Sentinel | Test equipment ($15k+), not a timing card |
| EndRun Technologies | Complete appliances, not PCIe cards |

**Note on Solarflare PTP variants (SFN6322F / SFN7322F):** These *would* qualify —
1 ns PHC resolution, SMA PPS IN + OUT, Stratum 3 oscillator (< 1 PPM/year),
`sfc` driver + sfptpd. But Bob has the base models (6122F / 7122F), not the
PTP variants, and the PPS bracket kit is unobtainable. Used SFN7322F cards
occasionally appear on eBay for $50-100 and would be worth grabbing if spotted.

## Recommendation for PePPAR Fix

**Development platform**: TimeHAT (i226 + TCXO, $200) on Pi 5. Same hardware
as SatPulse evaluation. Good enough for initial filter development — the 1 ns
timestamping exceeds what PPS+qErr can deliver anyway.

**Upgrade path**: Intel E810-XXVDA4T ($1,100). Once the PPP-AR filter is
producing sub-nanosecond clock estimates, the i226's 1 ns timestamping becomes
the bottleneck. The E810's sub-ns timestamping and onboard OCXO would let us
measure the filter's true performance.

### Dual-setup: PePPAR Fix + SatPulse simultaneously

Each setup needs: F9T receiver + NIC with PHC + PPS IN + PPS OUT.

**Current setup (TimeHat)**: TimeHAT v5 (i226) + F9T-TOP → runs SatPulse.
This is already deployed and working.

**Second setup needed for PePPAR Fix**: Needs another Pi 5 + NIC with PPS I/O,
or a PCIe host. Options:

| Option | Cost | Pros | Cons |
|--------|------|------|------|
| TimeHAT v6 + Pi 5 | ~$200 + Pi 5 | Identical to SatPulse setup, proven | Need another Pi 5 |
| TimeNIC + any x86 | ~$200 | Same i226+TCXO, works with any PCIe host | Needs an available PCIe slot |
| Intel i210 + breakout | ~$50 + wiring | Cheapest, best timing docs | Pin header needs SMA breakout board, no TCXO |
| Solarflare SFN7322F (used) | ~$50-100 if found | 10G, Stratum 3 osc, SMA PPS | Rare, old, sfptpd instead of standard tools |

**Recommendation**: Second TimeHAT ($200) on a second Pi 5. Keeps both setups
identical, both proven with SatPulse/PePPAR Fix, minimal integration risk.
The Solarflare cards Bob already has (6122F, 7122F) are disqualified — no PPS I/O.

**Order now**:
- [ ] TimeHAT v6 ($200) — second setup for PePPAR Fix
- [ ] Intel E810-XXVDA4T (~$1,100) — precision upgrade (later)

## Sources

- [Scott Laird — NIC timing features](https://scottstuff.net/posts/2025/05/20/time-nics/)
- [Scott Laird — Measuring NTP/PTP accuracy (Part 3: NICs)](https://scottstuff.net/posts/2025/06/07/measuring-ntp-accuracy-with-an-oscilloscope-3/)
- [jclark — PPS NIC guide](https://github.com/jclark/pc-ptp-ntp-guide/blob/main/pps-nic.md)
- [SatPulse — Intel build](https://satpulse.net/hardware/intel-build.html)
- [Linux igb_ptp.c](https://github.com/torvalds/linux/blob/master/drivers/net/ethernet/intel/igb/igb_ptp.c)
- [Linux igc_ptp.c](https://github.com/torvalds/linux/blob/master/drivers/net/ethernet/intel/igc/igc_ptp.c)
- [Intel E810-XXVDA4T User Guide](https://cdrdv2-public.intel.com/646265/646265_E810-XXVDA4T%20User%20Guide_Rev1.2.pdf)
- [TimeNIC (Tindie)](https://www.tindie.com/products/timeappliances/timenic-i226-pcie-nic-with-pps-inout-and-tcxo/)
- [TimeHAT (Tindie)](https://www.tindie.com/products/timeappliances/timehat-i226-nic-with-pps-inout-for-rpi5/)
- [OCP Time Card](https://github.com/Time-Appliances-Project/Time-Card)
- [Oregano syn1588](https://www.oreganosystems.at/products/syn1588/hardware/syn1588r-pcie-nic)
- [Timebeat DKMS ice driver guide](https://support.timebeat.app/hc/en-gb/articles/13199965947026)
- [Solarflare Enhanced PTP User Guide (Issue 8)](https://www.amd.com/content/dam/amd/en/support/downloads/solarflare/drivers-software/linux/ptp/SF-109110-CD-8_Solarflare_Enhanced_PTP_User_Guide.pdf)
- [sfptpd — AMD Solarflare Enhanced PTP Daemon](https://github.com/Xilinx-CNS/sfptpd)
- [Solarflare SFN6322F product brief](https://www.bhphotovideo.com/c/product/1017856-REG/solarflare_sfn6322f_solarflare_srvr_adptr_crd.html)
- [Solarflare PPS I/O specification (Manualzz)](https://manualzz.com/doc/o/k605n/solarflare-enhanced-ptp-user-guide-solarflare-sfptpd-1pps-i-o-specification)
