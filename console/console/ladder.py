"""The gate ladder of a personal console, decided once at start (never again, never by a run).

The harness runs its gates in the ``tinystream-dev:latest`` container when Docker is usable, and builds that ~1 GB image
on first use; with ``TS_LITE=1`` it runs the Docker-free ladder (zig). Left to itself, the Today page's setup check or a
"Check my code" would start that build in the background and undo a setup made with ``--no-docker``. So the console
decides at start (:func:`decide`):

* **full** when the console's own environment has ``TS_LITE=0`` (the stage rule: the harness never goes lite), without
  reading the marker or asking Docker; Docker stays visible, so with Docker down the gates fail as in the container;
* **lite** when the console's own environment has ``TS_LITE=1`` (why: ``ts-lite``), when the setup wrote ``lite`` in
  ``.tools/ladder`` of the checkout (``--no-docker``, no Docker, a failed image build), or when
  ``docker image inspect tinystream-dev:latest`` fails (no image);
* **full** otherwise: the image exists and the marker does not say lite.

In lite :func:`env` is added to every run, "Check my code" and the doctor: ``TS_LITE=1`` and a ``DOCKER_HOST`` that
points nowhere (the setup hides Docker from the doctor the same way), so nothing can start an image build. In full it
passes the console's own ``TS_LITE`` (``0`` or ``1``) on, so ``TS_LITE=0`` reaches the harness. The setup rewrites the
marker on every run, so a participant who installs Docker later and runs the setup again without ``--no-docker`` gets
``full``.
"""

from __future__ import annotations

import os
import subprocess
from pathlib import Path
from typing import Callable

__all__ = ["HIDDEN_DOCKER_HOST", "IMAGE", "MARKER", "NO_DOCKER", "NO_IMAGE", "TS_LITE", "decide", "env", "image_exists",
           "read_marker"]

IMAGE = "tinystream-dev:latest"
MARKER = Path(".tools") / "ladder"  # in the checkout; the setup writes ``lite`` or ``full``
HIDDEN_DOCKER_HOST = (  # as the setup scripts: an address nobody listens on
    "npipe:////./pipe/tinystream_no_docker" if os.name == "nt" else "unix:///nonexistent/tinystream-no-docker.sock"
)
_INSPECT_TIMEOUT_S = 20.0

NO_DOCKER = "no-docker"  # the setup recorded "no Docker" (the marker says lite)
NO_IMAGE = "no-image"  # the marker does not say lite, but the image is not there
TS_LITE = "ts-lite"  # the console was started with TS_LITE=1


def read_marker(root: Path) -> str:
    """``lite`` or ``full`` as the setup wrote it; ``""`` when the file is missing or says anything else."""
    try:
        text = (Path(root) / MARKER).read_text(encoding="utf-8", errors="replace").strip().lower()
    except OSError:
        return ""
    return text if text in ("lite", "full") else ""


def image_exists() -> bool:
    """Whether ``docker image inspect tinystream-dev:latest`` succeeds. Any failure (no docker, not running, slow) is
    "no image". Never builds anything."""
    try:
        done = subprocess.run(
            ["docker", "image", "inspect", IMAGE], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
            stderr=subprocess.DEVNULL, timeout=_INSPECT_TIMEOUT_S, check=False,
        )  # fmt: skip
    except (OSError, subprocess.SubprocessError):
        return False
    return done.returncode == 0


def decide(root: Path, image_exists: Callable[[], bool] | None = None) -> tuple[str, str]:
    """``(ladder, why)``: ``("full", "")``, ``("lite", "ts-lite")`` (``TS_LITE=1``), ``("lite", "no-docker")`` (marker)
    or ``("lite", "no-image")``. Under ``TS_LITE=0`` or ``1`` nothing is read or asked; Docker is not asked when the
    marker says lite."""
    forced = os.environ.get("TS_LITE")
    if forced == "0":  # the stage rule: never lite, whatever the marker says or Docker answers now
        return "full", ""
    if forced == "1":  # the console was started in the Docker-free ladder: runs and Today agree
        return "lite", TS_LITE
    if read_marker(root) == "lite":
        return "lite", NO_DOCKER
    ask = image_exists or globals()["image_exists"]  # looked up now: a test's monkeypatch of the module's reaches it
    if ask():
        return "full", ""
    return "lite", NO_IMAGE


def env(ladder: str) -> dict[str, str]:
    """What a personal console adds to the environment of every run, "Check my code" and the doctor (the room's console
    adds nothing: its callers ask only in personal mode). Lite: ``TS_LITE=1`` and Docker hidden. Otherwise the console's
    own ``TS_LITE`` when it is ``0`` or ``1`` (Docker untouched), else nothing."""
    if ladder == "lite":
        return {"TS_LITE": "1", "DOCKER_HOST": HIDDEN_DOCKER_HOST}
    value = os.environ.get("TS_LITE")
    return {"TS_LITE": value} if value in ("0", "1") else {}
