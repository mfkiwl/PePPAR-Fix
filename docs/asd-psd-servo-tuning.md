# ASD/PSD Analysis for Servo Tuning

## Why measure noise spectra

The optimal loop bandwidth of a PI servo is **the frequency where the
input noise PSD equals the Disciplined Oscillator's noise PSD**.
Below that crossover, the input is more truthful than the DO and the
loop should follow it.  Above the crossover, the DO is more truthful
than the input and the loop should reject input noise rather than
chase it.

Without measured PSDs, you have to guess.  With them, you can pick
loop gains directly from the data.

The shape of a PSD also tells you what kind of noise dominates at
each frequency:

| Slope (log-log) | Noise type | Source |
|---|---|---|
| 0 | white phase | pure measurement / quantization |
| -1 | flicker phase | mixed measurement / electronics |
| -2 | white FM | random walk in phase, oscillator drift |
| -3 | flicker FM | real oscillator at long tau |
| -4 | random walk FM | rare except in poor oscillators |

A signal with slope ≈ 0 is "as good as it gets at this frequency" —
no temporal correlation, just quantization noise.  A signal with
slope ≤ -2 is dominated by oscillator drift and the loop has to
either track it (high bandwidth) or accept the long-tau wander.

## Tools

- **`tools/plot_psd.py`** — computes Welch PSDs of every error source
  in a servo CSV (`pps_error_ns`, `qerr_ns`, `dt_rx_ns`,
  `carrier_error_ns`, `ticc_diff_ns`, `adjfine_ppb`).  Reports
  per-source RMS, ASD at 0.1 Hz and 0.01 Hz, and the dominant
  log-log slope.  Generates a stacked HTML plot.

```sh
python3 tools/plot_psd.py --label "run1" run1_servo.csv \
                          --label "run2" run2_servo.csv \
                          -o psd.html
```

## Lessons from the 2026-04-07 overnight runs

Two TimeHat runs with the i226 in TICC-driven mode: 2 hours and
5h20m.  Both used the same servo gains from the i226 profile.

### Source noise characteristics

| Source | σ | ASD@0.1Hz | ASD@0.01Hz | slope | noise type |
|---|---|---|---|---|---|
| **dt_rx (PPP)** | 2-9 ns | 0.1 ns/√Hz | 4-7 ns/√Hz | -2.6 to -3.0 | white/flicker FM |
| **qerr (TIM-TP)** | 2.25 ns | 3-6 ns/√Hz | 1.5-5 ns/√Hz | ≈0 | white phase |
| **Carrier** | 47 ns | 45-50 ns/√Hz | 90-107 ns/√Hz | -0.7 | flicker phase |
| **TICC diff** | 380-420 ns | 75-79 ns/√Hz | 1500-1700 ns/√Hz | -2.7 | white FM |
| **adjfine** | 46 ppb | 45 ppb/√Hz | 90-110 ppb/√Hz | -0.7 | (mirrors Carrier) |

### Findings

1. **qErr is genuinely white phase noise** (slope ≈ 0).  RMS 2.25 ns
   matches the F9T's published baseline.  TIM-TP correctly removes
   the 8 ns sawtooth, leaving only quantization residual with no
   temporal correlation.

2. **dt_rx has the cleanest high-frequency floor by far.**  At 0.1 Hz,
   dt_rx is **30× cleaner than qErr** (0.18 vs 5.8 ns/√Hz).  PPP
   carrier-phase processing massively suppresses high-frequency noise
   through dual-frequency observations.

3. **dt_rx degrades at low frequencies** with slope -2.6 to -3.0.
   That's the F9T receiver TCXO's random walk dominating.  The PPP
   filter integrates it naturally — clean at high frequencies, noisy
   at long timescales.

4. **The crossover between dt_rx and qErr is around 0.03 Hz** (33-second
   timescale).  Above 0.03 Hz, dt_rx wins.  Below, qErr wins.  An
   optimal hybrid tracks dt_rx for short tau and qErr for long tau —
   the inverse of the IIR complementary filter we tried earlier.

