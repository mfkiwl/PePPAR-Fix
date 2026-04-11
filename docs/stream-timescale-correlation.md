# Time Correlation Notes

This document records what we learned about correlating time-bearing events from different sources.

The central rule is:

- timestamp every event as close as possible to the moment it enters userspace
- carry that timestamp through the pipeline
- carry queue-state information through the pipeline when available
- correlate using explicit windows at the correlation point
- drop unmatched events only at the edge of the accepted correlation window

This matters because queuing can happen:

- immediately after a stream is opened
- later during normal operation if the host is delayed by CPU load or scheduler latency
- inside kernel drivers
- inside user-space readers and parser loops

## Why the model cannot stay simple

A simple read-and-deliver model is still acceptable for sinks that only care
about one stream at a time.

It is not acceptable for sinks that must reason across multiple streams with
different native timescales.

The reason is straightforward:

- queueing can distort arrival order
- queueing can distort apparent freshness
- two streams can be delayed independently or together
- some sinks are harmed more by a wrong match than by no match

For strict sinks such as the PHC servo, mis-correlated input is worse than
silence. A wrong PPS-to-observation match can create a wrong steering action,
while a dropped epoch merely delays correction.

That implies an architectural rule:

- every time-correlation-sensitive sink should sit behind an explicit
  correlation gate

The gate should be the component that decides:

- consume now because the required companion events are present
- defer because a valid match may still arrive
- drop because the event can no longer be matched inside policy

The sink should not make up its own queue-order heuristics on the fly.

This is also why one policy cannot be imposed globally.

Different sinks need different behavior:

- some want freshest-only
- some want loss-free
- some want correlated-window matching

The added metadata, correlation logic, drop logic, and testing machinery are
there to preserve correctness across those different sink contracts, not to
make the code abstract for its own sake.

Unexpected holdover in TimeLab testing should be treated as a failed run unless
holdover is the thing being tested.

## Streams we have today

### 1. GNSS observation stream

Primary source today:

- `RXM-RAWX` from the F9T

Common companion messages:

- `RXM-SFRBX`
- `NAV-PVT`
- `NAV-SAT`
- `TIM-TP`

Relevant code:

- reader and event creation:
  - [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)
- kernel GNSS wrapper:
  - [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py)
- event type:
  - [`scripts/peppar_fix/event_time.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/event_time.py)

What `RXM-RAWX` gives us:

- GNSS week
- receive time of week `rcvTow`
- leap second field
- one set of per-satellite measurements per epoch:
  - pseudorange
  - carrier phase
  - Doppler
  - C/N0
  - lock time
  - constellation ID
  - signal ID

Example outputs are logged by [`tools/log_observations.py`](/home/bob/git/PePPAR-Fix/tools/log_observations.py), which writes:

- `*_rawx.csv`
- `*_pvt.csv`
- `*_timtp.csv`
- raw `.ubx`

What we add in software:

- `ObservationEvent.gps_time`
- `ObservationEvent.recv_mono`
- `ObservationEvent.recv_utc`

### 2. PPS / PHC EXTS stream

Primary source today:

- `PTP_EXTTS_EVENT` from the Linux PTP device

Relevant code:

- PTP device IO:
  - [`scripts/peppar_fix/ptp_device.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/ptp_device.py)
- unified servo path:
  - [`old/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_cmd.py)
- legacy servo paths:
  - [`scripts/phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/phc_servo.py)
  - [`scripts/peppar_phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_phc_servo.py)
  - [`old/peppar_fix_main.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_main.py)

What the kernel event contains:

- `phc_sec`
- `phc_nsec`
- `index`

What we add in software in the unified path:

- `PpsEvent.recv_mono`

Interpretation:

- `phc_sec/phc_nsec` is the PHC timestamp of the PPS edge
- `recv_mono` is when userspace read the event

These are different clocks and should not be conflated.

### 3. F9T TIM-TP stream

Primary source:

- `TIM-TP`

Relevant code:

- storage:
  - [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)
- tests:
  - [`tools/qerr_test.py`](/home/bob/git/PePPAR-Fix/tools/qerr_test.py)

What it contains:

- `towMS`
- `towSubMS`
- `qErr`
- `week`
- flags

This is a GNSS-receiver-side timing-quality input. It is not a host timestamp and it is not a PHC timestamp.

This is a GNSS-receiver-side timing input.  It is not a host
timestamp and it is not a PHC timestamp.  It **predicts** the
quantization error on the **following** PPS edge — not the previous
one.

Reference (authoritative):

- u-blox F9 TIM 2.20 Interface Description (UBX-21048598 R01),
  §3.19.3 UBX-TIM-TP (0x0D 0x01), p. 184:
  Message comment: “This message contains information on the timing
  of the next pulse at the TIMEPULSE0 output.”
  `qErr` field (byte offset 8, I4, ps): “Quantization error of time
  pulse.”  Flag `qErrInvalid` (bit 4 of `flags`): indicates
  validity.
  https://content.u-blox.com/sites/default/files/u-blox-F9-TIM-2.20_InterfaceDescription_UBX-21048598.pdf

