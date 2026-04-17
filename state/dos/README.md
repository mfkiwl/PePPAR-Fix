# state/dos/ — Disciplined Oscillators

**Not the DOS operating system.**  "dos" is the plural of DO —
the **Disciplined Oscillator**, the crystal PePPAR Fix steers
(an i226's TCXO, an OCXO driven via DAC, a ClockMatrix-driven
OCXO, etc.).

Each file in this directory is state for one physical DO, keyed
by a hardware-unique identifier (e.g., the PHC's MAC address on
i226, or a user-assigned label for hardware without a queryable
ID).

Contents of each `<unique_id>.json`:

- `last_known_freq_offset_ppb` — adjfine / DAC frequency correction
  from the last clean shutdown; used to warm-start the servo on
  the next run (trust-but-verify: a fresh PPS measurement still
  happens before the bootstrap commits).
- `characterization` — free-running noise floor (ADEV/TDEV tables),
  dominant noise-type crossover τ, recommended loop bandwidth.
  Written by `build_do_characterization.py` and read by the engine
  at startup.
- `label`, `type`, `updated` — human-readable tag, kind, timestamp.

See `docs/state-persistence-design.md` for the full schema.
