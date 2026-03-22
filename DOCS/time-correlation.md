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

Example outputs are logged by [`scripts/log_observations.py`](/home/bob/git/PePPAR-Fix/scripts/log_observations.py), which writes:

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
  - [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)
- legacy servo paths:
  - [`scripts/phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/phc_servo.py)
  - [`scripts/peppar_phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_phc_servo.py)
  - [`scripts/peppar_fix_main.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_main.py)

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
  - [`scripts/qerr_test.py`](/home/bob/git/PePPAR-Fix/scripts/qerr_test.py)

What it contains:

- `towMS`
- `towSubMS`
- `qErr`
- `week`
- flags

This is a GNSS-receiver-side timing-quality input. It is not a host timestamp and it is not a PHC timestamp.

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

This suggests a future shared mechanism:

- each reader returns `recv_mono` plus a `queue_remains` boolean
- the parser combines embedded source time with that read metadata
- the code derives a confidence score for that sample's source-time to
  host-monotonic relationship
- a slow-moving estimator such as an EMA can track the nominal relationship,
  while sample confidence expresses how much to trust each new update

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

### What the first meaningful gate-forcing run looked like

After adding targeted delay injection, the most useful forcing case on `oxco`
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

## What we learned on `oxco`

On `oxco`, `/dev/gnss0` behaves badly for correlation purposes:

- the kernel GNSS char device delivers bursts roughly every `2.1s` to `2.4s`
- each burst may contain many UBX packets
- `RXM-RAWX` epochs often arrive `5s` to `11s` after the GNSS second they describe

Important detail:

- after adding packet-level receive timestamps in [`scripts/peppar_fix/gnss_stream.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix/gnss_stream.py), we verified that this lag is real at the device/kernel boundary, not just inside `pyubx2`
- the reusable probe for this is [`scripts/gnss_lag_probe.py`](/home/bob/git/PePPAR-Fix/scripts/gnss_lag_probe.py)

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

- [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)

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

- [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)

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
and [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)

What happens:

- the strict sink gate sits in front of the steady-state servo sink
- observations stay in history until the gate can do one of three things:
  - consume a valid matched observation/PPS pair
  - defer while waiting for more companion events
  - drop an observation once the window proves it can no longer match
- candidate PPS events are chosen from history using:
  - observation `recv_mono`
  - PPS `recv_mono`
  - acceptable receive-time window
  - closeness of rounded PHC second to target GNSS/UTC/TAI second

Why it matters:

- this is now the core strict-sink correlator in the current unified path
- the sink contract is explicit and measurable through gate stats:
  - `consumed_correlated`
  - `deferred_waiting`
  - `dropped_unmatched`
  - `dropped_outside_window`

### F. Servo timescale correlation

Current code:

- `_target_timescale_sec()`
- `_pps_fractional_error()`
- `_servo_epoch()`
in [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)

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
- [`scripts/peppar_fix_main.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_main.py)

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