Given USB serial latency (~100 ms from F9T UART through USB CDC ACM
to userspace read), a TIM-TP message arrives nearly a full second
before the PPS edge it describes.  This is the `expected_offset_s`
in the monotonic-time matching: the TIM-TP read timestamp is ~0.9 s
earlier than the PPS read timestamp.

Live probing on `timehat` showed:

- `RXM-RAWX.rcvTow ~= N.997`
- the immediately preceding `TIM-TP.towMS == round(RXM-RAWX.rcvTow) * 1000`

That is the right association for the PPS edge aligned with the RAWX
epoch.

### 4. TICC stream

Relevant code:

- [`scripts/ticc.py`](/home/bob/git/PePPAR-Fix/scripts/ticc.py)

What it contains:

- one text line per edge:
  - `<seconds_since_boot> chA|chB`
- parsed as:
  - `channel`
  - `ref_sec`
  - `ref_ps`

Important property:

- timestamps are relative to TICC boot, not to UTC, GPS, TAI, PHC, or host monotonic

The TICC is not fully correlated in the unified path today, but this stream should be treated as another event source that will need the same design discipline.

### TimeHat landing log

- Run `timehat-wrapper-horizon6-300s` (289 epochs, 300 s) with `--ticc-landing-horizon-s 6.0`, `--ticc-settled-threshold-ns 150`, `--ticc-settled-count 5` and the host `phase_step_bias_ns 2283.0`. The servo remained in `landing`, moved from about −1.26 µs at the beginning of steady state to −145 ns at the end, and logged no holdover events (`data/timehat-wrapper-horizon6-300s.csv` / `.log`).
- Slicing the final 120 s tail and running `tools/analysis/analyze_servo.py` yields TDEV(1 s)=61.6 ns for PPS OUT versus 0.59 ns for raw F9T PPS, confirming the short-τ crossover after pull-in (`data/timehat-wrapper-horizon6-300s_tail120_analysis_report.txt`).

The tuned parameters are kept host-specific (TimeHat profile) and injected through the wrapper `--engine-arg` chain. Promote them into shared defaults only after verifying the same landing behavior on additional platforms, otherwise treat them as TimeHat-specific overrides.

## Initial buffering and delayed delivery

There are two distinct problems:

### Startup backlog

When a stream is first opened, queued old data may already be waiting.

Examples:

- kernel GNSS devices may have old UBX packets queued
- serial devices may have stale bytes buffered
- the TICC may reboot on port open and the OS may still hold old lines

Current mitigations:

- kernel GNSS backlog drain in [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py)
- stale startup handling in [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)
- TICC boot sentinel handling in [`scripts/ticc.py`](/home/bob/git/PePPAR-Fix/scripts/ticc.py)

### Steady-state scheduling delay

Even after startup, a host under CPU pressure may not run our code promptly.

That means:

- bytes may sit unread in the kernel
- EXTS events may queue in the PTP device
- parsed events may wait in Python queues
- later events may be processed before older ones are correlated

This is why read-time timestamps and correlation windows matter even in steady state.

## Confidence of cross-timescale mapping

Not every sample should be treated as equally trustworthy for relating a
source-native timescale to host `CLOCK_MONOTONIC`.

The confidence of a sample should depend on:

- whether more events were visibly queued at read time
- whether the transport is known to batch
- whether the packet or line was read promptly
- whether the parse layer can estimate packet age inside userspace

This is now partly implemented:

- each reader returns `recv_mono` plus a `queue_remains` boolean
- the parser combines embedded source time with that read metadata
- the code derives a confidence score for that sample's source-time to
  host-monotonic relationship
- GNSS, PPS/EXTTS, RTCM, and TICC now all carry a first-pass
  `correlation_confidence`
- the strict observation/PPS gate now treats low-confidence matches
  differently from prompt ones instead of treating them all as equal
- GNSS and PPS now also maintain a slow-moving constant-offset estimator
  against host `CLOCK_MONOTONIC`
- estimator updates are weighted by sample trust, not recency
- samples read while backlog is still visible are only lightly weighted, or
  can be dropped entirely by policy later if we choose

What is still future work:

