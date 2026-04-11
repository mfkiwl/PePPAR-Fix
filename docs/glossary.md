# PePPAR Fix Glossary

Terms used in code, docs, and lab notes.  When a term has multiple
meanings in different contexts, the PePPAR-Fix-specific meaning is
listed first.

## Oscillators and clocks

| Term | Definition |
|---|---|
| **DO** | Disciplined Oscillator.  The crystal whose frequency is steered by the servo.  On TimeHat/MadHat this is the i226 TCXO controlled via `adjfine()`.  On Timebeat OTC this is the on-board OCXO controlled via ClockMatrix FCW.  Use "DO" when the statement applies regardless of the actuator. |
| **PHC** | PTP Hardware Clock.  The Linux kernel's interface to a NIC's hardware clock.  Use "PHC" only when referring to the specific `adjfine()` / `clock_settime()` / `EXTTS` kernel API.  Not all DOs are PHCs (e.g., ClockMatrix DPLL). |
| **rx TCXO** | The TCXO inside the GNSS receiver (F9T).  Drives the receiver's 125 MHz clock and determines where PPS edges fire (quantized to the 8 ns tick grid).  Do NOT use bare "TCXO" — it's ambiguous with the DO's crystal on i226 hosts, which is also a TCXO. |
| **OCXO** | Oven-Controlled Crystal Oscillator.  Higher stability than TCXO.  The DO on Timebeat OTC boards. |
| **adjfine** | PHC frequency adjustment.  Sets the clock rate in parts-per-billion via the `PTP_CLOCK_SETFINE` ioctl. |
| **FCW** | Frequency Control Word.  The digital command that sets the Renesas ClockMatrix DPLL output frequency. |

## GNSS and corrections

| Term | Definition |
|---|---|
| **GNSS** | Global Navigation Satellite System.  Umbrella for GPS, Galileo, BeiDou, GLONASS. |
| **PPP** | Precise Point Positioning.  Single-receiver positioning using satellite orbit and clock corrections from an SSR stream.  Achieves sub-ns clock estimates after convergence. |
| **PPP-AR** | PPP with Ambiguity Resolution.  Fixes carrier-phase integer ambiguities using phase biases from a single analysis center.  3x lower dt_rx noise than float PPP. |
| **SSR** | State Space Representation.  Satellite-specific corrections (orbit, clock, code bias, phase bias) distributed via NTRIP.  Contrasts with OSR (Observation Space) like VRS/RTK. |
| **NTRIP** | Networked Transport of RTCM via Internet Protocol.  The standard for streaming GNSS corrections over the internet. |
| **RTCM** | Radio Technical Commission for Maritime Services.  The binary message format for GNSS corrections (1019, 1045, 1060, 1265, etc.). |
| **ISB** | Inter-System Bias.  The constant time offset between two GNSS constellations (e.g., GPS-Galileo). |
| **ephemeris** | Satellite orbit and clock parameters.  "Broadcast" = from the satellite signal.  "Precise" = from SSR corrections. |
| **carrier phase** | The phase of the GNSS L-band carrier signal.  Sub-centimeter precision but ambiguous by an unknown integer number of wavelengths. |
| **integer ambiguity** | The unknown whole-cycle count in a carrier-phase measurement.  Resolving it ("fixing") eliminates a major noise source. |
| **phase bias** | Per-satellite, per-signal correction that makes integer ambiguity resolution possible.  Only available from single analysis center SSR streams (CAS, CNES, WHU). |
| **code bias** | Correction for signal-dependent pseudorange offsets between different GNSS signals. |
| **dt_rx** | Receiver clock offset from GNSS time, estimated by the PPP filter.  In nanoseconds.  Tracks the rx TCXO phase. |

## PPS and timing

| Term | Definition |
|---|---|
| **PPS** | Pulse Per Second.  A 1 Hz signal edge used as a timing reference.  Two PPS streams in the system: `gnss_pps` (F9T output) and `do_pps` (PHC PEROUT). |
| **gnss_pps** | The F9T's PPS output.  Fires at the nearest 125 MHz tick to the GPS second.  Subject to 8 ns quantization (qerr). |
| **do_pps** | The DO's PPS output (PEROUT on i226, SMA on E810).  What the servo disciplines. |
| **qErr** | Quantization error.  The sub-8 ns offset between the true GPS second and the rx TCXO tick where gnss_pps actually fires.  Reported by TIM-TP.  This is a **physical quantization** — the PPS edge snaps to a discrete 125 MHz tick grid.  Do not use "qErr" for PPP-derived corrections (those correct for rx TCXO drift, not tick quantization). |
| **PPS correction** | Any correction applied to a PPS measurement to improve its accuracy.  Two types: **qErr** (TIM-TP, corrects tick quantization, discrete ±4 ns) and **PPP drift-model correction** (from smoothed dt_rx, corrects rx TCXO drift, continuous ~0.1 ns).  CLI: `--no-qerr` disables qErr; `--pps-corr ppp` selects PPP drift-model instead of TIM-TP qErr. |
| **TIM-TP** | u-blox UBX-TIM-TP message.  Predicts the qErr of the **next** PPS edge.  Arrives ~0.9 s before the PPS it describes. |
| **TAI** | International Atomic Time.  Continuous timescale (no leap seconds).  TAI - UTC = 37 s as of 2026. |
| **sawtooth** | The periodic phase modulation of gnss_pps caused by the rx TCXO beating against GPS time.  Alternates between "smooth ramp" and "jumpy" regimes. |
| **holdover** | Free-running the DO when the GNSS reference is lost.  The DO drifts at its last-known rate. |

