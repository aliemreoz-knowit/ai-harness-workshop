# tinystream v2 development image (AI Harness Workshop)
# Debian bookworm: gcc 12, clang-14 (+ compiler-rt for libFuzzer/ASan), cmake, ninja,
# cppcheck (MISRA addon), lcov/gcovr, qemu-user-static + aarch64 cross toolchain,
# Node 22 + pinned Copilot CLI 1.0.100, Python 3.11 venv with github-copilot-sdk 1.0.23
# and pytest 9.0.3 (python -m pytest harness/tests harness/eval/tests).
#
# Reproducibility: the gates depend on exact tool versions (cppcheck decides the
# MISRA rule numbering the misra gate relies on, gcovr the coverage numbers, gcc the
# warnings), so everything is pinned to the state the golden tree was verified
# with (image built 2026-10-20):
#   - the base image by its dated tag and platform (append "@sha256:<digest>",
#     from `docker buildx imagetools inspect debian:bookworm-20261005`, to pin it fully);
#   - Debian packages by exact version from snapshot.debian.org at SNAPSHOT, so
#     the pinned versions stay installable after the live mirror moves on;
#   - Node.js by exact nodesource package version;
#   - pip and every Python package by exact version (.devcontainer/requirements.lock).
ARG BASE_IMAGE=debian:bookworm-20261005
FROM --platform=linux/amd64 ${BASE_IMAGE}

ARG SNAPSHOT=20261020T183000Z
ENV DEBIAN_FRONTEND=noninteractive

# Debian archive frozen at SNAPSHOT (snapshot Release files are past their Valid-Until)
RUN rm -f /etc/apt/sources.list.d/debian.sources /etc/apt/sources.list \
    && printf '%s\n' \
        "Types: deb" \
        "URIs: http://snapshot.debian.org/archive/debian/${SNAPSHOT}" \
        "Suites: bookworm bookworm-updates" \
        "Components: main" \
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" \
        "" \
        "Types: deb" \
        "URIs: http://snapshot.debian.org/archive/debian-security/${SNAPSHOT}" \
        "Suites: bookworm-security" \
        "Components: main" \
        "Signed-By: /usr/share/keyrings/debian-archive-keyring.gpg" \
        > /etc/apt/sources.list.d/snapshot.sources \
    && echo 'Acquire::Check-Valid-Until "false";' > /etc/apt/apt.conf.d/99snapshot

RUN apt-get update && apt-get install -y --no-install-recommends \
        build-essential=12.9 cmake=3.25.1-1 ninja-build=1.11.1-2~deb12u1 \
        gcc-12=12.2.0-14+deb12u1 \
        clang-14=1:14.0.6-12 libclang-rt-14-dev=1:14.0.6-12 \
        cppcheck=2.10-2 lcov=1.16-1 gcovr=5.2-1 \
        qemu-user-static=1:7.2+dfsg-7+deb12u18+b3 \
        gcc-aarch64-linux-gnu=4:12.2.0-3 libc6-dev-arm64-cross=2.36-8cross1 \
        git=1:2.39.5-0+deb12u3 python3=3.11.2-1+b1 python3-pip=23.0.1+dfsg-1 python3-venv=3.11.2-1+b1 \
        curl=7.88.1-10+deb12u15 ca-certificates=20250419~deb12u1 gnupg=2.2.40-1.1+deb12u2 \
        libsdl2-dev=2.26.5+dfsg-1 \
    && rm -rf /var/lib/apt/lists/*

# Node 22 from the nodesource apt repository, exact version (no piped setup script)
ARG NODE_VERSION=22.23.3-1nodesource1
RUN mkdir -p /etc/apt/keyrings \
    && curl -fsSL https://deb.nodesource.com/gpgkey/nodesource-repo.gpg.key \
        | gpg --dearmor -o /etc/apt/keyrings/nodesource.gpg \
    && echo "deb [signed-by=/etc/apt/keyrings/nodesource.gpg] https://deb.nodesource.com/node_22.x nodistro main" \
        > /etc/apt/sources.list.d/nodesource.list \
    && apt-get update && apt-get install -y --no-install-recommends "nodejs=${NODE_VERSION}" \
    && rm -rf /var/lib/apt/lists/*

# Pinned Copilot CLI (no auto update, no runtime download at run time)
ENV COPILOT_AUTO_UPDATE=false \
    COPILOT_SKIP_CLI_DOWNLOAD=1
RUN npm i -g @github/copilot@1.0.100 && npm cache clean --force

# Python venv with the pinned Copilot SDK, pytest and their pinned dependencies (bookworm python is 3.11)
COPY .devcontainer/requirements.lock /opt/requirements.lock
RUN python3 -m venv /opt/venv \
    && /opt/venv/bin/pip install --no-cache-dir pip==26.2.2 \
    && /opt/venv/bin/pip install --no-cache-dir --no-deps -r /opt/requirements.lock \
    && /opt/venv/bin/pip check

# The image is linux/amd64 (FROM --platform above), so the x64 CLI binary is the right one.
ENV COPILOT_CLI_PATH=/usr/lib/node_modules/@github/copilot/node_modules/@github/copilot-linux-x64/copilot \
    PATH=/opt/venv/bin:/usr/lib/node_modules/.bin:/usr/local/bin:/usr/bin:/bin:/usr/local/sbin:/usr/sbin:/sbin

WORKDIR /work
CMD ["/bin/bash"]
