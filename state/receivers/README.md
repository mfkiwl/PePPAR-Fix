# state/receivers/ — GNSS receivers

Each file is state for one physical GNSS receiver, keyed by its
application-layer unique ID (u-blox `SEC-UNIQID`, or a user-assigned
label if the receiver can't expose one).

Contents of each `<unique_id>.json`:

- **Identity**: `unique_id`, `unique_id_hex`, `module`, `firmware`,
  `protver`, `capabilities` (l2c, l5, glonass, etc.).
- **`last_known_position`**: ECEF + LLA from the last converged
  PPP solution.  Used as a warm-start seed on the next run so we
  skip Phase 1 bootstrap.  Each receiver carries its own antenna
  position — a host can have multiple receivers connected to
  different antennas.
- **`tcxo`**: rx TCXO frequency offset (ppb) and last-known `dt_rx`
  (ns) — used to seed DOFreqEst and the carrier-phase tracker.
- `last_known_port`, `last_seen` — diagnostic breadcrumbs.

See `docs/state-persistence-design.md` for the full schema and
the "position belongs to the receiver, not the host" rationale.
