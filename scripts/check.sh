#!/usr/bin/env bash
# SPDX-License-Identifier: MIT
# check.sh -- the tinystream gate ladder (design doc section 3.3): build, misra, no-heap, unit-tests,
# property-tests, sanitizers, fuzz, loopback, budgets, coverage, compliance, diff-policy.
#
# Usage: scripts/check.sh [gate ...]        e.g. scripts/check.sh build no-heap unit-tests
#        scripts/check.sh                   runs every gate
#        TS_FUZZ=1 scripts/check.sh fuzz    runs the fuzz smoke (off by default)
#        TS_BASE_REF=main scripts/check.sh compliance diff-policy
#                                           compliance + diff policy of the change since main
#
# compliance and diff-policy look at "the change": the working tree
# (committed, staged, unstaged and untracked files) against the merge base of
# HEAD and TS_BASE_REF (default HEAD: uncommitted changes only), plus the last
# commit message. Their knobs (all optional, set by the harness per ticket):
#   TS_BASE_REF=<ref>         base of the change (the ticket's base branch)
#   TS_REQUIRE_TICKET=1       the last commit message must carry a Jira key (KEY-123)
#   TS_TICKET=<KEY-123>       ... and it must be this one
#   TS_ALLOWED_PATHS="glob .." every changed path must match one glob (** = any depth)
#   TS_ALLOW_PROTECTED="glob" facilitator approval to touch these protected paths
#   TS_SPDX_LICENSE=MIT       license id every new or changed C file must declare
# git runs on the host when check.sh is started there (a worktree's .git points
# to a host path the container cannot see); the result is handed to the
# container in build-check/change/.
#
# Runs inside the dev container; when invoked on the host it re-executes itself
# through scripts/dockerbuild.sh. Without a usable Docker (not installed, or
# `docker info` fails or does not answer within 8 s) or with TS_LITE=1, the host
# runs the Docker-free ladder instead: scripts/lite_gates.py (zig cc), same
# result lines, and the gates it cannot run SKIP. TS_LITE=0 (the stage and the
# central eval batches) never runs it: check.sh waits for `docker info` however
# long it takes (the caller's timeout is the bound) and, when Docker does not
# work, exits 2 without a result line. Every gate prints one line
#     <gate> <PASS|FAIL|SKIP> <ms> (budget) <what it checks> [reason]
# The old codes g0 .. g10 and dp still work as arguments (harness/names.py has the table).
# and a summary at the end.
# Exit status: 0 only when every requested gate PASSed (fuzz may SKIP: the fuzz
# smoke runs in the central harness); 1 when any gate FAILed; 3 ("LADDER
# INCOMPLETE") when any other gate SKIPped. A gate function returns 0 PASS,
# 77 SKIP (with $REASON), anything else is a FAIL. Missing tests are a FAIL,
# never a SKIP: scripts/test_manifest.txt lists every test the ladder needs.
set -uo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO="$(cd "$SCRIPT_DIR/.." && pwd)"

# ------------------------------------------------- change context (compliance, diff-policy) ----
# build-check/change/: ctx.env (ids), files.txt (git name-status, untracked as A),
# diff.patch (-U0, untracked files as all-added), msg.txt (last commit message),
# base/<path> (base versions of BASE_FILES), base_tlv_ids.txt (TLV identifiers of the base).
CHANGE_DIR="$REPO/build-check/change"
BASE_FILES="PROTOCOL.md include/ts_proto.h include/ts_config.h src/proto/proto.c scripts/protected_paths.txt"
# (cd instead of git -C: with MSYS_NO_PATHCONV=1 Git for Windows cannot read /c/... paths)
gitc() { (cd "$REPO" && git -c safe.directory='*' -c core.fileMode=false -c core.quotePath=false "$@"); }
wants_change_ctx() {    # wants_change_ctx [gate ...]: true when compliance or diff-policy will run
    local g
    [ $# -eq 0 ] && return 0
    for g in "$@"; do case "$(echo "$g" | tr 'A-Z' 'a-z')" in compliance|diff-policy|g10|dp) return 0;; esac; done
    return 1
}
prepare_change_ctx() {  # needs a working git checkout; returns 1 (quietly) without one
    local base mb f
    gitc rev-parse --verify -q HEAD >/dev/null 2>&1 || return 1
    base="${TS_BASE_REF:-HEAD}"
    if ! mb=$(gitc merge-base "$base" HEAD 2>/dev/null); then
        TS_CHANGE_CTX_ERR="TS_BASE_REF=$base is not a commit with a merge base with HEAD"
        export TS_CHANGE_CTX_ERR
        return 2
    fi
    rm -rf "$CHANGE_DIR" && mkdir -p "$CHANGE_DIR/base" || return 1
    gitc diff --no-color --no-ext-diff --no-renames --name-status "$mb" >"$CHANGE_DIR/files.txt" || return 1
    gitc diff --no-color --no-ext-diff --no-renames -U0 "$mb" >"$CHANGE_DIR/diff.patch" || return 1
    gitc ls-files --others --exclude-standard >"$CHANGE_DIR/untracked.txt" || return 1
    while IFS= read -r f; do
        [ -n "$f" ] || continue
        printf 'A\t%s\n' "$f" >>"$CHANGE_DIR/files.txt"
        if grep -Iq . "$REPO/$f" 2>/dev/null; then     # text files only
            printf 'diff --git a/%s b/%s\nnew file\n--- /dev/null\n+++ b/%s\n@@ -0,0 +1 @@\n' "$f" "$f" "$f"
            sed 's/^/+/' "$REPO/$f"
        fi >>"$CHANGE_DIR/diff.patch"
    done <"$CHANGE_DIR/untracked.txt"
    for f in $BASE_FILES; do
        mkdir -p "$CHANGE_DIR/base/$(dirname "$f")"
        gitc show "$mb:$f" >"$CHANGE_DIR/base/$f" 2>/dev/null || rm -f "$CHANGE_DIR/base/$f"
    done
    gitc grep -I -h -o -E '\b(TS_)?TLV_[A-Z0-9_]+' "$mb" -- src include tools tests fuzz 2>/dev/null |
        sed 's/^.*://' | sort -u >"$CHANGE_DIR/base_tlv_ids.txt"
    gitc log -1 --format=%B HEAD >"$CHANGE_DIR/msg.txt" || return 1
    TS_CHANGE_CTX_ID="$(date +%s)-$$-$RANDOM"
    export TS_CHANGE_CTX_ID
    printf 'CTX_ID=%s\nBASE_REF=%s\nMERGE_BASE=%s\nHEAD=%s\n' "$TS_CHANGE_CTX_ID" "$base" "$mb" \
        "$(gitc rev-parse HEAD)" >"$CHANGE_DIR/ctx.env"
}

