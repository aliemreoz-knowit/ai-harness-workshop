"""pytest setup for tools/hil: import path, the `docker` marker, shared helpers.

    python -m pytest tools/hil/tests -q                 # everything (docker tests need the dev image)
    python -m pytest tools/hil/tests -q -m "not docker" # no container: seconds

Environment for the end-to-end tests (marker `docker`):
  HIL_GOLDEN_REF    candidate that must PASS file, camera and keys (skipped when unset:
                    the known-good ref depends on the checkout, e.g. the station's
                    release tag)
  HIL_FAIL_REF      candidate that must FAIL the file check (default: b0-start)
  HIL_PIPE_BAD_REF  candidate whose pipe source reads frames in pieces
                    (camera check must FAIL, file check PASS); skipped when unset
  HIL_STREAM_BAD_REF candidate that does not recover after the cut and ignores
                    viewer keys (file + keys checks FAIL); skipped when unset
  HIL_SKIP_DOCKER=1 skip the docker tests
"""
from __future__ import annotations

import os
import shutil
import subprocess
import sys
from pathlib import Path

import pytest

HIL_DIR = Path(__file__).resolve().parent.parent
ROOT = HIL_DIR.parent.parent
sys.path.insert(0, str(HIL_DIR))


def _docker_ready() -> bool:
    if os.environ.get("HIL_SKIP_DOCKER") == "1":
        return False
    if os.environ.get("TS_IN_CONTAINER") == "1" or os.path.exists("/.dockerenv"):
        return True
    if shutil.which("docker") is None:
        return False
    image = os.environ.get("TS_IMAGE", "tinystream-dev:latest")
    try:
        return subprocess.run(["docker", "image", "inspect", image], capture_output=True,
                              timeout=30).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_configure(config):
    config.addinivalue_line("markers", "docker: needs the tinystream-dev image (builds and runs daemons)")


def pytest_collection_modifyitems(config, items):
    if _docker_ready():
        return
    skip = pytest.mark.skip(reason="dev container not available (or HIL_SKIP_DOCKER=1)")
    for item in items:
        if "docker" in item.keywords:
            item.add_marker(skip)


@pytest.fixture
def cfg():
    import hilcore
    return hilcore.load_config()


@pytest.fixture
def root() -> Path:
    return ROOT
