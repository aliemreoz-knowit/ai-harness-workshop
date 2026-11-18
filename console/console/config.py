"""Console configuration: ``console.yaml`` plus a few environment overrides.

Resolution rules:

* Relative paths (``data_dir``, ``source_repo``, ``harness_repo``, ``hidden_tests``,
  ``env_file``) resolve against the directory of the config file, never the current directory.
* ``CONSOLE_HARNESS_REPO``, ``CONSOLE_DATA_DIR`` and ``CONSOLE_PORT`` override the file
  (an empty value counts as unset).
* ``harness_repo``: env, else the file, else ``<config dir>/harness-repo``. The harness YAML
  reader parses the file, so the repo is put on ``sys.path`` before the file is parsed.
* Lists replace the defaults; ``lanes``, ``jira`` and ``github`` are merged over the defaults.

``editable`` holds glob patterns (spec section 6). An entry starting with ``!`` removes
paths from the editable set (``!tools/*.py``); the exclusion wins over any positive entry.

Personal mode (``load_config(..., personal_root=<checkout>)``, ``serve --personal``): one participant's console on
their own laptop, built from their own checkout of the public repo (``harness/`` at its top, the console in
``console/``; ``find_personal_root`` finds it). The checkout is the source and the harness, the data folder is
``<checkout>/.console-data``, the console listens on loopback with one unit and no login. Those values replace what
the file and the environment say (``_PERSONAL``); the rest (the caps ``unit_budget`` and ``run_cap``, ``port``, the
globs, ``harness_cmd``) still comes from the file, ``CONSOLE_PORT`` included.
"""

from __future__ import annotations

import copy
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Mapping, Sequence

from console import harnesslink

__all__ = ["ConfigError", "ConsoleConfig", "DEFAULTS", "PERSONAL_DATA_DIR", "find_personal_root", "load_config"]

DEFAULT_CONFIG = Path(__file__).resolve().parents[1] / "console.yaml"
PERSONAL_DATA_DIR = ".console-data"  # personal mode: the data folder, inside the checkout (it ignores itself in git)

# Neutral values for a key the file leaves out (room mode's console.yaml at the repo root sets its own).
DEFAULTS: dict[str, Any] = {
    "host": "0.0.0.0",
    "port": 8765,
    "allowed_hosts": [],  # extra names a browser may use (Host header), besides localhost and this machine's addresses
    "units": 10,
    "start_ref": "main",
    "catchup_refs": [],
    "unit_budget": 60,
    "run_cap": 12,
    "lanes": {"agent": 3, "gates": 2},
    "allowed_models": ["claude-haiku-5.5", "claude-sonnet-5.5"],
    "data_dir": ".console-data",
    "source_repo": "source-repo",
    "harness_repo": "harness-repo",
    "hidden_tests": ".no-hidden-tests",
    "env_file": ".no-env",
    # Spec section 6: what a unit may edit. "!" entries carve exclusions out of the entries above them: the
    # scripts of tools/, and (R18) every Python file of the harness and the files Python runs by their names.
    "editable": [
        "src/**",
        "tests/**",
        "fuzz/**",
        "tools/**",
        "!tools/*.py",
        "!tools/**/*.py",
        "!tools/*.sh",
        "!tools/**/*.sh",
        "!tools/*.ps1",
        "!tools/**/*.ps1",
        "PROTOCOL.md",
        "AGENTS.md",
        "tickets/**",
        "docs/usage-guide.md",
        ".github/agents/**",
        ".github/skills/**/*.md",
        ".github/instructions/**",
        ".github/copilot-instructions.md",
        ".github/hooks/*.json",
        "harness/harness.yaml",
        "harness/gates.yaml",
        "harness/workflows/**/*.yaml",
        "harness/workflows/**/*.yml",
        "harness/prompts/**/*.md",
        "!harness/**/*.py",
        "!**/__init__.*",
        "!**/*.pth",
        "!**/sitecustomize.py",
        "!**/usercustomize.py",
    ],
    # Spec sections 4 and 6: the harness structure a run may take from another unit.
    "overlay": [
        "AGENTS.md",
        ".github/agents/**",
        ".github/skills/**",
        ".github/instructions/**",
        ".github/copilot-instructions.md",
        ".github/hooks/*.json",
        "harness/harness.yaml",
        "harness/gates.yaml",
        "harness/workflows/**",
        "harness/prompts/**",
    ],
    "harness_cmd": ["{python}", "-m", "harness"],
    "python": "",  # "" = the interpreter running the console
    "jira": {},
    "github": {},
}

_PATH_KEYS = ("data_dir", "source_repo", "harness_repo", "hidden_tests", "env_file")
_MAPPING_KEYS = ("lanes", "jira", "github")