- first pass is now in place for GNSS and PPS via
  [`timebase_estimator.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/timebase_estimator.py)
  and is propagated as `estimator_residual_s`
- recent samples are not privileged just for being recent; the estimator is
  trying to learn a constant offset and should move mainly on high-confidence,
  no-backlog reads
- TICC now uses the same weighted constant-offset estimator against host
  monotonic
- RTCM now uses the same estimator only for message families with a usable
  stream epoch, chiefly SSR; broadcast ephemeris toe/toc remain excluded
  because they are model epochs, not transport timestamps
- unified servo logs now emit observation, PPS, match, and correction
  confidence/residual fields so gate outcomes can be diagnosed from the sink
  output instead of inferred only from counters

## Testing strategy

The current multi-threaded blocking-read architecture is acceptable for now.
We do not need to move to `asyncio` to test timing robustness.

The right fault-injection point is:

- after a source `read()` or equivalent ingress returns
- before the event is handed to any sink or downstream queue

That is where host-side queuing distorts correlation while preserving the
real source-native timestamp.

### Queuing modes we must test

#### 1. Individual-stream queuing

Only one stream suffers a queuing event while the others continue to read and
deliver with low latency.

Examples:

- one GNSS reader thread is delayed by CPU starvation
- one PPS reader thread is delayed by scheduler latency
- one NTRIP reader is delayed while the others remain healthy

This tests whether sinks can isolate one bad stream instead of corrupting the
interpretation of the others.

#### 2. All-stream queuing

Many or all streams suffer a queuing event at nearly the same time.

Examples:

- local CPU starvation delays all source threads together
- host scheduling pauses all readers
- shared-path network delay affects several network streams at once

This matters because some delays are time-correlated across sources. A sink
must not assume that every queueing event is independent.

### Proposed randomized delay injection

Use strategically placed randomized `sleep()` calls in each source thread.

The delay should happen:

- after `read()` returns
- before delivery to any queue, history, or sink

#### Per-thread delay variables

These environment variables control independent per-thread delays:

- `THREAD_DELAY_PROB_PCT`
- `THREAD_DELAY_MEAN_MS`
- `THREAD_DELAY_RANGE_MS`
- `THREAD_DELAY_SOURCES`

Interpretation:

- `THREAD_DELAY_PROB_PCT`
  - floating-point probability in percent that a non-zero delay is injected
- `THREAD_DELAY_MEAN_MS`
  - mean delay in milliseconds
- `THREAD_DELAY_RANGE_MS`
  - uniformly distributed range around the mean in milliseconds

If `THREAD_DELAY_PROB_PCT` is unset, no per-thread injected delay occurs.

`THREAD_DELAY_SOURCES` is optional and should be a comma-separated list of
source-name substrings.

Examples:

- `gnss:/dev/gnss0`
- `ptp:/dev/ptp1`
- `ntrip:EPH`

If set, per-thread delays are only injected for matching sources.

#### System-correlated delay variables

These environment variables control time-correlated delays across all reader
threads:

- `SYS_DELAY_PROB_PCT`
- `SYS_DELAY_MEAN_MS`
- `SYS_DELAY_RANGE_MS`
- `SYS_DELAY_SOURCES`

Intended design:

- a small control thread performs the random check
- when it triggers, it sets shared state
- each source thread observes that state after its next read and inserts the
  same or closely related delay before delivery

If `SYS_DELAY_PROB_PCT` is unset, no correlated delay occurs.

`SYS_DELAY_SOURCES` is optional and uses the same comma-separated substring
matching model as `THREAD_DELAY_SOURCES`. If set, only matching sources apply
the triggered correlated delay.

This simulates:

- host-wide CPU starvation
- host scheduling pauses
- shared-path network queuing affecting multiple network streams

### Suggested operating ranges

The first implementation can stay simple:

- Bernoulli trigger from `_PROB_PCT`
- uniform random delay centered around `_MEAN_MS`
- total span or half-width controlled by `_RANGE_MS`

Suggested regimes:

- `< 1.0%` probability for occasional outlier simulation
- `> 1.0%` probability for torture testing
- sub-second delays for mild stress
- multi-second delays up to nearly `10s` for backlog and correlation-window
  stress

### Required injected-delay log

Whenever a non-zero synthetic delay is introduced, the test harness should
emit an event log entry.

Each entry should include:

- source thread name or source class
- delay type: `THREAD` or `SYS`
- delay start time on host `CLOCK_MONOTONIC`
- delay end time on host `CLOCK_MONOTONIC`
- planned delay duration
- actual sleep duration

This gives ground truth for later comparison with sink behavior and drop
decisions.

### What this should validate

This plan should let us verify whether:

- freshest-only sinks stay responsive under backlog
- loss-free sinks preserve data through bursts
- correlated-window sinks reject or accept events for the right reasons
- sink-specific drop policies behave correctly under isolated and shared delay
- source-time to host-monotonic confidence scores degrade appropriately when
  queueing is intentionally introduced

### Relationship to current code

This fits the current architecture:

- blocking reader threads can remain blocking
- no event loop rewrite is required
- the delay hook lives at the reader boundary
- the existing move toward event envelopes and timing metadata makes the
  resulting behavior diagnosable

### Startup verification vs runtime watchdogs

These should remain distinct:

- startup verification
  - used for configurable sources like GNSS receivers
  - confirms required message types before sinks begin consuming live data
  - for the F9T family, this should include at least:
    - `RXM-RAWX`
    - `RXM-SFRBX`
    - `NAV-PVT`
    - `TIM-TP`
  - if this check fails, re-running receiver configuration is reasonable
- runtime stream watchdogs
  - log when a source has been quiet longer than a configured timeout
  - should bark during synthetic stutter tests
  - should not automatically reconfigure hardware for ordinary runtime stutters

The runtime watchdog is a diagnostic signal:

- it tells us which source thread went quiet
- it does not replace sink-level gates
- it does not imply the process should stop

### What the first meaningful gate-forcing run looked like

After adding targeted delay injection, the most useful forcing case on `ocxo`
was:

- keep NTRIP healthy
- inject multi-second delays only on:
  - `gnss:/dev/gnss0`
  - `ptp:/dev/ptp1`

That produced the first run where the strict sink gate clearly did the right
thing instead of merely surviving:

- `strict_correlation.consumed_correlated = 0`
- `strict_correlation.dropped_outside_window = 1`
- `strict_correlation.dropped_unmatched = 1`
- `correction_freshness.consumed_fresh = 0`

Interpretation:

- the servo sink refused to consume bad GNSS/PPS pairings
- the correction gate was not the limiting factor in that run
- this is the kind of test that exercises the sink contract we actually care
  about

## What we learned on `ocxo`

On `ocxo`, `/dev/gnss0` behaves badly for correlation purposes:

- the kernel GNSS char device delivers bursts roughly every `2.1s` to `2.4s`
- each burst may contain many UBX packets
- `RXM-RAWX` epochs often arrive `5s` to `11s` after the GNSS second they describe

Important detail:

- after adding packet-level receive timestamps in [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py), we verified that this lag is real at the device/kernel boundary, not just inside `pyubx2`
- the reusable probe for this is [`tools/gnss_lag_probe.py`](/home/bob/git/PePPAR-Fix/tools/gnss_lag_probe.py)

This means the correlator must assume that:

- delayed observations are normal on some platforms
- order of arrival is not a safe proxy for closeness in time

## Correlation points in the code

This section lists the important points where time-bearing streams are compared or should be compared.

### A. GNSS packet receive timestamp assignment

Current code:

- [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py)
- [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)

What happens:

- the kernel GNSS wrapper records a monotonic timestamp when a complete UBX packet becomes available to userspace
- `serial_reader()` attaches that to the resulting `ObservationEvent`

Why it matters:

- parse completion time can differ materially from packet arrival time in bursty streams

### B. TIM-TP capture into `QErrStore`

Current code:

- [`scripts/realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)

