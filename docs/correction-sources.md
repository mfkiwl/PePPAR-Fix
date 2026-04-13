# Correction Sources for PePPAR Fix

PePPAR Fix needs real-time SSR corrections delivered via NTRIP. This
document covers what's available, how to get access, and how to choose.

## What you need

**For float PPP (position + clock, no integer ambiguity resolution):**
- Broadcast ephemeris (any NTRIP mount carrying RTCM 1019/1020/1042/1046)
- SSR orbit + clock + code bias corrections

The combined IGS stream works fine for this. Any mirror that carries it
will do.

**For PPP-AR (integer ambiguity resolution, sub-ns clock):**
- Everything above, plus **phase biases**
- All corrections must come from a **single analysis center (AC)**

Each AC makes an internal choice about how to partition the satellite
clock correction from the carrier-phase bias. This partition is arbitrary
but self-consistent within one AC's products. Mixing corrections from
different ACs destroys the integer nature of the ambiguities because the
partitioning conventions differ. The combined IGS stream does not include
phase biases and has consistency issues from the combination process.

## Available NTRIP casters

| Caster | Host | Port | Notes |
|--------|------|------|-------|
| BKG (observations) | igs-ip.net | 2101, 443 | Broadcast ephemeris, observation streams |
| BKG (products) | products.igs-ip.net | 2101, 443 | SSR corrections including single-AC streams with phase biases |
| Geoscience Australia | ntrip.data.gnss.ga.gov.au | 443 (TLS) | Mirror of BKG combined streams. Accepts BKG credentials |
| CDDIS (NASA) | caster.cddis.eosdis.nasa.gov | 443 | NASA mirror. Requires Earthdata login |
| UCAR/COSMIC | rt.igs.org | 2101 | Separate registration |

## Registration

All NTRIP casters require authentication. There is no anonymous access.

1. Register at https://register.rtcm-ntrip.org/ (free for research and education use)
2. Your credentials work immediately on the observation caster (igs-ip.net)
3. Product stream access (products.igs-ip.net) may require a separate request — email `igs-ip@bkg.bund.de` describing your project and which streams you need
4. The Australian mirror (ntrip.data.gnss.ga.gov.au) accepts BKG credentials and is a good fallback

## Recommended streams

### Float PPP (current PePPAR Fix capability)

Any of these work:
- `SSRA00BKG0` on the Australian mirror or products.igs-ip.net (combined IGS, GPS+GAL+BDS)
- `IGS03` on products.igs-ip.net (combined IGS)

Broadcast ephemeris:
- `BCEP00BKG0` on the Australian mirror
- `BCEP00CAS0` on products.igs-ip.net

### PPP-AR (future)

Use a single AC's complete product set. Recommended ACs (per BKG/Andrea,
2026-03):

| AC | SSR mount | Notes |
|----|-----------|-------|
| CAS (Chinese Academy of Sciences) | `SSRA01CAS1` | Commonly recommended |
| CNES (French space agency) | `SSRA00CNE1` | Commonly recommended |
| WHU (Wuhan University) | `SSRA00WHU1` + `OSBC00WHU1` | Needs two streams |

PPP-AR performance comparison across ACs:
https://igs.bkg.bund.de/ntrip/ppp

### Alternative: Galileo HAS

Galileo HAS provides free PPP-AR corrections (orbit, clock, code bias,
phase bias) for GPS and Galileo, broadcast via the Galileo E6-B signal.
No NTRIP registration needed, but requires an E6-capable receiver or an
internet gateway. See [galileo-has-research.md](galileo-has-research.md).

## Configuration

PePPAR Fix reads NTRIP credentials from `ntrip.conf` in the repo root
(gitignored — never commit credentials):

```ini
[ntrip]
caster = ntrip.data.gnss.ga.gov.au
port = 443
mount = SSRA00BKG0
user = <your-username>
password = <your-password>
tls = true
```

Broadcast ephemeris mount is passed separately via `--eph-mount`.

## Reference frames and correction services

SSR corrections place the satellite orbits (and therefore the user
position) in a specific ITRF realization.  IGS products use ITRF2020
(current epoch).  CAS and CNES use the same.  Positions computed with
different correction services may disagree by tens of centimeters if
the services use different ITRF epochs — the offset is accumulated
tectonic plate motion, not a bug.

This matters for PePPAR Fix only if we compare our AntPosEst position
against a local survey marker or a correction service that uses a
regional fixed-epoch frame (e.g., ETRF89, GDA2020).  Within a single
correction service, the frame is self-consistent.

For background on why different correction services produce offset
positions, see the u-blox white paper:
[Not just where are you, but when are you — reference frames and correction services](https://content.u-blox.com/sites/default/files/documents/reference-frames-correction-services-white-paper-online.pdf)
(Bastian Huck, u-blox AG).
