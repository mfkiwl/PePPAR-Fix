#!/usr/bin/env bash
# benchmark_position_finder.sh — PPP position convergence benchmark
#
# 2x2 matrix: (warm/cold start) x (NTRIP eph/F9T broadcast eph)
# 5 runs per cell = 20 runs total.
#
# Cells run in order (fastest first, longest last):
#   1. warm  + NTRIP eph  — receiver warm, ephemeris instant
#   2. warm  + F9T eph    — receiver warm, ephemeris from signal
#   3. cold  + NTRIP eph  — factory reset, ephemeris instant
#   4. cold  + F9T eph    — factory reset, ephemeris from signal (worst case)
#
# Usage:
#   ./benchmark_position_finder.sh                # full 2x2, 5 runs each
#   ./benchmark_position_finder.sh -n 2           # 2 runs per cell (quick test)
#   ./benchmark_position_finder.sh --cell warm_ntrip   # single cell only
#   ./benchmark_position_finder.sh --cell cold_f9t -n 3
#
# Prerequisites:
#   - Python 3 with pyubx2, pyserial, numpy
#   - /dev/gnss-top (F9T serial)
#   - ntrip.conf in rig root with caster/user/password and SSR mount

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "$0")" && pwd)"
RIG_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
FIND_POS="$SCRIPT_DIR/peppar_find_position.py"
CONFIGURE="$SCRIPT_DIR/configure_f9t.py"

# Defaults
SERIAL="${SERIAL:-/dev/gnss-top}"
BAUD="${BAUD:-115200}"
NTRIP_CONF="${NTRIP_CONF:-$RIG_DIR/ntrip.conf}"
EPH_MOUNT="BCEP00BKG0"           # NTRIP broadcast ephemeris
TIMEOUT="${TIMEOUT:-1800}"        # 30 min per run
SIGMA="${SIGMA:-0.1}"             # 10 cm convergence threshold
N_RUNS=5
CELL=""                           # empty = all cells
OUTDIR="$RIG_DIR/data/benchmark_$(date +%Y%m%d_%H%M%S)"

# Parse args
while [[ $# -gt 0 ]]; do
    case "$1" in
        -n)           N_RUNS="$2"; shift 2 ;;
        --cell)       CELL="$2"; shift 2 ;;
        --serial)     SERIAL="$2"; shift 2 ;;
        --timeout)    TIMEOUT="$2"; shift 2 ;;
        --sigma)      SIGMA="$2"; shift 2 ;;
        --eph-mount)  EPH_MOUNT="$2"; shift 2 ;;
        --outdir)     OUTDIR="$2"; shift 2 ;;
        -h|--help)
            sed -n '2,/^$/p' "$0" | sed 's/^# \?//'
            exit 0
            ;;
        *) echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

mkdir -p "$OUTDIR"

echo "=== PPP Position Finder Benchmark ==="
echo "  Serial:    $SERIAL @ $BAUD"
echo "  NTRIP:     $NTRIP_CONF"
echo "  Eph mount: $EPH_MOUNT"
echo "  Timeout:   ${TIMEOUT}s per run"
echo "  Sigma:     ${SIGMA}m"
echo "  Runs:      $N_RUNS per cell"
echo "  Cell:      ${CELL:-all}"
echo "  Output:    $OUTDIR"
echo ""


# ── Helpers ───────────────────────────────────────────────────────────── #

# Run one position-finder trial
# Args: $1=cell_label  $2=run_number  $3=extra_args (space-separated)
run_trial() {
    local label="$1"
    local run_num="$2"
    local extra_args="$3"
    local csv_file="$OUTDIR/${label}_run${run_num}.csv"
    local json_file="$OUTDIR/${label}_run${run_num}.json"
    local log_file="$OUTDIR/${label}_run${run_num}.log"

    echo "--- $label run $run_num/$N_RUNS ---"
    local start_ts
    start_ts=$(date +%s)

    # Build command
    local cmd="python3 $FIND_POS"
    cmd+=" --serial $SERIAL --baud $BAUD"
    cmd+=" --ntrip-conf $NTRIP_CONF"
    cmd+=" --timeout $TIMEOUT --sigma $SIGMA"
    cmd+=" --out $csv_file -v"
    cmd+=" $extra_args"

    set +e
    eval "$cmd" > "$json_file" 2> "$log_file"
    local exit_code=$?
    set -e

    local end_ts
    end_ts=$(date +%s)
    local wall_s=$(( end_ts - start_ts ))

    if [[ $exit_code -eq 0 ]] && [[ -s "$json_file" ]]; then
        local elapsed sigma_m epochs
        elapsed=$(python3 -c "import json; d=json.load(open('$json_file')); print(d.get('elapsed_s', '?'))")
        sigma_m=$(python3 -c "import json; d=json.load(open('$json_file')); print(d.get('sigma_m', '?'))")
        epochs=$(python3 -c "import json; d=json.load(open('$json_file')); print(d.get('epochs', '?'))")
        echo "  CONVERGED in ${elapsed}s (sigma=${sigma_m}m, ${epochs} epochs)"
    else
        echo "  DID NOT CONVERGE (exit=$exit_code, wall=${wall_s}s)"
        if [[ ! -s "$json_file" ]]; then
            echo "{\"converged\": false, \"exit_code\": $exit_code, \"wall_s\": $wall_s}" > "$json_file"
        fi
    fi

    # Brief cooldown between runs
    if [[ "$run_num" -lt "$N_RUNS" ]]; then
        echo "  (5s cooldown)"
        sleep 5
    fi
}

