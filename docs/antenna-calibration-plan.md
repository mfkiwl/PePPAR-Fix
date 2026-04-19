# Antenna Position and Timing Calibration Plan

Establish cm-level antenna position, sub-ns cable delay, and F9T
observation noise floor using zero-baseline and short-baseline
techniques with independent confirmation from local CORS.

## Goals

1. **Antenna position** to ±1 cm horizontal, ±2 cm vertical
2. **Cable delay** to ±1 ns
3. **F9T carrier-phase noise floor** (isolated from all common errors)
4. **PPS alignment to GPS time** — absolute timing to ±2 ns requires
   knowing antenna position + cable delay + receiver internal delay
5. **Independent confirmation** — cross-check against multiple CORS
   stations at known positions

## Equipment

| Item | Role |
|------|------|
| Choke ring antenna (survey-grade) | Primary antenna — published PCO/PCV, multipath rejection |
| Leica GRX 1200 GG Pro | Zero-baseline reference receiver (GPS+GLONASS L1/L2) |
| u-blox ZED-F9T (TimeHat) | Receiver under test |
| Splitter (Wilkinson or SMA tee) | Feed both receivers from one antenna |
| TICC #1 | PPS timing comparison (Leica PPS vs F9T PPS) |
| Local CORS (Naperville, DuPage) | Short-baseline position validation |

## Antenna Selection

The choke ring antenna is ideal for calibration:

- **Published phase center model**: IGS maintains absolute antenna
  calibration files (ANTEX format) for survey-grade choke ring
  antennas.  Look up the antenna model at
  https://files.igs.org/pub/station/general/igs20.atx
  The phase center offset (PCO) and phase center variation (PCV)
  are specified per frequency, per elevation/azimuth.
- **Multipath rejection**: the choke ring's ground plane suppresses
  multipath from below, giving cleaner carrier-phase observations.
- **Stable phase center**: unlike patch antennas, the choke ring's
  phase center is well-defined and stable across elevation angles.

Before starting, identify the antenna's IGS model name and verify
it appears in the ANTEX file.  If it doesn't, the antenna needs
to be calibrated (send to a calibration lab, or use the zero-baseline
method to characterize it relative to a known antenna).

## Experiment 1: Zero-Baseline (F9T + Leica on One Antenna)

### Setup

```
Choke ring antenna
      |
    Splitter (Wilkinson preferred — better isolation)
    /         \
F9T (TimeHat)  Leica GRX 1200
   |              |
   PPS OUT        PPS OUT (10 MHz if available)
   |              |
 TICC chA      TICC chB
```

Both receivers see identical signals.  All common-mode errors
cancel in the double difference:
- Ionosphere: identical (same signal path)
- Troposphere: identical
- Satellite clocks: identical
- Multipath: identical (same antenna)
- Antenna phase center: identical

Residual = receiver noise only.

### Procedure

1. **Connect** choke ring to splitter.  Splitter output 1 → F9T
   antenna input.  Splitter output 2 → Leica antenna input.  Note
   cable lengths (measure with tape, refine with TDR if available).

2. **Configure Leica** for RTCM3 or RINEX logging via Ethernet.
   Set to 1 Hz, GPS+GLONASS, all available signals.  Log raw
   observations.  If the Leica can output PPS, connect to TICC chB.

3. **Configure F9T** for RAWX at 1 Hz.  PPS to TICC chA.

4. **Start capture**: run both receivers + TICC simultaneously for
   **24 hours**.  The long duration gives:
   - Complete sky coverage (all azimuths sampled)
   - PPP convergence to mm level
   - Temperature cycle coverage (day/night thermal effects)

5. **Collect from local CORS**: stream NAPERVILLE-RTCM3.1-MSM5
   (port 12054) for the same 24 hours.  Also stream any DuPage
   stations within 10 km.

### Analysis: F9T Carrier-Phase Noise

Form GPS double-differences between F9T and Leica for each satellite
pair.  At zero baseline, the DD residual is:

    DD_residual = noise_F9T + noise_Leica + splitter_imbalance

The Leica's carrier-phase noise is specified at ~1 mm (L1) and ~1.5 mm
(L2) from the manufacturer.  The F9T noise is unknown — this experiment
measures it.

Expected F9T carrier-phase noise: 2-5 mm based on similar consumer
dual-frequency receivers.  If significantly worse, it limits the PPP
filter's achievable clock precision.

### Analysis: PPS Timing

TICC chA - chB gives the PPS timing difference between F9T and Leica.
This includes:
- Receiver internal PPS generation delay (different for each receiver)
- Cable delay difference (splitter to each receiver)
- Receiver clock offset (F9T TCXO vs Leica OCXO drift)

The Leica GRX 1200's internal PPS delay is published in its
specifications.  Subtracting this gives the F9T's internal delay.

### Analysis: Antenna Position

