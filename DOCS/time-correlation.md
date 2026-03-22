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

- `_match_pps_event_from_history()`
- `_find_pps_event_for_obs()`
- `_pop_correlatable_observation()`
in [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)

What happens:

- candidate PPS events are chosen from history using:
  - observation `recv_mono`
  - PPS `recv_mono`
  - acceptable receive-time window
  - closeness of rounded PHC second to target GNSS/UTC/TAI second

Why it matters:

- this is the core cross-stream correlator in the current unified path

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

### G. Legacy queue-order servo paths

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
