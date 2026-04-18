# SSR mount survey for F9T-based PPP-AR

*2026-04-17 research summary — what mounts other F9T users run with, and
where our current CNES setup stands.*

## The premise we had — partly wrong

Our early-2026-04-17 investigation of the wrong-integer epidemic
blamed **CNES publishing GPS L5 phase biases only under L5I (RTCM
sig_id=14)** while our F9T tracks L5Q (sig_id=7).  Subsequent
literature review contradicts the strong form of that claim:

- Banville et al., *Data and pilot biases in modern GNSS signals*,
  GPS Solutions 2023 — [link](https://link.springer.com/article/10.1007/s10291-023-01448-y)
- Wang et al., *GNSS OSB for all-frequency PPP-AR*, J Geodesy 2022 —
  [link](https://link.springer.com/article/10.1007/s00190-022-01602-3)

Both papers state that GPS L5I and L5Q phase biases are treated as
equal by every major analysis centre.  The physical difference is the
deterministic 90° quadrature offset between the data (I) and pilot (Q)
components of the same carrier, which is absorbed by the receiver's
tracking loop and by the integer ambiguity.  Applying an L5I phase
bias to an L5Q observation is canonical practice.

**So why did applying the L5I bias blow up MadHat in our failed
experiment on 2026-04-16?**  Best current hypothesis: we remapped
`SIG_TO_RINEX` for *both* phase and code, and code biases really do
differ between C5I and C5Q.  Alternatively, CNES's L5I phase bias
carries an integer offset chosen relative to an L5I-tracking reference
receiver — a constant per satellite — and that integer shift lands
LAMBDA on a different (wrong) integer.  Neither is the "sub-cycle
incompatibility" we assumed.

## u-blox ZED-F9T cannot be reconfigured to track L5I

- F9T Interface Description UBX-20033631 and Integration Manual
  UBX-21040375 expose no CFG key to select data (I) vs pilot (Q).
- `CFG-SIGNAL-GPS_L5_ENA` enables the band; pilot tracking is
  hard-wired in firmware for modernized signals.
- Scratch "configure F9T to track L5I" from the candidate-fix list.

## Mounts ranked for F9T + PPP-AR

Ordered by likelihood of working with our F9T's L5Q (pilot) tracking:

| Mount | Caster | Provider | Biases | Access | Verdict |
|---|---|---|---|---|---|
| **OSBC00WHU1** | `products.igs-ip.net` | Wuhan University | Per-code OSB (C1C, C2W, C5I, C5Q, etc.) | IGS login, free | **Strongest candidate** — OSB is per-code by design; validated for GPS+GAL+BDS AR in Geng et al. 2024. |
| MADOCA-PPP | JAXA NTRIP | JAXA | GPS/GLONASS/QZSS L1/L2/L5 code + phase | Free R&D registration | Entirely different AC; good second try. |
| Galileo HAS IDD | GSC-issued | European GNSS Service Centre | GPS + GAL | Free, GSC registration | Already on roadmap (`docs/galileo-has-research.md`). |
| SSRA01CAS1 | `ntrip.data.gnss.ga.gov.au` | Chinese Academy of Sciences (phase 2) | Code + phase | Existing credentials likely cover it | Unverified — probe its `avail=[…]` list like we did for CNES. |
| **SSRA00CNE0** | `products.igs-ip.net` | CNES | L1C/L2W/L5I phase | IGS login, free | Our current mount.  Produces sub-ideal GPS AR with F9T L5Q, for reasons we haven't fully isolated. |
| IGS combined (SSRA02IGS0, …) | `products.igs-ip.net` | IGS RTS Kalman combination | **Orbit + clock only, no phase bias** | IGS login, free | Skip for AR. |

Note CAS's primary mount `SSRA00BKG0` (on `ntrip.data.gnss.ga.gov.au`)
is already covered by project memory
[`reference_cas_ssr_mount`](../state/README.md) — 159 phase biases,
GPS L5Q among them per the memory, though earlier attempts hit signal-
code mismatches.  Worth re-evaluating in light of the literature above.

## Candidate failure modes beyond L5I/L5Q

The NL-residual monitor lets us distinguish these; instrumentation
landed 2026-04-17:

- **GPS L5 ISC / TGD handling** — `docs/f9t-firmware-capabilities.md`
  notes a 50 m bias still open as of 2026-04-16.
- **C5I vs C5Q code-bias difference** — code biases are genuinely
  per-code, unlike phase.  CNES publishes C5Q for GPS in at least some
  frames; need to check coverage per SV.
- **ZTD-ambiguity correlation on single-constellation runs** —
  partially addressed by the ISB-pin fix + ZTD state, but may still
  leak in GAL-only under poor geometry.
- **Sign/cycle-slip handling** — standard suspect list.

## Action plan for the morning of 2026-04-18

1. **Try `OSBC00WHU1`** — one-line mount change.  Existing RTCM decoder
   handles OSB messages.  Target the F9T L5Q question by changing
   providers rather than reconfiguring the receiver.
2. **Register for MADOCA-PPP and Galileo HAS IDD** so we have
   alternative paths tomorrow.
3. **Read the NL-residual instrumentation** from overnight runs — if
   WL residuals are clean but NL shows a per-satellite *fractional*
   cycle bias, the L5I/L5Q intuition is right after all.  If NL shows
   a per-satellite *integer* (multi-cycle) bias, it's code-bias or
   ISC/TGD.

## Sources
- Banville et al. 2023 (data + pilot bias handling): <https://link.springer.com/article/10.1007/s10291-023-01448-y>
- Wang et al. 2022 (GNSS OSB all-frequency PPP-AR): <https://link.springer.com/article/10.1007/s00190-022-01602-3>
- Geng et al. 2024 (WHU phase-bias stream): <https://link.springer.com/article/10.1007/s10291-023-01610-6>
- IGS RTS Products page: <https://igs.org/rts/products/>
- MADOCA-PPP internet distribution: <https://qzss.go.jp/en/technical/dod/madoca/madoca_internet_distribution.html>
- Galileo HAS IDD: <https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has/internet-data-distribution>
