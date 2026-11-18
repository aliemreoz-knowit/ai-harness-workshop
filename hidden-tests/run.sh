#!/usr/bin/env bash
# run.sh <path-to-tinystream> [host|asan] [ticket-id ...] -- build and run the
# hidden evaluation tests against a tinystream checkout, inside the tinystream
# dev image.
#
#   ./run.sh ../tinystream                    # host preset, every hidden test
#   ./run.sh ../tinystream asan               # same tests under ASan/UBSan
#   ./run.sh ../tinystream TS-104             # only the hidden tests of TS-104 (host)
#   ./run.sh ../tinystream asan TS-104 TS-110 # two tickets under ASan
#
# Test sources:
#   tickets/<ID>/tests/unit/test_*.c      -> tests/unit/      of the copy
#   tickets/<ID>/tests/loopback/test_*.c  -> tests/loopback/  of the copy
#   unit/test_*.c, loopback/test_*.c      -> shared hidden tests of no ticket (ticket
#                                            column "shared"; run only when no
#                                            ticket id is given)
# CMake globs both target directories, so the tests compile exactly like the
# public ones. The checkout is mounted read-only and copied to a
# container-local directory (build-* and .git left out); nothing is written to
# the checkout. A hidden file replaces a public file of the same name in the
# copy. Two selected hidden tests with the same file name are an error.
#
# Output: one line per hidden test, `PASS|FAIL <ticket> <test> <time>`, the
# ctest output of the failed tests, then `[hidden] PASS` or `[hidden] FAIL`.
# Exit status: 0 every selected hidden test passed, 1 at least one failed,
# 2 usage / unknown ticket / configure / build / registration error.
# Works from Git Bash on Windows (Docker Desktop).
set -euo pipefail

IMAGE="${TS_IMAGE:-tinystream-dev:latest}"
HT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

# ---- inside the container ----------------------------------------------------
if [ "${HT_INNER:-0}" = "1" ]; then
    preset="$1"
    shift
    work=/tmp/ts
    sel=/tmp/hidden_selection.txt     # lines: <ticket> <kind> <source path>
    rm -rf "$work"
    mkdir -p "$work"
    : >"$sel"
    tar -C /src --exclude='./build' --exclude='./build-*' --exclude='./.git' -cf - . | tar -C "$work" -xf -

    add_dir() {   # add_dir <ticket label> <dir containing unit/ and loopback/>
        local kind f
        for kind in unit loopback; do
            for f in "$2/$kind"/test_*.c; do
                [ -f "$f" ] || continue
                echo "$1 $kind $f" >>"$sel"
            done
        done
    }
    if [ "$#" -eq 0 ]; then
        add_dir shared /hidden
        for t in /hidden/tickets/*/; do
            [ -d "$t" ] || continue
            add_dir "$(basename "$t")" "${t%/}/tests"
        done
    else
        for id in "$@"; do
            add_dir "$id" "/hidden/tickets/$id/tests"
        done
    fi
    [ -s "$sel" ] || { echo "run.sh: no hidden tests selected" >&2; exit 2; }

    dups=$(awk '{ n = $3; sub(".*/", "", n); print n }' "$sel" | sort | uniq -d)
    if [ -n "$dups" ]; then
        echo "run.sh: hidden test file name used more than once: $dups" >&2
        grep -F "$dups" "$sel" >&2 || true
        exit 2
    fi

    names=""
    while read -r ticket kind f; do
        cp "$f" "$work/tests/$kind/"
        names="$names|$(basename "$f" .c)"
    done <"$sel"
    names="${names#|}"

    cd "$work"
    echo "[hidden] preset $preset, tickets: ${*:-all}, tests: ${names//|/ }"
    if ! cmake --preset "$preset" >/tmp/configure.log 2>&1; then
        cat /tmp/configure.log; echo "[hidden] configure failed" >&2; exit 2
    fi
    if ! cmake --build --preset "$preset" --parallel >/tmp/build.log 2>&1; then
        grep -E 'error|FAILED' /tmp/build.log | head -40; echo "[hidden] build failed" >&2; exit 2
    fi
    found=$(ctest --preset "$preset" -N -R "^($names)\$" | sed -n 's/^ *Test *#[0-9]*: //p' | wc -l)
    want=$(echo "$names" | tr '|' '\n' | wc -l)
    if [ "$found" -ne "$want" ]; then
        echo "[hidden] only $found of $want hidden tests registered by CMake" >&2; exit 2
    fi

    rc=0
    ctest --preset "$preset" -R "^($names)\$" --output-on-failure >/tmp/ctest.log 2>&1 || rc=$?
    failed=0
    while read -r ticket kind f; do
        n=$(basename "$f" .c)
        line=$(grep -E "Test +#[0-9]+: $n " /tmp/ctest.log | head -1 || true)
        secs=$(echo "$line" | grep -oE '[0-9]+\.[0-9]+ sec' || true)
        if echo "$line" | grep -qE ' Passed +[0-9]'; then
            printf 'PASS  %-8s %-34s %s\n' "$ticket" "$n" "${secs:--}"
        else
            printf 'FAIL  %-8s %-34s %s\n' "$ticket" "$n" "${secs:-not run}"
            failed=$((failed + 1))
        fi
    done <"$sel"
    if [ "$failed" -eq 0 ] && [ "$rc" -ne 0 ]; then
        failed=1    # ctest failed without a per-test failure line: never report PASS
        echo "[hidden] ctest exited with $rc" >&2
    fi
    if [ "$failed" -ne 0 ]; then
        echo "---- ctest output ----"
        cat /tmp/ctest.log
        echo "[hidden] FAIL ($failed of $want)"
        exit 1
    fi
    echo "[hidden] PASS ($want of $want)"
    exit 0
fi

# ---- on the host ---------------------------------------------------------------
usage() {
    echo "usage: $0 <path-to-tinystream checkout> [host|asan] [ticket-id ...]" >&2
    exit 2
}
if [ "$#" -lt 1 ] || [ ! -f "$1/CMakePresets.json" ]; then
    usage
fi
TS_DIR="$(cd "$1" && pwd)"
shift
PRESET=host
if [ "$#" -gt 0 ]; then
    case "$1" in
        host|asan) PRESET="$1"; shift;;
    esac
fi
for id in "$@"; do
    case "$id" in
        */*|.*|"") echo "run.sh: bad ticket id '$id'" >&2; exit 2;;
    esac
    if [ ! -d "$HT_DIR/tickets/$id" ]; then
        echo "run.sh: unknown ticket '$id' (no tickets/$id/; preset must be host or asan)" >&2
        exit 2
    fi
done

winpath() {   # Windows-style path for Docker Desktop under Git Bash, plain path elsewhere
    (cd "$1" && { pwd -W 2>/dev/null || pwd; })
}

exec env MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 \
    -e HOME=/tmp -e TS_IN_CONTAINER=1 -e HT_INNER=1 \
    -v "$(winpath "$TS_DIR"):/src:ro" -v "$(winpath "$HT_DIR"):/hidden:ro" -w /tmp \
    "$IMAGE" bash /hidden/run.sh "$PRESET" "$@"