# Factory reset and reconfigure for cold start
cold_reset() {
    local label="$1"
    local run_num="$2"
    local reset_log="$OUTDIR/${label}_reset_run${run_num}.log"

    echo "  Factory reset + reconfigure..."

    # Reset with minimal survey-in (position finder does its own convergence)
    set +e
    python3 "$CONFIGURE" "$SERIAL" \
        --survey-dur 1 \
        --survey-acc 100 \
        --target-baud "$BAUD" \
        --no-verify \
        > "$reset_log" 2>&1
    local rc=$?
    set -e

    if [[ $rc -ne 0 ]]; then
        echo "  WARNING: configure_f9t.py exited $rc (see $reset_log)"
        sleep 5
    fi

    # Wait for receiver to stabilize after reconfiguration
    echo "  Waiting 10s for receiver to stabilize..."
    sleep 10
}

# Run one cell of the matrix
# Args: $1=cell_name  $2=is_cold(true/false)  $3=extra_args
run_cell() {
    local cell_name="$1"
    local is_cold="$2"
    local extra_args="$3"

    echo ""
    echo "========================================="
    echo "  $cell_name ($N_RUNS runs)"
    echo "========================================="
    echo ""

    for i in $(seq 1 "$N_RUNS"); do
        if $is_cold; then
            cold_reset "$cell_name" "$i"
        fi
        run_trial "$cell_name" "$i" "$extra_args"
    done
}

should_run() {
    [[ -z "$CELL" ]] || [[ "$CELL" == "$1" ]]
}


# ── Run the 2x2 matrix ───────────────────────────────────────────────── #

# Cell 1: Warm start + NTRIP ephemeris (fastest expected)
if should_run "warm_ntrip"; then
    run_cell "warm_ntrip" false "--eph-mount $EPH_MOUNT"
fi

# Cell 2: Warm start + F9T broadcast ephemeris only (no NTRIP eph)
if should_run "warm_f9t"; then
    run_cell "warm_f9t" false ""
fi

# Cell 3: Cold start + NTRIP ephemeris
if should_run "cold_ntrip"; then
    run_cell "cold_ntrip" true "--eph-mount $EPH_MOUNT"
fi

# Cell 4: Cold start + F9T broadcast ephemeris only (slowest expected)
if should_run "cold_f9t"; then
    run_cell "cold_f9t" true ""
fi


# ── Summary table ─────────────────────────────────────────────────────── #

echo ""
echo "========================================="
echo "  RESULTS"
echo "========================================="

python3 - "$OUTDIR" "$N_RUNS" <<'PYEOF'
import json, os, sys

outdir = sys.argv[1]
n_runs = int(sys.argv[2])

cells = ["warm_ntrip", "warm_f9t", "cold_ntrip", "cold_f9t"]
labels = {
    "warm_ntrip": "Warm + NTRIP eph",
    "warm_f9t":   "Warm + F9T eph",
    "cold_ntrip": "Cold + NTRIP eph",
    "cold_f9t":   "Cold + F9T eph",
}

results = {}
for cell in cells:
    runs = []
    for i in range(1, n_runs + 1):
        jf = os.path.join(outdir, f"{cell}_run{i}.json")
        if os.path.exists(jf):
            try:
                with open(jf) as f:
                    runs.append(json.load(f))
            except json.JSONDecodeError:
                runs.append({"error": "parse_failed"})
    results[cell] = runs

# Per-run detail
for cell in cells:
    runs = results[cell]
    if not runs:
        continue
    print(f"\n  {labels[cell]}:")
    for i, r in enumerate(runs, 1):
        if r.get("converged"):
            print(f"    run {i}: {r['elapsed_s']:7.1f}s  sigma={r['sigma_m']:.4f}m  epochs={r['epochs']}")
        else:
            wall = r.get("wall_s", r.get("elapsed_s", "?"))
            print(f"    run {i}:    FAIL  (wall={wall}s)")

# Summary table
print("\n")
print("  ┌─────────────────────┬────────┬────────┬────────┬───────────┐")
print("  │ Cell                │  Mean  │  Min   │  Max   │ Converged │")
print("  ├─────────────────────┼────────┼────────┼────────┼───────────┤")
for cell in cells:
    runs = results[cell]
    if not runs:
        continue
    converged = [r for r in runs if r.get("converged")]
    times = [r["elapsed_s"] for r in converged]
    n_ok = len(converged)
    n_tot = len(runs)
    if times:
        mean_t = sum(times) / len(times)
        min_t = min(times)
        max_t = max(times)
        print(f"  │ {labels[cell]:<19} │ {mean_t:5.0f}s │ {min_t:5.0f}s │ {max_t:5.0f}s │   {n_ok}/{n_tot}     │")
    else:
        print(f"  │ {labels[cell]:<19} │    --  │    --  │    --  │   {n_ok}/{n_tot}     │")
print("  └─────────────────────┴────────┴────────┴────────┴───────────┘")

# Save full results
summary_path = os.path.join(outdir, "summary.json")
with open(summary_path, "w") as f:
    json.dump(results, f, indent=2)
print(f"\n  Results saved to: {outdir}/")
PYEOF