What happens:

- `TIM-TP.qErr` is stored with a host monotonic freshness time
- later consumers ask `QErrStore.get(max_age_s=...)`

Why it matters:

- this is already a time-windowed correlation of one stream against another
- stale `qErr` is dropped at the consumer edge, not blindly trusted forever

### C. PPS EXTS capture into `PpsEvent`

Current code:

- [`old/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_cmd.py)

What happens:

- EXTS reader thread receives `(phc_sec, phc_nsec, index)` from the kernel
- the code wraps it as `PpsEvent(..., recv_mono=time.monotonic())`
- the event is pushed into both:
  - a bounded queue
  - a short `pps_history`

Why it matters:

- the PHC timestamp is for servo math
- the host monotonic timestamp is for cross-stream correlation

### D. Observation history staging

Current code:

- [`old/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_cmd.py)

What happens:

- steady state appends raw observation events into `obs_history`
- observations are no longer immediately collapsed to “the newest one”
- the correlator waits until a given observation can be matched or has definitely aged out

Why it matters:

- this is the point where we stopped discarding potentially matchable observations too early

### E. Observation-to-PPS matching

Current code:

- `StrictCorrelationGate.pop_observation_match()`
- `_match_pps_event_from_history()`
- `_find_pps_event_for_obs()`
in [`scripts/peppar_fix/correlation_gate.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/correlation_gate.py)
and [`old/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_cmd.py)

What happens:

- the strict sink gate sits in front of the steady-state servo sink
- observations stay in history until the gate can do one of three things:
  - consume a valid matched observation/PPS pair
  - defer while waiting for more companion events
  - drop an observation once the window proves it can no longer match
- the gate now also enforces a configurable minimum correlation confidence,
  so queued or aged samples can be rejected even when they are nominally
  in-window
- candidate PPS events are chosen from history using:
  - observation `recv_mono`
  - PPS `recv_mono`
  - acceptable receive-time window
  - closeness of rounded PHC second to target GNSS/UTC/TAI second
  - combined sample confidence from the observation and PPS readers

