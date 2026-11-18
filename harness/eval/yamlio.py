"""YAML loading: PyYAML (safe loader) when installed, else the harness subset loader
(harness/yamlmini.py; the devcontainer venv has no PyYAML)."""
from __future__ import annotations

from pathlib import Path
from typing import Any

from .. import yamlmini


def load_yaml(path: Path) -> Any:
    with open(path, encoding="utf-8") as fh:
        return yamlmini.load(fh.read())
