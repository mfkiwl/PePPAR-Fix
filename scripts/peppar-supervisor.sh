#!/usr/bin/env bash
# peppar-supervisor.sh — exponential-backoff watchdog for a peppar-fix
# overnight run.  Designed to be invoked from cron once a minute.
#
# Goal: when the engine dies unexpectedly (e.g. position watchdog
# alarm, PHC divergence, NTRIP outage past max retries, kernel oops),
# restart it with exponential backoff so we don't hammer broken
# hardware.  When the engine completes its scheduled --duration
# cleanly, leave it alone.
#
# Why cron and not a sleep-loop daemon: prior debugging session hit
# practical issues with long sleep timers in a foreground tool runner
# (background tasks getting orphaned or losing context).  cron invokes
# us fresh every minute and we keep all state in a small file on disk,
# so there's no long-running process to break.
#
# State file format (JSON):
#   {
#     "command": "...",                    # the shell command to run
#     "log_file": "/path/to/engine.log",   # where engine writes its log
#     "last_attempt_unix": 1775701234,     # when we last invoked
#     "attempt_count": 0,                  # consecutive failed attempts
#     "max_attempts": 10,                  # give up after this many
#     "expect_clean_exit": true            # set true once duration elapses
#   }
#
# Backoff schedule (seconds since last attempt before retrying):
#   attempt 1: 60   (1 min)
#   attempt 2: 120  (2 min)
#   attempt 3: 240  (4 min)
#   attempt 4: 480  (8 min)
#   attempt 5: 960  (16 min)
#   attempt 6+: 1800 (30 min, capped)
#
# Usage:
#   # one-time setup of state file (call from your launch script):
#   peppar-supervisor.sh init <state_file> <log_file> <command...>
#
#   # invoked from cron every minute:
#   peppar-supervisor.sh check <state_file>
#
#   # disable a state file (after you're done with that run):
#   peppar-supervisor.sh stop <state_file>
#
# Cron entry (per host):
#   * * * * * /home/bob/peppar-fix/scripts/peppar-supervisor.sh check /tmp/peppar-supervisor.json >> /tmp/peppar-supervisor.log 2>&1

set -euo pipefail

ACTION="${1:-}"
STATE_FILE="${2:-}"

if [[ -z "$ACTION" || -z "$STATE_FILE" ]]; then
    echo "usage: $0 {init|check|stop} <state_file> [args...]" >&2
    exit 2
fi

now=$(date +%s)
ts=$(date '+%Y-%m-%dT%H:%M:%S%z')

# JSON helpers — no jq dependency, just python3.
# Output is shell-safe key=value pairs using shlex.quote on every value
# so commands containing spaces, quotes, and shell metacharacters round-trip
# correctly through `eval`.
read_state() {
    python3 -c "
import json, shlex, sys
try:
    with open('$STATE_FILE') as f:
        data = json.load(f)
    for k, v in data.items():
        print(f'{k}={shlex.quote(str(v))}')
except FileNotFoundError:
    sys.exit(2)
except Exception as e:
    print(f'STATE_READ_ERROR={e}', file=sys.stderr)
    sys.exit(3)
"
}

write_state_json() {
    python3 -c "
import json
data = $1
with open('$STATE_FILE', 'w') as f:
    json.dump(data, f, indent=2)
"
}

backoff_for_attempt() {
    local n="$1"
    case "$n" in
        0|1) echo 60 ;;
        2)   echo 120 ;;
        3)   echo 240 ;;
        4)   echo 480 ;;
        5)   echo 960 ;;
        *)   echo 1800 ;;
    esac
}

case "$ACTION" in
    init)
        LOG_FILE="${3:-}"
        if [[ -z "$LOG_FILE" ]]; then
            echo "init requires <log_file> as third arg" >&2
            exit 2
        fi
        shift 3
        CMD="$*"
        if [[ -z "$CMD" ]]; then
            echo "init requires <command> as remaining args" >&2
            exit 2
        fi
        # Escape CMD for JSON.  Python handles the quoting.
        python3 -c "
import json
data = {
    'command': '''$CMD''',
    'log_file': '''$LOG_FILE''',
    'last_attempt_unix': $now,
    'attempt_count': 0,
    'max_attempts': 10,
    'expect_clean_exit': False,
}
with open('$STATE_FILE', 'w') as f:
    json.dump(data, f, indent=2)
print('initialized', '$STATE_FILE')
"
        ;;

    check)
        # Read state.  If missing, exit silently — supervisor not active.
        if ! state_kv=$(read_state 2>/dev/null); then
            exit 0
        fi
        eval "$state_kv"

        # Helper: is an engine matching our cmdline running?
        engine_running=0
        if pgrep -f "peppar_fix_engine.py" >/dev/null 2>&1; then
            engine_running=1
        fi

        if [[ $engine_running -eq 1 ]]; then
            # Engine is alive — reset attempt counter if it had been growing
            if [[ "$attempt_count" != "0" ]]; then
                python3 -c "
import json
with open('$STATE_FILE') as f: d = json.load(f)
d['attempt_count'] = 0
with open('$STATE_FILE', 'w') as f: json.dump(d, f, indent=2)
"
                echo "$ts engine alive — reset attempt_count"
            fi
            exit 0
        fi

        # Engine is NOT running.  Check if log file shows clean shutdown.
        # If yes, the run completed normally — don't restart.
        if [[ -f "$log_file" ]]; then
            if tail -20 "$log_file" 2>/dev/null | grep -q 'peppar-fix: clean shutdown'; then
                if [[ "$expect_clean_exit" != "True" ]]; then
                    echo "$ts engine completed cleanly — supervisor done"
                    python3 -c "
import json
with open('$STATE_FILE') as f: d = json.load(f)
d['expect_clean_exit'] = True
with open('$STATE_FILE', 'w') as f: json.dump(d, f, indent=2)
"
                fi
                exit 0
            fi
        fi

        # Crash detected.  Apply backoff.
        if [[ "$attempt_count" -ge "$max_attempts" ]]; then
            echo "$ts engine dead, max_attempts ($max_attempts) reached — giving up"
            exit 0
        fi

        next_backoff=$(backoff_for_attempt "$attempt_count")
        elapsed=$((now - last_attempt_unix))
        if (( elapsed < next_backoff )); then
            wait_more=$((next_backoff - elapsed))
            echo "$ts engine dead (attempt $attempt_count/$max_attempts) — waiting ${wait_more}s more before next try"
            exit 0
        fi

        # Time to retry.  Increment counter and launch.
        new_attempt=$((attempt_count + 1))
        echo "$ts engine dead — restarting (attempt $new_attempt/$max_attempts) with command: $command"
        nohup bash -c "$command" >> "$log_file" 2>&1 &
        sleep 2
        if pgrep -f "peppar_fix_engine.py" >/dev/null 2>&1; then
            echo "$ts restart launched successfully"
        else
            echo "$ts WARNING: restart did not produce a peppar_fix_engine process"
        fi
        python3 -c "
import json
with open('$STATE_FILE') as f: d = json.load(f)
d['attempt_count'] = $new_attempt
d['last_attempt_unix'] = $now
with open('$STATE_FILE', 'w') as f: json.dump(d, f, indent=2)
"
        ;;

    stop)
        if [[ -f "$STATE_FILE" ]]; then
            rm -f "$STATE_FILE"
            echo "$ts removed $STATE_FILE — supervisor stopped"
        fi
        ;;

    *)
        echo "unknown action: $ACTION (use init|check|stop)" >&2
        exit 2
        ;;
esac
