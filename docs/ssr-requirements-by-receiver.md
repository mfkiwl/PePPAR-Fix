# SSR Stream Requirements by F9T Receiver Variant

What each ZED-F9T variant in the lab **can receive** (not what we've
enabled), and what an ideal SSR (State Space Representation) stream
would publish to make every observed signal fully bias-correctable.

Companion to `docs/f9t-firmware-capabilities.md` (which tabulates
firmware ACK/NAK per CFG key) and `docs/correction-sources.md`
(which catalogs SSR providers).  This doc fills the gap between
them: given the *receivable* signals, what *signal-set coverage*
must an SSR provide?

Motivated by the 2026-04-24 lab catastrophe (day0424i / day0424k):
F9T-20B fleet seeded at surveyed truth drifted 56 m below truth
within 13 minutes because CNES phase biases for GPS L5 are published
for L5I but the F9T tracks L5Q — and empirically (commits b71e2b1
reverted at 15f9b01) the L5I bias does **not** apply to L5Q
observations.  See `project_gps_l5i_l5q_bias_fix.md`.

## Why signal *variant* matters, not just frequency

Two signals on the same carrier can still be different signals
because they use different *codes* (chip patterns) and traverse
different hardware processing paths in both transmitter and
receiver.  Phase biases, code biases, and PCV/PCO calibrations are
all signal-variant-specific, not frequency-specific.

Common F9T-vs-CNES mismatches:

| Carrier | F9T tracks | CNES publishes | Substitutable? |
|---|---|---|---|
| 1227.60 MHz (L2) | **L2CL** (civil C, RINEX `L2L`) | **L2W** (semi-codeless P(Y)) | **No** — different code, different hardware delays |
| 1176.45 MHz (L5) | **L5Q** (pilot, RINEX `L5Q`) | **L5I** (data, RINEX `L5I`) | **No** — empirically falsified, see project_gps_l5i_l5q_bias_fix |
| 1575.42 MHz (L1) | **L1CA** (RINEX `L1C`) | **L1C** (same) | **Yes** — direct match |
| 1207.14 MHz (BDS B2I) | **B2I** (RINEX `L7I`) | L7I | Yes |
| 1176.45 MHz (BDS B2a) | **B2aI** (RINEX `L5I`) | (often missing) | Need explicit publication |

The rule: an ideal SSR publishes phase biases for **every signal
variant** the receiver actually tracks, not for a "close-enough"
proxy on the same carrier.

## Receiver capability matrix

Per `docs/f9t-firmware-capabilities.md`, three distinct F9T variants
exist in the lab, each with different RF hardware and firmware
acceptance behavior:

### ZED-F9T (TIM 2.20, L5-hardware) — TimeHat

L1/L2/L5 RF chains.  Two-band RF chip means L2 and L5 are
mutually exclusive (only one second-band signal at a time).

| Constellation | Band | Tracks | RINEX | F9T can-receive |
|---|---|---|---|---|
| GPS | L1 | L1CA | `L1C` | ✓ |
| GPS | L2 | L2CL or L2CM | `L2L` / `L2S` | ✓ (alternative to L5) |
| GPS | L5 | L5I or L5Q | `L5I` / `L5Q` | ✓ (alternative to L2) |
| GAL | E1 | E1B/E1C | `L1B` / `L1C` | ✓ |
| GAL | E5a | E5aI/E5aQ | `L5I` / `L5Q` | ✓ (with L5 hardware enabled) |
| GAL | E5b | — | — | **✗** (firmware NAK) |
| BDS | B1 | B1I | `L2I` | ✓ |
| BDS | B2I | B2I | `L7I` | (untested, expect ✓ if L2 hardware) |
| BDS | B2a | B2aI | `L5I` | (untested, expect ✓ if L5 hardware) |
| GLO | — | — | — | **✗** (firmware NAK) |
| NavIC | — | — | — | **✗** (firmware NAK) |
| QZS | L1 | L1CA | `L1C` | ✓ (when SBAS/QZS enabled) |
| QZS | L5 | L5I/L5Q | `L5I` / `L5Q` | ✓ |

