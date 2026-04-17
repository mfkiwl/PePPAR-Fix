# state/phcs/ — PTP Hardware Clocks

One file per PTP hardware clock, keyed by the underlying NIC's MAC
address (for i226, E810, etc.).  The PHC is distinct from the DO:
the PHC is the Linux kernel API surface (`/dev/ptp*`, `adjfine`,
`clock_settime`, EXTTS, PEROUT) used to steer the DO crystal.

Contents of each `<unique_id>.json`:

- **Identity**: `unique_id` (MAC), `driver` (`igc`, `ice`, etc.),
  `clock_name`, pin/channel counts.
- **Capabilities**: `max_adjustment_ppb`, whether the PHC supports
  EXTTS/PEROUT on which pins.
- **Bootstrap hints**: last-known good configuration, any
  platform-specific quirks observed (e.g., the i226 PEROUT
  500ms-phase issue).

See `docs/state-persistence-design.md`.
