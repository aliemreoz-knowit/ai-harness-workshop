#!/usr/bin/env bash
# dockerbuild.sh -- run a command inside the tinystream-dev image.
#
# Usage: scripts/dockerbuild.sh [--name NAME] <command> [args...]
#        scripts/dockerbuild.sh            (interactive shell)
#
# The CURRENT directory is mounted at /work (-w /work). The image is built from
# .devcontainer/Dockerfile when it is missing. Works from Git Bash on Windows:
# MSYS_NO_PATHCONV=1 stops MSYS from mangling "/work", and "pwd -W" yields a
# Windows-style path that Docker Desktop accepts as a bind-mount source.
# When already inside the container (TS_IN_CONTAINER=1) the command runs directly.
# --name NAME (or TS_CONTAINER_NAME) names the container: a caller that has to
# stop it after a timeout can "docker kill NAME" (killing this script's docker
# client leaves the container running). HARNESS_RUN_ID set: the container gets
# the label tinystream.run=<it>, so every container of one harness run can be
# found (docker ps --filter label=tinystream.run=<id>) and stopped with it.
# .git of the mounted directory (a file in a linked worktree, a directory in a main
# checkout) is mounted read-only at /work/.git: the code that runs here (participant C
# code, tests) can write the worktree but must not plant a gitfile, a config or a hook
# that the host's git follows later (core.fsmonitor, hook.<name>.command, filter drivers).
# Nothing in the container needs to write .git: check.sh prepares the change context
# with the host's git, and in here git only reads.
set -euo pipefail

IMAGE="${TS_IMAGE:-tinystream-dev:latest}"
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_DIR="$(cd "$SCRIPT_DIR/.." && pwd)"
NAME="${TS_CONTAINER_NAME:-}"
if [ "${1:-}" = "--name" ] && [ $# -ge 2 ]; then
    NAME="$2"
    shift 2
fi

if [ "${TS_IN_CONTAINER:-0}" = "1" ] || [ -f /.dockerenv ]; then
    exec "$@"
fi

if ! docker image inspect "$IMAGE" >/dev/null 2>&1; then
    echo "[dockerbuild] image $IMAGE not found, building from $REPO_DIR/.devcontainer/Dockerfile" >&2
    # Windows-style repo path (Git Bash): with MSYS_NO_PATHCONV=1 a /c/... path never reaches Docker Desktop
    REPO_W="$(cd "$REPO_DIR" && pwd -W 2>/dev/null)" || REPO_W=""
    [ -n "$REPO_W" ] || REPO_W="$REPO_DIR"
    MSYS_NO_PATHCONV=1 docker build --platform linux/amd64 -t "$IMAGE" -f "$REPO_W/.devcontainer/Dockerfile" "$REPO_W"
fi

# Windows-style path for Docker Desktop, plain path elsewhere.
if HOST_DIR="$(pwd -W 2>/dev/null)" && [ -n "$HOST_DIR" ]; then
    :
else
    HOST_DIR="$(pwd)"
fi

TTY_FLAGS=()
if [ -t 0 ] && [ -t 1 ]; then
    TTY_FLAGS=(-it)
fi

# Forward harness knobs (TS_FUZZ, FUZZ_SECS, TS_CHECK_LOG, ...) into the container.
ENV_FLAGS=()
while IFS='=' read -r name _; do
    ENV_FLAGS+=(-e "$name")
done < <(env | grep -E '^(TS_[A-Z0-9_]+|FUZZ_SECS)=' | grep -vE '^TS_(IN_CONTAINER|CONTAINER_NAME)=' || true)

NAME_FLAGS=()
if [ -n "$NAME" ]; then
    NAME_FLAGS=(--name "$NAME")
fi
if [ -n "${HARNESS_RUN_ID:-}" ]; then
    NAME_FLAGS+=(--label "tinystream.run=$HARNESS_RUN_ID")
fi

GIT_FLAGS=()
if [ -e .git ]; then      # a missing source would be created on the host as an empty directory
    GIT_FLAGS=(-v "$HOST_DIR/.git:/work/.git:ro")
fi

if [ "$#" -eq 0 ]; then
    set -- /bin/bash
fi

exec env MSYS_NO_PATHCONV=1 docker run --rm --platform linux/amd64 "${TTY_FLAGS[@]}" "${ENV_FLAGS[@]}" \
    "${NAME_FLAGS[@]}" -e HOME=/tmp -e TS_IN_CONTAINER=1 \
    -v "$HOST_DIR:/work" "${GIT_FLAGS[@]}" -w /work \
    "$IMAGE" "$@"
