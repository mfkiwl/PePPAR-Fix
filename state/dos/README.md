# state/dos/ — Disciplined Oscillators

**Not the DOS operating system.**  "dos" is the plural of DO —
the **Disciplined Oscillator**, the crystal PePPAR Fix steers
(an i226's TCXO, an OCXO driven via DAC, a ClockMatrix-driven
OCXO, etc.).

### DO and PHC are separable

A DO is a piece of physical hardware (a steerable oscillator).
A PHC is a Linux kernel timekeeping interface (`/dev/ptp*`).  Every
PHC has a DO at its core — some crystal is ticking to make the PHC
advance — but a DO can exist without any PHC (OCXO + DAC, ClockMatrix
driven via I²C, etc.).  This directory is keyed on the DO, whether
or not there happens to be a PHC associated with it.  When both
exist, the PHC identity lives under `state/phcs/` with a
cross-reference to its DO.

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
