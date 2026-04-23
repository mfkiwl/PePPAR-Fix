# External ionospheric data sources

Research note, 2026-04-23.  Not currently active work — filed for
reference if/when we want to correlate our trip events with regional
ionospheric state, or build a forecast-driven alert mode.

## Why this might be useful

The pre-dawn TEC slip storm that compromised clkPoC3 and MadHat on
day0422f-wlonly (while TimeHat and ptpmon rode through) is driven
by the same ionospheric dynamics that HF amateur radio operators
spend enormous effort monitoring.  Hams have built real-time
infrastructure — TEC measurement networks, ionosonde observations,
citizen-science platforms — that directly overlap with what we'd
want to pair against our lab data.

**Primary use case: post-hoc correlation.**  Overlay our
`[WL_DRIFT]` and `[FIX_SET_INTEGRITY] TRIPPED` events against
regional TEC maps or ionospheric indices.  Separates "ionospheric
cause" from "lab-specific cause" when a trip fires.

**Secondary use case: forecast-driven alert mode.**  When SWPC's
45-day Ap/F10.7 forecast predicts a disturbed period, we could
raise sensitivity on the drift monitor and schedule overnight runs
with tighter logging.

**Marginal use case: real-time reaction.**  TEC products at 15–20
minute cadence are borderline inside our 30–45 minute
pull-to-catastrophe window.  The WL drift monitor measures our
own receiver's response to the ionosphere directly and is much
faster — unlikely that real-time external TEC would beat our own
signal for reaction.

## The physical link

Sunrise and sunset drive strong **F-layer photoionization
gradients**.

For GNSS: TEC gradient causes decorrelated cycle slips across many
SVs at once, carrier-phase discontinuities, and fractional cycle
shifts that land MW commits on wrong integers.  What we see.

For HF amateur radio: the terminator-line region has low D-layer
absorption (no sunlight in D yet) but intact F-layer reflection —
**grayline propagation**.  DX contacts open for minutes at dawn/
dusk.  Hams schedule around these windows.

Same photoionization physics, different observables in different
frequency bands.

## Data sources

### Tier 1 — IGS real-time Global Ionospheric Maps

Most operationally relevant.  Global TEC grids at 15–20 minute
cadence, combined from the IGS Analysis Centers:

- JPL, CODE, UPC, ESA/ESOC, CAS, WHU, NRCan, OPTIMAP

Product stream names:
- **IRTG** — real-time combined (~20 min latency)
- **IGRG** — rapid combined (<24 hr)
- **IGSG** — final combined (~11 days)

Performance per the 2023 MDPI study: real-time GIMs achieve
"comparable positioning performance" to post-processed GIMs under
disturbed conditions, with larger variability at short tau.

Entry points:
- IGS Iono WG: https://igs.org/wg/ionosphere/
- JPL GDGPS TEC maps: https://gdgps.jpl.nasa.gov/products/tec-maps.html
- DLR IMPC (German Aerospace) near-real-time: https://impc.dlr.de/products/total-electron-content/near-real-time-tec/near-real-time-tec-maps-global

### Tier 2 — HamSCI citizen-science network

Distributed sensor network with direct applicability to our
regional conditions.  Coverage across North America with
particularly good density in the northeastern US (closest to our
DuPage County site).

Components:
- **ScintPi**: GNSS-based TEC + scintillation monitors on a Raspberry
  Pi.  Same form factor as our lab hosts — conceivably deployable
  on-site as a dedicated TEC witness receiver.
- **Personal Space Weather Station (PSWS)**: modular multi-band
  radio receivers (VLF through VHF) + ground magnetometer.
- Real-time data streams exist, though "not currently used in any
  official capacity" — early-stage but active.

Lead institutions: U. of Scranton (W3USR), Case Western (W8EDU),
U. Alabama, NJIT (K2MFF), MIT Haystack, TAPR.

Entry points:
- https://hamsci.org/
- https://hamsci.org/TEC-Measurement
- NASA citizen-science page: https://science.nasa.gov/citizen-science/ham-radio-science-citizen-investigation/
- Overview paper (Frontiers 2023): https://www.frontiersin.org/journals/astronomy-and-space-sciences/articles/10.3389/fspas.2023.1184171/full

### Tier 3 — NOAA SWPC bulk indicators

Coarse baseline context.  Too blunt to predict a specific sunrise
TEC gradient but useful for long-run correlation.

- **K-index** (3-hr geomagnetic, planetary).  Derived from 8
  ground magnetometers (Sitka, Meanook, Ottawa, Fredericksburg,
  Hartland, Wingst, Niemegk, Canberra).
- **F10.7** (daily solar flux at 2800 MHz, the canonical solar
  activity proxy).
- **Ap** (daily geomagnetic).
- **45-day Ap and F10.7 forecast**.

JSON API via `services.swpc.noaa.gov`.  The 45-day forecast text
is at `services.swpc.noaa.gov/text/45-day-forecast.txt` with
JSON alongside.

Entry points:
- https://www.swpc.noaa.gov/products/planetary-k-index
- https://www.swpc.noaa.gov/phenomena/f107-cm-radio-emissions
- https://www.swpc.noaa.gov/news/new-json-data-now-available

## What a first-pass analysis would look like

When we do pick this up, the minimum-viable correlation pass:

1. Pull our log's `[WL_DRIFT]` + `TRIPPED` event timestamps across
   the four hosts over a week.
2. Pull corresponding IGS IRTG TEC tiles for our region (41.8°N,
   -88°W) at matching timestamps.
3. Compute gradient `dTEC/dt` and `|∇_horizontal TEC|` across the
   region.
4. Scatter: our trip rate vs regional TEC gradient, binned by UTC
   hour-of-day.
5. Overlay K-index on top.

Expected outcome sketch:
- Clear visual cluster of trips at sunrise/sunset TEC peaks
- Residual trips (anything that fires when TEC is flat) = not
  ionospheric → look for other cause (multipath, SSR artifact,
  thermal, lab-specific)

One weekend's work with a Jupyter notebook — don't need to wire
anything into the engine.

## What's NOT recommended

- **Don't build real-time SWPC/IGS integration into the engine.**
  Our own observables (MW drift, per-SV PR residual, cross-host
  agreement) are faster and directly measure what affects us.
- **Don't deploy a ScintPi on-site as a witness receiver** until
  we've exhausted what our existing four F9Ts can tell us about
  local conditions.
- **Don't pre-emptively re-time runs around space-weather
  forecasts.**  If we want storm data, we can get it from a
  regular overnight cadence — storms are common enough.

The whole area is worth knowing about.  Operationalizing any of
it is a "later, if we need it" call.
