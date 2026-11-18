#!/usr/bin/env bash
# device.sh -- the simulated device: the stream of the projector, on this laptop.
#
# Usage: scripts/device.sh start [--source file|synth] [--ref REF] [--port 8000] [--second-viewer [--viewer2-rate <kbps>]] [--dry-run]
#        scripts/device.sh pipe-demo [--ref REF] [--port 8000] [--second-viewer [--viewer2-rate <kbps>]] [--dry-run]
#        scripts/device.sh send keyframe [--dry-run]
#        scripts/device.sh send keyframe --one-shot [--dry-run]
#        scripts/device.sh send set-rate=<kbps> [--dry-run]
#        scripts/device.sh stop
#
# start     one dev container named ts-device, port <port> published to the host:
#             tinystreamd   --source file:tests/data/poster_30f.y8 (or synth), udp 5700 / tcp 5701
#             tsnetem       relay for DATA/NACK, --slider on THIS terminal (5, 0, +, -, burst 50,
#                           blackout 1s, pull, plug, flip=2, s, q; q ends the device)
#             tsrecv        writes .harness/board/metrics.json and frame.pgm for the board
#             http.server   build-device/web only (mission.html + board files), on 127.0.0.1:<port>
#           and prints the board address: http://localhost:<port>/tools/board/mission.html
#           The daemon, relay and receiver are the host build of REF (git archive into
#           build-device/<sha>, built once per commit); default: the working tree.
# --viewer2-rate <kbps>  (with --second-viewer) viewer 2 keeps that rate: tsrecv --kbps <kbps> with a rate
#           heartbeat (--heartbeat-ms 1000), as a viewer whose user lowered its rate and stays there.
# --second-viewer  a second tsrecv in the same container, a second client of the daemon from the
#           same host: udp 5611, straight to the daemon (the slider's relay carries the first viewer
#           only), board files in .harness/board2/. The board shows it as the "rx 2" thumbnail
#           (mission.html?feed2=../../.harness/board2; ?data=../../.harness/board2 shows it alone).
# pipe-demo like start, with the camera stand-in: --source pipe fed by tools/hil/pipe_source.py
#           (frames in 32 KiB pieces, the source pauses 3 s at t=4 s: the camera-stall demo).
# send keyframe  a 'k' key of viewer 1: written into the FIFO (/tmp/ts-keys) that is viewer 1's stdin, which
#           also runs a rate heartbeat (--heartbeat-ms 1000) as the hil keys check does. The board's
#           "viewer keys" tile counts it: sent, answered, ignored.
# send keyframe --one-shot | send set-rate=<kbps>  tsrecv's one-shot --send, run in ts-device with
#           docker exec (a client of its own for about a second; tsrecv prints what it sent and, for a
#           rate, what the daemon reports for it). A rate is that client's own.
# stop      removes the container ts-device.
# --dry-run prints the docker lines and the viewers' commands, and starts nothing.
#
# Missing clip (tests/data/poster_30f.y8 is not in the repository): the stand-in of
# scripts/make_clip.py is written to build-device/poster_30f.y8 and used instead.
# Hil windows are unchanged: python tools/hil/hil.py run --ref REF (target local).
# From Git Bash on Windows run it with MSYS_NO_PATHCONV=1.
set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
IMAGE="${TS_IMAGE:-tinystream-dev:latest}"
NAME=ts-device
CLIP=tests/data/poster_30f.y8
WEB=build-device/web             # the web root: mission.html and the board files, nothing else
TREE_LABEL=tinystream.device.tree
KEYS_FIFO=/tmp/ts-keys          # inside ts-device: viewer 1's stdin

die() { echo "device.sh: $*" >&2; exit 2; }
usage() { sed -n '2,9p' "${BASH_SOURCE[0]}" | sed 's/^# \{0,1\}//' >&2; exit 2; }

# viewer <tree> <1|2>: the tsrecv command of a viewer, in RX. Viewer 1 takes DATA through the
# slider's relay (announces 5620) and writes board/; viewer 2 (--second-viewer) is another client
# from the same host, udp 5611 straight to the daemon, and writes board2/.
viewer() {
    local rx="$1/build-host/tsrecv"
    if [ "$2" = 1 ]; then
        RX=("$rx" --server 127.0.0.1:5701 --udp-port 5610 --announce-port 5620 --status
            --heartbeat-ms 1000
            --metrics "$WEB/.harness/board/metrics.json" --frame "$WEB/.harness/board/frame.pgm")
    else
        RX=("$rx" --server 127.0.0.1:5701 --udp-port 5611 --status
            --metrics "$WEB/.harness/board2/metrics.json" --frame "$WEB/.harness/board2/frame.pgm")
        [ "${V2RATE:-0}" = 0 ] || RX+=(--kbps "$V2RATE" --heartbeat-ms 1000)
    fi
}