Why it matters:

- this is now the core strict-sink correlator in the current unified path
- the sink contract is explicit and measurable through gate stats:
  - `consumed_correlated`
  - `deferred_waiting`
  - `dropped_low_confidence`
  - `dropped_unmatched`
  - `dropped_outside_window`

Recent `ocxo` result:

- baseline run:
  - `consumed_correlated=15`
  - `dropped_low_confidence=0`
- targeted GNSS/PTP stutter run:
  - `consumed_correlated=1`
  - `deferred_waiting=8`
  - `dropped_low_confidence=4`
  - `dropped_unmatched=1`
  - `dropped_outside_window=1`

Current knobs:

- `min_correlation_confidence`
- `min_broadcast_confidence`
- `min_ssr_confidence`

Those are now explicit CLI/profile settings in the live entrypoints rather
than hidden constants in the gate logic.

### F. Servo timescale correlation

Current code:

- `_target_timescale_sec()`
- `_pps_fractional_error()`
- `_servo_epoch()`
in [`old/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_cmd.py)

What happens:

- GNSS epoch time is mapped to `gps`, `utc`, or `tai`
- PPS PHC timestamp is decomposed into:
  - whole-second offset
  - fractional PHC phase error

Why it matters:

- this is where second alignment and sub-second phase are combined into one servo error source

### G. Correction freshness gating for EKFs

Current code:

- `CorrectionFreshnessGate.accept()`
in [`scripts/peppar_fix/correlation_gate.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/correlation_gate.py)
- `RealtimeCorrections.freshness()`
in [`scripts/ssr_corrections.py`](/home/bob/git/PePPAR-Fix/scripts/ssr_corrections.py)

What happens:

- the live EKF loops now check correction freshness before LS init,
  ambiguity seeding, or `filt.update(...)`
- this is a softer gate than PPS matching:
  - it defers when broadcast state is not ready yet
  - it drops when broadcast state is stale on host monotonic time
  - it does not require one-to-one event pairing

Why it matters:

- the EKF observation epoch is not enough by itself
- the satellite position/clock model also has to be fresh enough to trust
- this moves the EKF paths away from implicit “latest correction wins”
  behavior
### H. Legacy queue-order servo paths

Current code:

- [`scripts/phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/phc_servo.py)
- [`scripts/peppar_phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_phc_servo.py)
- [`old/peppar_fix_main.py`](/home/bob/git/PePPAR-Fix/old/peppar_fix_main.py)

These paths still contain older queue-order assumptions. They should be reviewed against the newer history-and-window design in the unified path.

## Design rules going forward

### Rule 1: Every stream gets a local receive timestamp

For each event source, stamp at read time with host monotonic:

- GNSS packet/event
- PPS EXTS event
- TICC line
- any future packet or file-backed event source

Also carry whether the reader knows additional events were queued behind the
one just read.

### Rule 2: Preserve source-native time too

Do not replace source-native time with host monotonic. Keep both.

Examples:

- GNSS: `week + rcvTow`
- PPS: `phc_sec + phc_nsec`
- TICC: `ref_sec + ref_ps`

### Rule 3: Correlate from history, not queue order

Use short rolling histories and explicit windows. Do not assume:

- newest observation matches newest PPS
- startup backlog is the only backlog
- one queue consumer equals one event per second

### Rule 4: Drop only at the edge

Discard an event only when:

- it has no possible match left inside the accepted correlation window, or
- it is invalid on its own merits

Do not discard events earlier just because they are not the newest item in the queue.

### Rule 5: Log enough to debug correlation

Correlation logs should include at least:

- source-native event time
- local receive time
- queue-remains state
- matched peer receive-time delta
- matched peer native-time delta
- queue/history depth
- reason for discard

### Rule 6: CLOCK_REALTIME is a transfer standard only

CLOCK_REALTIME tracks UTC via NTP with ~1 ms phase error.  Any code that
uses it as an *absolute* time source inherits that error.  This matters
because the PHC bootstrap sets the hardware clock — if we set PHC =
CLOCK_REALTIME + offset, the PHC inherits the NTP error, and any
downstream consumer (PTP, servo, CLOCK_REALTIME itself) inherits it too.
The tautology is invisible: readback checks that compare PHC against
CLOCK_REALTIME will always agree because they share the same bias.

**Safe use — transfer standard:**

Read CLOCK_REALTIME twice to measure the elapsed time between two events
on different timescales.  The NTP phase error appears in both reads and
cancels in the subtraction:

```
rt_at_pps   = CLOCK_REALTIME           # read at PPS edge
rt_at_step  = CLOCK_REALTIME           # read at step time
elapsed     = rt_at_step - rt_at_pps   # NTP error cancels
target_phc  = pps_truth + elapsed      # anchored to PPS, not NTP
```

