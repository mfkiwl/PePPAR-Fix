# NTRIP Caster Broadcast Ephemeris

## Problem

The peppar-fix NTRIP caster (`ntrip_caster.py`) serves raw GNSS
observations (RTCM MSM4) and the station reference position (RTCM
1005).  Clients that use PPP for position bootstrap also need
broadcast ephemeris — satellite orbit and clock parameters — to
compute satellite positions.  Today they get ephemeris from an
external NTRIP service (BKG).  Adding ephemeris to the local caster
removes this external dependency.

## Source: RXM-SFRBX

The F9T already outputs `RXM-SFRBX` messages containing decoded
navigation subframes.  These arrive continuously as the receiver
tracks satellites — a full GPS ephemeris set (all visible SVs)
typically arrives within 30 seconds of first lock.

Each SFRBX message contains:
- `gnssId`: constellation (0=GPS, 2=Galileo, 3=BDS)
- `svId`: satellite number
- `freqId`: signal (0 for primary)
- `numWords`: number of 32-bit words
- `dwrd_01` .. `dwrd_10`: the subframe data words

## RTCM encoding

Each constellation's ephemeris has a different RTCM message type
and bit layout:

### GPS (RTCM 1019)

GPS L1 C/A navigation uses subframes 1-3 (3 × 10 words × 30 bits).
A complete ephemeris requires all three subframes for the same SV.

SFRBX delivers one subframe per message.  The caster must:
1. Buffer subframes by (svId, subframeId) — subframeId is in word 2
   bits 8-10
2. When all three subframes for an SV are collected, decode the
   ephemeris parameters (Crs, dn, M0, Cuc, e, Cus, sqrtA, toe, etc.)
3. Encode as RTCM 1019 (488 bits payload)

The u-blox SFRBX for GPS contains the raw 30-bit navigation words
with parity bits included.  Strip the 6 parity bits from each word
to get the 24-bit data payload, then extract fields per IS-GPS-200
Table 20-I through 20-III.

### Galileo (RTCM 1045 / 1046)

Galileo I/NAV (E1-B) and F/NAV (E5a-I) carry the same ephemeris
with different encoding.  The F9T outputs both.

SFRBX for Galileo delivers page parts.  A complete I/NAV ephemeris
needs word types 1-4 (from pages within a subframe).  The `sigId`
field distinguishes I/NAV (sigId=0, E1-B) from F/NAV (sigId=3,
E5a-I).

Encode as:
- RTCM 1046 for I/NAV
- RTCM 1045 for F/NAV

Either one suffices for the PPP solver.  Prefer 1046 (I/NAV) since
it arrives faster (2-second page rate vs 10-second for F/NAV).

### BDS (RTCM 1042)

BDS D1 (MEO/IGSO) and D2 (GEO) navigation messages.  A complete
ephemeris needs subframes 1-3 for D1, or pages within subframe 1
for D2.

**Note:** BDS is currently disabled in peppar-fix (broken ISB
handling, see CLAUDE.md).  Implement GPS and Galileo first; BDS
encoding can wait until the ISB issue is resolved.

## Implementation plan

### Phase 1: GPS ephemeris (RTCM 1019)

1. Add `SfrbxCollector` class to `rtcm_encoder.py` (or new file
   `sfrbx_ephemeris.py`):
   - Buffers SFRBX messages by (gnssId, svId, subframeId)
   - Detects when a complete set of 3 GPS subframes is available
   - Decodes ephemeris parameters from the raw words
   - Returns the parameter dict for RTCM encoding

2. Add `encode_1019(eph_params)` to `rtcm_encoder.py`:
   - Pack GPS ephemeris into RTCM 1019 bit layout (488 data bits)
   - Wrap in RTCM3 frame (preamble + length + CRC-24Q)

3. Modify `caster_serial_loop()` in `ntrip_caster.py`:
   - Pass SFRBX messages to `SfrbxCollector`
   - When a complete ephemeris is ready, encode and broadcast
   - Rate-limit: broadcast each SV's ephemeris at most once per
     IODE change (typically every 2 hours)

### Phase 2: Galileo ephemeris (RTCM 1046)

Same pattern as GPS.  Galileo I/NAV page structure is different
(128-bit pages with even/odd halves) but the approach is identical:
collect pages, decode parameters, encode RTCM.

### Phase 3: BDS ephemeris (RTCM 1042)

Deferred until BDS ISB handling is fixed.

## Verification

The encoded RTCM ephemeris must be bit-identical to what the
existing `BroadcastEphemeris` class would produce from decoding
the same satellite.  Test by:

1. Running the caster on one F9T
2. Connecting peppar-fix's `ntrip_reader` as a client
3. Comparing the decoded ephemeris parameters against the F9T's
   own NAV-EPH output (or against the BKG caster's stream for
   the same satellites)

The acceptance criterion: `phc_bootstrap.py` completes successfully
using the local caster as its sole NTRIP source (no BKG connection).

## Complexity estimate

- GPS 1019 encoding: ~200 lines (subframe parsing + RTCM bit packing)
- Galileo 1046 encoding: ~250 lines (I/NAV page assembly + encoding)
- SFRBX collector: ~100 lines (buffering, completeness detection)
- Caster integration: ~30 lines (wire SFRBX into the broadcast loop)
- Tests: ~100 lines

Total: ~700 lines for GPS + Galileo.  The bit packing is tedious but
mechanical — each field's position, scale, and bit width are defined
in the RTCM 3.3 standard and the corresponding ICDs.

## References

- IS-GPS-200: GPS L1 C/A navigation message structure
- Galileo OS SIS ICD: I/NAV and F/NAV page layout
- RTCM 3.3 Amendment 2: message type definitions (1019, 1042, 1045, 1046)
- u-blox F9 Interface Description: RXM-SFRBX message format
