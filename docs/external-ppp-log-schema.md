# External PPP Solution Log — Schema

A documented, engine-agnostic log format for PPP / PPP-AR solutions
produced by **any** engine, intended for cross-engine comparison.
PePPAR-Fix's own engine, BNC + PPP-Wizard, RTKLIB, PRIDE replay
output, etc. all map onto this schema via per-engine adapters that
live in their own repos (e.g. `peppar-bnc-glue` for BNC).

PePPAR-Fix consumes this format only via `scripts/overlay/` —
nothing in the engine, the wrapper, or peppar-mon depends on it.

## Format

**JSONL** — one JSON object per line, UTF-8, LF-terminated.  Lines
that fail to parse are logged and skipped, not fatal.  Comment
lines starting with `#` are ignored.

JSONL is preferred over CSV because the schema will evolve (per-AC
flags, multipath flags, regime labels), and JSONL adds keys without
breaking older consumers.

## Required fields

Every record MUST contain these keys.  A record missing any
required field is invalid and SHOULD be skipped by the overlay
tool with a single warning.

| Key | Type | Units / values | Notes |
|---|---|---|---|
| `epoch_unix` | float | seconds since 1970-01-01 UTC | Sub-second precision encouraged. |
| `engine` | string | free-text identifier | e.g. `"peppar-fix"`, `"bnc-ppp-wizard"`, `"pride-batch"`. Stable across runs of the same engine. |
| `corrections_source` | string | free-text identifier | e.g. `"CNES_O/C+WHU_bias"`, `"PPP-Wizard_BNC_SSRA00CNE0"`, `"PRIDE_WUM0MGXRAP"`. **The bias-source axis matters more than the algorithmic axis** given the engine's measured 1.22 m CNES-vs-WHU gap (`project_2x2_ssr_isolation_20260425`). Make this visible. |
| `fix_mode` | string enum | see below | Coarse-grained AR state. |
| `pos` | object | `{ "ecef": [x, y, z] }` OR `{ "llh": [lat_deg, lon_deg, alt_m] }` | At least one position representation. Both is fine. ECEF preferred for engine comparison; LLH preferred for human inspection. |

### `fix_mode` enum

Aligned with PePPAR-Fix's `SvAmbState` lifecycle vocabulary
(rename Commit (b), 2026-04-22).  Adapter normalizes external
engine state names onto these.

| Value | Meaning |
|---|---|
| `"none"` | No solution this epoch (e.g. insufficient SVs, divergence). |
| `"float"` | Float PPP, no integer ambiguity resolution attempted. |
| `"wl_fixed"` | Wide-lane integers fixed; narrow-lane still float. |
| `"nl_fixed"` | Narrow-lane integers fixed (short-term — analogous to `NL_SHORT_FIXED`). |
| `"validated"` | Integer fix has survived validation (azimuth-motion, time-on-fix; analogous to `NL_LONG_FIXED` / `ANCHORED`). |

Engines that don't distinguish all five states emit the most
specific value they can. PRIDE-batch typically emits `"float"`
or `"validated"` only. PPP-Wizard exposes WL/NL distinctly.

## Recommended fields

Consumers SHOULD handle these when present, but MUST tolerate
their absence.

| Key | Type | Units | Notes |
|---|---|---|---|
| `sigma` | object | `{ "e": σe, "n": σn, "u": σu }` (meters, 1-σ) | OR `{ "ecef": [σx, σy, σz] }`. Filter / formal sigmas. |
| `n_used` | int | count | SVs contributing to the solution this epoch. |
| `ar_ratio` | float | dimensionless | LAMBDA ratio test, when AR was attempted. |
| `clock_offset_ns` | float | nanoseconds | Receiver clock estimate. PePPAR-Fix engine emits `dt_rx_ns`; map onto this key. |
| `ztd_residual` | float | meters | Tropospheric residual above the dry a-priori. |
| `ztd_sigma` | float | meters, 1-σ | Filter sigma on the ZTD state. |
| `host` | string | hostname | The host running the engine.  Useful when the same engine runs on multiple hosts simultaneously. |

## Optional / engine-specific fields

Anything else the engine wants to report.  These keys MUST start
with `_` to mark them as engine-private and SHOULD NOT be relied on
across engines.  Examples:

