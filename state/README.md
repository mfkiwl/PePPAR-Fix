# state/

Runtime state that persists across engine invocations.  Not to be
confused with `data/` (experiment artifacts) or `config/` (static
configuration).

Layout — each subdirectory is keyed by a *unique identifier of the
physical hardware*, not by host.  A host can have more than one of
each kind, and a piece of hardware can move between hosts; storing
state per-hardware (rather than per-host) makes both cases work:

| Directory | What it holds |
|---|---|
| `dos/` | Disciplined Oscillators — one per crystal being steered |
| `receivers/` | GNSS receivers — one per physical unit (by SEC-UNIQID on u-blox) |
| `timestampers/` | PPS timestampers — TICCs (by Arduino serial), EXTTS channels (by PHC MAC + channel) |
| `phcs/` | PTP Hardware Clocks — one per PHC (by underlying NIC MAC) |

See `docs/state-persistence-design.md` for the full entity model and
schema for each file type.

At runtime each subdirectory holds `<unique_id>.json` files.  The
`.gitignore` blocks those from being checked in — only the `README.md`
files in each subdirectory are tracked, so lab hosts get directory
descriptions automatically on `git pull`.