# ------------------------------------------- host: container or Docker-free ladder ----
docker_usable() {   # true when `docker info` answers within 8 s (Docker Desktop stopped: it fails or hangs)
    local pid n=0
    command -v docker >/dev/null 2>&1 || return 1
    if command -v timeout >/dev/null 2>&1; then
        timeout 8 docker info >/dev/null 2>&1
        return
    fi
    docker info >/dev/null 2>&1 &                 # no timeout(1), e.g. macOS
    pid=$!
    while kill -0 "$pid" 2>/dev/null; do
        if [ "$n" -ge 80 ]; then kill "$pid" 2>/dev/null; wait "$pid" 2>/dev/null; return 1; fi
        sleep 0.1; n=$((n + 1))
    done
    wait "$pid"
}
lite_python() {     # the Python for scripts/lite_gates.py: the harness's (TS_PYTHON), the .venv of this
    local c common   # checkout or of the main checkout of a worktree, then python3, python
    common="$(gitc rev-parse --path-format=absolute --git-common-dir 2>/dev/null)"
    common="${common%/.git}"
    for c in "${TS_PYTHON:-}" "$REPO/.venv/Scripts/python.exe" "$REPO/.venv/bin/python" \
             ${common:+"$common/.venv/Scripts/python.exe" "$common/.venv/bin/python"} python3 python; do
        [ -n "$c" ] || continue
        if "$c" -c 'import sys; sys.exit(sys.version_info < (3, 8))' >/dev/null 2>&1; then
            printf '%s\n' "$c"
            return 0
        fi
    done
    return 1
}

if [ "${TS_IN_CONTAINER:-0}" != "1" ] && [ ! -f /.dockerenv ]; then
    if wants_change_ctx "$@"; then
        prepare_change_ctx
        case $? in
            0|2) ;;
            *) echo "check.sh: no git metadata on the host either (compliance/diff-policy will SKIP)" >&2;;
        esac
    fi
    # TS_LITE=0: the container ladder or nothing. `docker info` without the 8 s bound: a busy
    # Docker Desktop is waited for, a stopped one fails here like the container path would.
    if [ "${TS_LITE:-}" = "0" ]; then
        if ! command -v docker >/dev/null 2>&1 || ! docker info >/dev/null 2>&1; then
            echo "check.sh: TS_LITE=0 and Docker is not usable (docker info failed): the gates need the" \
                 "container, and TS_LITE=0 never runs the Docker-free ladder; start Docker and run again" >&2
            exit 2
        fi
        exec "$SCRIPT_DIR/dockerbuild.sh" scripts/check.sh "$@"
    fi
    # Without a usable Docker (or with TS_LITE=1) the Docker-free ladder runs the gates it can
    # (zig cc; the others SKIP "needs Docker (not run here)"), with the change context prepared above.
    if [ "${TS_LITE:-0}" = "1" ] || ! docker_usable; then
        if ! py="$(lite_python)"; then
            echo "check.sh: Docker is not usable and no Python 3 was found for scripts/lite_gates.py" >&2
            exit 2
        fi
        echo "check.sh: $([ "${TS_LITE:-0}" = "1" ] && echo "TS_LITE=1" || echo "no usable Docker"):" \
             "the Docker-free ladder (scripts/lite_gates.py, zig cc)" >&2
        cd "$REPO" || exit 2
        exec "$py" scripts/lite_gates.py "$@"     # relative: a native Python cannot open a /c/... path
    fi
    exec "$SCRIPT_DIR/dockerbuild.sh" scripts/check.sh "$@"
fi
cd "$REPO" || exit 2

# ---------------------------------------------------------------- helpers ----
LOG_DIR="${TS_CHECK_LOG:-$REPO/build-check}"
mkdir -p "$LOG_DIR"
FAILS=0; PASSES=0; SKIPS=0
SKIPPED=""
RESULTS=()
SKIP_RC=77
MANIFEST="scripts/test_manifest.txt"

# Per-gate budgets (design doc 3.3), milliseconds. Printed next to each time;
# a gate over its budget is flagged, not failed.
declare -A BUDGET_MS=([build]=4000 [misra]=30000 [no-heap]=3000 [unit-tests]=2000 [property-tests]=2000 \
                      [sanitizers]=20000 [fuzz]=90000 [loopback]=4000 [budgets]=2000 [coverage]=15000 \
                      [compliance]=1000 [diff-policy]=1000)

now_ms() { date +%s%3N; }

# gate <id> <name> <fn>  -- runs fn: 0 PASS, $SKIP_RC SKIP (reason in $REASON), anything else FAIL
gate() {
    local id="$1" name="$2" fn="$3"
    local t0 t1 rc key ms budget over=""
    REASON=""
    key="$id"
    t0=$(now_ms)
    "$fn" >"$LOG_DIR/$id.log" 2>&1
    rc=$?
    t1=$(now_ms)
    ms=$((t1 - t0))
    budget="${BUDGET_MS[$key]:-0}"
    local status
    case $rc in
        0) status=PASS; PASSES=$((PASSES+1));;
        "$SKIP_RC") status=SKIP; SKIPS=$((SKIPS+1)); [ "$key" = fuzz ] || SKIPPED="$SKIPPED $id";;
        *) status=FAIL; FAILS=$((FAILS+1)); REASON="${REASON:-see $LOG_DIR/$id.log}";;
    esac
    if [ "$budget" -gt 0 ] && [ "$ms" -gt "$budget" ] && [ "$status" != SKIP ]; then
        over=" OVER BUDGET"
    fi
    printf '%-14s %-4s %6d ms (budget %3d s%s)  %-22s %s\n' "$id" "$status" "$ms" "$((budget / 1000))" "$over" \
        "$name" "$REASON"
    RESULTS+=("$id $status")
}