The residual error is CLOCK_REALTIME's *frequency* error (NTP steers this
to < 1 ppb) multiplied by the interval between the two reads.  For a 10s
interval at 1 ppb, that's 10 ns — negligible.

**Safe use — whole-second identification:**

`round(CLOCK_REALTIME)` identifies which UTC second a PPS edge belongs to.
This requires NTP accuracy < 0.5 s, which NTP guarantees under all
non-pathological conditions.

**Unsafe — absolute phase reference:**

```
# BAD: PHC inherits NTP's ~1 ms error
target_phc = CLOCK_REALTIME + tai_offset
```

**Where this applies:**

- `ptp_device.py: step_to()` — PPS-anchored mode uses CLOCK_REALTIME as
  transfer standard; sys_ns from PTP_SYS_OFFSET cross-timestamp provides
  the second transfer event for residual computation
- `phc_bootstrap.py` — PPS capture + CLOCK_REALTIME identifies the second;
  the PPS edge *is* the sub-second truth
- Any future code that relates PHC time to wall time

**Patrol guidance:**

Review any new use of `CLOCK_REALTIME` or `time.time()` in timing-critical
paths.  Ask: "Is this a transfer (two reads, subtracted) or an absolute
use (one read, used directly)?"  Absolute uses in paths that feed the PHC
or affect time accuracy are bugs.

## Recommended follow-up work

- move the legacy servo paths toward the same event-history model as the unified path
- add monotonic receive timestamps to any future TICC integration point
- centralize correlation-window configuration instead of scattering constants
- make discard reasons explicit in logs
- treat platform-specific buffering behavior as part of platform support, not as an incidental runtime anomaly

## Engineering checklist

Use this as the concrete checklist for future time-correlation work.

### Event stamping

- [ ] Every event source must carry a host monotonic receive timestamp
- [ ] Every event source must also preserve its source-native time
- [ ] TICC integration must create an explicit event envelope, not pass around raw tuples
- [ ] Legacy servo paths must stop relying on implicit queue order as a time proxy

### Correlators

- [ ] Correlation windows must be explicit configuration, not hidden constants
- [ ] Each correlator must keep short rolling histories on both sides of the match
- [ ] Each correlator must log why an event was accepted, deferred, or dropped
- [ ] Queue depth and history depth must be visible in diagnostics

### Drop policy

- [ ] No stream should discard events merely because a newer event arrived
- [ ] Events should only be dropped when invalid or definitively outside the match window
- [ ] Startup backlog discard should be isolated from steady-state discard logic
- [ ] Each drop path should identify whether the event was stale at source, stale in host queues, or unmatched at correlation time

### Platform-specific validation

- [ ] For each new GNSS transport, measure packet arrival lag directly at the device boundary
- [ ] For each new PPS path, verify both PHC timestamp capture and host receive timing
- [ ] For each platform, record whether the dominant delay is in hardware, kernel buffering, or userspace parsing
- [x] Add at least one reproducible diagnostic script per platform to measure real arrival lag
## Deterministic Holdover Testing

The randomized delay-injection environment variables are useful for testing
queueing and stutter, but they are not the best mechanism for deterministic
holdover tests. They delay delivery after `read()` rather than making a source
go silent.

For holdover tests, the engine now supports signal-controlled source muting:

- `SIGUSR1`
  - enable mute for configured source classes
- `SIGUSR2`
  - disable mute and resume delivery

Current default mute target:

- `gnss:`
  - drops GNSS observation delivery from the reader to the sink

This path is implemented in:

- [`fault_injection.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/fault_injection.py)
- [`realtime_ppp.py`](/home/bob/git/PePPAR-Fix/scripts/realtime_ppp.py)
- [`peppar_fix_engine.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_engine.py)
- [`servo_fault_smoke.py`](/home/bob/git/PePPAR-Fix/tests/servo_fault_smoke.py)

Observed deterministic `timehat` result:

- `SIGUSR1` muted GNSS delivery and produced:
  - `Entering holdover: reason=no_obs_input`
- `SIGUSR2` restored GNSS delivery and produced:
  - `Leaving holdover: reason=no_obs_input`
- the smoke harness failed the run by default because unexpected holdover is a
  TimeLab test failure

The current code distinguishes:

- `no_obs_input`
  - no observation epochs arrived from the reader
  - may enter holdover
- `obs_received_but_deferred`
  - observations arrived but correlation/freshness policy did not yet allow
    consumption
  - logged as a pipeline stall, not a holdover reason
- `obs_received_but_dropped`
  - observations arrived but expired or failed policy checks before use
  - logged separately from true source silence

## The universal correlation principle: CLOCK_MONOTONIC

### Why there is no alternative

A PPS edge is just a voltage transition.  It has no inherent meaning
as a "GNSS epoch" — that meaning exists only because **we** correlate
it with a GNSS time source.  We must establish this correlation by
measurement.

