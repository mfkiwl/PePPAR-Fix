# Time And Platform TODO

This file turns the recent bring-up and correlation findings into a concrete work list.

## 1. `oxco` / E810

### Confirm the real platform limitation

- [x] Preserve a repeatable diagnostic script for `/dev/gnss0` burst cadence and RAWX lag
- [ ] Record one representative capture from `oxco` in the repo or docs
- [ ] Confirm whether the burst behavior is specific to the current kernel/driver revision or intrinsic to this path

### Decide on the GNSS ingest strategy

- [ ] Evaluate whether `/dev/gnss0` is acceptable for real-time PHC discipline at all
- [ ] If not, define an alternate ingest path for the E810-hosted receiver
- [ ] If yes, define the maximum accepted observation lag and how the servo should react when it is exceeded

### Servo follow-up

- [ ] Stop tuning E810 gains until the GNSS delivery path is judged acceptable
- [ ] Add diagnostics that separate:
  - PHC fractional PPS error
  - whole-second offset
  - observation receive lag
  - correlation match delta
- [ ] Verify whether any remaining `epoch_offset_s` excursions survive once lag-based rejection is made explicit

## 2. `timehat` / i226

### PPS hardware bring-up

- [ ] Identify the actual SDP input carrying F9T PPS
- [ ] Verify board routing or jumper settings for PPS into the i226
- [ ] Verify EXTS events on `/dev/ptp0` after hardware routing is confirmed

### Software verification after PPS appears

- [ ] Rerun the unified path with `--receiver f9t-l5`
- [ ] Verify the i226 profile end-to-end with real PPS events
- [ ] Confirm that the current pin/channel programming assumptions are correct for TimeHAT

## 3. Unified correlation model

### Event envelopes

- [ ] Keep `ObservationEvent` and `PpsEvent` as the minimum standard for new time-bearing streams
- [ ] Add a corresponding event wrapper for TICC lines
- [ ] Ensure every new stream carries:
  - source-native time
  - host monotonic receive time
  - enough metadata to explain origin and validity

### Correlation windows

- [ ] Move correlation window settings into explicit configuration
- [ ] Define default windows for:
  - PPS to observation matching
  - TIM-TP freshness
  - future TICC matching
- [ ] Log window-based drop reasons explicitly

### Drop policy

- [ ] Audit the code for places that still discard events before correlation
- [ ] Replace “newest event wins” logic with history-and-window logic where needed
- [ ] Separate startup backlog cleanup from steady-state stale-event handling

## 4. Legacy code cleanup

These files still contain older queue-order assumptions or duplicate timing logic:

- [`scripts/phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/phc_servo.py)
- [`scripts/peppar_phc_servo.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_phc_servo.py)
- [`scripts/peppar_fix_main.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_main.py)

Tasks:

- [ ] Compare each legacy path against the unified event-history logic in [`scripts/peppar_fix_cmd.py`](/home/bob/git/PePPAR-Fix/scripts/peppar_fix_cmd.py)
- [ ] Decide whether each path should be migrated, reduced, or removed
- [ ] Avoid maintaining multiple subtly different correlation models

## 5. Diagnostics and tooling

- [x] Keep a simple raw device probe for GNSS burst timing
- [ ] Keep a simple EXTS probe for PPS verification
- [ ] Add a comparable probe for TICC line receive timing once TICC is integrated
- [ ] Make it easy to emit correlation diagnostics without modifying core servo logic each time

## 6. Documentation

- [ ] Keep [`platform-support.md`](/home/bob/git/PePPAR-Fix/DOCS/platform-support.md) updated as each platform changes state
- [ ] Keep [`time-correlation.md`](/home/bob/git/PePPAR-Fix/DOCS/time-correlation.md) aligned with the actual code paths
- [ ] Add concrete examples of accepted and rejected correlations once the model stabilizes
