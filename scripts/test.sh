#!/usr/bin/env bash
# test.sh <preset> [ctest args...] -- run ctest for a preset with zig cc (no Docker; TS_LITE=0: in the dev container).
# The preset must have been built (scripts/build.sh <preset>); it is built if missing.
# Extra arguments are passed to ctest, e.g. scripts/test.sh host -L unit -R proto
set -euo pipefail
PRESET="${1:-host}"
shift || true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
if [ "${TS_LITE:-1}" != "0" ] && [ "${TS_IN_CONTAINER:-0}" != "1" ] && [ ! -f /.dockerenv ]; then
    # no Docker: zig cc through scripts/lite_gates.py (TS_LITE=0 asks for the container)
    cd "$SCRIPT_DIR/.." || exit 2
    PY="${TS_PYTHON:-}"
    for cand in .venv/Scripts/python.exe .venv/bin/python python3 python; do
        [ -n "$PY" ] && break
        if [ -x "$cand" ] || command -v "$cand" >/dev/null 2>&1; then PY="$cand"; fi
    done
    exec "$PY" scripts/lite_gates.py --ctest "$PRESET" "$@"
fi
ARGS=""
for a in "$@"; do ARGS="$ARGS '$a'"; done
exec "$SCRIPT_DIR/dockerbuild.sh" sh -c "[ -f build-$PRESET/CMakeCache.txt ] || cmake --preset '$PRESET'; cmake --build --preset '$PRESET' --parallel && ctest --preset '$PRESET' --output-on-failure $ARGS"