configure() {   # configure <preset>
    [ -f "build-$1/CMakeCache.txt" ] || cmake --preset "$1" || return 1
}
build() {       # build <preset>
    configure "$1" && cmake --build --preset "$1" --parallel
}
list_tests() {  # list_tests <preset> [ctest selection args]: one test name per line
    local p="$1"; shift
    ctest --preset "$p" -N "$@" 2>/dev/null | sed -n 's/^ *Test *#[0-9]*: //p'
}
manifest_tests() { # manifest_tests [label]: names from the manifest (all when no label)
    awk -v want="${1:-}" '$0 !~ /^[[:space:]]*#/ && NF >= 2 {
        if (want == "") { print $1; next }
        for (i = 2; i <= NF; i++) if ($i == want) { print $1; next } }' "$MANIFEST"
}
check_manifest() { # check_manifest <preset> [label]: FAIL when a manifest test is not registered
    local p="$1" label="${2:-}" have missing="" t
    [ -f "$MANIFEST" ] || { REASON="missing $MANIFEST"; return 1; }
    if [ -n "$label" ]; then have=$(list_tests "$p" -L "^${label}\$"); else have=$(list_tests "$p"); fi
    for t in $(manifest_tests "$label"); do
        echo "$have" | grep -qx "$t" || missing="$missing $t"
    done
    if [ -n "$missing" ]; then
        echo "tests in $MANIFEST but not registered in preset $p:$missing"
        REASON="missing tests${label:+ (label $label)}:$missing"
        return 1
    fi
}
run_ctest() {   # run_ctest <preset> <label>
    build "$1" || return 1
    check_manifest "$1" "$2" || return 1
    ctest --preset "$1" -L "^$2\$" --output-on-failure
}

# ------------------------------------------------------------------ gates ----
# build: host + aarch64 cross, -std=c11 -Wall -Wextra -Wpedantic -Werror (flags in CMakeLists.txt)
gate_build() {
    build host && build pi
}

# misra static: cppcheck MISRA subset (scripts/misra.json) + justified suppressions.
#   0. no ticket ids in code, tests, tools, fuzz targets or PROTOCOL.md (source trees only:
#      commit messages are not scanned here, compliance requires the Jira key there)
#   1. canary: scripts/misra_canary/canary.c must trip 21.3, 21.6, 21.8 and 21.21
#   2. the tree with the log compiled out (as the daemon builds it)
#   3. tools/, tests/support and src/log with TS_LOG_ENABLED=1 (as tools and tests build them)
# cppcheck runs on a container-local copy: on a Docker Desktop bind mount the
# many small reads made this gate take ~25 s instead of ~3 s.
MISRA_OPTS=(--addon=scripts/misra.json --std=c11 --enable=style --error-exitcode=1 --inline-suppr
         --suppressions-list=scripts/cppcheck-suppressions.txt --quiet --library=posix -D__GNUC__)
MISRA_INC=(-I include -I tests/support -I tests/vectors -I third_party/unity)
gate_misra() {
    local ids tmp rc=0 out want miss=""
    ids=$(grep -rnE 'TS-[0-9]{3}' src include tests tools fuzz PROTOCOL.md 2>/dev/null)
    if [ -n "$ids" ]; then
        echo "ticket ids must not appear in code, tests, tools, fuzz or PROTOCOL.md:"; echo "$ids"
        REASON="ticket ids in the tree ($(echo "$ids" | wc -l) lines, see $LOG_DIR/misra.log)"
        return 1
    fi
    tmp=$(mktemp -d /tmp/ts-misra.XXXXXX) || return 1
    mkdir -p "$tmp/tests"
    cp -r src include tools scripts "$tmp/" && cp -r tests/support tests/vectors "$tmp/tests/" &&
        mkdir -p "$tmp/third_party" && cp -r third_party/unity "$tmp/third_party/" || { rm -rf "$tmp"; return 1; }
    (
        cd "$tmp" || exit 1
        out=$(cppcheck "${MISRA_OPTS[@]}" -DTS_LOG_ENABLED=0 --template='{id}' scripts/misra_canary/canary.c 2>&1)
        for want in misra-c2012-21.3 misra-c2012-21.6 misra-c2012-21.8 misra-c2012-21.21; do
            echo "$out" | grep -qx "$want" || miss="$miss $want"
        done
        if [ -n "$miss" ]; then
            echo "misra canary: rule(s) not detected on scripts/misra_canary/canary.c:$miss"; echo "$out"
            exit 2
        fi
        echo "canary: 21.3 21.6 21.8 21.21 detected"
        cppcheck -j "$(nproc)" "${MISRA_OPTS[@]}" -DTS_LOG_ENABLED=0 "${MISRA_INC[@]}" src include tools tests/support || exit 1
        cppcheck -j "$(nproc)" "${MISRA_OPTS[@]}" -DTS_LOG_ENABLED=1 "${MISRA_INC[@]}" tools tests/support src/log || exit 1
    )
    rc=$?
    rm -rf "$tmp"
    case $rc in
        0) ;;
        2) REASON="MISRA canary failed: a forbidden-function rule is suppressed or renumbered"; return 1;;
        *) return 1;;
    esac
}

