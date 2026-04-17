# state/timestampers/ — PPS timestampers

Anything that turns a PPS edge into a timestamp: TAPR TICCs,
EXTTS channels on PHCs, future TDC hardware.

Each file is state for one timestamper, keyed by:

- **TICC**: `ticc-<arduino-serial>` (each TICC has a unique USB
  serial burnt into its Arduino Mega).
- **EXTTS channel**: `extts-<phc-mac>-ch<channel>` (per-channel,
  per-PHC; different channels on the same PHC have different
  noise characteristics).

Contents of each `<unique_id>.json`:

- **Measurement noise**: `resolution_ns`/`resolution_ps`,
  `single_shot_noise_ns`, `measurement_noise_ns` — the Kalman
  filter's R matrix should come from these, not hard-coded magic
  numbers.
- **Reference oscillator (RO) state** (TICCs only — EXTTS uses
  the PHC's own crystal): tracks the frequency of the oscillator
  driving the TICC's 10 MHz reference input.  Includes
  `last_known_offset_ppb`, `last_known_freq_ppb`, and
  characterization of drift (nominal offset, temperature
  sensitivity, ageing).  The RO parallels the DO: we can't steer
  the RO, but we expect and assert long-term stability by
  comparing its frequency against GPS time via the F9T PPS train.
- `updated`, `method` — provenance.

See `docs/state-persistence-design.md` for schema and
`docs/future-work.md` for the RO-tracking design.