# Personal mode: what replaces the file's values, whatever it says. The paths are below the checkout (``_personal_paths``).
_PERSONAL: dict[str, Any] = {
    "host": "127.0.0.1",
    "allowed_hosts": [],
    "units": 1,
    "lanes": {"agent": 1, "gates": 1},
    "start_ref": "main",
    "catchup_refs": [],
}


class ConfigError(ValueError):
    """The config file or an override is invalid."""


@dataclass(frozen=True)
class ConsoleConfig:
    root: Path  # directory of the config file
    data_dir: Path
    source_repo: Path
    harness_repo: Path
    hidden_tests: Path
    env_file: Path
    host: str
    port: int
    allowed_hosts: tuple[str, ...]
    units: int
    start_ref: str
    catchup_refs: tuple[str, ...]
    unit_budget: float
    run_cap: float
    lanes: dict[str, int]
    allowed_models: tuple[str, ...]
    editable: tuple[str, ...]
    overlay: tuple[str, ...]
    python: str
    harness_cmd: tuple[str, ...]  # "{python}" is filled in when a process is spawned
    jira: dict
    github: dict
    personal: bool = False  # personal mode: loopback, no login, one unit from the participant's checkout
    runtime: str = "facilitator"  # the harness runtime of a run (``copilot-user`` in personal mode: their own login)
    ladder: str = ""  # personal mode: ``lite`` or ``full``, decided once at start (``console.ladder``); "" in the room
    ladder_why: str = ""  # lite only: ``ts-lite`` (TS_LITE=1), ``no-docker`` (the setup recorded it) or ``no-image``

    @property
    def harness_argv(self) -> list[str]:
        """``harness_cmd`` with ``{python}`` replaced by the interpreter: the command that starts the harness."""
        return [part.replace("{python}", self.python) for part in self.harness_cmd]


def find_personal_root(start: Path | None = None) -> Path:
    """The participant's checkout for personal mode: the first folder at or above ``start`` (default: this package's
    folder) that holds a ``harness/`` folder and a ``.git``. In the public repo the package is ``<root>/console/console``,
    so the walk passes ``<root>/console`` (no ``harness/``) and stops at ``<root>``.

    ``RuntimeError`` when there is none: a downloaded zip is no git repository until the setup script has made it one.
    """
    here = Path(__file__).resolve().parent if start is None else Path(start).resolve()
    for folder in (here, *here.parents):
        if (folder / "harness").is_dir() and (folder / ".git").exists():
            return folder
    raise RuntimeError(
        f"no checkout found at or above {here}: personal mode needs the folder that holds harness/ and .git "
        "(run the setup script first: it makes a downloaded zip a git repository)"
    )


def load_config(
    path: Path | None = None, env: Mapping[str, str] | None = None, personal_root: Path | None = None
) -> ConsoleConfig:
    """Read ``path`` (default: ``console.yaml`` next to the package) and apply ``env`` overrides.

    With ``personal_root`` (the participant's checkout) the config is personal mode's: ``_PERSONAL`` and the paths
    below the checkout replace the file's and the environment's values, ``personal`` is True and ``runtime`` is
    ``copilot-user``. The file is still read and checked: it gives everything else.
    """
    environ = os.environ if env is None else env
    cfg_path = (Path(path) if path is not None else DEFAULT_CONFIG).resolve()
    text = cfg_path.read_text(encoding="utf-8-sig")  # FileNotFoundError names the file
    base = cfg_path.parent
    personal = None if personal_root is None else Path(personal_root).resolve()

    if personal is not None:  # the checkout is the harness repo, whatever the file names
        linked = personal
    else:
        repo_hint = _env(environ, "CONSOLE_HARNESS_REPO") or _peek_harness_repo(text) or DEFAULTS["harness_repo"]
        linked = _path("harness_repo", repo_hint, base)
    harnesslink.bootstrap(linked)
    parsed = harnesslink.yaml_load(text)

    if parsed is None:
        parsed = {}
    if not isinstance(parsed, dict):
        raise ConfigError(f"{cfg_path.name}: the top level must be a mapping of settings")
    unknown = sorted(str(k) for k in parsed if k not in DEFAULTS)
    if unknown:
        raise ConfigError(f"{cfg_path.name}: unknown setting(s): {', '.join(unknown)}")
    given = {k: v for k, v in parsed.items() if v is not None}
    values = {k: copy.deepcopy(v) for k, v in DEFAULTS.items()}
    for key, value in given.items():
        if key in _MAPPING_KEYS:
            values[key] = {**values[key], **_mapping(key, value)}
        else:
            values[key] = value

    if (v := _env(environ, "CONSOLE_HARNESS_REPO")) is not None:
        values["harness_repo"] = v
    if (v := _env(environ, "CONSOLE_DATA_DIR")) is not None:
        values["data_dir"] = v
    port = values["port"]
    if (v := _env(environ, "CONSOLE_PORT")) is not None:
        try:
            port = int(v)
        except ValueError:
            raise ConfigError(f"CONSOLE_PORT must be an integer, got {v!r}") from None

    if personal is not None:
        values.update(copy.deepcopy(_PERSONAL))
        paths = _personal_paths(personal)
    else:
        paths = {k: _path(k, values[k], base) for k in _PATH_KEYS}
        if paths["harness_repo"] != linked:  # the file named a repo the pre-scan did not see
            harnesslink.bootstrap(paths["harness_repo"])

    lanes = {k: _int(f"lanes.{k}", v, 1, None) for k, v in values["lanes"].items()}
    return ConsoleConfig(
        root=base,
        **paths,
        host=_str("host", values["host"]),
        port=_int("port", port, 0, 65535),
        allowed_hosts=_str_seq("allowed_hosts", values["allowed_hosts"]),
        units=_int("units", values["units"], 1, 99),
        start_ref=_str("start_ref", values["start_ref"]),
        catchup_refs=_str_seq("catchup_refs", values["catchup_refs"]),
        unit_budget=_num("unit_budget", values["unit_budget"], 0, allow_min=True),
        run_cap=_num("run_cap", values["run_cap"], 0, allow_min=False),
        lanes=lanes,
        allowed_models=_str_seq("allowed_models", values["allowed_models"]),
        editable=_str_seq("editable", values["editable"]),
        overlay=_str_seq("overlay", values["overlay"]),
        python=_str("python", values["python"]) or sys.executable,
        harness_cmd=_str_seq("harness_cmd", values["harness_cmd"], non_empty=True),
        jira=dict(values["jira"]),
        github=dict(values["github"]),
        personal=personal is not None,
        runtime="copilot-user" if personal is not None else "facilitator",
    )


