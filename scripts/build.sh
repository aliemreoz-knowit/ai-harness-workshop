#!/usr/bin/env bash
# build.sh <preset> -- configure + build a CMake preset inside the dev container.
# Presets: host | asan | fuzz | pi | coverage   (see CMakePresets.json)
set -euo pipefail
PRESET="${1:-host}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
exec "$SCRIPT_DIR/dockerbuild.sh" sh -c "cmake --preset '$PRESET' && cmake --build --preset '$PRESET' --parallel"