**Most capable variant.**  Can run two distinct dual-frequency
profiles for diagnostic comparison (e.g., L1+L2 on TimeHat while
F9T-20B fleet is on L1+L5).

### ZED-F9T (TIM 2.20, L2-only-hardware) — ptpmon

L1/L2 RF only — no 1176.45 MHz front-end.  L5/E5a/B2a all NAK
regardless of CFG-VALSET attempts.  Identified in software via
MON-HW3 vpManager_07=0.

| Constellation | Band | Tracks | RINEX | F9T can-receive |
|---|---|---|---|---|
| GPS | L1 | L1CA | `L1C` | ✓ |
| GPS | L2 | L2CL or L2CM | `L2L` / `L2S` | ✓ |
| GPS | L5 | — | — | **✗** (no RF) |
| GAL | E1 | E1B/E1C | `L1B` / `L1C` | ✓ |
| GAL | E5a | — | — | **✗** (no RF) |
| GAL | E5b | E5bI/E5bQ | `L7I` / `L7Q` | ✓ |
| BDS | B1 | B1I | `L2I` | ✓ |
| BDS | B2I | B2I | `L7I` | ✓ |
| BDS | B2a | — | — | **✗** (no RF) |
| GLO | — | — | — | **✗** (firmware NAK) |
| NavIC | — | — | — | (untested) |

**Locked to L2 / E5b / B2I as second-band signals.**  No L5 path.

### ZED-F9T-20B (TIM 2.25) — clkPoC3, MadHat

L1/L5 RF only — firmware NAKs L2C signals regardless of hardware
presence.  Adds NavIC support (no SSR available for NavIC, so
not currently exploitable).

| Constellation | Band | Tracks | RINEX | F9T can-receive |
|---|---|---|---|---|
| GPS | L1 | L1CA | `L1C` | ✓ |
| GPS | L2 | — | — | **✗** (firmware NAK on L2C) |
| GPS | L5 | L5I or L5Q | `L5I` / `L5Q` | ✓ |
| GAL | E1 | E1B/E1C | `L1B` / `L1C` | ✓ |
| GAL | E5a | E5aI/E5aQ | `L5I` / `L5Q` | ✓ |
| GAL | E5b | — | — | **✗** (firmware NAK) |
| GAL | E6 | (untested) | — | possible? hardware unknown |
| BDS | B1 | B1I | `L2I` | ✓ |
| BDS | B2I | (untested) | `L7I` | possibly NAK if locked to L5 band |
| BDS | B2a | B2aI | `L5I` | ✓ |
| BDS | B3I | (untested) | `L6I` | possibly |
| GLO | — | — | — | **✗** (firmware NAK) |
| NavIC | L5 | (untested) | — | ✓ (firmware advertises) |

**Locked to L5 / E5a / B2a as second-band signals.**  No L2 path.

## SSR requirements per receiver variant

For each variant, the *minimum signal coverage* an SSR stream must
publish so every observable signal is correctable:

### F9T 2.20 L5-hw (TimeHat) — needs **two** alternative profiles

Either of these two SSR profiles provides full coverage; we run
whichever the lab is configured for:

**L5 profile (`f9t-l5`)**:
- GPS code+phase: **L1C**, **L5Q**
- GAL code+phase: **L1C**, **L5Q** (E5aQ → RINEX L5Q)
- BDS code+phase: **L2I** (B1I), **L5I** (B2aI)

