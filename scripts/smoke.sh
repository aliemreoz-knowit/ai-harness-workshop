#!/usr/bin/env bash
# smoke.sh -- real-socket end-to-end run on 127.0.0.1 (not a gate; loopback is the
# deterministic in-process version of the same thing).
#
# Usage: scripts/smoke.sh [build-dir] [seconds]       default build-host, 5 s
#
# 1. tinystreamd --source synth          -> tsrecv --truth synth      (direct)
# 2. tinystreamd -> tsnetem loss=5%      -> tsrecv --announce-port    (relay)
# Each receiver writes metrics.json + frame.pgm under build-smoke/; the script
# prints the key metrics and fails when a run decoded nothing or showed a wrong
# picture (exact-match below 100 % on the clean run).
# Runs inside the dev container; on the host it re-executes through dockerbuild.sh.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ "${TS_IN_CONTAINER:-0}" != "1" ] && [ ! -f /.dockerenv ]; then
    exec "$SCRIPT_DIR/dockerbuild.sh" scripts/smoke.sh "$@"
fi
cd "$REPO" || exit 2

B="${1:-build-host}"
SECS="${2:-5}"
OUT="build-smoke"
mkdir -p "$OUT"
for bin in tinystreamd tsrecv tsnetem; do
    [ -x "$B/$bin" ] || { echo "missing $B/$bin (build the preset first)"; exit 2; }
done

PIDS=()
cleanup() { for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done; wait 2>/dev/null; }
trap cleanup EXIT

metric() { # metric <file> <key>: LAST numeric value of "key": (greedy match; keys used here are unique)
    sed -n "s/.*\"$2\": *\([0-9.]*\).*/\1/p" "$1" | head -1
}

report() { # report <tag> <metrics.json>
    local f="$2" k line="$1"
    for k in frames_decoded fps loss_pct residual_loss_pct nacks_sent retx_rx crc_fail freezes exact_match_pct; do
        line="$line $k=$(metric "$f" "$k")"
    done
    echo "$line"
}

rc=0

# ---- 1. direct ----------------------------------------------------------------
"$B/tinystreamd" --source synth --seed 1 --udp-port 5600 --tcp-port 5601 >"$OUT/daemon1.log" 2>&1 &
PIDS+=($!)
sleep 0.3
"$B/tsrecv" --server 127.0.0.1:5601 --udp-port 5610 --seconds "$SECS" --truth synth:1 \
    --metrics "$OUT/direct.json" --frame "$OUT/direct.pgm" --quiet </dev/null >"$OUT/rx1.log" 2>&1 || rc=1
report direct "$OUT/direct.json"
[ "$(metric "$OUT/direct.json" frames_decoded)" -gt 0 ] 2>/dev/null || { echo "direct: nothing decoded"; rc=1; }
[ "$(metric "$OUT/direct.json" exact_match_pct)" = 100 ] || { echo "direct: exact match below 100 %"; rc=1; }
cleanup; PIDS=()

# ---- 2. through tsnetem (5 % loss both ways) ----------------------------------
"$B/tinystreamd" --source synth --seed 1 --udp-port 5600 --tcp-port 5601 >"$OUT/daemon2.log" 2>&1 &
PIDS+=($!)
"$B/tsnetem" --listen 127.0.0.1:5620 --forward 127.0.0.1:5610 --spec loss=5 --seed 7 --quiet \
    </dev/null >"$OUT/netem.log" 2>&1 &
PIDS+=($!)
sleep 0.3
"$B/tsrecv" --server 127.0.0.1:5601 --udp-port 5610 --announce-port 5620 --seconds "$SECS" --truth synth:1 \
    --metrics "$OUT/netem.json" --frame "$OUT/netem.pgm" --quiet </dev/null >"$OUT/rx2.log" 2>&1 || rc=1
report netem5 "$OUT/netem.json"
[ "$(metric "$OUT/netem.json" frames_decoded)" -gt 0 ] 2>/dev/null || { echo "netem: nothing decoded"; rc=1; }
[ "$(metric "$OUT/netem.json" nacks_sent)" -gt 0 ] 2>/dev/null || { echo "netem: no NACKs at 5 % loss (relay bypassed?)"; rc=1; }

[ $rc -eq 0 ] && echo "smoke PASS (outputs in $OUT/)" || echo "smoke FAIL (logs in $OUT/)"
exit $rc
