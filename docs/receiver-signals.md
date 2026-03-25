# Receiver Signal Requirements

## Required signals

PePPAR-Fix forms ionosphere-free (IF) linear combinations from dual-frequency
pseudorange and carrier-phase observations. This requires two signals per
constellation on every satellite:

| Constellation | Primary (f1) | Secondary (f2) | Notes |
|---------------|-------------|----------------|-------|
| GPS | L1 C/A (1575.42 MHz) | L5 Q (1176.45 MHz) | L5 health override required |
| Galileo | E1 C (1575.42 MHz) | E5a Q (1176.45 MHz) | Same frequency as L5 |
| BeiDou | B1I (1561.098 MHz) | B2a I (1176.45 MHz) | MEO/IGSO only (PRN >= 19) |

GLONASS is excluded (FDMA complicates IF processing). SBAS and QZSS are
disabled.

### Why L1+L5 (not L1+L2)

The F9T supports either L1+L2 or L1+L5 but not both simultaneously (two
frequency bands maximum). We standardize on L1+L5 because:

- L5/E5a is a modernized signal with better code structure and lower noise
- GPS L5 availability is now sufficient (30+ SVs as of 2025)
- L5 and E5a share the same center frequency (1176.45 MHz), simplifying
  the IF math for cross-constellation consistency
- BeiDou B2a also shares this frequency

### GPS L5 health override

GPS satellites broadcast a health flag for each signal. As of 2026, many
GPS L5 signals are still marked "unhealthy" even though they are fully
usable. Without an explicit override, the F9T will not track these signals.

The override is set via UBX CFG-VALSET with key `0x10320001` (value 1).
This key is documented in u-blox Application Note UBX-21038688 ("GPS L5
configuration") but is not yet exposed in pyubx2's key database.

After setting the override, a warm restart is required for the receiver
to begin tracking the newly-enabled L5 signals. The warm restart preserves
ephemeris data, so there is no cold-start penalty.

If the receiver NAKs this key, it means the firmware does not support L5
health override. L5 signals will still be tracked for SVs that broadcast
healthy L5 status, but some satellites will be unavailable.

## Required UBX messages

The following messages must be enabled on whichever port the host reads:

| Message | Purpose | Expected rate |
|---------|---------|---------------|
| RXM-RAWX | Pseudorange, carrier phase, Doppler, C/N0 | Every epoch (1 Hz) |
| RXM-SFRBX | Broadcast navigation data (ephemeris) | Per subframe (~2-6s) |
| NAV-PVT | Position/velocity/time solution | Every epoch (1 Hz) |
| TIM-TP | PPS quantization error (qErr) | Every epoch (1 Hz) |

NAV-SAT (satellite status) is optional, enabled at 1/5 rate when available.

## Startup signal validation

At startup, the code listens for RAWX observations and checks that
dual-frequency GPS+GAL observations are arriving. If they are not:

1. Configure signals via CFG-VALSET (L1+L5 config)
2. Apply GPS L5 health override
3. Warm restart
4. Re-check for dual-frequency observations

If dual-frequency observations still aren't arriving after reconfiguration,
startup fails with a clear error.

## UBX command/response sequencing

UBX CFG-VALSET commands produce an ACK-ACK (success) or ACK-NAK (failure)
response. These responses are sequenced: each ACK/NAK corresponds to the
oldest unacknowledged command.

**Always wait for ACK/NAK before sending the next command.** If you send
multiple commands without waiting, the responses arrive in order but you
lose the ability to correlate a NAK with the specific command that failed.

The `send_cfg()` function in `receiver.py` enforces this by calling
`wait_ack()` synchronously after each VALSET. A timeout (default 3s) is
treated as equivalent to NAK — the command is assumed to have failed.

This matters because:
- Signal configuration, message routing, and rate changes are separate
  VALSET commands
- A NAK on signal config (wrong key for this firmware) is very different
  from a NAK on message routing (wrong port ID)
- Without synchronous waiting, a NAK from command 1 might be misattributed
  to command 2

## Port types

The F9T exposes multiple communication ports. The port ID determines
which `CFG_MSGOUT_*` suffix to use:

| Port | ID | Suffix | Typical use |
|------|----|--------|-------------|
| UART1 | 1 | `_UART1` | External serial (ArduSimple, EVK) |
| UART2 | 2 | `_UART2` | Secondary serial |
| USB | 3 | `_USB` | USB connection (most common for external F9T) |
| SPI | 4 | `_SPI` | SPI bus |
| I2C/DDC | 0 | `_I2C` | I2C bus (E810 onboard F9T uses this) |

The E810's onboard F9T connects via I2C (port 0). External F9T boards
(ArduSimple, EVK) typically use USB (port 3). Message routing must target
the correct port or observations won't arrive on the host.

## Hardware variants

Two F9T firmware generations exist in the lab.  The key difference:
**TIM 2.20 NAKs L5/E5a/B2a signal config keys** — it only accepts L2C/E5b/B2.
TIM 2.25 (-20B module) accepts both L5 and L2C configs.

| | ocxo (E810) | PiPuss | TimeHat |
|---|---|---|---|
| **MOD** | ZED-F9T | ZED-F9T | ZED-F9T-20B |
| **FWVER** | TIM 2.20 | TIM 2.20 | TIM 2.25 |
| **PROTVER** | 29.20 | 29.20 | 29.25 |
| **ROM** | 0x118B2060 | 0x118B2060 | 0x3BFC8935 |
| **Constellations** | GPS;GLO;GAL;BDS | GPS;GLO;GAL;BDS | GPS;GAL;BDS (no GLO) |
| **Second freq** | L2C, E5b, B2 only | L2C, E5b, B2 only | L5, E5a, B2a (preferred); also L2C |
| **L5 signal config** | NAK | NAK (assumed) | OK |
| **Transport** | I2C (/dev/gnss0, kernel) | USB serial | USB serial |
| **Host** | x86 E810-XXVDA4T, OCXO | Raspberry Pi 4 | Raspberry Pi 4, i226 |

### Auto-detection in peppar-fix

`ensure_receiver_ready()` handles this automatically:
1. Tries L5 signal config (F9TL5Driver)
2. If NAK'd, falls back to L2C (F9TDriver)
3. Returns the appropriate driver for the detected signal plan

No profile or manual configuration is needed — the receiver's firmware
response determines which driver is used.  PROTVER is available in the
driver object for code that needs to branch on firmware generation.