Run 24-hour PPP on the F9T observations (or the Leica observations
if tools are available).  Cross-check:
- F9T PPP position vs Leica PPP position (should agree within noise)
- Both vs CORS baseline solution (NAPERVILLE at ~5 km)

The 24-hour PPP solution gives absolute position in ITRF2020 to
~5 mm horizontal, ~15 mm vertical.  Apply the antenna PCO/PCV from
the ANTEX file to get the ARP (antenna reference point).

Measure the ARP height above the monument (or ground mark) with a
tape measure to ±1 mm.


## Experiment 2: Short-Baseline to CORS

### Why Multiple CORS Stations

Using multiple CORS stations within 10 km provides:

- **Redundant position check**: if all baselines agree on your
  position to ±1 cm, the position is trustworthy.
- **Troposphere validation**: at <10 km, troposphere is nearly
  common.  Any residual troposphere shows up as a vertical bias
  that's consistent across all baselines.
- **Error detection**: if one baseline disagrees, it flags a
  problem with that CORS station (antenna change, coordinate
  error) rather than with your setup.

### Available CORS (within ~15 km of lab)

Check which stations are closest and which have the best sky
coverage.  From the NTRIP sourcetable:

**Port 12055 (DuPage)**:
- WHEATON-RTCM3 — closest? Check coordinates
- NAPERVILLEPD-RTCM3
- ELMHURST-RTCM3
- WOODDALE-RTCM3
- HANOVERPARK-RTCM3
- KNOLLWOOD-RTCM3

**Port 12054 (ISTHA)**:
- NAPERVILLE-RTCM3.1-MSM5 — ~5 km, GPS+GLO+GAL
- BOLINGBROOK-RTCM3.1-MSM5
- BENSENVILLE-RTCM3.1-MSM5
- SCHAUMBURG-RTCM3.1-MSM5

Use 2-3 closest stations.  Naperville (ISTHA) is preferred because
it provides MSM5 (full carrier-phase, multi-constellation).

### Procedure

1. **Collect simultaneous observations** from your F9T and 2-3
   CORS stations for 24 hours.
2. **Process baselines** using RTKLIB (or similar) in static
   post-processing mode.
3. **Compare positions**: all baselines should give the same
   antenna position (in the same reference frame) to ±1 cm.
4. **Absolute position**: use the CORS published coordinates
   (in NAD83 or ITRF) plus your computed baseline to derive
   your antenna position in the same frame.

### Baseline Processing Notes

- At 5-10 km baseline, L1/L2 iono-free combination resolves
  integer ambiguities within minutes.
- Fix rate should be >99% for a 24-hour session at this baseline.
- The Leica's L1/L2 observations (from the zero-baseline experiment)
  can also be used for baseline processing, providing a second
  independent check.


## Experiment 3: Cable Delay Measurement

### Method A: PPS Comparison (preferred)

With the zero-baseline setup, the TICC measures:

    TICC_diff = PPS_F9T - PPS_Leica
              = (cable_F9T + delay_F9T) - (cable_Leica + delay_Leica)

If both cables are the same length (same splitter output):

    TICC_diff ≈ delay_F9T - delay_Leica

The Leica's PPS delay is published.  Solving for `delay_F9T` gives
the F9T's total delay (internal + cable).

