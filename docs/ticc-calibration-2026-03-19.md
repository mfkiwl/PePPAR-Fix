# TICC Calibration and USB Isolator Test — 2026-03-19

## Setup

Three TAPR TICCs in "skeptical" configuration: SMA TEE trees distribute
two PPS signals so each signal is measured by multiple TICCs independently.

```
F9T-3RD PPS ──┬── TICC #1 chB (TimeHat)
  (PPS IN)    ├── TEE ── TICC #3 chA (PiPuss)
              └── TEE ── TICC #3 chB (PiPuss)

TimeHAT PHC  ──┬── TICC #1 chA (TimeHat)
PPS OUT (SDP0) ├── TEE ── TICC #2 chA (Onocoy)
               └── TEE ── TICC #2 chB (Onocoy)
```

- PHC state: free-running (no servo)
- Heatsink: attached to TimeHAT v5 TCXO
- 10 MHz reference: Geppetto GPSDO → SV1AFN dist amp → all 3 TICCs
- Three 5-minute runs: baseline (no isolators), isolators on TimeHat
  (run 1), isolators on TimeHat (run 2)

## TICC noise floor

Same signal on both channels (TICCs #2 and #3):

| TICC | std(chA-chB) | TDEV(1s) |
|---|---|---|
| #2 (Onocoy) | 52 ps | 30 ps |
| #3 (PiPuss) | 48 ps | 28 ps |

All three TICCs are consistent. The TEE distribution adds no detectable
noise at tau=1s.

## Cross-TICC agreement

Single-channel TDEV(1s) for each PPS train, measured independently by
three TICCs:

| Signal | TICC1 | TICC2/3 (two channels) | Spread |
|---|---|---|---|
| PPS OUT (PHC) | 0.126 ns | 0.106, 0.106 ns | 19 ps |
| PPS IN (F9T) | 1.677 ns | 1.681, 1.682 ns | 5 ps |

Cross-TICC agreement is within the noise floor. The TICCs are calibrated
and trustworthy.

## USB isolator test

USB isolators placed on both the TICC #1 and F9T-3RD USB cables between
the devices and TimeHat (Raspberry Pi 5).

| Channel | Signal | Baseline | Isolators r1 | Isolators r2 |
|---|---|---|---|---|
| TICC1 chA | PPS OUT | 0.126 ns | 0.082 ns | 0.123 ns |
| TICC2 chA | PPS OUT | 0.106 ns | 0.073 ns | 0.115 ns |
| TICC1 chB | PPS IN | 1.677 ns | 0.720 ns | 1.604 ns |
| TICC3 chA | PPS IN | 1.681 ns | 0.738 ns | 1.597 ns |

Run 1 appeared to show a dramatic improvement. Run 2 did not reproduce
it. The cause is the F9T sawtooth character, not the isolators — see
analysis below.

## Sawtooth smooth/jumpy analysis

The F9T PPS sawtooth alternates between two regimes: "smooth" (monotonic
ramp through ±4 ns, low TDEV) and "jumpy" (sign-alternating, high TDEV).
The regime depends on the instantaneous frequency relationship between
the GPS second and the F9T TCXO, which drifts over minutes.

**Method**: Break the detrended phase series into 5-second groups. Count
sign flips in the first differences within each group. 0-1 flips =
smooth (ramp). 2+ flips = jumpy (alternating).

| Run | % Smooth | TDEV(1s) PPS IN |
|---|---|---|
| Baseline | 0% | 1.68 ns |
| Isolators r1 | 88% | 0.74 ns |
| Isolators r2 | 7% | 1.60 ns |

The TDEV tracks the smooth/jumpy ratio, not the isolator presence.
Run 1 happened to capture an 88%-smooth window. The PPS OUT (TCXO)
is consistently 83-90% smooth across all runs with TDEV 0.07-0.13 ns.

## Conclusions

1. **TICCs are calibrated**: all three agree to within their 30 ps
   noise floor.
2. **USB isolators**: no detectable effect at 5-minute duration. The
   dominant noise source is the F9T sawtooth character, not ground
   loops. Isolators left in place (no harm) but not required.
3. **Free-running TCXO**: TDEV(1s) = 0.1-0.13 ns (100-130 ps). This
   is the discipline floor — the PHC cannot be steered quieter than
   this at tau=1s.
4. **F9T raw PPS**: TDEV(1s) = 1.6-1.7 ns during jumpy periods, as
   low as 0.7 ns during smooth ramp periods. Average over long runs
   expected ~1.3 ns (consistent with ±4 ns uniform sawtooth theory).
5. **Proper isolator testing** would require 30+ minute runs to average
   over smooth/jumpy cycles. Given the null result at 5 minutes, the
   effect (if any) is well below the sawtooth noise.

## Sawtooth smooth/jumpy classification technique

Useful for future analysis: the smooth/jumpy ratio characterizes the
instantaneous behavior of any PPS sawtooth and explains TDEV variability
across short runs. The method:

```python
def classify_groups(detrended_phase_ns, group_size=5):
    """Classify 5-second groups as smooth (ramp) or jumpy (alternating).

    Args:
        detrended_phase_ns: array of detrended cumulative phase in ns
        group_size: samples per group (default 5, at 1 Hz PPS)

    Returns:
        (n_smooth, n_jumpy, n_total)

    A group is 'smooth' if there are 0-1 sign flips in consecutive
    first differences (monotonic or nearly so). 2+ flips = jumpy.
    """
    n = len(detrended_phase_ns)
    n_groups = n // group_size
    smooth = jumpy = 0
    for g in range(n_groups):
        chunk = detrended_phase_ns[g * group_size : (g + 1) * group_size]
        diffs = np.diff(chunk)
        signs = np.sign(diffs)
        flips = np.sum(signs[1:] != signs[:-1])
        if flips <= 1:
            smooth += 1
        else:
            jumpy += 1
    return smooth, jumpy, n_groups
```

## References

- NIST Technical Publication 3280, Montare et al. (2024) — F9T survey
  and timing evaluation
- TAPR TICC documentation: http://www.tapr.org/ticc.html
