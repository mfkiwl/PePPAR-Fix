# ocxo Platform Matrix — E810 Driver Tradeoffs

## Update: 2026-04-07 afternoon — ocxo runs end-to-end!

**Root cause of the morning's failures**: the engine's EXTTS pin
programming had no sysfs fallback (only the PEROUT path did).  On
E810 with `program_pin=False`, the SDP pins were never programmed
for EXTTS at all — leaving them in whatever state they were in from
the previous boot.  EXTTS reads against an unprogrammed pin returned
either I/O errors or stale frozen values.

The fix (commit `2c76229`): apply the same try-ioctl-then-sysfs
pattern to EXTTS in both `phc_bootstrap.py` and `peppar_fix_engine.py`.
A second fix (`408f452`) replaced the hardcoded SMA1/SMA2 names with
index-based lookup, since newer ice driver versions use SDP20-SDP23
naming.

**Result**: with both fixes, peppar-fix on ocxo now bootstraps
successfully and runs the Carrier-driven servo to convergence.  10
minute run on 2026-04-07 afternoon produced:

| Metric | TimeHat (i226+TCXO) | ocxo (E810+OCXO) |
|---|---|---|
| TICC chA-chB σ (last 30s) | **3.5 ns** | 17.5 ns |
| TICC range | 15 ns | 60 ns |
| TICC constant offset | +96 ns | +206,958 ns |
| Engine pps_error_ns σ | ~100 ns | ~2 µs |

**Surprising finding**: TimeHat is *tighter* than ocxo despite the
inferior oscillator.  The cause is two-fold:

1. The e810 servo gains (kp=0.015, ki=0.001) are tuned for managing
   PHC drift, but the OCXO is much quieter than the loop assumes.
   The loop overshoots and oscillates at ~18 ns even though the
   oscillator's natural noise floor is sub-ns.

2. The E810 EXTTS appears to be much noisier than i226 EXTTS.  The
   engine sees pps_error_ns σ = 2 µs vs ~100 ns on TimeHat.  The
   servo's ability to settle is limited by its measurement quality.

