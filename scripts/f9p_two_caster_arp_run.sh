#!/bin/bash
# Sequential two-caster ARP run on clkPoC3.
#
# Each caster gets a 30-min run with both F9Ps fed from the SAME
# NTRIP stream (broadcast pattern — works around the Leica caster's
# one-data-stream-per-source-IP rule).  The two F9Ps share UFO1 via
# the lab splitter; their per-caster mean ECEF differences expose
# receiver+port bias.  The diff between the two casters' means
# (averaged across F9Ps) is the cross-caster bias.
#
# Why both F9Ps every run instead of pure single-rover sequential:
# inside-caster receiver+port-bias check is essentially free here
# (one extra USB write per RTCM frame).  It catches "F9P-2 has a
# Y-axis bias unique to its splitter port" that would otherwise be
# absorbed into the cross-caster diff.
#
# Per dayplan I-220529-charlie #1 follow-on (UFO1 ARP fix), 2026-04-29.

set -euo pipefail

cd "$(dirname "$0")/.."
source venv/bin/activate

LOG_DIR="data/cors-arp-$(date -u +%Y%m%dT%H%M%SZ)"
mkdir -p "$LOG_DIR"

DURATION="${DURATION:-1800}"
USER_ID="${NTRIP_USER:-VANVALZAH}"
PASS="${NTRIP_PASS:-8888}"

echo "Two-caster ARP run, $((DURATION / 60)) min per caster, log dir: $LOG_DIR"

run_one() {
    local mount=$1 caster=$2 port=$3 logname=$4
    echo
    echo "=== $logname: $mount on both F9Ps ==="
    python3 scripts/f9p_dual_cors_rover.py \
        --r1-port /dev/ttyACM0 --r1-mount "$mount" \
                                --r1-caster "$caster" --r1-port-tcp "$port" \
        --r2-port /dev/ttyACM1 \
        --shared \
        --user "$USER_ID" --password "$PASS" \
        --duration "$DURATION" --report-every 120 \
        2>&1 | tee "$LOG_DIR/$logname.log"
}

# Run 1: NAPERVILLE on ISTHA (4.0 km, RTCM 3.1 MSM5, GPS+GLO+GAL)
run_one "NAPERVILLE-RTCM3.1-MSM5" "50.149.86.86" "12054" "naperville"

# Run 2: ELMHURST on DuPage (12.7 km, plain RTCM 3, GPS+GLO no GAL)
run_one "ELMHURST-RTCM3"          "50.149.86.86" "12055" "elmhurst"

echo
echo "Done.  Logs in $LOG_DIR"