Every data stream in the system operates on its own timescale:

- **TICC**: seconds since TICC boot (resets on serial port open
  unless HUPCL is cleared — see `ticc.py`).  This makes the
  independence from other timescales obvious.
- **EXTTS / PHC**: timestamps against the PHC clock.  But the PHC
  may have no established relationship to GPS, UTC, or wall time —
  witness the 500 ms offset bugs from i226 PEROUT.  A PHC timestamp
  is only meaningful if we've measured how the PHC relates to
  something else.
- **TIM-TP / RAWX**: timestamps in GPS TOW.  But these arrive over
  USB serial with variable latency.  The GPS time in the message
  tells us what the receiver thinks, not when we received it.
- **NTRIP**: SSR corrections with their own timestamps, arriving
  over the network with variable delay.

The only timescale that all reads share is `CLOCK_MONOTONIC`.  When
we read a TICC timestamp from serial port X, we log
`time.monotonic()`.  When we read an EXTTS event through the PTP
driver, we log `time.monotonic()`.  When we read a TIM-TP message
from serial port Y, we log `time.monotonic()`.  These monotonic
timestamps are the **sole** basis for correlating events across
streams.

### Establishing and maintaining the relationship

At startup, during CPU starvation, or during network congestion,
multi-second lags can occur between when a physical event happens
and when we read its data.  Messages can be queued in kernel
buffers, USB FIFOs, or userspace parsers.  Messages can be dropped
entirely — USB glitches, kernel buffer overflows, serial overruns.

**However**, when we read a timestamp and there are no queued
timestamps behind it, we have a solid correlation point through
`CLOCK_MONOTONIC` — the absence of queueing means the read happened
close to the physical event.  This relationship can be established
at startup and refined continuously as we run, so that it survives
dropped messages and transient stalls.

We cannot match by index (messages get dropped).  We cannot match by
GPS TOW (the TICC has no concept of GPS time).  We cannot match by
arrival order (queueing distorts order).  `CLOCK_MONOTONIC` is the
only reliable shared reference.

### When in-stream timestamps help (and when they don't)

Some streams carry timestamps **within** their messages:

- **TIM-TP**: GPS TOW in `towMS`
- **RAWX**: GPS TOW in `rcvTow`
- **NTRIP/SSR**: GNSS epoch timestamps in RTCM corrections

When two streams share an in-stream timescale, correlation is
trivial: match qErr to RAWX by GPS TOW and you're done.  No
`CLOCK_MONOTONIC` needed because both messages carry their own
relationship to the same reference.

But this is of no help when you need to correlate a timestamped
stream with a stream that has **no in-stream timestamp**:

- **PPS**: just a voltage edge, no embedded time
- **TICC**: timestamps in seconds-since-boot, no GPS relationship
- **EXTTS**: PHC timestamps with no guaranteed GPS relationship

For these cross-timescale correlations, `CLOCK_MONOTONIC` is the
only option.

### Staleness detection from in-stream timestamps

Streams that carry in-stream timestamps give us a powerful tool:
**staleness detection**.

When the read queue is empty after a read, we know the read
happened close to the event.  At that moment, we can measure the
latency between the in-stream timestamp and `CLOCK_MONOTONIC`:

```
latency = time.monotonic() - in_stream_to_mono(msg.gps_tow)
```

We gently update our estimate of this latency as we run (it
reflects serial + USB + kernel + scheduling delays).  Then for
every subsequent read, we check:

- **latency ≈ established**: message is fresh, normal confidence
- **latency increasing**: something is queuing — in our read path,
  in the network, or even at the point of production (e.g., the
  receiver is slow to output).  Lower confidence in results where
  freshness matters.
- **latency jumped**: a burst of stale messages arrived.  The
  in-stream timestamps tell us exactly how stale each one is.

This gives us per-message freshness with no special protocol — any
stream with in-stream timestamps gets staleness detection for free
once we calibrate the in-stream-to-monotonic relationship.  Streams
without in-stream timestamps (PPS, TICC) can only detect staleness
by checking whether `CLOCK_MONOTONIC` spacing matches the expected
event rate.

## TICC–qErr correlation (2026-04-11 discovery)

### What TIM-TP qErr predicts

Per u-blox F9 TIM 2.20 Interface Description (UBX-21048598 R01),
§3.19.3 (p. 184): "This message contains information on the timing
of the **next** pulse at the TIMEPULSE0 output."  The `qErr` field
(byte offset 8, I4, picoseconds) **predicts** the quantization error
on the **following** PPS edge — the one that hasn't fired yet.

Given USB serial latency, the TIM-TP message arrives at the host
nearly a full elapsed second before the PPS edge it describes.
This is what creates the ~0.9 s expected offset in the monotonic
matching.

### The sign

The TICC measurement contains the F9T PPS quantization.  To remove
it:

