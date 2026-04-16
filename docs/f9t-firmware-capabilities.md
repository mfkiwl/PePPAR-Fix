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

However, there is a sequencing constraint when changing bands via
**individual** CFG-VALSET keys (not relevant when sending all keys
in a single VALSET, which is what `configure_signals()` does):

- Setting L5_ENA=1 when L2C is on: **ACK** — L2C auto-clears
- Setting L2C_ENA=1 when L5 is on: **ACK** — appears to succeed
- Setting L5_ENA=0 after L2C_ENA=1: **NAK** — even though L5 is
  already off (the receiver treats this as a conflicting request)
- After the NAK, both L2C and L5 read as 0 — the receiver is in
  L1-only mode

The safe pattern for individual key changes: always set the desired
band to 1 first (which auto-clears the other), then explicitly set
the other to 0. But the production code avoids this entirely —
`configure_signals()` sends a complete signal config in one VALSET
message, and the receiver applies it atomically.

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

## Why L5 is preferred for PPP-AR

PePPAR Fix always configures L5 when the receiver accepts it.  This
is a deliberate choice driven by our SSR correction source (CNES)
and the receiver's tracking mode.

### Reason 1: CNES phase bias compatibility (decisive)

CNES SSR provides GPS phase biases for these tracking modes:

    GPS: L1C (hit), L2W, L5I
    GAL: L1C (hit), E5aQ (hit), E7Q (hit)

The F9T tracks GPS L2 as **L2CL** (civil L2C, L-code).  CNES
provides **L2W** (semi-codeless Z-tracking, used by geodetic
receivers).  L2CL and L2W are different signal processing approaches
with different hardware delay characteristics — the L2W phase bias
does not apply to L2CL observations.  Result: **GPS L2 AR silently
fails** because the bias lookup misses.

For L5, CNES provides **L5I** biases.  The F9T tracks **L5Q**.  L5I
and L5Q share the same carrier frequency (1176.45 MHz), so the phase
bias applies despite the tracking mode difference.  GPS L5 AR works.