5. **The TICC differential is dominated by random walk** (slope -2.7).
   That's the *closed-loop* residual: the disciplined PHC's wander
   relative to F9T PPS.  It's not the open-loop DO noise floor — to
   measure that we need a freerun characterization.

6. **Two runs agree very closely.** At all frequencies and on all
   sources, run1 and run2 produce nearly identical PSDs.  The small
   differences track statistical scatter from finite averaging.
   The noise structure is reproducible.

### TDEV/ADEV analysis (from same runs)

See `plots/overnight_tdev_2026-04-07.html`.

| τ (s) | F9T PPS | F9T+qErr | Disciplined PEROUT |
|---|---|---|---|
| 1 | 2.0 ns | **0.17 ns** | 16-17 ns |
| 4 | 1.2 ns | 0.09 ns | 61 ns |
| 64 | 0.4 ns | 0.34 ns | 300 ns (peak) |
| 1024 | 1.0-1.8 ns | 1.0-1.8 ns | 19 ns |

The disciplined PEROUT is much *worse* than the raw F9T PPS at all
τ < 1000 s.  The PEROUT TDEV peaks at τ ≈ 64 s — that's the loop
bandwidth, where the servo amplifies measurement noise instead of
rejecting it.  This is the "pick the wrong loop bandwidth and the
output gets worse than the reference" failure mode that motivates
the PSD-based tuning approach.

## Update 2026-04-07 afternoon: PEROUT fix unmasks Carrier performance

A 30-minute Carrier-driven servo run on TimeHat with the PEROUT
duty-cycle fix (commit `01a401c`) showed dramatically better numbers
than the TICC-driven overnight runs:

| Metric | TICC-driven (overnight) | Carrier-driven (PEROUT fixed) | Improvement |
|---|---|---|---|
| adjfine σ | 46 ppb | **1.8 ppb** | 25× |
| TICC diff σ | 380 ns | **113 ns** | 3.4× |
| adjfine ASD@0.1Hz | 50 ppb/√Hz | **0.044 ppb/√Hz** | ~1000× |
| Carrier vs PPS RMS | (not measured) | **2.4 ns** | — |

The Carrier source is now tracking PPS truth within 2.4 ns RMS with
essentially zero drift (-0.25 ns per 1000 epochs).  The 32 ns mean
offset is the static phase anchor.  The ~100 ns σ on the closed-loop
output is the actual TimeHat (i226 + PHC servo) performance bound.

The earlier "1.5 µs Carrier bias" investigation was a red herring:
that bias was the PEROUT 500ms misalignment masquerading as a Carrier
source defect.  Once PEROUT was firing on the second boundary, the
Carrier source's anchor and drift correction were exactly correct.

### Implication for future tuning

The PSD analysis tells us the Carrier source is operating well within
its intended regime: the loop bandwidth (~0.005 Hz with current i226
gains) is below the dt_rx-vs-qErr crossover (~0.01-0.03 Hz), so the
loop is benefiting from PPP precision at every frequency it cares
about.  Pushing the bandwidth higher would only help if the actuator
quantization (1 ppb adjfine on i226) allowed it.

For the moonshot — better than 100 ns RMS on TimeHat — we'd need
higher actuator resolution (ClockMatrix FCW at 0.111 fppb) or a
better DO (OCXO).  The Carrier source itself is not the bottleneck.

## Strategy: per-host characterization via `--freerun`

Noise floors are physical properties of the oscillator and electronics.
They change very slowly (temperature drift, aging) and don't need to
be re-measured every bootstrap.

**Open question to investigate**: temperature is expected to shift
crystal frequency offsets but probably not noise characteristics.
Lab measurements over a temperature range will confirm or refute.

### Plan

1. **One-time per-host characterization.**  When the orchestration
   wrapper detects no DO characterization file for the current host,
   run a 30-minute freerun before the first normal bootstrap.  Pass
   the resulting servo CSV through `plot_psd.py` and save a
   **characterization file** with summary stats and a coarsely-sampled
   PSD curve (enough points to identify crossover frequencies, not
   enough to plot a perfectly smooth curve).

