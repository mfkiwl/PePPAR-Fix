# Timebeat ClockMatrix Integration Status — 2026-04-06

## What works

**DPLL_1 FCW steers the physical PPS output** (confirmed via TICC #2
on PiPuss, ±200000 ppb applied, ~80000 ns/s swing measured).
Gain is ~0.2 — the FCW-to-output relationship needs calibration but
the control path is proven.

**DPLL_0 is GPS-locked** (status=locked, ref=IN2/CLK2 = F9T PPS).
Generates 25 MHz for the i226 PHC and PPS output. Stays locked even
with Timebeat stopped (EEPROM configuration persists).

**adjfine servo works on otcBob1** for PHC frequency correction.
Converged to adjfine=~77665 ppb, err=±21 ns. This is the fallback
when ClockMatrix steering isn't active.

**Both physical PPS outputs** track identically — same clock tree
or same physical signal.

## What doesn't work (yet)

**Internal phase measurement**: None of the on-chip registers track
the actual post-FOD PPS output phase:
- DPLL_1 PHASE_STATUS in write_freq mode: updates but at 1/166 of
  actual (PFD feedback tap is before the FOD)
- Output TDC_0: EEPROM-frozen at 1704 ps, unresponsive to config
- Output TDC_1-3: never produce measurements
- DPLL_2 in phase_measurement mode (5): returns zeros
- DPLL_FILTER_STATUS: all zeros

**The "standalone servo success" was an illusion**: DPLL_2's PLL was
acquiring lock on its own; DPLL_3 FCW writes had no measurable effect.
The phase convergence to ±300 ps was the hardware PLL, not our servo.

## Architecture (confirmed)

```
DPLL_0 (DPLL mode, locked to CLK2/PPS)
  ├── 25 MHz → i226 PHY + PHC (adjfine steers this)
  └── 1 PPS OUT → both external connectors

DPLL_1 (synthesizer mode by default, can switch to write_freq)
  ├── FCW steers PPS OUT (gain ~0.2, confirmed by TICC)
  └── 1 PPS nominal from FOD_FREQ

DPLL_2, DPLL_3: in DPLL/PLL mode, not currently useful for peppar-fix
```

**Key insight**: DPLL_0's PLL drives the 25 MHz and both PPS outputs.
When DPLL_1 is switched to write_freq, its FCW somehow modulates the
PPS output (possibly through the shared clock tree or output mux),
but does NOT affect the PHC's 25 MHz.

## Open questions (for Timebeat or further investigation)

1. **Output routing**: Which physical output comes from which DPLL?
   Both connectors track identically — are they both from DPLL_0?
   If so, how does DPLL_1 FCW affect them?

2. **FCW gain**: Measured ~0.2, expected 1.0. Why the attenuation?
   May be a clock tree multiplication/division stage between FCW
   and the output.

3. **Output TDC configuration**: TDC_0 has an EEPROM-loaded config
   that works (DPLL3 vs DPLL0, 1704 ps). TDC_1-3 refuse to produce
   measurements regardless of configuration. May need EEPROM or
   Timing Commander to initialize them.

4. **Wiring differences between OTC and OTC Mini**: Timebeat uses
   8A34002 on both, but DPLL-to-output routing may differ. Don't
   generalize from one model to another.

5. **Can we route DPLL_1 output to a connector independently?**
   This would give us a steerable PPS output separate from DPLL_0's
   GPS-locked PPS.

## Where we're going (after PPP Carrier Phase architecture change)

The PPP Carrier Phase servo drive eliminates the need for precise
PPS edge timestamps in the steady-state loop. dt_rx from the PPP
filter provides the TCXO-to-GPS relationship directly. This means:

- **For PHC discipline** (TimeHat, any host): PPP dt_rx drives adjfine
  via PI servo. No TICC or precise TDC needed. EXTTS is fine for
  bootstrap only.

- **For ClockMatrix PPS steering** (Timebeat OTC): PPP dt_rx drives
  DPLL_1 FCW. The internal TDC limitation doesn't matter — we don't
  need a precise PPS phase measurement because PPP provides the
  frequency information directly. The TICC can verify the result
  but isn't needed in the loop.

- **Phase measurement still needed for**: bootstrap (EXTTS at 8 ns
  is sufficient) and optional verification/monitoring.

## Chip identity

Renesas 8A34002 (confirmed by Timebeat, same chip on both OTC and
OTC Mini). The 8A34xxx family shares a common register set — the
Linux kernel driver treats all variants identically.