If cables are different lengths, measure the difference with a TDR
or swap cables and re-measure (the cable difference reverses, the
receiver difference doesn't).

### Method B: Cable-Swap

1. Measure PPS_F9T on TICC with cable A
2. Swap to cable B (different length)
3. The TICC difference = cable_B - cable_A
4. Measure cable_A and cable_B independently (TDR or VNA)
5. Cross-check: does the measured cable difference match the
   TICC difference?

### Method C: TDR/VNA

If a time-domain reflectometer or vector network analyzer is
available, measure each cable's electrical length directly.
Typical coax velocity factor: 66% (RG-58) to 85% (LMR-400).
A 10m cable at 66% VF has 50.5 ns delay.

### Target

Know the total signal path delay (antenna → receiver PPS output)
to ±1 ns.  This sets the accuracy of the PPS alignment to GPS
time.  The components:
- Cable delay: 3-5 ns/m × cable length
- Receiver internal delay: ~28 ns (F9T, see reference below)
- Antenna phase center: ~mm level, negligible at ns scale
- Splitter: ~1 ns insertion delay (characterize from VNA S21)

### Independent F9T delay reference

Ricardo Piriz at GMV (Madrid) published F9T timing calibration
measurements on LinkedIn (2019-2020):

- **F9T device internal delay: ~28 ns**
- Full chain (antenna 16 ns + cable 52 ns + device 28 ns): 95.9 ns
- Day-to-day repeatability: 0.3 ns (1σ) over one week
- PPS jitter: ±4 ns (larger than the older M8F's ±2 ns)
- More rigorous calibration (April 2020): 93.9 ns total chain

The ~28 ns internal delay is our benchmark.  Our zero-baseline
experiment should produce a consistent value when we subtract the
known cable and antenna delays.

References:
- [Testing the new ublox F9T (part 2)](https://www.linkedin.com/pulse/testing-new-ublox-f9t-part-2-ricardo-p%C3%ADriz) — Aug 2019
- [Calibrating mass-market GNSS timing receivers](https://www.linkedin.com/pulse/calibrating-mass-market-gnss-timing-receivers-ricardo-p%C3%ADriz) — Apr 2020


## Data Products

After all three experiments:

| Product | Source | Accuracy |
|---------|--------|----------|
| Antenna ARP position (ITRF) | 24h PPP + CORS baseline | ±1 cm H, ±2 cm V |
| Antenna PCO/PCV | ANTEX file (or zero-baseline cal) | ±1 mm |
| F9T carrier-phase noise | Zero-baseline DD residual | measured |
| F9T PPS internal delay | PPS comparison vs Leica | ±2 ns |
| Cable delay (ant → F9T) | TDR or PPS swap | ±1 ns |
| Absolute PPS-to-GPS alignment | position + cable + internal | ±2 ns |

These calibration products enable:
- Correct PPS-to-TAI alignment for time transfer
- Accurate PPP filter initialization
- Confidence in TDEV measurements (known systematic biases)


## Quick Position Check: RTKLIB PPP on a Pi

Experiments 1–3 above are comprehensive calibration campaigns.  For
a quicker **second-opinion position check** (±5 mm H, ±15 mm V) using
only the existing F9T and a Raspberry Pi, RTKLIB can do standalone PPP
post-processing without any reference station or Leica.

### Software

Use the [rtklibexplorer fork](https://github.com/rtklibexplorer/RTKLIB)
(RTKLIB-EX 2.5.0, successor to "demo5").  It has the best u-blox
dual-frequency support.

```bash
git clone https://github.com/rtklibexplorer/RTKLIB.git
cd RTKLIB/app/convbin/gcc && make        # UBX → RINEX
cd ../../rnx2rtkp/gcc && make            # post-processing PPP
```

Both build cleanly on aarch64 Raspberry Pi OS.

### Procedure

1. **Log 2–4 hours of raw observations** from the F9T.  peppar-fix
   already captures RXM-RAWX; alternatively use `str2str` or a simple
   UBX logger.  Longer is better — 24h gives mm-level, 2h gives ~1 cm.

2. **Convert to RINEX**:

       ./convbin -r ubx -o obs.rinex raw_log.ubx

3. **Download IGS precise products** for the observation date from
   [CDDIS](https://cddis.nasa.gov/archive/gnss/products/) or
   [IGN](http://igs.ign.fr/products):
   - **SP3** (precise orbits): `igsWWWWD.sp3` or rapid `igrWWWWD.sp3`
   - **CLK** (precise clocks): `igsWWWWD.clk` or rapid `igrWWWWD.clk`
   - Final products appear ~12–18 days after observation; rapid
     products in ~1 day.  For a quick check, rapid is fine.

4. **Run PPP-Static**:

       ./rnx2rtkp -p 7 -sys G,E obs.rinex nav.rinex \
           -sp3 igsWWWWD.sp3 -clk igsWWWWD.clk -o ppp_result.pos

   `-p 7` selects PPP-Static.  `-sys G,E` processes GPS+Galileo
   (matching our F9T signal config).

5. **Compare** the RTKLIB solution against peppar-fix's
   `data/position.json`.  Agreement within 2–3 cm confirms both
   are correct.  A larger discrepancy warrants investigation
   (antenna PCO difference, troposphere model, observation quality).

### What this gives vs doesn't give

| Provides | Does not provide |
|----------|------------------|
| Independent position in ITRF (±5 mm H / ±15 mm V with 24h) | Cable delay |
| Cross-check of peppar-fix PPP position | F9T internal delay |
| Completely different software + correction products | PPS alignment to GPS time |
| No additional hardware needed | Carrier-phase noise floor (need zero-baseline) |

This is not a substitute for the full calibration campaign — it only
validates the antenna position.  But it's a useful quick sanity check
that can be done today with existing hardware.

### Notes

- RTKLIB uses IGS final/rapid products (orbit + clock files), which
  are different from the SSR corrections peppar-fix uses in real time.
  This makes the position fix genuinely independent.
- The F9T's L5 signal config (GPS L1+L5, GAL E1+E5a) is fully
  supported by RTKLIB-EX.
- `rtkrcv` can also do real-time PPP if you feed it the F9T's serial
  stream + an NTRIP SSR correction stream, but post-processing with
  final products gives a better position.


## Dependencies

- [ ] Identify choke ring antenna model, verify ANTEX entry
- [ ] Set up Leica GRX 1200 Ethernet access, test RTCM output
- [ ] Acquire splitter (Wilkinson preferred for isolation)
- [ ] Verify Leica PPS output availability and connector type
- [ ] Determine closest CORS stations and coordinates
- [ ] Install RTKLIB on a lab Pi for baseline processing and PPP
