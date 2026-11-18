#!/usr/bin/env bash
# opening_demo.sh -- the projector stream of the workshop opening (real sockets on
# 127.0.0.1, inside the dev container): tinystreamd -> tsnetem -> tsrecv, with the
# receiver's metrics and picture written where the mission board reads them.
#
# Usage: scripts/opening_demo.sh [build-dir] [seconds]      default build-host, 300 s
#
#   tinystreamd --source synth --noise-after-s $NOISE_AFTER_S [--no-loss-adapt]
#               (restarted after a crash like the systemd unit on the Pi,
#               Restart=always, after 1 s)
#   tsnetem     --spec $NETEM_SPEC                              (relay for the DATA/NACK path)
#   tsrecv      --heartbeat-ms $HEARTBEAT_MS --press $PRESS --status
#               --metrics .harness/board/metrics.json --frame .harness/board/frame.pgm
#
# MODE=crash (default) runs the daemon with --no-loss-adapt: the quality stays at step 1
# whatever the NACK ratio, so the delayed noise burst crashes the daemon about once a
# minute (the picture freezes, the viewer reconnects, the script restarts the daemon).
# MODE=coarse leaves the loss adaptation on: a coarse picture instead of the crash.
# --noise-after-s keeps the synth noise burst (the most expensive frame for the codec)
# out of the first NOISE_AFTER_S seconds of the stream. --heartbeat-ms sends a rate
# heartbeat (a SET_RATE at the current rate) every N ms;
# --press presses keys on a timer, so nobody has to type during the opening. The
# status line (one per second: fps, kbps, CRC / NACK / retransmit counters, keyframe
# requests answered or ignored, viewers reported by the daemon) goes to the terminal
# and to build-opening/rx.log; the board shows the same numbers.
#
# Board: on the host, from the repository root, `python -m http.server 8000`, then
# http://localhost:8000/tools/board/mission.html
#
# Environment: MODE (crash | coarse, default crash), NOISE_AFTER_S (60), NETEM_SPEC
# (loss=0), HEARTBEAT_MS (1000), PRESS (k:7000), KBPS (4000), PORT_BASE (5600),
# DAEMON_ARGS (extra tinystreamd options). Logs in build-opening/.
# Runs inside the dev container; on the host it re-executes through dockerbuild.sh.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"
if [ "${TS_IN_CONTAINER:-0}" != "1" ] && [ ! -f /.dockerenv ]; then
    export TS_OPENING_MODE="${MODE:-crash}" \
           TS_OPENING_NOISE_AFTER_S="${NOISE_AFTER_S:-60}" TS_OPENING_NETEM_SPEC="${NETEM_SPEC:-loss=0}" \
           TS_OPENING_HEARTBEAT_MS="${HEARTBEAT_MS:-1000}" TS_OPENING_PRESS="${PRESS:-k:7000}" \
           TS_OPENING_KBPS="${KBPS:-4000}" TS_OPENING_PORT_BASE="${PORT_BASE:-5600}" \
           TS_OPENING_DAEMON_ARGS="${DAEMON_ARGS:-}"
    exec "$SCRIPT_DIR/dockerbuild.sh" scripts/opening_demo.sh "$@"
fi
cd "$REPO" || exit 2

B="${1:-build-host}"
SECS="${2:-300}"
MODE="${TS_OPENING_MODE:-${MODE:-crash}}"
NOISE_AFTER_S="${TS_OPENING_NOISE_AFTER_S:-${NOISE_AFTER_S:-60}}"
NETEM_SPEC="${TS_OPENING_NETEM_SPEC:-${NETEM_SPEC:-loss=0}}"
HEARTBEAT_MS="${TS_OPENING_HEARTBEAT_MS:-${HEARTBEAT_MS:-1000}}"
PRESS="${TS_OPENING_PRESS:-${PRESS:-k:7000}}"
KBPS="${TS_OPENING_KBPS:-${KBPS:-4000}}"
P="${TS_OPENING_PORT_BASE:-${PORT_BASE:-5600}}"
DAEMON_ARGS="${TS_OPENING_DAEMON_ARGS:-${DAEMON_ARGS:-}}"
case "$MODE" in
    crash)  MODE_ARGS="--no-loss-adapt" ;;
    coarse) MODE_ARGS="" ;;
    *) echo "[opening] MODE must be crash or coarse, not '$MODE'"; exit 2 ;;
esac
OUT="build-opening"
BOARD=".harness/board"
mkdir -p "$OUT" "$BOARD"

if [ ! -x "$B/tinystreamd" ] || [ ! -x "$B/tsrecv" ] || [ ! -x "$B/tsnetem" ]; then
    echo "[opening] building the host preset"
    scripts/build.sh host >"$OUT/build.log" 2>&1 || { echo "[opening] build failed, see $OUT/build.log"; exit 2; }
    B="build-host"
fi

UDP=$P; TCP=$((P + 1)); RX=$((P + 10)); RELAY=$((P + 20))
: >"$OUT/daemon.log"; : >"$OUT/restarts.log"
rm -f "$OUT/stop"

daemon_loop() {
    local n=0 rc
    while [ ! -f "$OUT/stop" ]; do
        # shellcheck disable=SC2086  # DAEMON_ARGS is a word list on purpose
        "$B/tinystreamd" --source synth --seed 1 --noise-after-s "$NOISE_AFTER_S" $MODE_ARGS $DAEMON_ARGS \
            --udp-port "$UDP" --tcp-port "$TCP" >>"$OUT/daemon.log" 2>&1 &
        echo $! >"$OUT/daemon.pid"
        wait $!
        rc=$?
        [ -f "$OUT/stop" ] && break
        n=$((n + 1))
        echo "$(date +%T) daemon exited with $rc (restart $n in 1 s)" | tee -a "$OUT/restarts.log" >&2
        sleep 1
    done
}

PIDS=()
cleanup() {
    touch "$OUT/stop"
    [ -f "$OUT/daemon.pid" ] && kill "$(cat "$OUT/daemon.pid")" 2>/dev/null
    for p in "${PIDS[@]}"; do kill "$p" 2>/dev/null; done
    wait 2>/dev/null
}
trap cleanup EXIT INT TERM

echo "[opening] $MODE mode: daemon (noise after ${NOISE_AFTER_S} s${MODE_ARGS:+, $MODE_ARGS}) -> tsnetem ($NETEM_SPEC) -> tsrecv (heartbeat ${HEARTBEAT_MS} ms, press $PRESS), ${SECS} s"
daemon_loop &
PIDS+=($!)
"$B/tsnetem" --listen "127.0.0.1:$RELAY" --forward "127.0.0.1:$RX" --spec "$NETEM_SPEC" --seed 7 --quiet \
    </dev/null >"$OUT/netem.log" 2>&1 &
PIDS+=($!)
sleep 0.5
"$B/tsrecv" --server "127.0.0.1:$TCP" --udp-port "$RX" --announce-port "$RELAY" --kbps "$KBPS" \
    --seconds "$SECS" --heartbeat-ms "$HEARTBEAT_MS" --press "$PRESS" --status \
    --metrics "$BOARD/metrics.json" --frame "$BOARD/frame.pgm" </dev/null 2>&1 | tee "$OUT/rx.log"
echo "[opening] done: $(wc -l <"$OUT/restarts.log") daemon restart(s); logs in $OUT/, metrics in $BOARD/metrics.json"