# ---- inside the container ---------------------------------------------------------------
if [ "${1:-}" = "_inside" ]; then
    shift
    TREE="$1"; SPEC="$3"          # $2 is --source
    SECOND=0; [ "${4:-}" != "--second-viewer" ] || SECOND=1
    V2RATE=0; [ "${5:-}" != "--viewer2-rate" ] || V2RATE="$6"
    SOURCE="${SPEC%%:*}"
    cd "$REPO_DIR"
    B="$TREE/build-host"
    rm -rf "$WEB"; mkdir -p "$WEB/tools/board" "$WEB/.harness/board"
    [ "$SECOND" = 0 ] || mkdir -p "$WEB/.harness/board2"
    if [ -f "$TREE/tools/board/mission.html" ]; then cp "$TREE/tools/board/mission.html" "$WEB/tools/board/"
    else cp tools/board/mission.html "$WEB/tools/board/"; fi
    clip="$TREE/$CLIP"
    if [ "$SOURCE" = "file" ] || [ "$SOURCE" = "pipe" ]; then
        if [ ! -f "$clip" ]; then
            clip="build-device/poster_30f.y8"
            echo "[device] source: stand-in clip ($CLIP not found)"
            [ -f "$clip" ] || python3 scripts/make_clip.py --out "$clip" >/dev/null
        fi
    fi
    LOG=build-device/logs; mkdir -p "$LOG"
    PIDS=()
    cleanup() { for p in ${PIDS[@]+"${PIDS[@]}"}; do kill "$p" 2>/dev/null || true; done; wait 2>/dev/null || true; }
    trap cleanup EXIT INT TERM

    python3 -m http.server 8000 --directory "$WEB" >"$LOG/http.log" 2>&1 &
    PIDS+=($!)
    case "$SOURCE" in
        file)  "$B/tinystreamd" --source "file:$clip" --udp-port 5700 --tcp-port 5701 >"$LOG/daemon.log" 2>&1 & ;;
        synth) "$B/tinystreamd" --source synth --udp-port 5700 --tcp-port 5701 >"$LOG/daemon.log" 2>&1 & ;;
        pipe)  python3 tools/hil/pipe_source.py --clip "$clip" --fps 30 --seconds 0 --chunk 32768 \
                   --chunk-gap-ms 5 --pause-at 4 --pause-s 3 2>"$LOG/pipe.log" |
               "$B/tinystreamd" --source pipe --udp-port 5700 --tcp-port 5701 >"$LOG/daemon.log" 2>&1 & ;;
    esac
    PIDS+=($!)
    sleep 1
    viewer "$TREE" 1
    # viewer 1 reads its keys from a FIFO (opened read-write, so it never sees EOF); `send keyframe` writes k
    rm -f "$KEYS_FIFO"; mkfifo "$KEYS_FIFO"; exec 3<>"$KEYS_FIFO"
    "${RX[@]}" <&3 >"$LOG/rx.log" 2>&1 &
    PIDS+=($!)
    if [ "$SECOND" = 1 ]; then
        sleep 1                   # it joins a running stream
        viewer "$TREE" 2
        "${RX[@]}" </dev/null >"$LOG/rx2.log" 2>&1 &
        PIDS+=($!)
        echo "[device] second viewer: udp 5611 straight to the daemon, board files in .harness/board2/, log rx2.log"
    fi
    echo "[device] source $SOURCE; logs in build-device/logs/; slider: 5, 0, +, -, burst 50, blackout 1s, pull, plug, s, q"
    "$B/tsnetem" --listen 0.0.0.0:5620 --forward 127.0.0.1:5610 --peer 127.0.0.1:5700 --spec loss=0 --slider --stats-ms 5000
    exit 0
fi

# ---- on the host ------------------------------------------------------------------------
CMD="${1:-}"
[ -n "$CMD" ] || usage
shift
SOURCE=file; REF=""; PORT=8000; DRY=0; SECOND=0; V2RATE=""
case "$CMD" in
    start|pipe-demo|stop|send) ;;
    *) usage ;;