def _personal_paths(root: Path) -> dict[str, Path]:
    """Personal mode's paths: the checkout is source and harness; data, hidden tests and ``.env`` are below it. The
    last two never exist: no hidden tests on a participant's laptop, and no facilitator credentials."""
    return {
        "data_dir": root / PERSONAL_DATA_DIR,
        "source_repo": root,
        "harness_repo": root,
        "hidden_tests": root / ".no-hidden-tests",
        "env_file": root / ".no-env",
    }


def _env(environ: Mapping[str, str], name: str) -> str | None:
    value = environ.get(name)
    return value if value else None


def _peek_harness_repo(text: str) -> str | None:
    """The top-level ``harness_repo:`` value, read without a YAML parser (none is loaded yet)."""
    for line in text.splitlines():
        if line.startswith("harness_repo:"):
            value = line[len("harness_repo:"):].strip()
            if value[:1] in ('"', "'"):
                end = value.find(value[0], 1)
                return value[1:end] if end > 0 else None
            return value.split(" #", 1)[0].strip() or None
    return None


def _path(key: str, value: Any, base: Path) -> Path:
    if not isinstance(value, str) or not value.strip():
        raise ConfigError(f"{key} must be a path, got {value!r}")
    p = Path(value.strip()).expanduser()
    return (p if p.is_absolute() else base / p).resolve()


def _str(key: str, value: Any) -> str:
    if not isinstance(value, str):
        raise ConfigError(f"{key} must be a string, got {value!r}")
    return value


def _int(key: str, value: Any, lo: int, hi: int | None) -> int:
    if isinstance(value, bool) or not isinstance(value, int):
        raise ConfigError(f"{key} must be a whole number, got {value!r}")
    if value < lo or (hi is not None and value > hi):
        bound = f"at least {lo}" if hi is None else f"between {lo} and {hi}"
        raise ConfigError(f"{key} must be {bound}, got {value}")
    return value


def _num(key: str, value: Any, lo: float, allow_min: bool) -> float:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        raise ConfigError(f"{key} must be a number, got {value!r}")
    if value < lo or (value == lo and not allow_min):
        raise ConfigError(f"{key} must be {'at least' if allow_min else 'more than'} {lo:g}, got {value}")
    return float(value)


def _str_seq(key: str, value: Any, non_empty: bool = False) -> tuple[str, ...]:
    if isinstance(value, str) or not isinstance(value, Sequence) or not all(isinstance(x, str) for x in value):
        raise ConfigError(f"{key} must be a list of strings, got {value!r}")
    if non_empty and not value:
        raise ConfigError(f"{key} must not be empty")
    return tuple(value)


def _mapping(key: str, value: Any) -> dict:
    if not isinstance(value, dict) or not all(isinstance(k, str) for k in value):
        raise ConfigError(f"{key} must be a mapping, got {value!r}")
    return value
