# state/phcs/ — PTP Hardware Clocks

One file per PTP hardware clock, keyed by the underlying NIC's MAC
address (for i226, E810, etc.).

### PHC and DO are separable

A PHC is the Linux kernel API surface (`/dev/ptp*`, `adjfine`,
`clock_settime`, EXTTS, PEROUT).  It is always backed by some
physical oscillator, which we call the DO (Disciplined Oscillator).
Every PHC has a DO — a crystal ticking underneath it — but a DO
can exist without a PHC (OCXO + DAC, ClockMatrix-driven OCXO, etc.).
That's why DOs and PHCs live in separate state directories with a
cross-reference: the PHC record points to its DO by `unique_id`,
but the DO record is primary — it stores frequency offset,
characterization, and everything else that depends on the physical
crystal rather than the kernel interface.

Contents of each `<unique_id>.json`:

- **Identity**: `unique_id` (MAC), `driver` (`igc`, `ice`, etc.),
  `clock_name`, pin/channel counts.
- **Capabilities**: `max_adjustment_ppb`, whether the PHC supports
  EXTTS/PEROUT on which pins.
- **Bootstrap hints**: last-known good configuration, any
  platform-specific quirks observed (e.g., the i226 PEROUT
  500ms-phase issue).

See `docs/state-persistence-design.md`.
