# Fleet Self-Survey Diagnostic

A standalone diagnostic tool that takes the L5 receiver fleet out of
fixed-position mode, lets each receiver run its own survey-in, and
reports a consensus position with NIST-derived error bars.

Purpose: earn an **independent** position fix to anchor the pre-WL
Foundation gate (Gate #2 in `docs/pre-wl-foundation.md`).  Today,
the only position we know is the one the engine's PPP filter
computes — if the fleet agrees on a wrong answer (as observed
2026-04-24), we have no way to tell.  F9T self-survey uses the
receiver's own NAV-PVT SPP pipeline, entirely decoupled from the
engine's PPP/SSR path.

## Why this is independent

The F9T's survey-in mode:

- Runs SPP (single-point position) solutions on receiver-side
  firmware every second
- Averages those SPP fixes until convergence target met
- Has NO access to our PPP filter, our SSR corrections, our
  phase-bias tables, or our IF combination

Three F9Ts on the same splitter running independent surveys, then
consensus via median, gives us *one independent estimate per
splitter-connected receiver*.  Agreement tightens the bound;
disagreement is diagnostic of per-receiver bias.

Three paired-antenna F9T surveys give us one independent estimate.
Adding the Leica GRX 1200 (`antenna-calibration-plan.md`) and/or a
local-NTRIP-RTK solution gives us a second.  Two independent
estimates is the "one or two" bar Bob set on 2026-04-24.

## NIST error bounds

From Montare et al. 2024 (NIST Tech Note 3280), 14 independent
24-h cold-start F9T surveys:

| duration | vertical accuracy | horizontal accuracy |
|---       |---                |---                  |
| 6 h      | 2 m               | ~50 cm              |
| 18 h     | 1 m               | ~20 cm              |
| 24 h     | ~50 cm final floor| ~15 cm final floor  |

These set Gate #2's threshold for the fleet-survey anchor.  Once a
24-h survey has run on all three paired-antenna F9Ts and their
consensus is taken, the anchor confidence is ~50 cm vertical /
~15 cm horizontal (before any additional consensus tightening
from three independent receivers).

Averaging three independent surveys reduces the bound by √3 in
the limit of uncorrelated errors, to ~30 cm vertical / ~10 cm
horizontal.  Correlated errors (same antenna, same iono, same
multipath environment) mean we shouldn't expect the full √3
reduction; assume ~20-30% tightening in practice.

## Implementation sketch

### Procedure (lab-side)

1. **Stop the engine on all three paired-antenna hosts** (TimeHat,
   clkPoC3, MadHat).  The engine holds the F9T's serial port
   exclusively; survey-in tool needs it.

2. **Back up current TMODE3** (if any) so we can restore.  Current
   PePPAR-Fix runs the F9T in flexible mode, not fixed-position
   TMODE3 — but verify with UBX-CFG-VALGET before clobbering.

3. **Send UBX-CFG-TMODE3 survey-in** on each host in parallel:
   - Mode: 1 (survey-in)
   - Minimum observation time: 86400 s (24 h)
   - 3D accuracy target: 0.5 m (receiver stops early when met)

4. **Poll NAV-SVIN every minute per host**.  The message reports:
   - Position (ECEF + WGS84 LLA)
   - 3D accuracy achieved
   - Observation time
   - Completion flag

5. **Collect until all three complete OR 24 h elapsed**, whichever
   first.  If any host hits the 0.5 m target before 24 h, keep
   surveying — we want the tighter final bound.

6. **Compute consensus**: median ECEF across the three survey
   results.  Bound: max of per-host `sigma_3d`.

7. **Write `state/independent_anchor.json`**:
   ```json
   {
     "ecef_m": [x, y, z],
     "sigma_m": {"3d": 0.4, "horizontal": 0.15, "vertical": 0.35},
     "source": "fleet_survey",
     "captured_utc": "2026-04-25T12:00:00Z",
     "per_host": {
       "TimeHat":   {"ecef_m": [...], "sigma_3d_m": 0.45, ...},
       "clkPoC3":   {"ecef_m": [...], "sigma_3d_m": 0.40, ...},
       "MadHat":    {"ecef_m": [...], "sigma_3d_m": 0.42, ...}
     }
   }
   ```

8. **Restore TMODE3 flexible mode** on all three hosts.  Restart
   engines.

### Script shape

`scripts/diag_fleet_survey.py` (to write):

- Argparse: `--hosts TimeHat,clkPoC3,MadHat.local`, `--duration-h
  24`, `--target-sigma 0.5`, `--anchor-out state/independent_anchor.json`.
- SSH fan-out: each host runs a thin worker that sends
  UBX-CFG-TMODE3 and polls NAV-SVIN.  Report progress every minute
  via SSH stdout.
- Main process tracks progress, computes consensus when all done.
- Safety: re-enable the receiver's default mode on exit (trap
  SIGINT/SIGTERM so an interrupted survey doesn't leave the fleet
  in survey-in mode).

### UBX message references

- `UBX-CFG-TMODE3` (0x06 0x71): configure time mode.
  Mode field bits: 0=disabled, 1=survey-in, 2=fixed.
  See u-blox F9T Integration Manual section 3.1.
- `UBX-NAV-SVIN` (0x01 0x3B): survey-in status.  Polled with
  `UBX-POLL-NAV-SVIN`.

Existing `scripts/ubx.py` and `scripts/peppar_fix/receiver.py`
have UBX codec + I/O machinery we can reuse.

## Budget

One continuous 24-h window per survey.  The lab is in A/B
diagnostic mode today (2026-04-24); the earliest a clean 24 h is
available depends on when we want to pause other experiments.

Running the survey *in parallel* to the engine isn't an option —
F9T firmware can be in exactly one TMODE3 at a time, and the
engine expects the receiver to not be in survey-in.

## Open questions

- **Should survey-in use all constellations or GPS-only?**  Montare
  2024 used GPS+GLO+GAL+BDS.  Using the same gives us direct
  applicability of their error bounds.
- **Cold vs warm start.**  NIST's characterisation was cold-start
  (receiver freshly booted, no prior state).  Our receivers have
  been running continuously; TMODE3 survey-in uses current
  observations so "warm" doesn't carry meaningfully.  Expect
  NIST bounds to apply.
- **What if the three per-host surveys disagree by > NIST bound?**
  That's itself diagnostic — says the receivers have material
  per-unit biases.  Handle by reporting the scatter and flagging
  Gate #2 as inconclusive (don't write anchor file).
- **When do we re-survey?**  Anchor file's `captured_utc` tells us
  its age.  Propose: re-survey every ~90 days, or whenever the
  antenna is physically disturbed.

## Cross-references

- `docs/pre-wl-foundation.md` — the gate this anchor feeds.
- `docs/position-convergence.md` — survey-in convergence physics +
  NIST reference (Montare 2024 Tech Note 3280).
- `docs/antenna-calibration-plan.md` — parallel work earning a
  GRX 1200 independent anchor.
