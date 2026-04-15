# DOFreqEst Process Model Sign Chain

**Date**: 2026-04-14
**Commit**: 779a412 (fix: correct DOFreqEst process model sign)

## The bug

The EKF process model had the wrong sign on F[2,3] and B[2], causing
the prediction step to move in the opposite direction from reality.
The filter still converged because the measurement update corrected
for the wrong prediction each epoch, but performance was suboptimal.

## Sign chain trace

### Physical conventions

- `diff_ns` = phc_event − ref_event = (DO PPS time) − (GNSS PPS time)
  on the TICC's internal timescale.
- When the DO is slow (behind GPS), its PPS fires **later** than
  GNSS PPS → `diff_ns > 0`.

### Engine (peppar_fix_engine.py line 3222)

```
pps_err_ticc_ns = -(ticc_measurement.diff_ns - args.ticc_target_ns)
```

After auto-target removes the initial cable-delay offset:
- DO falls further behind → `diff_ns` increases →
  `pps_err_ticc_ns` **decreases** (goes negative).
- **Negative `pps_err_ticc_ns` = DO is late.**

The previous comment "Positive = DO is late" was wrong and has been
corrected.

### DOFreqEst state x[2] = φ_do ("lateness")

The measurement model is `h(x) = -x[2] - qerr(x[0])`.

The seed (first epoch) sets `x[2] = -offset_ns - qerr(x[0])`:
- When `offset_ns < 0` (DO late): `x[2] > 0`.
- **Positive x[2] = DO is late.** This is the "lateness" convention.

The measurement model is consistent:
- `z = offset_ns < 0` (late), `h(x) = -x[2] < 0` (late). Match. ✓

### Process model — where the bug was

The state x[2] = φ_do represents "lateness" = GPS_phase − DO_phase.
The physical rate of change is:

```
d(lateness)/dt = -(crystal_drift + adjfine)
```

Because positive adjfine speeds up the DO, **reducing** lateness.
And positive crystal drift (fast crystal) also reduces lateness.

The process model encodes this as:

```
x[2]' = x[2] + F[2,3] · x[3] · dt + B[2] · u · dt
```

Where x[3] = crystal drift (negative for slow crystal) and
u = adjfine applied (via the engine's double negation: engine
passes `-servo.update()` to the actuator, and `_last_u = u`
where `servo.update()` returns `-u`).

**Old (buggy):** `F[2,3] = +dt`, `B[2] = +dt`

```
x[2]' = x[2] + x[3]·dt + u·dt
```

Freerun test (crystal slow 100 ppb, x[3] = -100, u = 0):
x[2]' = x[2] - 100·dt → lateness **decreases**.
But a slow crystal makes the DO fall further behind →
lateness should **increase**. ❌

**Fixed:** `F[2,3] = -dt`, `B[2] = -dt`

```
x[2]' = x[2] - x[3]·dt - u·dt
```

Same freerun test:
x[2]' = x[2] - (-100)·dt = x[2] + 100·dt → lateness
**increases**. ✓

Compensated test (x[3] = -100, u = +100):
x[2]' = x[2] + 100 - 100 = x[2] → stable. ✓

Pull-in test (x[2] = +10 late, x[3] = -100, u = +100.5):
x[2]' = 10 + 100 - 100.5 = 9.5 → catching up. ✓

### LQR gain L[2] sign flip

The LQR computes `u = -(L @ x)`.  With the corrected process model,
positive x[2] (late) must produce **more** u (more adjfine) to
reduce lateness.  The chain:

```
x[2] > 0  →  L[2]·x[2] contributes to (L @ x)
          →  u = -(L @ x) is reduced if L[2] > 0
          →  less adjfine → DO stays late  ← wrong!
```

Fix: `L[2] = -0.05` (was `+0.05`).  Now:

```
x[2] = +10, x[3] = -100
u = -((-0.05)·10 + 1.0·(-100)) = -(-0.5 - 100) = +100.5
adjfine = +100.5 → more than drift compensation → catches up ✓
```

### Engine interface (unchanged)

The double negation between DOFreqEst and the engine is unchanged:

```
servo.update() returns adjfine = -u
engine: adjfine_ppb = -servo.update() = u
actuator.adjust_frequency_ppb(u)
DOFreqEst._last_u = u
```

### Why clkPoC3 worked despite the bug

The EKF measurement update is powerful enough to override the wrong
prediction each epoch.  With 1 Hz TICC measurements and ~0.2 ns
measurement noise, the Kalman gain trusts the measurement far more
than the prediction.  The filter converges but:

1. The state estimates are noisier than they should be (the filter
   partially trusts its wrong prediction).
2. The P matrix doesn't reflect the true uncertainty structure
   (cross-correlations between x[2] and x[3] have wrong sign).
3. The LQR gain, computed from these wrong-sign states, produces
   slightly wrong corrections that the next measurement must fix.

The 300-epoch clkPoC3 run (TDEV 1.15 ns) succeeded because the
measurement update dominated.  Longer runs or faster servos would
show worse degradation.

## Summary of changes

Three sign flips in `scripts/peppar_fix/do_freq_est.py`:

| Item | Old | New | Why |
|------|-----|-----|-----|
| `F[2,3]` | `+dt` | `-dt` | Crystal drift reduces lateness |
| `B[2]` | `+dt` | `-dt` | Adjfine reduces lateness |
| `L[2]` | `+0.05` | `-0.05` | Late → more correction |

Plus the dynamic `dt` update in `update()` and the engine comment
at line 3206 corrected from "Positive = DO is late" to the actual
convention.

## Validation (2026-04-14)

10-minute parallel runs on TimeHat, MadHat, and clkPoC3.

**TimeHat** (PHC + TICC #1 + DOFreqEst): the key test host.
- DOFreqEst converged `adj` from ~148 ppb to ~156 ppb over ~250
  epochs, matching the bootstrap crystal drift of 156.2 ppb.
- Once converged, `adj` remained stable at 155–157 ppb with no
  oscillation or divergence (observed through epoch 520).
- The "Carrier: err=" in the log is the PPP dt_rx (rx TCXO phase),
  NOT the DO phase.  It drifted ~90 ns during the first 250 epochs
  (while adj was converging) then stabilized once adj reached the
  correct value — confirming the DO is tracking at the right rate.
- Scheduler widened interval from 1 to 2–3, indicating convergence.

**MadHat** (PHC + TICC #2): did not reach servo — Phase 1 position
re-bootstrap ran for the full 600s without completing.  Pre-existing
issue: stale position file triggered LS validation mismatch (112m).

**clkPoC3** (DAC + TICC #3, TICC-drive): reached servo briefly but
crashed after 60 epochs due to pre-existing dt_rx bootstrap issue.
The FixedPosFilter returned dt_rx=0.0 ±3.3s (didn't converge —
no SSR mount in config, broadcast-only).  DOFreqEst was seeded with
wrong rx TCXO state → outlier detection fired → servo exit.

### Pre-existing issues exposed by this test

1. **Stale position files** (MadHat, clkPoC3): legacy
   `data/position.json` written from `known_pos` is rejected by
   LS validation.  Receiver state in `state/receivers/` has good
   positions but the validation path still triggers re-bootstrap.
2. **clkPoC3 missing SSR mount**: config has `eph_mount` but no
   `ssr_mount`.  Without SSR, the 10-epoch dt_rx bootstrap doesn't
   converge.
3. **TimeHat missing `ticc_port`**: the host config didn't specify
   `/dev/ticc1` — had to add via CLI for this test.