```
corrected_ticc = ticc_diff_ns + qerr_ns
```

**Sign is `+` (plus).**  The TICC sees `-(PHC_phase) - qerr(TCXO)`.
Adding `qerr` cancels the TCXO quantization, leaving `-(PHC_phase)`.
Subtracting qerr or using the wrong PPS edge's qerr **makes TDEV
worse**, not better.

### Why off-by-one is catastrophic

The qerr must correspond to the **same PPS edge** that the TICC
measured.  "Same PPS edge" has no inherent meaning — it means the
two reads (TICC data from serial port X, TIM-TP from serial port Y)
were close enough in `CLOCK_MONOTONIC` that they refer to the same
physical voltage transition, as established by the expected timing
relationship.

Off-by-one (matching to an adjacent PPS edge) is catastrophic: at
~22 ppb TCXO drift, the TCXO sweeps through one 8 ns tick every
~0.36 seconds.  A 1-second mismatch means the qerr is ~2.75 ticks
off — it becomes uncorrelated noise that the servo faithfully tracks
into the PHC.

### How the mismatch happened

Three reads occur per PPS edge, each timestamped against
`CLOCK_MONOTONIC`:

1. **TIM-TP read** at `mono_A` — predicts qErr for the next PPS edge
2. **PPS fires** → EXTTS read at `mono_B` (≈ `mono_A + 0.9s`)
3. **TICC read** at `mono_C` (≈ `mono_B + 0.05s`, USB serial latency)

The PPP observation is processed at `~mono_B + 1s`.  At that point,
`ticc_tracker.latest()` returns the **freshest** TICC measurement —
which corresponds to the **next** PPS edge (its read arrived at
`mono_C + 1s`).  If qerr was matched to the EXTTS PPS read
(`mono_B`), but the TICC measurement corresponds to the next PPS
edge, the qerr and TICC refer to different edges.

### The fix

Match qerr using the **TICC measurement's** `recv_mono`, not the
EXTTS PPS event's:

```python
ticc_qerr, _ = qerr_store.match_pps_mono(
    ticc_measurement.recv_mono,
    expected_offset_s=0.95,  # 0.9s TIM-TP lead + 0.05s TICC latency
    tolerance_s=0.2)
if ticc_qerr is not None:
    qerr_ns = ticc_qerr
```

This uses `CLOCK_MONOTONIC` on both sides: the TICC read timestamp
and the TIM-TP read timestamp.  The `expected_offset_s=0.95` encodes
the measured timing relationship between TIM-TP arrival and TICC
arrival for the same PPS edge.

### The litmus test

The qerr alignment litmus test computes the variance ratio:

```
ratio = Δvar(raw) / Δvar(raw + qerr)
```

- **ratio > 1.5**: qerr is working (reducing variance)
- **ratio ≈ 1.0**: qerr is uncorrelated (wrong edge — likely
  off-by-one)
- **ratio < 1.0**: qerr is anticorrelated (adding noise — wrong
  sign or edge)

**Both EXTTS+qErr and TICC+qErr must have their own litmus tests.**
If either shows ratio ≤ 1.0, the correlation is broken and the servo
must stop using qerr immediately.  Applying wrong-edge qerr makes
TDEV **worse** than raw PPS (3.3 ns vs 2.1 ns in the 2026-04-11
incident).  This must be caught in real time — the whole reason the
litmus exists is to prevent discovering a correlation failure after
an overnight run.

## Code audit: non-CLOCK_MONOTONIC correlation (2026-04-11)

All real-time correlation paths in the codebase use
`CLOCK_MONOTONIC`.  Two methods use GPS TOW or TICC-internal time:

### QErrStore.match_gps_time() — GPS TOW matching

`scripts/realtime_ppp.py:287-322`.  Matches qErr to GNSS epochs by
GPS Time-of-Week.  Only called by `tools/rcvtow_dt_rx_probe.py`
(offline analysis, not real-time servo).  Acceptable for offline use
where both timestamps come from the same receiver and TOW is
internally consistent.  **Must not be used for real-time
cross-stream correlation.**

### TiccPairTracker — TICC ref_sec matching

`scripts/peppar_fix_engine.py:159-250`.  Pairs TICC chA and chB
events by integer `ref_sec` (seconds since TICC boot).  This is
safe because both channels share the same TICC timebase — there is
no cross-timescale correlation.  It is matching **within** a single
timescale, not across timescales.

### Correct real-time paths

- `QErrStore.match_pps_mono()` — `CLOCK_MONOTONIC` for qErr-to-PPS
- `match_pps_event_from_history()` — `CLOCK_MONOTONIC` for
  obs-to-PPS
- TICC-to-qErr re-matching — `CLOCK_MONOTONIC` via
  `match_pps_mono(ticc_measurement.recv_mono)`