**This is CNES-specific.**  An SSR provider that published **L2L or
L2X** phase biases (matching the F9T's civil L2C tracking) would make
GPS L2 AR viable.  The L5 preference is downstream of our SSR source
choice, not a fundamental limitation.  See `docs/correction-sources.md`
for SSR stream options.

### Reason 2: Lower code noise (significant)

L5 uses BPSK(10) modulation vs L2C's BPSK(1) — roughly 10× better
code precision.  This directly affects Melbourne-Wubbena averaging:
lower code noise means faster WL convergence (fewer epochs to fix
N_WL).  With L5, WL fixing completes in ~60 epochs; L2 would need
proportionally more.

### Reason 3: Firmware universality (practical)

L5 works on both ZED-F9T (TIM 2.20) and ZED-F9T-20B (TIM 2.25).
L2C only works on TIM 2.20.  Preferring L5 avoids firmware-dependent
behavior in the field.

### What would change with a different SSR source?

If we switched to an SSR provider with L2L biases:
- GPS L2 AR would work (bias lookup would hit)
- L2 has a **longer** wide-lane wavelength (86.2 cm vs 75.2 cm),
  which is actually easier to fix — wider tolerance for code noise
- But L2's higher code noise partly cancels that advantage
- L5 would still be preferred on balance (code noise + universality)
  but the margin would be "better" rather than "required"

The current policy: **always L5, documented as an SSR-driven choice,
not a hardware limitation.**

### Reason 4: TGD considerations for L1/L5 (under investigation)

The GPS broadcast satellite clock is referenced to the L1/L2
ionosphere-free combination.  When using L1/L5 instead, there is a
differential group delay between L2 and L5 hardware paths on the
satellite that is not corrected by the broadcast TGD parameter.  The
correction requires `ISC_L5` from the CNAV message (not broadcast in
LNAV) or an equivalent SSR code bias.

In practice, the SSR code bias for L5 (if provided) absorbs this
differential, so SSR-corrected processing should handle it.  Without
SSR code biases, the L5 group delay differential produces a ~3-5m
per-satellite pseudorange bias — small enough to be absorbed by the
receiver clock state in the filter.

**Status (2026-04-16)**: A systematic 50m PPP position bias is under
investigation.  The TGD handling was tested (removing TGD worsened the
bias from 50m to 100m, confirming the subtraction is needed in our
pipeline).  The bias appears on both CNES and BKG SSR streams,
pointing to a measurement model issue rather than SSR-specific error.
See `memory/project_50m_bias_investigation.md` for current findings.

### Diagnostic benefit of running one host on L2

TimeHat's ZED-F9T (TIM 2.20) can run L1/L2, while MadHat and clkPoC3
are locked to L1/L5.  Briefly running TimeHat on L2 while the others
stay on L5 would provide:

1. **TGD isolation**: If the 50m bias disappears on L2 but persists on
   L5, the bias is L5-specific (group delay, ISC_L5, or L5 code bias)
2. **Signal quality comparison**: L2 and L5 pseudorange residuals can
   be compared for systematic patterns
3. **Cross-frequency AR validation**: if an L2L-compatible SSR source
   is found, L2 AR results can be compared against L5 AR

This is a one-time diagnostic, not a permanent configuration.  After
the investigation, TimeHat should return to L5 for consistency.

## Full signal/correction chain for PPP-AR

The choice of L2 vs L5 cannot be made in isolation — it propagates
through every stage of the processing chain.  Here is the full
dependency:

```
Receiver hardware (ZED-F9T variant)
  ↓ determines available signals
Signal tracking mode (L2CL vs L5Q)
  ↓ determines RINEX observation codes
SSR code bias lookup (C2L vs C5Q)
  ↓ corrects pseudorange hardware delays
SSR phase bias lookup (L2L vs L5Q → L5I)
  ↓ makes carrier-phase ambiguities integer-valued
IF combination (L1/L2 vs L1/L5)
  ↓ determines noise amplification factor and wavelengths
Melbourne-Wubbena wide-lane (λ_WL depends on f2 choice)
  ↓ convergence speed depends on code noise
Narrow-lane / LAMBDA resolution
  ↓ integer validation depends on float convergence
Position fix
```

At each stage, L2 and L5 have different characteristics:

| Stage | L1/L2 | L1/L5 | Winner |
|---|---|---|---|
| Hardware support | TIM 2.20 only | Both firmwares | L5 |
| SSR code bias | C2L (rare in SSR) | C5Q (common) | L5 |
| SSR phase bias | L2W (CNES) ≠ L2CL (F9T) | L5I ≈ L5Q (same carrier) | L5 |
| IF noise amplification | α ≈ 2.55 | α ≈ 2.26 | L5 |
| Code precision | BPSK(1), ~3m | BPSK(10), ~0.3m | L5 |
| WL wavelength | 86.2 cm | 75.2 cm | L2 |
| WL convergence | ~600 epochs | ~60 epochs | L5 |
| Broadcast TGD | Referenced to L1/L2 IF | Needs ISC_L5 correction | L2 |

L5 wins on 6 of 8 criteria.  L2's only advantages are a slightly
wider WL wavelength (easier integer fixing) and native TGD
compatibility.  Neither advantage overcomes L5's decisive phase bias
match with CNES and 10× better code precision.

## Implications for PePPAR Fix

1. **F9T-TOP on TimeHat is the only receiver that can test L2 AR.**
   Force `--receiver f9t` to prevent `ensure_receiver_ready()` from
   auto-switching to L5.  Requires an SSR source with L2L biases
   to be meaningful (CNES L2W won't work).

2. **`F9TDriver` L2 profile E5b bug (fixed 096dbdc)**: the L2
   profile previously specified `GAL_E5B_ENA=1` which NAKs on all
   tested firmware.  Now uses E5a for Galileo in both profiles.

3. **`docs/receiver-signals.md` needs correction**: the claim that
   TIM 2.20 NAKs L5 is false. Both firmwares support L5.