# no-heap no-malloc:
#   0. canary: scripts/no_heap_canary/canary.c compiled to an object must trip the
#      forbidden list for every name in NO_HEAP_CANARY (the list itself is tested)
#   1. nm -u on every daemon object (tsproto, tsd, tinystreamd), not on the static binary:
#      no allocator or allocating libc call, no process creation or program
#      loading (exec*, fork, vfork, posix_spawn*, popen, system, dlopen)
#   2. run time: tinystreamd (host, and the aarch64 binary under qemu) runs 1 s
#      on free ports and stops cleanly on SIGTERM; the --wrap malloc guard
#      aborts (exit 134) on any allocation after ts_init_done()
NO_HEAP_ALLOC='malloc|free|calloc|realloc|reallocarray|strdup|strndup|wcsdup|posix_memalign|aligned_alloc|memalign|valloc|pvalloc|asprintf|vasprintf|getline|getdelim|open_memstream|open_wmemstream|fmemopen|fopen|fdopen|freopen|tmpfile|opendir|fdopendir|scandir|popen|system'
NO_HEAP_ALLOC="$NO_HEAP_ALLOC|execl|execle|execlp|execv|execve|execvp|execvpe|execveat|fexecve|fork|vfork|_Fork|posix_spawn|posix_spawnp|dlopen|dlmopen"
NO_HEAP_CANARY='malloc popen system execl execle execlp execv execve execvp execvpe fexecve fork vfork posix_spawn posix_spawnp dlopen'
no_heap_canary() {   # the forbidden list must catch every canary call
    local tmp hits miss="" s
    tmp=$(mktemp -d /tmp/ts-no-heap.XXXXXX) || return 1
    if ! gcc -std=c11 -O0 -w -c -o "$tmp/canary.o" scripts/no_heap_canary/canary.c; then
        rm -rf "$tmp"; echo "no-heap canary: scripts/no_heap_canary/canary.c does not compile"; return 1
    fi
    hits=$(nm -u "$tmp/canary.o" | grep -E " U (${NO_HEAP_ALLOC})(@|\$)" | awk '{print $NF}' | sed 's/@.*//')
    rm -rf "$tmp"
    for s in $NO_HEAP_CANARY; do
        echo "$hits" | grep -qx "$s" || miss="$miss $s"
    done
    if [ -n "$miss" ]; then
        echo "no-heap canary: forbidden list misses:$miss"
        REASON="no-heap canary not detected:$miss"
        return 1
    fi
    echo "canary: $(echo "$NO_HEAP_CANARY" | wc -w) forbidden calls detected"
}
no_heap_run() {      # no_heap_run <tag> <udp port> <command...>: 1 s run, SIGTERM, expect a clean exit 0
    local tag="$1" port="$2" rc; shift 2
    timeout --preserve-status -k 3 -s TERM 1 "$@" --udp-port "$port" --tcp-port "$((port + 1))" \
        >"$LOG_DIR/no-heap-$tag.txt" 2>&1
    rc=$?
    case $rc in
        0)   echo "$tag: ran 1 s, clean stop on SIGTERM";;
        134) echo "$tag: aborted (SIGABRT): allocation after init (malloc guard)"; cat "$LOG_DIR/no-heap-$tag.txt";;
        *)   echo "$tag: exit $rc (expected 0 after SIGTERM)"; cat "$LOG_DIR/no-heap-$tag.txt";;
    esac
    return $rc
}
gate_no_heap() {
    build host || return 1
    build pi || return 1
    local objs hits rc=0 port pids=() p
    no_heap_canary || return 1
    objs=$(find build-host/CMakeFiles/tsproto.dir build-host/CMakeFiles/tsd.dir build-host/CMakeFiles/tinystreamd.dir \
                -name '*.o' ! -name 'io_fake.c.o' 2>/dev/null)
    [ -n "$objs" ] || { REASON="no daemon objects found"; return 1; }
    # shellcheck disable=SC2086
    hits=$(nm -u -A $objs 2>/dev/null | grep -E " U (${NO_HEAP_ALLOC})(@|\$)" || true)
    if [ -n "$hits" ]; then
        echo "forbidden calls referenced by daemon objects:"; echo "$hits"
        REASON="forbidden call referenced: $(echo "$hits" | awk '{print $NF}' | sort -u | tr '\n' ' ')"
        return 1
    fi
    echo "no forbidden call in $(echo "$objs" | wc -l) daemon objects"
    port=$((20000 + (RANDOM % 20000) * 2))
    no_heap_run host "$port" build-host/tinystreamd & pids+=($!)
    no_heap_run pi "$((port + 2))" qemu-aarch64-static build-pi/tinystreamd & pids+=($!)
    for p in "${pids[@]}"; do wait "$p" || rc=1; done
    [ $rc -eq 0 ] || REASON="tinystreamd did not run and stop cleanly (malloc guard? see $LOG_DIR/no-heap.log)"
    return $rc
}

# unit-tests unit (Unity, virtual clock)
gate_unit_tests() { run_ctest host unit; }

# property-tests property tests (test_prop_*.c): round-trip, reference-sync, header round-trip, seq wrap
gate_property_tests() { run_ctest host property; }

# sanitizers: every test (unit, property, loopback, budget) under ASan/UBSan, -fno-sanitize-recover=all
gate_sanitizers() {
    build asan || return 1
    check_manifest asan || return 1
    ctest --preset asan --output-on-failure
}

# fuzz smoke: 3 targets x FUZZ_SECS (central harness only; TS_FUZZ=1 enables)
gate_fuzz() {
    if [ "${TS_FUZZ:-0}" != "1" ]; then
        REASON="fuzz smoke runs in the central harness only; set TS_FUZZ=1 (3 x ${FUZZ_SECS:-30} s)"
        return $SKIP_RC
    fi
    build fuzz || return 1
    local t rc=0 max_len log stats summary=""
    for t in fuzz_pkt_parse fuzz_ctrl_parse fuzz_codec_decode; do
        # libFuzzer writes new inputs into the first corpus dir: keep the committed seeds clean
        local seeds="fuzz/corpus/${t#fuzz_}" work="build-fuzz/corpus/${t#fuzz_}"
        # a whole encoded frame must fit: TS_ENC_MAX is 153664 bytes (4096 truncated the 77 KB seeds)
        case "$t" in
            fuzz_codec_decode) max_len=160000;;
            *) max_len=4096;;
        esac
        mkdir -p "$seeds" "$work"
        log="$LOG_DIR/fuzz-$t.txt"
        "build-fuzz/$t" -max_total_time="${FUZZ_SECS:-30}" -max_len="$max_len" -rss_limit_mb=1024 \
            "$work" "$seeds" >"$log" 2>&1 || rc=1
        # last libFuzzer status line, e.g. "#123 DONE cov: 58 ft: 130 corp: 9/151Kb ... exec/s: 900"
        stats=$(grep -E '^#[0-9]+[[:space:]]+DONE' "$log" | tail -1 | grep -oE '(cov|ft|exec/s): [0-9]+' | paste -sd ' ' -)
        echo "${t}: ${stats:-no DONE line} (max_len $max_len, log $log)"
        summary="$summary ${t#fuzz_}: ${stats:-?};"
    done
    REASON="${summary# }"
    [ $rc -eq 0 ] || REASON="crash or leak found:$summary (see $LOG_DIR/fuzz-*.txt)"
    return $rc
}

# loopback budgets: clean, 5 % loss, burst, reorder, corrupt, cable-pull, two clients, seq-wrap,
# SET_RATE, worst-case frames, file source
gate_loopback() { run_ctest host loopback; }

# budgets: ratio, PSNR, TS_ENC_MAX (tests/budget, label "budget"), .text+.bss
# of the daemon objects (host and pi), -fstack-usage max frame. Limits can be
# overridden from the environment; the defaults are the documented budgets.
BUDGET_TEXT_MAX="${TS_BUDGET_TEXT_MAX:-65536}"      # bytes: Berkeley "text" (code + rodata) of the daemon objects
BUDGET_BSS_MAX="${TS_BUDGET_BSS_MAX:-1572864}"      # bytes: 1.5 MiB, same bound as the pool _Static_assert
BUDGET_STACK_MAX="${TS_BUDGET_STACK_MAX:-2048}"     # bytes: largest single stack frame (.su), no unbounded frames

daemon_objs() { # daemon_objs <build dir>: tsproto + tsd + tinystreamd objects; io_fake is test-only
    find "$1/CMakeFiles/tsproto.dir" "$1/CMakeFiles/tsd.dir" "$1/CMakeFiles/tinystreamd.dir" \
        -name '*.o' ! -name 'io_fake.c.o' 2>/dev/null | sort
}
daemon_su() {   # daemon_su <build dir>: matching -fstack-usage files
    find "$1/CMakeFiles/tsproto.dir" "$1/CMakeFiles/tsd.dir" "$1/CMakeFiles/tinystreamd.dir" \
        -name '*.su' ! -name 'io_fake.c.su' 2>/dev/null | sort
}