2. **Subsequent runs read the file.**  The engine logs the DO noise
   floor and the active source's expected loop bandwidth at startup,
   so the operator can see what the servo is up against.  No
   automatic retuning yet — just inform.

3. **Manual refresh.**  Run `peppar-fix --freerun` explicitly to
   refresh the file (e.g., after hardware change, or to test whether
   noise has drifted).  Each freerun overwrites the file.

4. **Validity assumption.**  The file is valid forever unless deleted.
   If we observe staleness (noise PSD shifts measurably between two
   freeruns), we'll add an expiration policy.  Otherwise stay simple.

### Characterization file design

Path: `data/do_characterization.json` (per-host, not committed).

Structure (proposed):

```json
{
  "host": "TimeHat",
  "phc": "/dev/ptp0",
  "do_label": "i226 TCXO",
  "captured": "2026-04-07T12:34:56Z",
  "duration_s": 1800,
  "n_samples": 1800,

  "sources": {
    "dt_rx": {
      "rms_ns": 2.0,
      "asd_at_0.1Hz_ns_per_rthz": 0.18,
      "asd_at_0.01Hz_ns_per_rthz": 6.5,
      "slope": -2.8,
      "noise_type": "flicker_FM",
      "psd_curve": [
        [0.001, 50.0],
        [0.003, 25.0],
        [0.01, 6.5],
        [0.03, 1.5],
        [0.1, 0.18],
        [0.3, 0.10],
        [0.5, 0.09]
      ]
    },
    "qerr": { ... },
    "pps_error_ns": { ... }
  },

  "crossovers": {
    "dt_rx_vs_qerr_hz": 0.03,
    "dt_rx_vs_pps_hz": 0.05
  },

  "recommended_loop_bw_hz": {
    "PPS Phase": 0.005,
    "PPS+qErr Phase": 0.02,
    "PPP Carrier Phase": 0.05
  }
}
```

The `psd_curve` is sparse (~10-20 points covering 0.001 Hz to 0.5 Hz)
— enough to identify crossover frequencies and slopes without storing
the full Welch output.  Summary stats are precomputed for fast lookup.

The `recommended_loop_bw_hz` field is the interesting payload: for
each candidate servo input, the recommended loop bandwidth is where
that input's noise PSD crosses the DO's noise PSD.  At first this is
informational — eventually we'd use it to auto-tune `track_kp` and
`track_ki` per-source.

### Wrapper integration

```bash
# In peppar-fix orchestration script, before normal bootstrap:
if [[ ! -f "$DATA_DIR/do_characterization.json" ]]; then
    echo "First-time DO characterization (30 min freerun)..."
    "$PYTHON_BIN" "$SCRIPT_DIR/peppar_fix_engine.py" \
        $(build_servo_args) --freerun --duration 1800 \
        --servo-log "$DATA_DIR/freerun_char.csv"
    "$PYTHON_BIN" "$SCRIPT_DIR/build_do_characterization.py" \
        --input "$DATA_DIR/freerun_char.csv" \
        --output "$DATA_DIR/do_characterization.json"
fi
```

A new tool `tools/build_do_characterization.py` (or similar) reads
the freerun servo CSV, computes per-source PSDs via the same code as
`plot_psd.py`, and writes the JSON file.

The engine reads `do_characterization.json` at startup (if present)
and logs:

```
DO characterization: i226 TCXO @ TimeHat (captured 2026-04-07)
  dt_rx noise floor: 0.18 ns/√Hz @ 0.1 Hz, slope -2.8
  Recommended loop BW for active source (PPP Carrier Phase): 0.05 Hz
  Current servo gains: kp=0.03 ki=0.001 → effective BW ~0.03 Hz
  Suggestion: increase kp to ~0.05 for optimal noise rejection
```

This is "inform" mode — no automatic action, just showing the user
what the data implies.  Once we trust it, we can promote to
auto-tuning.
