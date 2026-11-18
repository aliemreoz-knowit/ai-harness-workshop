#!/usr/bin/env sh
# build.sh DIR PRESET TARGET... -- configure + build some targets of a tree (inside the dev container).
# hil.py calls it through scripts/dockerbuild.sh with the station root as /work:
#   DIR = . (the station: tsrecv, tsnetem) or build-hil/cand/<name> (a candidate: tinystreamd)
set -eu
dir="$1"; preset="$2"; shift 2
cd "$dir"
cmake --preset "$preset" >/dev/null
cmake --build --preset "$preset" --target "$@"