gate_budgets() {
    local rc=0 summary=""
    build host || return 1
    build pi || return 1

    # 1. codec budgets (ratio, PSNR, TS_ENC_MAX) -- the test prints BUDGET lines
    if ! check_manifest host budget; then
        rc=1
    elif ! ctest --preset host -L '^budget$' -V >"$LOG_DIR/budgets-budget.txt" 2>&1; then
        echo "codec budget test failed:"; grep -E 'BUDGET|FAIL' "$LOG_DIR/budgets-budget.txt"; rc=1
    fi
    grep -ho 'BUDGET .*' "$LOG_DIR/budgets-budget.txt" 2>/dev/null
    local ratio
    ratio=$(sed -n 's/.*ratio_q1_x100=\([0-9]*\).*/\1/p' "$LOG_DIR/budgets-budget.txt" 2>/dev/null | head -1)
    [ -n "$ratio" ] && summary="ratio ${ratio:0:-2}.${ratio: -2}"

    # 2. .text + .bss of the daemon objects (host and aarch64)
    local p sz objs text bss
    for p in host pi; do
        objs=$(daemon_objs "build-$p")
        [ -n "$objs" ] || { echo "no daemon objects in build-$p"; rc=1; continue; }
        if [ "$p" = pi ]; then sz=aarch64-linux-gnu-size; else sz=size; fi
        # shellcheck disable=SC2086
        read -r text _ bss _ < <($sz -t $objs | tail -1)
        echo "SIZE $p text=$text bss=$bss text+bss=$((text + bss)) (max text $BUDGET_TEXT_MAX, bss $BUDGET_BSS_MAX)"
        if [ "$text" -gt "$BUDGET_TEXT_MAX" ]; then echo "  $p: .text over budget"; rc=1; fi
        if [ "$bss" -gt "$BUDGET_BSS_MAX" ]; then echo "  $p: .bss over budget"; rc=1; fi
        [ "$p" = pi ] && summary="$summary, pi text $text bss $bss"
    done

    # 3. -fstack-usage: largest frame, no unbounded (dynamic) frames
    local su worst unb
    for p in host pi; do
        su=$(daemon_su "build-$p")
        [ -n "$su" ] || { echo "no .su files in build-$p (-fstack-usage missing?)"; rc=1; continue; }
        # shellcheck disable=SC2086
        worst=$(cat $su | sort -t"$(printf '\t')" -k2,2 -n -r | head -1)
        # shellcheck disable=SC2086
        unb=$(cat $su | awk -F'\t' '$3 == "dynamic"')
        echo "STACK $p max frame: $worst (max $BUDGET_STACK_MAX)"
        if [ "$(echo "$worst" | cut -f2)" -gt "$BUDGET_STACK_MAX" ]; then echo "  $p: stack frame over budget"; rc=1; fi
        if [ -n "$unb" ]; then echo "  $p: unbounded stack frames:"; echo "$unb"; rc=1; fi
        [ "$p" = pi ] && summary="$summary, pi stack $(echo "$worst" | cut -f2)"
    done
    if [ $rc -ne 0 ]; then
        REASON="${REASON:-budget exceeded (see $LOG_DIR/budgets.log)}"
    else
        REASON="$summary"
    fi
    return $rc
}

# coverage: gcovr per-module line thresholds (scripts/coverage_thresholds.txt),
# each module measured on ITS OWN unit tests only: test_<module> and
# test_prop_<module> (counters reset before each module). Coverage contributed
# by loopback or other modules' tests does not count, so deleting a module's
# tests drops it below its threshold. The all-tests number is printed as info.
# gcovr is pointed at the module's own object directory only: scanning the whole
# build tree on a Docker Desktop bind mount takes minutes, and merging the
# header-inline functions of every test TU trips a gcovr 5.2 assertion.
gate_coverage() {
    local thr="scripts/coverage_thresholds.txt"
    [ -f "$thr" ] || { REASON="missing $thr"; return 1; }
    build coverage || return 1
    check_manifest coverage unit || return 1
    local rc=0 summary="" info="" module dir pct od pctv sel n
    while read -r module dir pct; do
        case "$module" in ''|'#'*) continue;; esac
        od=$(find build-coverage/CMakeFiles -type d -path "*.dir/${dir%/}" | head -1)
        if [ -z "$od" ]; then
            echo "no objects for $module ($dir)"; rc=1; summary="$summary $module=?"; continue
        fi
        sel="^test_(prop_)?${module}\$"
        n=$(list_tests coverage -R "$sel" | wc -l)
        if [ "$n" -eq 0 ]; then
            echo "no unit tests for $module (tests/unit/test_${module}.c)"; rc=1; summary="$summary $module=no-tests"; continue
        fi
        find build-coverage -name '*.gcda' -delete
        if ! ctest --preset coverage -R "$sel" --output-on-failure >"$LOG_DIR/coverage-ctest-$module.txt" 2>&1; then
            echo "tests of $module failed in the coverage build"; rc=1; summary="$summary $module=fail"; continue
        fi
        if ! gcovr --root . --filter "$dir" --object-directory build-coverage --fail-under-line "$pct" \
                --print-summary "$od" >"$LOG_DIR/coverage-$module.txt" 2>&1; then
            echo "coverage below $pct% for $module ($dir) from its own tests:"; cat "$LOG_DIR/coverage-$module.txt"
            rc=1
        fi
        pctv=$(sed -n 's/^lines: \([0-9.]*\)%.*/\1/p' "$LOG_DIR/coverage-$module.txt")
        echo "$module lines ${pctv:-?}% from $n own test(s) (min $pct%)"
        summary="$summary $module=${pctv:-?}"
    done < "$thr"
    # info only: every test together
    find build-coverage -name '*.gcda' -delete
    if ctest --preset coverage >"$LOG_DIR/coverage-ctest-all.txt" 2>&1; then
        while read -r module dir pct; do
            case "$module" in ''|'#'*) continue;; esac
            od=$(find build-coverage/CMakeFiles -type d -path "*.dir/${dir%/}" | head -1)
            [ -n "$od" ] || continue
            pctv=$(gcovr --root . --filter "$dir" --object-directory build-coverage --print-summary "$od" 2>/dev/null |
                   sed -n 's/^lines: \([0-9.]*\)%.*/\1/p')
            info="$info $module=${pctv:-?}"
        done < "$thr"
        echo "info, all tests together:$info"
    else
        echo "info: a test failed in the all-tests coverage run (see $LOG_DIR/coverage-ctest-all.txt)"; rc=1
    fi
    if [ $rc -ne 0 ]; then
        REASON="per-module coverage (own tests) not met:$summary"
        return 1
    fi
    REASON="own-test lines%:$summary"
}