esac

if [ "$CMD" = "send" ]; then
    WHAT="${1:-}"
    [ $# -eq 0 ] || shift
    case "$WHAT" in
        keyframe) ;;
        set-rate=*)
            kbps="${WHAT#set-rate=}"
            case "$kbps" in ''|*[!0-9]*) die "send set-rate=<kbps>: <kbps> must be a number" ;; esac
            [ "$kbps" -gt 0 ] 2>/dev/null || die "send set-rate=<kbps>: <kbps> must be above 0" ;;
        *) die "send keyframe | send set-rate=<kbps>" ;;
    esac
    ONESHOT=0
    while [ $# -gt 0 ]; do
        case "$1" in
            --dry-run) DRY=1; shift ;;
            --one-shot) ONESHOT=1; shift ;;
            *) usage ;;
        esac
    done
    [ "$ONESHOT" = 0 ] || [ "$WHAT" = keyframe ] || die "--one-shot is for send keyframe"
    export MSYS_NO_PATHCONV=1
    if [ "$WHAT" = keyframe ] && [ "$ONESHOT" = 0 ]; then
        # a 'k' into viewer 1's stdin FIFO: the viewer's own key, counted by the board's "viewer keys" tile
        KEY=(docker exec "$NAME" sh -c "printf k > $KEYS_FIFO")
        if [ "$DRY" = 1 ]; then echo "${KEY[*]}"; exit 0; fi
        "${KEY[@]}" || die "$NAME is not running (or was started by an older device.sh): scripts/device.sh start"
        echo "device.sh: k sent to viewer 1 (the board's \"viewer keys\" tile counts it)"
        exit 0
    fi
    # the running device's tree (its build of the ref), from the label start put on the container
    TREE=""
    if command -v docker >/dev/null 2>&1; then
        TREE="$(docker inspect -f "{{index .Config.Labels \"$TREE_LABEL\"}}" "$NAME" 2>/dev/null)" || TREE=""
    fi
    [ "$TREE" != "<no value>" ] || TREE=""
    if [ -z "$TREE" ]; then
        [ "$DRY" = 1 ] || die "$NAME is not running (or was started by an older device.sh): scripts/device.sh start"
        TREE="<tree>"
    fi
    SEND=(docker exec "$NAME" "$TREE/build-host/tsrecv" --server 127.0.0.1:5701 --send "$WHAT")
    if [ "$DRY" = 1 ]; then
        echo "${SEND[*]}"
        exit 0
    fi
    exec "${SEND[@]}"
fi

while [ $# -gt 0 ]; do
    case "$1" in
        --source)  [ $# -ge 2 ] || die "--source needs a value"; SOURCE="$2"; shift 2 ;;
        --ref)     [ $# -ge 2 ] || die "--ref needs a value"; REF="$2"; shift 2 ;;
        --port)    [ $# -ge 2 ] || die "--port needs a value"; PORT="$2"; shift 2 ;;
        --second-viewer) SECOND=1; shift ;;
        --viewer2-rate) [ $# -ge 2 ] || die "--viewer2-rate needs a value"; V2RATE="$2"; shift 2 ;;
        --dry-run) DRY=1; shift ;;
        *) usage ;;
    esac
done
case "$SOURCE" in file|synth) ;; *) die "--source must be file or synth" ;; esac
case "$PORT" in ''|*[!0-9]*) die "--port must be a number" ;; esac
[ "$CMD" != "pipe-demo" ] || SOURCE=pipe
if [ -n "$V2RATE" ]; then
    case "$V2RATE" in ''|*[!0-9]*|0*) die "--viewer2-rate <kbps> must be a number above 0" ;; esac
    [ "$SECOND" = 1 ] || die "--viewer2-rate needs --second-viewer"
fi

if [ "$CMD" = "stop" ]; then
    docker rm -f "$NAME"
    exit 0
fi

cd "$REPO_DIR"
if HOST_DIR="$(pwd -W 2>/dev/null)" && [ -n "$HOST_DIR" ]; then :; else HOST_DIR="$(pwd)"; fi

TREE=.
if [ -n "$REF" ]; then
    SHA="$(git rev-parse --verify --quiet "$REF^{commit}")" || die "unknown ref: $REF"
    TREE="build-device/${SHA:0:12}"
fi

