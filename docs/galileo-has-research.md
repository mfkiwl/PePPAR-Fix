# Galileo High Accuracy Service (HAS) — Research

## What is it?

Galileo HAS provides **free, worldwide PPP-AR corrections** — orbit,
clock, code bias, and **phase bias** for GPS and Galileo. Operational
since January 2023.

This is exactly what we're missing: our current SSR source (SSRA00BKG0)
provides orbit + clock + code bias but **zero phase biases**. HAS would
give us the phase biases needed for PPP-AR, potentially cutting
convergence from 30-60 minutes to 7-14 minutes.

## How to access it

Two paths:

### 1. E6-B signal (satellite)
Corrections broadcast on Galileo E6-B. Requires an E6-capable receiver.
The F9T does **not** support E6. The F10T might (needs verification).
Latency: ~6 seconds.

### 2. Internet Data Distribution (IDD)
Corrections via **NTRIP** — same protocol we already use for SSRA00BKG0.
Free, requires [registration at GSC](https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has/internet-data-distribution).

- Caster hostname/port/mountpoint provided after registration
- NTRIP v2 with optional TLS
- Format: RTCM-like SSR messages (orbit, clock, code bias, **phase bias**)
- Registration is free, authorized by EUSPA

**Action: Register at GSC to get IDD credentials.**

## What corrections are provided?

| Correction | GPS | Galileo | BDS |
|---|---|---|---|
| Orbit | Yes | Yes | No (initial service) |
| Clock | Yes | Yes | No |
| Code bias | Yes | Yes | No |
| **Phase bias** | **Yes** | **Yes** | No |

BDS is NOT covered in the initial HAS service. GPS + Galileo only.
This is fine for us — BDS is broken in our code anyway.

## Correction quality

From published evaluations:
- Orbit accuracy: ~5 cm (GPS), ~4 cm (Galileo)
- Clock accuracy: ~0.1-0.2 ns
- Code bias: sub-ns
- Phase bias: enables integer ambiguity resolution

## PPP-AR convergence with HAS

| Scenario | Convergence |
|---|---|
| GPS+GAL float PPP (no AR) | 30-60 min |
| GPS+GAL PPP-AR with HAS | **7-14 min** |
| GPS+GAL+BDS triple-freq PPP-AR | ~7 min (needs BDS support) |

Source: [Springer GPS Solutions](https://link.springer.com/article/10.1007/s10291-024-01617-7)

## Python libraries

### HASlib (recommended)
[github.com/nlsfi/HASlib](https://github.com/nlsfi/HASlib) — Finnish
National Land Survey. Decodes HAS corrections from E6-B or IDD and
converts to RTCM-SSR or IGS-SSR format. Open source (MIT license).

Can feed corrections directly into our `SSRState` via the same RTCM
parsing path we already use for SSRA00BKG0.

### CSSRlib
Python toolkit for SSR corrections from multiple free services
including HAS. Can decode and apply corrections.

### Integration with RTKLIB
[Published paper](https://link.springer.com/article/10.1007/s10291-024-01617-7)
demonstrates HASlib + RTKLIB integration for PPP-AR. Proves the
corrections work for real-time positioning.

## How this fits peppar-fix

```
Current:  F9T → RAWX → peppar-fix → SSR (SSRA00BKG0, no phase bias) → float PPP (30-60 min)
With HAS: F9T → RAWX → peppar-fix → SSR (HAS IDD, WITH phase bias) → PPP-AR (7-14 min)
```

The change is small: add HAS IDD as an NTRIP source (alongside or
replacing SSRA00BKG0). The SSR message parsing in `ssr_corrections.py`
already handles the message types HAS provides. The PPP filter needs
phase bias application added to enable integer ambiguity resolution.

## Comparison: HAS vs our current SSR (SSRA00BKG0)

| Aspect | SSRA00BKG0 (BKG) | HAS IDD (Galileo) |
|---|---|---|
| Cost | Free | Free |
| Registration | BKG account | GSC account |
| Orbit/Clock | Yes | Yes |
| Code bias | Yes (96 biases) | Yes |
| **Phase bias** | **No (0)** | **Yes** |
| PPP-AR | Not possible | **Possible** |
| Constellations | GPS+GLO+GAL+BDS | GPS+GAL only |
| Latency | ~5-10s | ~10-30s (IDD) |

## Recommended next steps

1. **Register for HAS IDD** at https://www.gsc-europa.eu (Bob)
2. **Connect HAS as NTRIP source** — add as `--ssr-mount` alongside
   existing BKG source
3. **Verify phase biases arrive** — check `ssr.summary()` shows
   non-zero phase bias count
4. **Implement phase bias application** in the PPP filter for AR
5. **Measure convergence time** improvement with HAS vs without

## References

- [Galileo HAS overview (GSC)](https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has)
- [HAS IDD registration](https://www.gsc-europa.eu/galileo/services/galileo-high-accuracy-service-has/internet-data-distribution)
- [HASlib (GitHub)](https://github.com/nlsfi/HASlib)
- [HASlib + RTKLIB integration (Springer)](https://link.springer.com/article/10.1007/s10291-024-01617-7)
- [HAS performance evaluation (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0273117724011438)
- [HAS timing/time transfer (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0263224124000368)
- [HAS + BDS-3 PPP-B2b fusion (ScienceDirect)](https://www.sciencedirect.com/science/article/abs/pii/S0263224125021888)
