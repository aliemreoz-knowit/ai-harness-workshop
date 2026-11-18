#!/usr/bin/env bash
# test.sh <preset> [ctest args...] -- run ctest for a preset inside the dev container.
# The preset must have been built (scripts/build.sh <preset>); it is built if missing.
# Extra arguments are passed to ctest, e.g. scripts/test.sh host -L unit -R proto
set -euo pipefail
PRESET="${1:-host}"
shift || true
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
ARGS=""
for a in "$@"; do ARGS="$ARGS '$a'"; done
exec "$SCRIPT_DIR/dockerbuild.sh" sh -c "[ -f build-$PRESET/CMakeCache.txt ] || cmake --preset '$PRESET'; cmake --build --preset '$PRESET' --parallel && ctest --preset '$PRESET' --output-on-failure $ARGS"
