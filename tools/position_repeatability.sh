#!/usr/bin/env bash
# position_repeatability.sh — PPP position convergence repeatability test
#
# Runs peppar-fix N times from cold start (no prior position), collecting
# the converged position from each run.  Uses the standard peppar-fix
# orchestration wrapper — no special scripts or modes.
#
# Usage:
#   ./tools/position_repeatability.sh                    # defaults
#   ./tools/position_repeatability.sh --runs 5 --duration 600
#
# Options:
#   --runs N          Number of runs (default: 10)
#   --duration S      Seconds per run (default: 1800 = 30 min)
#   --output DIR      Output directory (default: data/pos-repeat-YYYYMMDD-HHMM)
#
# Requirements:
#   - peppar-fix configured for your host (config/<hostname>.toml + ntrip.conf)
#   - No --known-pos in host config (position must bootstrap from scratch)
#
# Each run produces: run-NN/position.json, run-NN/peppar-fix.log
# After all runs:    summary.json with scatter statistics

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(cd "$SCRIPT_DIR/.." && pwd)"

RUNS=10
DURATION=1800
OUTPUT=""

while [[ $# -gt 0 ]]; do
    case "$1" in
        --runs)     RUNS="$2"; shift 2 ;;
        --duration) DURATION="$2"; shift 2 ;;
        --output)   OUTPUT="$2"; shift 2 ;;
        -h|--help)  sed -n '2,/^$/p' "$0" | sed 's/^# \?//'; exit 0 ;;
        *)          echo "Unknown option: $1" >&2; exit 1 ;;
    esac
done

if [[ -z "$OUTPUT" ]]; then
    OUTPUT="$REPO_ROOT/data/pos-repeat-$(date +%Y%m%d-%H%M)"
fi
mkdir -p "$OUTPUT"

echo "=== Position Repeatability Test ==="
echo "  Runs: $RUNS x ${DURATION}s"
echo "  Output: $OUTPUT"
echo ""

for ((i=1; i<=RUNS; i++)); do
    RUN_DIR="$OUTPUT/run-$(printf '%02d' $i)"
    mkdir -p "$RUN_DIR"
    POS_FILE="$RUN_DIR/position.json"

    echo "--- Run $i/$RUNS ($(date)) ---"

    # Cold start: delete position file so peppar-fix bootstraps from scratch
    rm -f "$POS_FILE"

    # Run peppar-fix with this run's position file.  It will:
    #   1. Configure receiver
    #   2. Bootstrap position (PPP, ~90s convergence)
    #   3. Run steady state for the remaining duration
    #   4. Save position.json when sigma < 0.1m
    bash "$REPO_ROOT/scripts/peppar-fix" \
        --position-file "$POS_FILE" \
        --duration "$DURATION" \
        > "$RUN_DIR/peppar-fix.log" 2>&1 || true

    if [[ -f "$POS_FILE" ]]; then
        echo "  $(python3 -c "
import json, sys
d = json.load(open('$POS_FILE'))
print(f\"Converged: {d['lat']:.7f}, {d['lon']:.7f}, {d['alt_m']:.3f}m  sigma={d.get('sigma_m','?')}m\")
")"
    else
        echo "  FAILED — no position file"
    fi

    if [[ $i -lt $RUNS ]]; then
        echo "  Resting 30s..."
        sleep 30
    fi
done

# Summarize
echo ""
echo "=== Summary ==="
python3 << PYEOF
import json, os, glob
import numpy as np

runs = sorted(glob.glob("$OUTPUT/run-*/position.json"))
results = []
for path in runs:
    run = os.path.basename(os.path.dirname(path))
    with open(path) as f:
        d = json.load(f)
    d["run"] = run
    results.append(d)

if not results:
    print("No converged positions")
    exit(0)

lats = np.array([r["lat"] for r in results])
lons = np.array([r["lon"] for r in results])
alts = np.array([r["alt_m"] for r in results])

cos_lat = np.cos(np.radians(np.mean(lats)))
std_n = np.std(lats) * 111319
std_e = np.std(lons) * 111319 * cos_lat
std_u = np.std(alts)

print(f"  Converged: {len(results)}/{len(glob.glob('$OUTPUT/run-*'))} runs")
print(f"  North scatter: {std_n:.3f}m (1σ)")
print(f"  East scatter:  {std_e:.3f}m (1σ)")
print(f"  Up scatter:    {std_u:.3f}m (1σ)")
print(f"  Mean alt:      {np.mean(alts):.3f}m")

summary = {
    "n_runs": len(results),
    "mean_lat": float(np.mean(lats)),
    "mean_lon": float(np.mean(lons)),
    "mean_alt_m": float(np.mean(alts)),
    "std_north_m": float(std_n),
    "std_east_m": float(std_e),
    "std_up_m": float(std_u),
    "runs": results,
}
out = os.path.join("$OUTPUT", "summary.json")
with open(out, "w") as f:
    json.dump(summary, f, indent=2)
print(f"  Written: {out}")
PYEOF

echo ""
echo "Visualize: python3 tools/plot_position_scatter.py $OUTPUT/summary.json"