SPEC="$SOURCE"
[ "$SOURCE" != "file" ] || SPEC="file:$CLIP"
TTY=(-i)
if [ -t 0 ] && [ -t 1 ]; then TTY=(-it); fi
# files written in the repo belong to the host user (Docker Desktop on Windows ignores owners)
# arrays that can be empty are expanded as ${A[@]+"${A[@]}"}: bash older than 4.4 (macOS's 3.2) calls an empty
# "${A[@]}" an unbound variable under set -u
USER_ARGS=()
case "${TS_DEVICE_UNAME:-$(uname -s)}" in MINGW*|MSYS*|CYGWIN*) ;; *) USER_ARGS=(-u "$(id -u):$(id -g)") ;; esac
INSIDE_ARGS=()
[ "$SECOND" = 0 ] || INSIDE_ARGS=(--second-viewer)
[ -z "$V2RATE" ] || INSIDE_ARGS+=(--viewer2-rate "$V2RATE")
# the build runs in its own container with the same user, so build-host/ is host-owned too
# (scripts/dockerbuild.sh passes no -u)
if [ -n "$REF" ]; then
    BUILD_SH="sh tools/hil/build.sh $TREE host tinystreamd tsrecv tsnetem"
else
    BUILD_SH="cmake --preset host >/dev/null && cmake --build --preset host --target tinystreamd tsrecv tsnetem"
fi
BUILD=(docker run --rm --platform linux/amd64 ${USER_ARGS[@]+"${USER_ARGS[@]}"} -e HOME=/tmp -e TS_IN_CONTAINER=1
       -v "$HOST_DIR:/work" -w /work "$IMAGE" sh -c "$BUILD_SH")
# published on loopback only; the web server serves build-device/web, not the repository.
# The label names the tree, so `send` runs the device's own tsrecv.
RUN=(docker run --rm --platform linux/amd64 "${TTY[@]}" ${USER_ARGS[@]+"${USER_ARGS[@]}"} --name "$NAME" -p "127.0.0.1:$PORT:8000"
     --label "$TREE_LABEL=$TREE" -e HOME=/tmp -e TS_IN_CONTAINER=1 -v "$HOST_DIR:/work" -w /work "$IMAGE"
     bash scripts/device.sh _inside "$TREE" --source "$SPEC" ${INSIDE_ARGS[@]+"${INSIDE_ARGS[@]}"})
BOARD="http://localhost:$PORT/tools/board/mission.html"
[ "$SECOND" = 0 ] || BOARD="$BOARD?feed2=../../.harness/board2"
second_note() {
    [ "$SECOND" = 0 ] || echo "second viewer: the \"rx 2\" thumbnail on that board" \
        "(alone: http://localhost:$PORT/tools/board/mission.html?data=../../.harness/board2)"
}

if [ "$DRY" = 1 ]; then
    echo "build: ${BUILD[*]}"
    echo "${RUN[*]}"
    viewer "$TREE" 1
    echo "keys: mkfifo $KEYS_FIFO"
    echo "viewer: ${RX[*]} < $KEYS_FIFO"
    if [ "$SECOND" = 1 ]; then
        viewer "$TREE" 2
        echo "viewer 2: ${RX[*]}"
    fi
    echo "serving build-device/web (mission.html and the board files only), on 127.0.0.1"
    echo "open $BOARD"
    second_note
    exit 0
fi

command -v docker >/dev/null || die "docker not found"
docker info >/dev/null 2>&1 || die "docker is not running (start Docker Desktop)"
if docker ps -a --format '{{.Names}}' | grep -qx "$NAME"; then
    die "$NAME already exists: scripts/device.sh stop"
fi

export MSYS_NO_PATHCONV=1
if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    scripts/dockerbuild.sh true      # builds the image when it is missing
fi
if [ -n "$REF" ]; then
    # -f, not -x: Git Bash sees no exec bit on the container's binaries in a Windows checkout
    if [ ! -f "$TREE/build-host/tinystreamd" ] || [ ! -f "$TREE/build-host/tsrecv" ] || [ ! -f "$TREE/build-host/tsnetem" ]; then
        echo "[device] building $REF ($SHA) into $TREE"
        rm -rf "$TREE"; mkdir -p "$TREE"
        git archive --format=tar "$SHA" | tar -x -C "$TREE"
        "${BUILD[@]}"
    fi
else
    echo "[device] building the working tree (host preset)"
    "${BUILD[@]}"
fi
echo "open $BOARD"
second_note
exec "${RUN[@]}"