- `_lambda_bootstrap_p` — PePPAR-Fix engine's LAMBDA bootstrap success probability
- `_anchor_count_short` / `_anchor_count_long` — PePPAR-Fix anchor counts
- `_pppw_phase_bias_age_s` — PPP-Wizard's age of phase-bias data

Consumers MUST ignore unknown `_`-prefixed keys silently.

## Example records

PePPAR-Fix engine, full feature set:

```json
{"epoch_unix": 1714153425.0, "engine": "peppar-fix", "corrections_source": "CNES_O/C+WHU_bias", "fix_mode": "validated", "pos": {"ecef": [-565421.119, -4830876.243, 4099879.402]}, "sigma": {"e": 0.018, "n": 0.022, "u": 0.041}, "n_used": 17, "ar_ratio": 4.7, "clock_offset_ns": -317.4, "ztd_residual": 0.012, "ztd_sigma": 0.008, "host": "clkPoC3", "_anchor_count_short": 3, "_anchor_count_long": 11}
```

BNC + PPP-Wizard, minimum viable fields:

```json
{"epoch_unix": 1714153425.5, "engine": "bnc-ppp-wizard", "corrections_source": "PPP-Wizard_BNC_SSRA00CNE0", "fix_mode": "nl_fixed", "pos": {"ecef": [-565421.103, -4830876.251, 4099879.418]}, "sigma": {"e": 0.025, "n": 0.030, "u": 0.058}, "n_used": 14, "ar_ratio": 3.2, "host": "ptpmon"}
```

PRIDE batch replay (post-processed), float-only:

```json
{"epoch_unix": 1714153425.0, "engine": "pride-batch", "corrections_source": "PRIDE_WUM0MGXRAP", "fix_mode": "validated", "pos": {"ecef": [-565421.118, -4830876.244, 4099879.401]}, "sigma": {"e": 0.001, "n": 0.001, "u": 0.002}}
```

## Producer responsibilities

- **One file per engine run.**  Filenames SHOULD follow
  `<engine>-<host>-<startISO8601>.jsonl` for human navigation
  (e.g. `peppar-fix-clkPoC3-20260426T160000Z.jsonl`).  This is a
  convention, not enforced by the schema.
- **Append-only.**  No epoch SHOULD ever be edited or removed once
  written.
- **Monotonic `epoch_unix`.**  Records SHOULD be sorted; out-of-
  order epochs MAY be tolerated by consumers but are discouraged.
- **Same time scale across engines.**  All `epoch_unix` MUST be
  GPS-aligned UTC (the same scale PePPAR-Fix's `[AntPosEst]`
  emits today).  Adapter responsibility to convert from
  engine-native time tags.

## Consumer responsibilities

- Tolerate absent recommended/optional fields.
- Ignore unknown `_`-prefixed keys.
- Surface `corrections_source` prominently in any comparison output
  — it's the most informative axis when two engines disagree.
- Never silently coerce one position representation to the other;
  if comparing ECEF and LLH from two engines, document the
  conversion.

## Why JSONL not CSV

- **Schema evolution**: a new field is a new key; old consumers
  skip it.  CSV header changes break parsers.
- **Nested structure**: `pos.ecef`, `sigma.e/n/u` are natural;
  CSV flattens them awkwardly.
- **Engine-private fields**: `_`-prefix + JSON's open
  object model is cleaner than CSV's positional rigidity.
- **Cost**: ~3× larger than CSV.  At 1 Hz × 24 h × 200 bytes/line
  it's 17 MB/day.  Negligible for lab data; trim or compress on
  the rare host where it matters.

## Versioning

This schema is **v1**, defined 2026-04-26.  The file MAY include a
single `# schema-version: 1` comment at the top to declare its
version.  Future incompatible changes will increment the integer
and ship as `external-ppp-log-schema-v2.md` alongside this file.
v1 will remain stable indefinitely; consumers will continue to be
able to read v1 records.

## Cross-references

- **Producer adapters**: `peppar-bnc-glue/adapters/` (BNC),
  PePPAR-Fix engine emits this format directly via a future engine
  flag (TBD), PRIDE replay adapter (TBD).
- **Consumer**: `scripts/overlay/overlay_engine_solutions.py`.
- **Lifecycle vocabulary** that `fix_mode` aligns with:
  `scripts/peppar_fix/states.py` (post-rename Commits (a)–(d)).
- **Bias-source matters** rationale:
  `project_2x2_ssr_isolation_20260425` (engine's 1.22 m
  CNES-vs-WHU gap on identical input).