## Measurement instruments

| Term | Definition |
|---|---|
| **TICC** | Time Interval Counter/Counter.  TAPR open-hardware instrument.  60 ps single-shot resolution.  Measures two input channels (chA, chB) independently against an internal timebase. |
| **TAPR** | Tucson Amateur Packet Radio.  The organization that designs and sells the TICC. |
| **EXTTS** | External Timestamp.  Linux PTP subsystem feature: timestamps an external GPIO edge against the PHC clock. |
| **PEROUT** | Periodic Output.  Linux PTP subsystem feature: generates a periodic pulse (e.g., 1 PPS) from the PHC clock. |
| **TDC** | Time-to-Digital Converter.  Hardware that converts a time interval to a digital measurement (used in ClockMatrix). |

## Servo and control

| Term | Definition |
|---|---|
| **qVIR** | qErr Variance Improvement Ratio.  `Δvar(uncorrected) / Δvar(corrected)`.  Measures how much qErr correction reduces the variance of a PPS timestamp stream.  >1.5 = good (qErr is helping).  ≈1.0 = qErr is uncorrelated with the measurement (wrong epoch match).  <1.0 = qErr is making things worse (wrong sign or epoch).  Computed separately for EXTTS (qVIR_extts) and TICC (qVIR_ticc).  High qVIR correlates with low TDEV; low qVIR correlates with high TDEV.  Primary use: detect qErr mis-correlation early so we don't waste a run collecting data with terrible TDEV.  qErr has so much variance (±4 ns) that applying it to the wrong PPS edge is immediately visible in the ratio — smoother correction streams would mask the error. |
| **EKF** | Extended Kalman Filter.  Nonlinear state estimator.  Used in the 4-state DOFreqEst for TICC+PPP fusion. |
| **LQR** | Linear-Quadratic Regulator.  Optimal control law that minimizes a cost function.  Used in the Kalman servo for frequency steering. |
| **PI servo** | Proportional-Integral servo.  Simple feedback controller with gain (Kp) and integral (Ki) terms. |
| **LS** | Least Squares.  Used for initial position estimation before the PPP Kalman filter takes over. |
| **IF** | Ionosphere-Free combination.  Dual-frequency linear combination that cancels first-order ionospheric delay. |
| **TDEV** | Time Deviation.  Stability measure in time units (ns).  TDEV at tau=1s is the primary short-term metric. |
| **ADEV** | Allan Deviation.  Stability measure in fractional frequency units.  Related to TDEV by `TDEV(tau) = tau * ADEV(tau) / sqrt(3)`. |
| **pull-in** | Initial convergence phase where the servo acquires lock from a large phase offset. |
| **glide slope** | Smooth frequency ramp applied during bootstrap to converge the DO's phase offset without overshooting. |
| **loop bandwidth** | The frequency at which the servo's gain crosses unity.  Determines the crossover between DO noise (below) and reference noise (above). |
| **noise floor** | The minimum achievable TDEV of an oscillator or measurement system. |

## Hardware

| Term | Definition |
|---|---|
| **F9T** | u-blox ZED-F9T.  Multi-band GNSS timing receiver with L1/L5 support. |
| **i226** | Intel I226-LM/V.  2.5G Ethernet NIC with PTP hardware clock (PHC).  Used on TimeHAT v5. |
| **E810** | Intel E810-XXVDA4T.  100G Ethernet NIC with PTP hardware clock, DPLL, GNSS input.  Used on ocxo host. |
| **TimeHAT** | Time-Appliances-Project Raspberry Pi HAT with i226 NIC and F9T receiver. |
| **ClockMatrix** | Renesas 8A34002.  Programmable clock generator with DPLL and TDC.  Used on Timebeat OTC boards. |
| **DPLL** | Digital Phase-Locked Loop.  On ClockMatrix: locks an output frequency to an input reference. |
