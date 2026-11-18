"""Link to the facilitator's tinystream checkout.

The console imports ``harness.*`` (the harness package) and the flat modules under
``tools/bridge`` (``from jira_client import ...``) from that checkout instead of
copying them, so both directories must be on ``sys.path``.

Python keeps the first ``harness`` package it imports for the life of the process, so
call :func:`bootstrap` once, with the repo the console is configured for, before any
harness import.
"""

from __future__ import annotations

import os
import sys
from pathlib import Path
from typing import Any

__all__ = ["bootstrap", "names", "yaml_load"]


def bootstrap(harness_repo: Path) -> None:
    """Put ``harness_repo`` and ``harness_repo/tools/bridge`` on ``sys.path`` (idempotent).

    Raises ``RuntimeError("harness not found at <path>")`` when ``harness/__init__.py``
    is missing, and leaves ``sys.path`` untouched in that case.
    """
    repo = Path(os.path.abspath(harness_repo))
    if not (repo / "harness" / "__init__.py").is_file():
        raise RuntimeError(f"harness not found at {repo}")
    wanted = [str(repo), str(repo / "tools" / "bridge")]
    sys.path[0:0] = [p for p in wanted if p not in sys.path]


class _SameNames:
    """A harness without ``harness/names.py`` (older than the plain names): every id stays as it is."""

    @staticmethod
    def gate(name: str) -> str:
        return name.strip()

    @staticmethod
    def variant(name: str) -> str:
        return name.strip()


def names() -> Any:
    """``harness.names``: the plain names of gates and variants, and the old codes (build, raw) they replace.

    Needs :func:`bootstrap` first.
    """
    try:
        from harness import names as mod
    except ImportError:
        return _SameNames
    return mod


def yaml_load(text: str) -> Any:
    """Parse YAML with the harness's own loader (``harness.yamlmini.load``).

    Needs :func:`bootstrap` first. Uses PyYAML when installed, else the harness subset loader.
    """
    from harness.yamlmini import load

    return load(text)