**L2 profile (`f9t-l2`)**:
- GPS code+phase: **L1C**, **L2L** (NOT L2W — that's a different signal)
- GAL code+phase: **L1C**, **L5Q** (E5a still works on this RF variant)
- BDS code+phase: **L2I**, **L7I** (B2I)

### F9T 2.20 L2-only-hw (ptpmon) — single profile only

**L2/E5b profile (`f9t-l2-e5b`)**:
- GPS code+phase: **L1C**, **L2L**
- GAL code+phase: **L1C**, **L7Q** (E5bQ → RINEX L7Q)
- BDS code+phase: **L2I**, **L7I**

This is the *only* viable profile — no L5/E5a/B2a hardware exists.

### F9T-20B 2.25 (clkPoC3, MadHat) — single profile only

**L5 profile (`f9t-l5`, default)**:
- GPS code+phase: **L1C**, **L5Q**
- GAL code+phase: **L1C**, **L5Q**
- BDS code+phase: **L2I**, **L5I**
- (Optional) NavIC code+phase: NavIC L5 — no SSR currently publishes this

## What CNES SSRA00CNE0 actually publishes

Empirically observed in lab logs 2026-04-24 (and consistent with
CNES SBAS Open documentation):

| Constellation | Code biases | Phase biases | Match for F9T tracking |
|---|---|---|---|
| GPS | C1C, C1P, C1W, C2L, C2S, C2W, C2X, C5Q | **L1C**, **L2W**, **L5I** | L1 ✓; L2 ✗ (publishes L2W, F9T tracks L2CL); L5 ✗ (publishes L5I, F9T tracks L5Q) |
| GAL | C1C, C5Q, C6C, C7Q | L1C, L5Q, L7Q, **L6C** | L1 ✓; E5a ✓; E5b ✓; E6C optional |
| BDS | C2I, C2X, C5P, C5D, C5X, C7I, C7X, C6I | L2I, L7I, **L6I** | B1 ✓; B2I ✓; B2a ✗ (no L5I publication); B3I (L6I) optional |

CNES is excellent for **GAL** on either F9T variant.  CNES is
**incomplete** for GPS regardless of F9T variant, and incomplete
for BDS B2a on F9T-20B.

## Signal-coverage scorecard for current SSR + lab fleet

| receiver variant | profile | GPS L1 | GPS L2/L5 | GAL E1 | GAL E5 | BDS B1 | BDS B2 | overall |
|---|---|---|---|---|---|---|---|---|
| F9T 2.20 L5-hw | L5 (default) | ✓ | **✗** | ✓ | ✓ | ✓ | **✗** | partial |
| F9T 2.20 L5-hw | L2 (alt) | ✓ | **✗** | ✓ | ✓ | ✓ | ✓ | better but still GPS L2 ✗ |
| F9T 2.20 L2-only | L2/E5b | ✓ | **✗** | ✓ | ✓ | ✓ | ✓ | partial |
| F9T-20B 2.25 | L5 (forced) | ✓ | **✗** | ✓ | ✓ | ✓ | **✗** | worst — both GPS L5 and BDS B2a missing |

The F9T-20B is the *worst* fit for CNES SSR: every GPS observation
has unmodeled L5Q phase bias, and every BDS-3 modernized SV (B2a)
has no phase bias at all.  This explains why the lab F9T-20B fleet
fails to converge honestly while ptpmon (L2-only-hw) at least
gets matched GAL biases — its GPS is broken too but it has fewer
biased observations to be pulled by.

## What we need from an alternative or additional SSR stream

In priority order:

1. **GPS L5Q phase bias** (or L5X — combined I+Q tracking).  Required
   for every F9T-20B in the fleet.  Currently the dominant lab error
   source (~13-22 m PR residuals at seeded truth, 56 m altitude
   drift in 13 min).
2. **GPS L2L phase bias** (or L2X).  Required for L1+L2 profile on
   F9T 2.20 L5-hw or L2-only-hw.  Would let us run the L2-only F9T
   fleet variants productively against AR.
3. **BDS B2a (L5I) phase bias**.  Without this, F9T-20B can't use
   BDS at all — already known to wreck the float solution
   (project_bds_gf_phase_units_bug, project_50m_bias_investigation,
   etc.).
4. **L6 (Galileo HAS) availability**.  HAS distributes orbit/clock
   corrections via the satellite signal itself, no NTRIP needed.
   Free.  If F9T-20B's E6 RF is present (untested), HAS would be a
   second-source independent of CNES with potentially different
   bias coverage.

## Stream survey

Catalog of NTRIP-streamed real-time SSR providers, with phase + code
bias coverage per signal-variant.  Sources: provider documentation,
recent peer-reviewed literature (cited inline), and lab probe logs.

### Free streams (IGS REGISTER, free) on `products.igs-ip.net`

| Mountpoint | Provider | Phase biases? | F9T signal coverage |
|---|---|---|---|
| **CNES** SSRA00CNE0, SSRC00CNE0 | CNES (France) | yes | GPS L1C ✓, L2W ✗, L5I (often, currently absent on lab) ✗.  GAL L1C/L5Q/L7Q/L6C ✓✓✓✓.  BDS L2I/L7I/L6I ✓ — **B2a (L5I) currently absent** on live stream (avail=[L2I,L7I,L6I] per day0421b log) |
| **WHU** OSBC00WHU1 | Wuhan Univ. | yes — per-observable OSB (Geng 2024) | GPS L1C/**L5Q**✓ + L2W (probing for L2L).  GAL L1C/L5Q/L7Q ✓.  BDS L2I/L7I/**L5I** ✓ — uses L5Q as reference signal (Liu 2021) |
| **CAS** SSRA01CAS1 (`-01` only; `-00` is no-phase) | Chinese Academy of Sciences | yes (~150 biases observed) | **Validated 2026-04-25 post-bug-fix.**  Uses IGS-SSR `4076_*` proprietary message IDs.  An engine bug (commit 485612d) caused 280 m position divergence on first try; fixed.  Post-fix CAS works for multi-constellation `gps,gal,bds` (single-host clkPoC3 ~+1.7 m east of Leica truth, σ 0.28 m at 13 min).  GAL `sig_id=2` IGS-SSR phase bias still unmapped (one signal dropped per SV, ~0.34 m impact). |
| **CHC** SSRA00CHC1, SSRC00CHC1 | CHC Navigation | yes | claimed full GREC, needs probing |
| **SHAO** SSRA01SHA*, SSRC01SHA* (`01` only) | Shanghai Astronomical Observatory | partial | needs probing |
| **IGS combined** SSRA02IGS1, SSRC02IGS1, SSRA03IGS1, SSRC03IGS1 | IGS RTS | **none** | orbit + clock + code only; **not useful for AR** |

### Free streams on other casters

| Mountpoint | Caster | Provider | Notes |
|---|---|---|---|
| **HAS00GAL0** (and similar) | gha-ntrip.gsc-europa.eu | Galileo HAS SL1 | Phase biases "structurally present, value=unavailable" in 2022 testing — re-probe needed.  **Uniquely** publishes GPS C1C+C2L+C2P code biases (matches F9T's L2CL).  GAL E1+E5a+E5b+E6.  Free, requires GSC registration.  Also broadcast directly via Galileo E6-B (no NTRIP needed if F9T's E6 RF works). |
| **MADOCA-PPP** MADO* mountpoints | mgmcaster.qzss.go.jp | JAXA / QZSS | Phase biases as FCB / WL+NL form (not OSB).  GPS+GLO+GAL+QZS, **no BDS**.  Free, JAXA R&D registration.  Also broadcast via QZSS L6E. |

### Commercial (subscription required)

| Service | Provider | F9T coverage | Cost |
|---|---|---|---|
| **PointPerfect Flex** | u-blox / Thingstream | GPS L1CA/L2C/L5, GAL E1/E5a/E5b, BDS B1I/B1C/B2I/B2a — full match for F9T tracking, **including L2L and L5Q** | commercial |
| **RTX (CenterPoint)** | Trimble | GPS/GLO/GAL/BDS/QZSS, all signals, proprietary format | commercial |
| **TerraStar-C PRO** | Hexagon / NovAtel | GPS/GLO/GAL/BDS, full coverage, NTRIP or L-band | commercial |

### Key signal-by-signal scorecard

What each free stream publishes for the signals our F9Ts actually track:

| Stream | GPS L1C | GPS **L2L** | GPS **L5Q** | GAL L1C | GAL L5Q | GAL L7Q | BDS L2I | BDS L7I | BDS **L5I** (B2a) |
|---|---|---|---|---|---|---|---|---|---|
| CNES SSRA00CNE0 | ✓ | ✗ (L2W only) | ✗ (L5I only) | ✓ | ✓ | ✓ | ✓ | ✓ | ✗ (currently) |
| WHU OSBC00WHU1 | ✓ | ? probe | **✓** | ✓ | ✓ | ✓ | ✓ | ✓ | **✓** |
| CAS SSRA01CAS1 (post-fix) | ✓ | ? probe | ? probe | partial (E1C `sig_id=2` unmapped in IGS-SSR map) | ✓ | ✓ | ✓ | ✓ | ? probe |
| Galileo HAS SL1 | ✓ | **✓ code only; phase TBD** | ✗ | ✓ | ✓ | ✓ | – | – | – |
| MADOCA-PPP | ✓ FCB | ? | ✓ | partial | ✓ | – | – | – | – |

The bolded items are the gaps we have most acutely — GPS L2L (for L1+L2
profile receivers) and GPS L5Q (for L1+L5 profile receivers, our
F9T-20B fleet).  WHU appears to fill both.

## Empirical winners (2026-04-25 testing)

After the 2x2 SSR isolation and CAS bug fix, today's measurements
favor a small number of configurations.  All numbers are east of
Leica truth on UFO1 (Leica accuracy ~0.5-1 m, OPUS post-processed
truth coming in days; ±0.5 m is "indistinguishable from truth"):

| Config | Result | When to use |
|---|---|---|
| **CNES O/C + WHU biases (clean), GAL-only, WL-only or WL+NL** | -0.65 to +0.07 m at 30 min, 18 cm cohort spread | Best accuracy + tightest cohort.  Production-candidate. |
| **CAS gps,gal,bds, WL+NL** | +1.7 to +1.95 m at 13 min, σ < 0.4 m, sub-meter single-host | Multi-constellation needs.  Stable, slightly biased. |
| CNES alone, GAL-only | -0.77 m at 30 min | Worse than CNES O/C + WHU biases by ~1.6 m. |

**Multi-constellation now usable post-CAS-fix.**  Adding constellations
beyond GAL on the WHU bias path was previously degraded by TimeHat's
older firmware under-admitting GPS L5 SVs (4.7 m fleet spread on
gps,gal in day0425i).  CAS as primary (single-AC, internally consistent
multi-const) avoids that bias-source mismatch and works on the F9T-20B
hosts (clkPoC3 + MadHat) cleanly.

## Best-fit recommendations per F9T variant

### F9T 2.20 L5-hw (TimeHat) running L5 profile, OR F9T-20B (clkPoC3, MadHat)

**Both run the same `f9t-l5` profile and have the same need: GPS L5Q
phase bias.**

Best fit: **WHU OSBC00WHU1**, used either standalone or composed
with CNES (dual-mount per `docs/ssr-mount-survey.md` — already have
working pattern from day0419+).
- WHU provides L5Q phase bias (matching F9T tracking) for GPS+GAL
- WHU provides BDS L5I (B2a) phase bias (which CNES is currently
  missing on live stream)
- Compose with CNES for orbit/clock continuity if WHU's
  orbit/clock isn't independently good enough

Action: **probe `OSBC00WHU1` live to confirm L5Q phase bias is
non-zero on the current stream**.  If yes, switching the mount
to WHU (or running WHU as primary in a dual-mount) probably
unblocks the F9T-20B fleet immediately.

### F9T 2.20 L5-hw (TimeHat) running L2 profile (`--receiver f9t-l2`)

Need GPS L2L phase bias.

Best fit: **Galileo HAS** when its phase biases come fully online
— it's the only stream that uniquely publishes GPS C2L code (and
plans phase) directly matching the F9T's L2CL tracking.

Today: GPS L2 AR on this profile is structurally blocked.  WHU
might also publish L2L (probing required); if not, no free stream
covers this case.

Workaround: switch TimeHat to L5 profile (use F9T-20B-style
config) and use WHU L5Q.

### F9T 2.20 L2-only-hw (ptpmon) — no L5 hardware

Same as above: needs GPS L2L phase bias.  Needs Galileo HAS
phase-bias arrival or WHU L2L confirmation.

Today: GPS L2 AR blocked; this F9T can use GAL+BDS via CNES
(both work) but GPS contributes geometry only.

## Access summary — getting streams we don't already have

We already have access to **products.igs-ip.net** (used for CNES).
The same credentials work for **WHU OSBC00WHU1**, **CAS SSRA01CAS1**,
**CHC**, **SHAO** mountpoints — just change the mount name in
`ntrip.conf`.  No new registration needed.

For streams not on products.igs-ip.net:

| Stream | Caster | How to get access |
|---|---|---|
| Galileo HAS NTRIP | gha-ntrip.gsc-europa.eu | Register at the [GSC](https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has).  Free.  May also need IDD (Internet Data Distribution) registration. |
| MADOCA-PPP NTRIP | mgmcaster.qzss.go.jp | [JAXA QZSS R&D registration](https://qzss.go.jp/en/technical/dod/madoca/madoca_internet_distribution.html).  Free. |
| u-blox PointPerfect Flex | Thingstream caster | Commercial subscription via u-blox.  Quote required. |
| Trimble RTX | rtxdata.trimble.com | CenterPoint RTX subscription.  Commercial. |
| Hexagon TerraStar | NovAtel caster | TerraStar-C PRO subscription.  Commercial. |

**Practical priority for tonight / this week**:

1. **Probe WHU OSBC00WHU1 for L5Q phase bias** (5 min: change
   mount in ntrip.conf, restart one host, look for `Phase bias
   lookup: ... L5Q ... HIT`).  If present, this is the immediate
   fix for F9T-20B fleet.  No new credentials.
2. **Re-probe CNES SSRA00CNE0 for B2a (L5I) phase bias** (same
   procedure, just look for BDS C5I/L5I in the avail list).
   Coverage changes; our last log was day0421b.
3. **Register Galileo HAS** at GSC.  Future-looking; will help
   F9T-L2 hardware paths when phase biases land.
4. **Skip commercial unless WHU and HAS both prove inadequate.**

## Cross-references

- `docs/f9t-firmware-capabilities.md` — firmware ACK/NAK matrix
- `docs/correction-sources.md` — original SSR provider catalog
- `docs/ssr-mount-survey.md` — earlier F9T-focused survey + dual-mount pattern
- `project_gps_l5i_l5q_bias_fix.md` — empirical L5I≠L5Q substitution failure
- `project_cnes_phase_bias_signals.md` — CNES signal coverage notes
- Geng et al. 2024 — WHU OSBC00WHU1 stream, https://link.springer.com/article/10.1007/s10291-023-01610-6
- Liu et al. 2021 — WHU L5Q reference signal, https://link.springer.com/article/10.1007/s00190-021-01500-0
- CNES PPP-Wizard SSR ICD — http://www.ppp-wizard.net/ssr.html
- Galileo HAS — https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has
- IGS RTS mountpoints — https://igs.org/rts/products/

## Cross-references

- `docs/f9t-firmware-capabilities.md` — firmware ACK/NAK matrix per
  CFG key, with experimental setup.
- `docs/correction-sources.md` — original SSR provider catalog,
  written before the L5I/L5Q substitution failure was identified.
- `project_gps_l5i_l5q_bias_fix.md` — empirical demonstration that
  L5I phase bias does NOT substitute for L5Q (commits b71e2b1
  applied, 15f9b01 reverted).
- `project_50m_bias_investigation.md` — earlier altitude-bias
  investigation.
- `project_to_main_pride_resid_histogram_20260424.md` — Bravo's
  ABMF residual analysis showing that on clean reference data
  (gps:l2 profile), the engine achieves ~1.5 m mean 3D — the floor
  is observation-model bias, not filter tuning.
