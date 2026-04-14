# ZED-F9T Firmware Capability Matrix

Experimentally determined 2026-04-14 via factory reset + individual
CFG-VALSET probing on lab receivers sharing a common antenna/splitter.

## Test receivers

| Name | Module | Firmware | PROTVER | SEC-UNIQID | Host |
|------|--------|----------|---------|------------|------|
| F9T-TOP | ZED-F9T | TIM 2.20 | 29.20 | 136395244089 | TimeHat |
| F9T-BOT | ZED-F9T-20B | TIM 2.25 | 29.25 | 262843023907 | MadHat |

## Signal capability matrix

Tested by sending CFG-VALSET (RAM layer) for each key individually
after a CFG-CFG factory reset (F9T-TOP) or from running state
(F9T-BOT). ACK = accepted, NAK = rejected by firmware.

| Capability | CFG key | ZED-F9T (TIM 2.20) | ZED-F9T-20B (TIM 2.25) |
|---|---|---|---|
| GPS L1 C/A | CFG_SIGNAL_GPS_L1CA_ENA | ACK | ACK |
| GPS L2C | CFG_SIGNAL_GPS_L2C_ENA | **ACK** | **NAK** |
| GPS L5 | CFG_SIGNAL_GPS_L5_ENA | **ACK** | ACK |
| GPS L5 health override | 0x10320001 | **ACK** | ACK |
| GAL E1 | CFG_SIGNAL_GAL_E1_ENA | ACK | ACK |
| GAL E5a | CFG_SIGNAL_GAL_E5A_ENA | ACK | ACK |
| GAL E5b | CFG_SIGNAL_GAL_E5B_ENA | **NAK** | **NAK** |
| GLONASS | CFG_SIGNAL_GLO_ENA | **NAK** | **NAK** |
| NavIC | CFG_SIGNAL_NAVIC_ENA | **NAK** | **ACK** |
| BeiDou B1 | CFG_SIGNAL_BDS_B1_ENA | ACK | ACK |
| BeiDou B2a | CFG_SIGNAL_BDS_B2A_ENA | (not tested) | (not tested) |

## Key findings

### Neither receiver supports GLONASS

Both NAK `GLO_ENA`. The MON-VER extension string on TIM 2.20 lists
"GPS;GLO;GAL;BDS" but this appears to be a static ROM string, not a
reflection of actual capability. The -20B drops GLO from the string
entirely.

### ZED-F9T (TIM 2.20) supports BOTH L2C and L5

Contrary to prior documentation (`docs/receiver-signals.md` line
114-126 which claimed TIM 2.20 NAKs L5), the non-20B ZED-F9T
**accepts both L2C and L5 configuration**. It can run either signal
plan, just not simultaneously (two-band RF chain limit).

However, there is a sequencing constraint: when L2C is already
enabled, setting L5_ENA=0 is NAK'd. Setting L5_ENA=1 succeeds and
the receiver auto-clears L2C_ENA. This suggests L5 takes priority
internally — switching from L5→L2 requires clearing L5 first.

### ZED-F9T-20B (TIM 2.25) lost L2C, gained NavIC

The -20B module **NAKs L2C**. This is a hard firmware restriction,
not a default preference. The -20B can only run L5 as its second
frequency.

In exchange, the -20B gained NavIC support.

### GAL E5b is dead on both

Neither firmware accepts E5b. The `F9T_SIGNAL_CONFIG` in
`receiver.py` pairs L2C with E5b, but E5b NAKs on both firmware
versions tested. This means the L2 signal plan cannot include GAL
E5b — it would need GAL E5a (which is accepted by both).

**This is a bug in `receiver.py`**: the `F9TDriver` (L2 profile)
specifies `GAL_E5B_ENA=1` in `F9T_SIGNAL_CONFIG`, which will NAK.
The code should use `GAL_E5A_ENA=1` for both L2 and L5 profiles,
or handle the NAK gracefully.

### Default config after factory reset

After CFG-CFG factory reset, both L2C_ENA and L5_ENA are **OFF** (0).
The receiver boots to L1-only mode. `ensure_receiver_ready()` detects
single-frequency and applies the L5 signal plan, which is why all
receivers end up on L5 regardless of hardware variant.

### L5 SV count is identical across firmware versions

With L5 enabled and health override applied, all three lab receivers
(TIM 2.20 + two TIM 2.25) track the same GPS L5 and GAL E5a SVs
(7 GPS L5, 10-11 GAL E5a at the time of measurement). No SV
dropout difference between firmware versions.

## Summary: what each firmware can actually do

| Feature | ZED-F9T (TIM 2.20) | ZED-F9T-20B (TIM 2.25) |
|---|---|---|
| L1 + L2C | Yes | **No** |
| L1 + L5 | **Yes** | Yes |
| L5 health override | **Yes** | Yes |
| GLONASS | No | No |
| NavIC | No | **Yes** |
| GAL E5b | No | No |
| GAL E5a | Yes | Yes |

The ZED-F9T (TIM 2.20) is strictly more capable for our purposes:
it can run either L2 or L5, while the -20B is locked to L5 only.
The -20B's NavIC support is not useful for PPP-AR (no SSR corrections
available for NavIC).

## Implications for PePPAR Fix

1. **F9T-TOP on TimeHat is the only receiver that can test L2 AR.**
   Force `--receiver f9t` to prevent `ensure_receiver_ready()` from
   auto-switching to L5.

2. **The `F9TDriver` (L2 profile) has a bug**: it specifies
   `GAL_E5B_ENA=1` which NAKs on both firmware versions. The L2
   if_pairs also reference `GAL-E5bQ` which won't be tracked.
   Fix: use E5a for GAL in both profiles.

3. **`ensure_receiver_ready()` should detect the firmware variant**
   and offer L2 as a configuration option on TIM 2.20 hardware,
   rather than always preferring L5.

4. **`docs/receiver-signals.md` needs correction**: the claim that
   TIM 2.20 NAKs L5 is false. Both firmwares support L5.