# ------------------------------------------------ compliance / diff-policy: the change ----
# Both gates read build-check/change/ (see prepare_change_ctx at the top).
CTX_STATE=""; CTX_REASON=""
ensure_ctx() {  # 0 ready, $SKIP_RC no git metadata at all, 1 unusable base
    if [ -z "$CTX_STATE" ]; then
        if [ -n "${TS_CHANGE_CTX_ERR:-}" ]; then
            CTX_STATE=fail; CTX_REASON="$TS_CHANGE_CTX_ERR"
        elif [ -n "${TS_CHANGE_CTX_ID:-}" ] && [ -f "$CHANGE_DIR/ctx.env" ] &&
             grep -qx "CTX_ID=$TS_CHANGE_CTX_ID" "$CHANGE_DIR/ctx.env"; then
            CTX_STATE=ok                                  # prepared on the host for this run
        else
            prepare_change_ctx
            case $? in
                0) CTX_STATE=ok;;
                2) CTX_STATE=fail; CTX_REASON="${TS_CHANGE_CTX_ERR:-bad TS_BASE_REF}";;
                *) CTX_STATE=skip
                   CTX_REASON="no git metadata (an export without .git, or a worktree seen from the container): run check.sh from the host";;
            esac
        fi
    fi
    case "$CTX_STATE" in
        ok)   sed 's/^/change: /' "$CHANGE_DIR/ctx.env"; return 0;;
        skip) REASON="$CTX_REASON"; return $SKIP_RC;;
        *)    REASON="$CTX_REASON"; return 1;;
    esac
}
ctx_paths()      { awk -F'\t' 'NF >= 2 { print $2 }' "$CHANGE_DIR/files.txt" | sort -u; }
ctx_live_paths() { awk -F'\t' 'NF >= 2 && $1 !~ /^D/ { print $2 }' "$CHANGE_DIR/files.txt" | sort -u; }
added_lines() {  # added_lines <ERE on the path>: "path<TAB>added line" for every added line of the change
    # (no backslash in the ERE: gawk, the host's awk under the Docker-free ladder, drops it from -v values)
    awk -v want="$1" '
        /^diff --git / { hdr = 1; f = ""; next }
        hdr && /^\+\+\+ / { f = substr($0, 5); sub(/^b\//, "", f); next }
        /^@@/ { hdr = 0; next }
        !hdr && /^\+/ && f != "" && f ~ want { print f "\t" substr($0, 2) }' "$CHANGE_DIR/diff.patch"
}
declare -A GLOB_RE_CACHE=()
glob_ere() {     # glob_ere <glob>: sets GLOB_RE to an anchored ERE; ** any depth, * one segment, trailing / = directory
    local g="$1" out="" i=0 c
    if [ -n "${GLOB_RE_CACHE[$1]+x}" ]; then GLOB_RE="${GLOB_RE_CACHE[$1]}"; return 0; fi
    case "$g" in */) g="${g}**";; esac
    while [ "$i" -lt "${#g}" ]; do
        if [ "${g:$i:3}" = "**/" ]; then out="$out(.*/)?"; i=$((i + 3)); continue; fi
        if [ "${g:$i:2}" = "**" ]; then out="$out.*"; i=$((i + 2)); continue; fi
        c="${g:$i:1}"
        case "$c" in
            '*') out="$out[^/]*";;
            '?') out="$out[^/]";;
            '.'|'^'|'$'|'+'|'{'|'}'|'('|')'|'|'|'['|']'|'\') out="$out\\$c";;
            *) out="$out$c";;
        esac
        i=$((i + 1))
    done
    GLOB_RE="^$out\$"
    GLOB_RE_CACHE[$1]="$GLOB_RE"
}
path_in_globs() { # path_in_globs <path> <glob>...
    local p="$1" g; shift
    for g in "$@"; do
        glob_ere "$g"
        [[ $p =~ $GLOB_RE ]] && return 0
        case "$g" in */) [ "$p" = "${g%/}" ] && return 0;; esac
    done
    return 1
}
env_globs() {    # env_globs <value>: one glob per line (space or comma separated, never expanded)
    printf '%s\n' "$1" | tr ', \t' '\n\n\n' | sed '/^$/d'
}
# Protected paths: the union of the base list, the current list and these
# built-in entries, so that a change cannot unprotect a path by editing the list.
DP_BUILTIN_PROTECTED="include/ src/pool/ src/malloc_guard.c .github/ harness/ scripts/ !scripts/test_manifest.txt"
protected_list() {
    { printf '%s\n' $DP_BUILTIN_PROTECTED
      cat "$CHANGE_DIR/base/scripts/protected_paths.txt" scripts/protected_paths.txt 2>/dev/null
    } | sed 's/#.*//; s/[[:space:]]*$//; s/^[[:space:]]*//' | sed '/^$/d' | sort -u
}

