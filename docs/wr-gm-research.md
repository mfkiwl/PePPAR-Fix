# White Rabbit Grandmaster — Architecture Review and peppar-fix Integration

Research from 2026-03-31 conversation reviewing the WR softpll codebase
and evaluating how peppar-fix could drive a WR GM.

Source: https://gitlab.com/ohwr/project/wrpc-sw/-/tree/master/softpll

## WR SoftPLL Architecture (GM mode)

The WR SoftPLL is a three-layer DDMTD PLL running on an LM32 soft core
inside the WRPC FPGA:

### Layer 1: Helper PLL (`spll_helper.c`)

Locks the DMTD helper oscillator to the reference with a slight
frequency offset (2^-HPLL_N = 2^-14).  This creates a beat note that
makes high-frequency phase comparison measurable at a low rate (~15 kHz).
Simple PI loop, bias set to max to ensure positive offset lock.
Default gains: kp=-150, ki=-2.

### Layer 2: Main PLL (`spll_main.c`)

Two-stage servo:
1. **Frequency lock**: compares tag-to-tag deltas (`dref_dt` vs
   `dout_dt`) with 20x gain boost.
2. **Phase lock**: accumulated phase error
   `(adder_ref + tag_ref) - (adder_out + tag_out)`, with wraparound
   masking at 2^HPLL_N.

The PI output drives a DAC that tunes the local oscillator.  Phase
shifting is done by incrementing/decrementing `adder_ref` by 1 count
per sample (slow ramp).  One DDMTD count ≈ 16 ps.

### Layer 3: External / Grandmaster (`spll_external.c`)

For GM mode only.  Aligns local PPS output to an external PPS input
using a two-stage process:

1. **Coarse sync**: `PPSG_ESCR_SYNC` hardware resets the PPS generator's
   nanosecond counter to match the next input PPS edge.  Gets within
   ~ns.
2. **Fine alignment**: `external_align_fsm()` measures the offset
   between input and output PPS at 100 Hz using an aligner that counts
   10 MHz ticks since the PPS.  Walks the phase in 100 ps steps
   (`mpll_set_phase_shift`) until aligned.  Adds platform-specific PPS
   latency compensation.

## How the GM Uses Its Inputs

The GM takes two inputs: a 10 MHz frequency reference and a 1 PPS
epoch marker.

- **10 MHz**: The helper and main PLLs lock the local oscillator to it.
  This is the continuous frequency reference — the GM distributes time
  derived from this oscillator to all WR slaves.
- **PPS**: Used only during the alignment phase to establish the epoch.
  The aligner measures input-to-output PPS offset (modulo 10 ms,
  100 ns resolution from 10 MHz tick counting), and the FSM walks
  until aligned.

**After alignment, the PPS input is ignored.**  The `ALIGN_STATE_LOCKED`
handler only checks for loss-of-lock and static latency recalibration.
There is no continuous PPS tracking loop.

This means the alignment phase picks a fixed phase relationship between
the 10 MHz-locked oscillator and the PPS epoch, based on whatever PPS
edges happen to arrive during the walk.  Any quantization error (qErr)
on those edges is baked in as a static bias for the duration of the run.

## Comparison with peppar-fix

| Aspect | WR GM | peppar-fix |
|--------|-------|------------|
| PPS usage | One-shot alignment, then ignored | Continuous discipline, every epoch |
| Phase measurement | DDMTD tags, ~16 ps resolution | EXTTS, 8 ns resolution (i226) |
| qErr correction | Not applied | 2–3.7x variance reduction |
| Frequency reference | External 10 MHz | PHC oscillator (TCXO or OCXO) |
| Steering | DAC to VCO | adjfine to PHC |
| Holdover | Trusts 10 MHz PLL indefinitely | Preserves last adjfine |

The WR GM's `external_align_fsm` is architecturally the weakest link in
the WR timing chain: one-shot alignment to a bare PPS edge with no
averaging and no sub-cycle correction.

## Where qErr Could Be Injected in WR

The phase error fed to the PI controller in `mpll_update()`:

```c
err = s->adder_ref + s->tag_ref - s->adder_out - s->tag_out;
```

A qErr correction subtracts the predicted sawtooth error before
`pi_update()`.  However, this only matters if the GM continuously
tracks the PPS — which it currently does not.  For the existing one-shot
alignment, qErr would improve the initial epoch accuracy by a few ns
but nothing more.

## PEROUT at 10 MHz

Both i226 and E810 support 10 MHz PEROUT:

- **i226 (igc)**: Frequency mode via FREQOUT register.  Min half-period
  8 ns, max frequency ~62.5 MHz.  10 MHz (50 ns half-period) is well
  within range.
- **E810 (ice)**: Intel documents 10 MHz output as a standard SMA
  configuration.  Period written to GLTSYN_CLKO, min ~3 ns.

Signal quality (jitter, rise time) at 10 MHz from NIC GPIO drivers is
uncharacterized and would need measurement.

## Integration Paths

### Path A: PHC PEROUT as WR GM Reference

The i226 or E810 generates a disciplined 10 MHz + 1 PPS from the
peppar-fix-steered PHC via PEROUT.  The WR GM hardware receives these
as its external reference inputs and runs its softpll unmodified.

- **Pro**: No WR firmware changes.  The GM just sees a better reference.
- **Con**: The 10 MHz output edges are quantized to the PHC tick grid
  (8 ns for i226 at 125 MHz).  This is coarse compared to the DDMTD's
  16 ps resolution.  The WR PLLs will clean this up to some degree,
  but the input jitter may limit downstream performance.
- **Con**: Uncharacterized PEROUT signal quality at 10 MHz.

### Path B: OCXO + Renesas ClockMatrix (Software Steering)

A stable oscillator (OCXO) provides the frequency reference.  The
Renesas 8A34002 ClockMatrix generates the DDMTD main and helper clocks.
peppar-fix steers the ClockMatrix via I2C in `write_phase_set` mode
(pll_mode=3), applying PPS+PPP-AR corrections with qErr compensation.

- **Pro**: The OCXO gives excellent short-term stability and holdover.
  The ClockMatrix TDC provides 0.39 ps resolution (DPLL0_FILTER_STATUS)
  — far better than EXTTS.  No PHC quantization in the output path.
- **Pro**: The otcBob1/ptBoat hardware already has ClockMatrix + OCXO +
  F9T, with I2C access working (`renesas_init.py`, `renesas_tdc.py`).
- **Pro**: peppar-fix already solves the hard problem (carrier-phase PPP
  with qErr correction).  This path reuses that work directly.
- **Con**: Requires WR FPGA integration — the ClockMatrix outputs must
  feed the WR timing core's reference and DDMTD helper clock inputs.
- **Con**: The software steering loop runs at 1 Hz (PPS rate), while the
  WR DDMTD runs at ~15 kHz.  The OCXO stability bridges this rate gap,
  but the control bandwidth is fundamentally limited to ~0.1 Hz.

### Path B is the stronger option

Path B keeps the high-resolution phase measurement (ClockMatrix TDC)
and stable oscillator (OCXO) in the loop, avoiding the PHC tick
quantization that limits Path A.  The existing peppar-fix servo provides
continuous discipline that is strictly better than the WR GM's one-shot
PPS alignment.  Every downstream WR slave would benefit from the
improved GM reference.