**Constant +207 µs PEROUT offset**: TICC consistently shows the
disciplined PEROUT firing 206,958 ns after the F9T PPS, even though
the engine's EXTTS reports the PHC near zero error.  Either:
- The E810 PEROUT has a fixed ~207 µs hardware delay, or
- The EXTTS capture has a corresponding latency (so the engine
  thinks the PHC is on-time when it's actually 207 µs ahead).

This is a pure phase calibration issue — the disciplined output is
stable, just offset by a constant.  Could be characterized once and
compensated via `phase_step_bias_ns` (which is currently a stub
field that is parsed but not used by the engine).

## Critical caveat: the PHC is NOT on the OCXO

With the stock in-kernel ice driver, the E810 PHC free-runs on its
own internal oscillator (not the onboard OCXO).  The ZL30795 DPLL is
present and the OCXO is present, but the stock driver does not expose
DPLL control at all — so the DPLL is in whatever state the firmware
left it in, and the OCXO is not steering the PHC.  Everything we
measured above (17.5 ns σ, 207 µs offset, 2 µs EXTTS noise) is the
PHC's internal clock being steered by software `adjfine`, the same
way we steer the i226 TCXO — just with a different, noisier underlying
oscillator than the OCXO we thought we were using.

**To actually run on the OCXO** requires the Intel out-of-tree ice
driver (v2.4.5) which exposes DPLL control.  In that mode the DPLL
locks to the internal F9T PPS, the OCXO drives the DPLL output, and
the PHC follows the DPLL — no `adjfine` needed.

**What we give up to get there**: the Intel out-of-tree driver does
not implement `PTP_EXTTS_REQUEST` / `PTP_PIN_SETFUNC` on the SDP
pins — it manages SDP pins through the DPLL subsystem instead.  That
means no userspace PPS capture through the PHC at all, so the engine
cannot use EXTTS for bootstrap phase verification or for any servo
source that depends on PPS edge timestamps (PPS Phase, PPS+qErr,
PPS+PPP).  See `drivers/ice-gnss-streaming/README.md:104-110`.

Access to the onboard F9T at `/dev/gnss0` is *not* lost — both
drivers read the F9T over the same I2C path.  (With the Intel OOT
driver you additionally want our streaming patch `0001-...-delivery`
for low-latency reads, but EXTTS stays broken with it.)

The only servo sources that can run under the OOT driver are
therefore ones that don't need PHC EXTTS at all:
- **TICC-drive**: PPS edge capture happens on a TICC, not the PHC.
  Bootstrap phase verification also needs a non-EXTTS path — which
  currently does not exist in `phc_bootstrap.py`.
- **Carrier Phase**: in principle doesn't need PPS edges after
  init, but the bootstrap step still does.

A two-driver workflow (boot with in-kernel for bootstrap, switch to
OOT for run) was tried around 2026-03-29 and rejected as not
production-viable.

### Can the DPLL subsystem replace EXTTS?

Short answer: no — but it gives us useful supervisory telemetry.

Linux kernel 6.7+ exposes a `dpll` genl family (and Intel ships a
legacy sysfs view for the same data).  On `ocxo` today:

```
/sys/class/net/enp1s0f0np0/device/dpll_1_offset = -241   # picoseconds
/sys/class/net/enp1s0f0np0/device/dpll_1_state  = 4      # LOCKED_HO_ACQ
```

What's exposed: `DPLL_A_PHASE_OFFSET` / `DPLL_A_PIN_PHASE_OFFSET`
(signed ps, phase of an input pin vs. DPLL output), `DPLL_A_LOCK_STATUS`,
`DPLL_A_PIN_PHASE_ADJUST` (writable steering), and a 2024 addition
that monitors *all* PPS inputs with notifications.

Why it is not a servo input substitute for EXTTS:

1. **It's a phase scalar, not a timestamped edge event.** No way to
   align it to other event streams (TICC chB, PPP epochs, the engine's
   1 Hz schedule).
2. **It's already inside the ZL30795's loop filter.** Servoing on it
   would cascade our PI loop on top of the chip's loop with unknown
   inner-loop dynamics — and we have no access to the raw TDC.
3. **linuxptp/ts2phc agree**: they consume the dpll family only for
   clockClass / lock-state supervision in the BC's Announce, never as
   a servo error term.  Servo input remains EXTTS.

What it *is* good for, and what we should do with it:

- **Lock supervision and holdover detection** — drive PTP clockClass
  reporting from `DPLL_A_LOCK_STATUS` rather than from PPS-loss
  heuristics.
- **Telemetry** — log `dpll_1_offset` alongside TICC and `pps_error_ns`
  in the servo CSV.  When the OOT driver is loaded and the DPLL is
  actually disciplining the OCXO, this is the only number we have
  that reflects the OCXO's residual against the F9T PPS reference.
- **Holdover transitions** — notifications on lock state change let
  the engine react immediately rather than after the next 1 Hz tick.

Worth doing on the in-kernel driver too — `dpll_1_offset` is already
populated even though the DPLL isn't actively disciplining the PHC.

## On E810, the OCXO is not our DO

The natural parallel to the Timebeat OTC's Renesas 8A34002 ClockMatrix
breaks down on E810.  On the OTC we read the chip's TDC for measurement
*and* write a frequency control word (FCW) to steer the OCXO directly,
running our PI servo entirely in userspace.  On E810 we only get the
first half.

### What a "software incrementer" is

Every NIC PHC is conceptually a counter clocked from a hardware oscillator
(here, the OCXO via the ZL30632 EEC).  But the counter doesn't advance
by 1 tick per oscillator cycle — it advances by a programmable
**increment value** held in a register Intel calls `GLTSYN_INCVAL`.
If the hardware tick is 1 ns and `INCVAL = 1.0`, the PHC tracks real
time.  If we want the PHC to run 100 ppb fast, we write `INCVAL = 1
+ 1e-7`.  The oscillator still ticks at exactly the same rate; we
have only changed how much *PHC time* is added per tick.

This is the "software incrementer".  It is a rate adjustment applied
in arithmetic, layered on top of an oscillator the host did not
actually retune.  Phase is real (the PHC really does report a
different time), but the underlying frequency reference is
unchanged — and any short-term phase noise of the OCXO passes
through the incrementer untouched.

`clock_adjfine()` and `adjtimex(ADJ_FREQUENCY)` on E810 both terminate
in `ice_ptp_adjfine()` → `GLTSYN_INCVAL`.  They do not reach the
OCXO.  `ADJ_SETOFFSET` similarly pokes `GLTSYN_TIME`, jumping the
counter without touching the oscillator.

### Stock in-kernel ice driver: PHC behavior

- Underlying clock: OCXO, locked by the EEC firmware to whichever
  input pin it last latched onto (often holdover, since the F9T PPS
  may not be wired to the EEC reference selector by default).
- `adjfine`: writes `GLTSYN_INCVAL` only.  This is the only knob we
  have, and it's a *software rate overlay*, not OCXO control.
- `ADJ_SETOFFSET`: writes `GLTSYN_TIME`, instantaneous PHC step.
- EXTTS: works (with the sysfs-pin-program fallback added in 2c76229).
- DPLL: not exposed as a writable interface, but `dpll_1_offset` is
  populated read-only and reflects the EEC's TDC.
- **Net result**: from the servo's point of view the PHC behaves like
  a normal Linux PHC and PePPAR Fix can run end-to-end (and does, as
  of this afternoon).  But the OCXO is effectively frozen — we are
  servoing the *software incrementer* against GNSS, not the oscillator.
  Any moonshot goal that depends on the OCXO's short-term stability
  shining through is unreachable on this driver.

### Intel out-of-tree ice v2.4.5: PHC behavior

- Underlying clock: still the OCXO, but now the DPLL is actively
  managed via firmware-mediated reference selection and lock.  The
  OCXO frequency *can* track the chosen reference (e.g., the internal
  F9T PPS).
- `adjfine`: still writes `GLTSYN_INCVAL` (same code path).  It does
  not bypass the DPLL or steer the OCXO directly — it's still a
  software rate overlay layered on top of whatever frequency the
  EEC is producing.  Whether userspace `adjfine` makes practical
  sense in this mode is unclear: if the DPLL is locked to GNSS the
  PHC's underlying rate is already correct and `adjfine` only adds
  a software-side bias.
- `ADJ_SETOFFSET`: same as in-kernel.
- EXTTS: **broken on SDP pins** — the OOT driver routes SDP pin
  management through the DPLL subsystem and does not honor
  `PTP_EXTTS_REQUEST` / `PTP_PIN_SETFUNC`.  No PHC-referenced PPS
  edge timestamps available.
- DPLL: writable for *reference selection* (`dpll_N_ref_pin`,
  `pin_cfg`) and *phase adjust* (`DPLL_A_PIN_PHASE_ADJUST` in ps),
  but **no frequency-write attribute** in the upstream `dpll`
  netlink uapi.  The Vadim Fedorenko / Arkadiusz Kubalewski 2023–24
  dpll series was explicitly scoped phase-only; freq write was
  deferred and never landed.
- **Net result**: the OCXO is finally being disciplined to GNSS, but
  by the EEC firmware — not by us.  We have lost the EXTTS-based
  measurement chain that would let our servo close the loop, and
  the kernel offers no frequency-write path even if we wanted to
  override the firmware.  The OCXO is a black box managed by Intel.

### Intel `ice.ieps.0` auxiliary device

Present on `ocxo` today (visible under `/sys/bus/auxiliary/devices/`).
This is Intel's closed "Ethernet Precision Synchronization" hook,
and it is the only known path that exposes a DCO/FCW write to the
ZL30632.  CTI and Meinberg E810-based timing products use it.  It
requires Intel's SDK, is not upstream, and is not documented for
third parties.  In principle it would let us implement the full
8A34002 architecture on E810 — read TDC via `dpll_1_offset`, write
FCW via IEPS — but only under an Intel agreement.

### Implication for the moonshot

On E810 the right mental model is: **the OCXO is not our DO**.
Intel's firmware owns it.  Our actual DO is the PHC's software
incrementer, which is *softer* than even the i226 TCXO because it
adds zero physical inertia — it's pure arithmetic on top of a
firmware-controlled oscillator.

The clean ClockMatrix-style architecture (host reads TDC, host
writes FCW, userspace PI servo, oscillator's short-term stability
shines through) requires a timing chip on a host-accessible bus
with an open register map.  That is exactly what the Timebeat OTC
provides and exactly what the E810 does not.  E810 remains useful
for PTP transport and for experiments that don't require touching
the OCXO, but it is not a moonshot platform.

## EXTTS quantization: why E810 looks "noisier" than i226

Both ends of the PPS measurement happen to live on ~125 MHz clocks,
but they are *independent* clocks — nothing about the F9T drives the
NIC.  The F9T generates its PPS edge from its internal ~125/128 MHz
TCXO (~8 ns step), and the NIC's PHC captures that edge against its
own ~125 MHz PHY/PHC clock (also ~8 ns step).  So the EXTTS reading
is quantized to ~8 ns at *both* the source and the receiver, by
coincidence of two independent design choices.

The difference between i226 and E810 is how much measurement noise
each NIC adds on top of that quantum:

- **i226**: ~1.7 ns RSS noise dithers samples across quantization
  bin boundaries.  Averaging reveals sub-bin motion; 0% identical
  adjacent timestamps.  Looks "clean" because noise hides quantization.
- **E810**: near-zero capture noise.  Samples snap to the same 8 ns
  bin over and over — **77% of adjacent EXTTS reads return the same
  value**.  The 2 µs σ the engine sees is *quantization dominated*,
  not hardware phase noise: the capture circuit is actually more
  precise than i226, but you can see the grid.

Counterintuitively this means the i226's noisier EXTTS gives the
servo *better* short-term feedback than the E810's quieter one.  To
get below the E810 quantization floor we need a different measurement
chain — TICC (60 ps), or once we're on the out-of-tree driver, the
DPLL's own phase detector.

See `docs/ticc-baseline-2026-04-01.md` for the empirical analysis.

## Next ocxo improvements

In rough priority order:

1. **Per-profile servo tuning for OCXO**: lower ki to ~0.0001 and
   slow the loop down, since the oscillator can be trusted at long
   tau.  Should drop the 18 ns σ toward the EXTTS measurement floor.
2. **Investigate E810 EXTTS noise**: 2 µs σ is much worse than i226.
   Possible causes: hardware capture quantization, kernel timestamping
   delay, or interaction with the holdover DPLL state.
3. **Implement `phase_step_bias_ns`**: actually use the parsed value
   to apply a fixed offset to PEROUT after bootstrap, removing the
   207 µs constant.
4. **TICC-driven servo on ocxo**: now that EXTTS works for bootstrap,
   the engine could be told to drive from TICC (60 ps measurement
   precision) instead of EXTTS (~2 µs).  Expected to be much better.
5. **Out-of-tree driver path**: still potentially useful for OCXO
   discipline through the DPLL, but no longer required for basic
   peppar-fix operation.

## Status as of 2026-04-07 morning (superseded — see above)

The ocxo host (Intel E810-XXVDA4T NIC + onboard OCXO) cannot run
peppar-fix end-to-end with the current code without significant
work-arounds.  This doc captures the matrix of tradeoffs so we know
what's blocking and what would be needed.

## The hardware

- **NIC**: Intel E810-XXVDA4T (PCI 0000:01:00.x), 4 ports
- **PHC**: `/dev/ptp2` = `ice-0000:01:00.0-clk` (the timing PHC)
- **DPLL**: ZL30795 onboard, currently in holdover state
- **OCXO**: onboard, drives the DPLL when locked
- **Internal F9T**: exposed as kernel GNSS at `/dev/gnss0` (I2C)
- **External F9T-BOT**: USB-serial at `/dev/ttyACM2` (used by current config)
- **TICC #3**: `/dev/ticc3` (USB Arduino), wired to the SMA bracket
  - chA: E810 PHC PEROUT (SMA1 / SDP20)
  - chB: F9T-BOT raw PPS (SMA2 / SDP21)

## Driver matrix

| Driver | Source | EXTTS | DPLL/OCXO discipline | Status |
|---|---|---|---|---|
| **stock ice (in-kernel)** | Linux 6.8 | works after sysfs pin program (2c76229); 8 ns quantized, ~77% identical-adjacent | not exposed — PHC free-runs on internal osc, NOT on OCXO | currently loaded |
| **patched in-kernel ice** | Ubuntu source + `0002` streaming patch | works | not exposed | recommended for stock-driver use; fast F9T reads |
| **Intel out-of-tree ice v2.4.5** + `0001` patch | Intel github | **not supported** (no EXTTS/PIN ioctls on SDP pins) | DPLL exposed, OCXO can drive PHC | required for OCXO discipline; F9T still works |

The fundamental tradeoff: stock driver gives no DPLL control but at
least runs reliably; out-of-tree driver enables OCXO discipline but
historically broke EXTTS pin mapping.

We last tried both around 2026-03-29 and concluded that the only
path that produced clean data was a two-driver workflow:

1. Boot with stock driver → EXTTS works for the bootstrap step
2. Manually switch to out-of-tree driver → DPLL locks to OCXO
3. Run peppar-fix in `--ticc-drive` mode → no further EXTTS needed

That's not a stable production setup.  And `--ticc-drive` requires
the bootstrap to also avoid EXTTS, which it currently can't.

## What's blocking a 10-minute test today (2026-04-07)

### 1. Stock ice EXTTS is frozen

Confirmed empirically: setting `adjfine=0` and reading 8 consecutive
PPS events via EXTTS returned identical timestamps (1,219,783 ns).
With adjfine=0 the PHC should drift visibly between PPS events, but
EXTTS never updates.

This breaks:
- `phc_bootstrap.measure_pps_frequency()` (uses EXTTS to measure drift)
- Engine PPS event reader (used by all servo sources except --ticc-drive)
- Bootstrap phase verification (uses EXTTS to confirm step accuracy)

### 2. Bootstrap step lands 200 µs off GPS

The bootstrap's `ADJ_SETOFFSET` step on E810 leaves a ~200 µs residual
phase error.  TICC #3 confirms PEROUT is firing 200 µs after F9T PPS,
matching what the broken EXTTS reports.

The e810 profile sets `phc_settime_lag_ns = 16_000_000` (16 ms)
suggesting clock_settime is the preferred path for E810, but the
bootstrap tries ADJ_SETOFFSET first and accepts whatever residual
results.

### 3. TICC-drive servo can't recover

Even with `--track-outlier-ns` bumped to 1 ms, the TICC-driven servo
on ocxo today shows runaway behavior: error grows from -203,590 ns
to -203,719 ns over 50 seconds while adjfine ramps from +2,544 to
+8,655 ppb.  Either:

- The E810 adjfine sign is inverted relative to i226
- The PHC isn't responding to adjfine writes (DPLL might be controlling it)
- Some other interaction with the holdover DPLL state

I didn't have time to nail down which.

## What would be needed to make ocxo work

In rough order of cost:

1. **Bootstrap without EXTTS**: add a code path to phc_bootstrap.py
   that uses TICC for the phase step verification when EXTTS is
   unavailable.  Requires TICC to be the only PPS measurement.
2. **E810 adjfine sign characterization**: empirically test which
   direction adjfine moves the PHC on E810, then either fix the sign
   in `phc_actuator.py` or add a per-profile sign override.
3. **Investigate the 200 µs bootstrap residual**: understand whether
   it's an ADJ_SETOFFSET bug, a hardware capture delay, or something
   we can correct.
4. **Out-of-tree driver workflow**: document the exact patches and
   the bootstrap-then-switch procedure for OCXO discipline mode.
5. **Single-driver path**: investigate whether newer kernels (6.10+)
   have fixed the stock ice EXTTS issue.

None of these is a 10-minute fix.  The current state is: **ocxo is
not a usable peppar-fix host today**, even though the hardware is
capable of being one.

## Recommended next session work

- Check upstream kernel changelog for ice driver EXTTS fixes since 6.8
- If unavailable, implement TICC-only bootstrap path
- Verify E810 adjfine sign with direct PHC manipulation test
- Document the working out-of-tree driver setup as a fallback