proto_ver_md() { sed -n 's/^PROTOCOL_VERSION:[[:space:]]*\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -1; }
proto_ver_h()  { sed -n 's/^#define[[:space:]]\{1,\}TS_PROTOCOL_VERSION[[:space:]]\{1,\}\([0-9][0-9]*\).*/\1/p' "$1" 2>/dev/null | head -1; }
hdr_layout() {   # hdr_layout <tree root>: what defines the DATA header packing, comments and blanks dropped
    local d="$1"
    {
        echo "## PROTOCOL.md section 3 (packet layout)"
        awk '/^## 3\./ { f = 1 } /^### 3\.1/ { f = 0 } f' "$d/PROTOCOL.md"
        echo "## ts_hdr_t"
        awk '/^typedef struct ts_hdr[[:space:]]*\{/ { f = 1 } f { print } f && /^\}[[:space:]]*ts_hdr_t;/ { f = 0 }' "$d/include/ts_proto.h"
        echo "## header constants"
        grep -hE '^#define[[:space:]]+(TS_HDR_LEN|TS_MAGIC_DATA|TS_F_[A-Z_]+)[[:space:]]' "$d/include/ts_config.h" "$d/include/ts_proto.h"
        echo "## field offsets and hdr_write20()"
        grep -hE '^#define[[:space:]]+HDR_[A-Z0-9_]+[[:space:]]' "$d/src/proto/proto.c"
        awk '/^static void hdr_write20\(/ { f = 1 } f { print } f && /^\}/ { f = 0 }' "$d/src/proto/proto.c"
    } 2>/dev/null | sed 's:/\*.*\*/::g; s://.*$::; s/[[:space:]]\{1,\}/ /g; s/ $//' | sed '/^ *$/d'
}

# compliance: (1) Jira key in the last commit message (TS_REQUIRE_TICKET=1),
#   (2) SPDX license header on every new or changed C file, (3) PROTOCOL_VERSION:
#   PROTOCOL.md and include/ts_config.h agree, and a change of the DATA header
#   packing or of PROTOCOL.md itself comes with a version bump, (4) every changed
#   path is in TS_ALLOWED_PATHS (when set).
gate_compliance() {
    ensure_ctx || return $?
    local rc=0 summary="" f miss="" key lic="${TS_SPDX_LICENSE:-MIT}" n=0
    echo "changed paths: $(ctx_paths | wc -l)"
    # 1. ticket key
    if [ "${TS_REQUIRE_TICKET:-0}" = "1" ]; then
        if [ -n "${TS_TICKET:-}" ]; then
            if ! printf '%s' "$TS_TICKET" | grep -qE '^[A-Z][A-Z0-9]+-[0-9]+$'; then
                echo "ticket: TS_TICKET=$TS_TICKET is not a Jira key"; rc=1; summary="$summary bad TS_TICKET;"
            elif grep -qE "(^|[^A-Za-z0-9_-])${TS_TICKET}([^0-9]|\$)" "$CHANGE_DIR/msg.txt"; then
                echo "ticket: $TS_TICKET named in the last commit message"; summary="$summary key $TS_TICKET;"
            else
                echo "ticket: the last commit message does not name $TS_TICKET:"; sed 's/^/  | /' "$CHANGE_DIR/msg.txt"
                rc=1; summary="$summary commit lacks $TS_TICKET;"
            fi
        else
            key=$(grep -oE '(^|[^A-Za-z0-9_-])[A-Z][A-Z0-9]+-[0-9]+' "$CHANGE_DIR/msg.txt" | head -1 | sed 's/^[^A-Z]*//')
            if [ -n "$key" ]; then
                echo "ticket: $key named in the last commit message"; summary="$summary key $key;"
            else
                echo "ticket: no Jira key (KEY-123) in the last commit message:"; sed 's/^/  | /' "$CHANGE_DIR/msg.txt"
                rc=1; summary="$summary no Jira key in commit;"
            fi
        fi
    else
        echo "ticket: not required (TS_REQUIRE_TICKET != 1)"
    fi
    # 2. SPDX header on new / changed C files
    while IFS= read -r f; do
        case "$f" in third_party/*) continue;; esac
        [ -f "$f" ] || continue
        n=$((n + 1))
        head -n 5 "$f" | grep -qE "SPDX-License-Identifier:[[:space:]]*${lic}([^A-Za-z0-9.+-]|\$)" || miss="$miss $f"
    done < <(ctx_live_paths | grep -E '\.(c|h)$')
    if [ -n "$miss" ]; then
        echo "license: missing '/* SPDX-License-Identifier: $lic */' in the first 5 lines of:$miss"
        rc=1; summary="$summary SPDX missing:$miss;"
    else
        echo "license: $n new/changed C file(s), all with SPDX-License-Identifier: $lic"
    fi
    # 3. protocol version
    local v_md v_h v0 hdr_changed=0 doc_changed=0
    v_md=$(proto_ver_md PROTOCOL.md); v_h=$(proto_ver_h include/ts_config.h); v0=$(proto_ver_md "$CHANGE_DIR/base/PROTOCOL.md")
    if [ -z "$v_md" ] || [ -z "$v_h" ]; then
        echo "protocol: cannot read PROTOCOL_VERSION (PROTOCOL.md '$v_md', include/ts_config.h '$v_h')"
        rc=1; summary="$summary PROTOCOL_VERSION unreadable;"
    else
        if [ "$v_md" != "$v_h" ]; then
            echo "protocol: PROTOCOL.md says version $v_md, include/ts_config.h TS_PROTOCOL_VERSION $v_h"
            rc=1; summary="$summary PROTOCOL_VERSION mismatch $v_md/$v_h;"
        fi
        if [ -f "$CHANGE_DIR/base/PROTOCOL.md" ]; then
            diff <(hdr_layout "$CHANGE_DIR/base") <(hdr_layout .) >"$LOG_DIR/compliance-hdr.diff" || hdr_changed=1
            diff <(grep -v '^PROTOCOL_VERSION:' "$CHANGE_DIR/base/PROTOCOL.md") <(grep -v '^PROTOCOL_VERSION:' PROTOCOL.md) \
                >"$LOG_DIR/compliance-protocol.diff" || doc_changed=1
        else
            echo "protocol: no PROTOCOL.md in the base, layout comparison skipped"
        fi
        if [ "$hdr_changed" = 1 ]; then echo "protocol: the DATA header packing changed:"; head -40 "$LOG_DIR/compliance-hdr.diff"; fi
        if [ "$doc_changed" = 1 ]; then echo "protocol: PROTOCOL.md changed (diff in $LOG_DIR/compliance-protocol.diff)"; fi
        if [ -n "$v0" ] && [ "$v_md" -lt "$v0" ]; then
            echo "protocol: PROTOCOL_VERSION went down ($v0 -> $v_md)"; rc=1; summary="$summary version $v0->$v_md;"
        elif [ $((hdr_changed + doc_changed)) -gt 0 ] && [ -n "$v0" ] && [ "$v_md" -le "$v0" ]; then
            echo "protocol: wire format / PROTOCOL.md changed without a PROTOCOL_VERSION bump (still $v_md):" \
                 "bump it in PROTOCOL.md and include/ts_config.h, or keep the wire format unchanged"
            rc=1; summary="$summary protocol changed without version bump;"
        else
            echo "protocol: version $v_md (base ${v0:-?}), header packing $([ $hdr_changed = 1 ] && echo changed || echo unchanged)"
        fi
    fi
    # 4. allowed paths
    if [ -n "${TS_ALLOWED_PATHS:-}" ]; then
        local -a globs=(); local bad="" p
        mapfile -t globs < <(env_globs "$TS_ALLOWED_PATHS")
        while IFS= read -r p; do
            path_in_globs "$p" "${globs[@]}" || bad="$bad $p"
        done < <(ctx_paths)
        if [ -n "$bad" ]; then
            echo "paths: outside TS_ALLOWED_PATHS (${globs[*]}):$bad"; rc=1; summary="$summary outside allowed paths:$bad;"
        else
            echo "paths: every changed path within TS_ALLOWED_PATHS (${globs[*]})"
        fi
    else
        echo "paths: no allowlist (TS_ALLOWED_PATHS unset)"
    fi
    if [ $rc -ne 0 ]; then REASON="${summary# }"; else REASON="$(ctx_paths | wc -l) changed path(s)${summary:+;$summary}"; fi
    REASON="${REASON%;}"
    return $rc
}

# diff-policy diff policy: (1) no change under a protected path (scripts/protected_paths.txt,
#   unless TS_ALLOW_PROTECTED approves it), (2) no added call that creates a process
#   or loads code (system, popen, exec*, fork, posix_spawn*, dlopen) in any C file,
#   (3) no new TLV type (enumerator / #define with a value, or a new row in the
#   PROTOCOL.md type table).
DP_CALLS='(^|[^A-Za-z0-9_.>])(system|popen|execl|execle|execlp|execv|execve|execvp|execvpe|execveat|fexecve|fork|vfork|_Fork|posix_spawn|posix_spawnp|dlopen|dlmopen)[[:space:]]*\('
DP_TLV_DEF='^[[:space:]]*(#[[:space:]]*define[[:space:]]+)?((TS_)?TLV_[A-Z0-9_]+)([[:space:]]*=[[:space:]]*|[[:space:]]+\(?[[:space:]]*)(0[xX][0-9A-Fa-f]+|[0-9]+)'
gate_diff_policy() {
    ensure_ctx || return $?
    local rc=0 summary="" p hits="" approved="" calls tlv="" name rows0 rows1 t
    local -a prot=() exc=() allow=()
    mapfile -t prot < <(protected_list | grep -v '^!')
    mapfile -t exc < <(protected_list | sed -n 's/^!//p')
    mapfile -t allow < <(env_globs "${TS_ALLOW_PROTECTED:-}")
    echo "protected list: ${prot[*]} (exceptions: ${exc[*]:-none}; approved: ${allow[*]:-none})"
    # 1. protected paths
    while IFS= read -r p; do
        [ ${#exc[@]} -gt 0 ] && path_in_globs "$p" "${exc[@]}" && continue
        path_in_globs "$p" "${prot[@]}" || continue
        if [ ${#allow[@]} -gt 0 ] && path_in_globs "$p" "${allow[@]}"; then approved="$approved $p"; else hits="$hits $p"; fi
    done < <(ctx_paths)
    [ -n "$approved" ] && echo "protected: approved by TS_ALLOW_PROTECTED:$approved"
    if [ -n "$hits" ]; then
        echo "protected: the change touches protected paths:$hits"; rc=1; summary="$summary protected:$hits;"
    else
        echo "protected: no unapproved protected path touched"
    fi
    # 2. process creation / program loading
    calls=$(added_lines '[.](c|h|inc)$' | grep -vE '^scripts/g[12]_canary/' |
            awk -F'\t' '{ l = $2; gsub(/\/\*.*\*\//, "", l); sub(/\/\/.*$/, "", l)
                          if (l !~ /^[[:space:]]*(\*|\/\*)/) print $1 "\t" l }' | grep -E "$DP_CALLS" || true)
    if [ -n "$calls" ]; then
        echo "calls: new process creation / program loading:"; echo "$calls" | sed 's/^/  /'
        rc=1; summary="$summary new $(echo "$calls" | grep -oE "$DP_CALLS" | grep -oE '[A-Za-z_]+' | sort -u | tr '\n' ' ')call(s);"
    else
        echo "calls: no new system/popen/exec*/fork/posix_spawn/dlopen"
    fi
    # 3. new TLV types
    while IFS= read -r name; do
        case "$name" in *_LEN|*_MAX|*_MIN|*_CAP|*_SIZE|*_OFF|*_HDR) continue;; esac
        grep -qx "$name" "$CHANGE_DIR/base_tlv_ids.txt" 2>/dev/null && continue
        tlv="$tlv $name"
    done < <(added_lines '[.](c|h)$' | cut -f2- | grep -E "$DP_TLV_DEF" | sed -E "s/$DP_TLV_DEF.*/\2/" | sort -u)
    if [ -f "$CHANGE_DIR/base/PROTOCOL.md" ]; then
        rows0=$(awk '/^## 5\./ { f = 1 } /^## 6\./ { f = 0 } f && /^\|[[:space:]]*[0-9]+[[:space:]]*\|/' \
                "$CHANGE_DIR/base/PROTOCOL.md" | awk -F'|' '{ gsub(/[[:space:]]/, "", $2); print $2 }' | sort -u)
        rows1=$(awk '/^## 5\./ { f = 1 } /^## 6\./ { f = 0 } f && /^\|[[:space:]]*[0-9]+[[:space:]]*\|/' \
                PROTOCOL.md | awk -F'|' '{ gsub(/[[:space:]]/, "", $2); print $2 }' | sort -u)
        for t in $(comm -13 <(echo "$rows0") <(echo "$rows1")); do tlv="$tlv PROTOCOL.md:type-$t"; done
    fi
    if [ -n "$tlv" ]; then
        echo "tlv: new TLV type(s):$tlv (the control channel is closed, PROTOCOL.md section 5)"
        rc=1; summary="$summary new TLV type:$tlv;"
    else
        echo "tlv: no new TLV type"
    fi
    if [ $rc -ne 0 ]; then REASON="${summary# }"; REASON="${REASON%;}"; fi
    return $rc
}

# --------------------------------------------------------------- run list ----
ALL="build misra no-heap unit-tests property-tests sanitizers fuzz loopback budgets coverage compliance diff-policy"
declare -A NAMES=([build]="host + Pi" [misra]="cppcheck subset" [no-heap]="nm + daemon run" [unit-tests]="virtual clock" \
                  [property-tests]="round trips" [sanitizers]="ASan + UBSan" [fuzz]="smoke" [loopback]="with budgets" \
                  [budgets]="codec, size, stack" [coverage]="per module" [compliance]="diff rules" [diff-policy]="protected paths")
# the old codes, as arguments
declare -A OLD=([g0]=build [g1]=misra [g2]=no-heap [g3]=unit-tests [g4]=property-tests [g5]=sanitizers [g6]=fuzz [g7]=loopback [g8]=budgets [g9]=coverage [g10]=compliance [dp]=diff-policy)
LIST="${*:-$ALL}"
T0=$(now_ms)
for g in $LIST; do
    g=$(echo "$g" | tr 'A-Z' 'a-z')
    g="${OLD[$g]:-$g}"
    case " $ALL " in
        *" $g "*) gate "$g" "${NAMES[$g]}" "gate_${g//-/_}";;
        *) echo "unknown gate: $g"; exit 2;;
    esac
done
T1=$(now_ms)
echo "---- ladder: $PASSES PASS, $FAILS FAIL, $SKIPS SKIP in $((T1-T0)) ms (logs in $LOG_DIR)"
if [ "$FAILS" -ne 0 ]; then
    exit 1
fi
if [ -n "$SKIPPED" ]; then
    echo "---- LADDER INCOMPLETE: skipped$SKIPPED (only fuzz may skip)"
    exit 3
fi
exit 0
