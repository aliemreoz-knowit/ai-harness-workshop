"""Shared fixtures: a throw-away git repo shaped like tinystream, the harness
config pointed at it, a container-style environment and a fake check.sh.

No test starts the Copilot CLI or sends a prompt: agent sessions come from
``harness.fake.FakeRuntime``; check.sh is replaced by a fake subprocess runner
that prints check.sh-style result lines and writes gate logs.
"""

from __future__ import annotations

import functools
import subprocess
import sys
from pathlib import Path
from typing import Callable

import pytest

from harness.config import HarnessConfig, load_config
from harness.envdetect import ZIG_VERSION, Environment
from harness.gates import GateRunner


def pytest_configure(config: pytest.Config) -> None:
    config.addinivalue_line("markers", "zig: builds C with zig cc (the ziglang pip package); skipped without it")


@functools.lru_cache(maxsize=1)
def zig_works() -> bool:
    try:
        return subprocess.run([sys.executable, "-m", "ziglang", "version"], capture_output=True,
                              timeout=120).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def pytest_collection_modifyitems(config: pytest.Config, items: list[pytest.Item]) -> None:
    marked = [item for item in items if item.get_closest_marker("zig")]
    if marked and not zig_works():
        skip = pytest.mark.skip(reason=f"python -m ziglang version fails (pip install ziglang=={ZIG_VERSION})")
        for item in marked:
            item.add_marker(skip)

FILES = {
    ".gitignore": ".harness/\nbuild-*/\n",
    "PROTOCOL.md": "# protocol\n\nPROTOCOL_VERSION: 2\n\nbody\n",
    "include/ts_proto.h": "/*\n * tinystream v2 -- include/ts_proto.h\n */\nint ts_parse(int x);\n",
    "src/proto/proto.c": "/*\n * tinystream v2 -- src/proto/proto.c\n */\nint ts_parse(int x)\n{\n    return x + 1;\n}\n",
    "src/pool/pool.c": "/*\n * tinystream v2 -- src/pool/pool.c\n */\nint pool;\n",
    "tests/unit/test_proto.c": "/*\n * tinystream v2 -- tests/unit/test_proto.c\n */\nvoid test_parse(void) {}\n",
    "scripts/check.sh": "#!/usr/bin/env bash\necho fake\n",
    "docs/ARCHITECTURE.md": "# architecture\n",
}

TICKET = """# DEMO-7: Viewer shows wrong numbers

**Type:** Bug
**Component:** daemon

## Description

The counter on the board is off by one after every restart.

## Steps to reproduce

1. Start the daemon.
2. Watch the board.

## Expected

The right number.
"""


def git(repo: Path, *args: str) -> str:
    return subprocess.run(["git", "-C", str(repo), *args], check=True, capture_output=True, text=True).stdout


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # identity for commits in the throw-away repos only (environment, never git config)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Harness Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "harness-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Harness Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "harness-test@example.invalid")
    monkeypatch.delenv("HARNESS_RUNTIME", raising=False)
    monkeypatch.delenv("HARNESS_RETRY_BUDGET", raising=False)


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    root = tmp_path / "tinystream"
    for rel, text in FILES.items():
        p = root / rel
        p.parent.mkdir(parents=True, exist_ok=True)
        p.write_text(text, encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(root)], check=True)
    git(root, "add", "-A")
    git(root, "commit", "-q", "-m", "seed")
    return root


@pytest.fixture
def ticket_file(tmp_path: Path) -> Path:
    p = tmp_path / "DEMO-7.md"
    p.write_text(TICKET, encoding="utf-8")
    return p


@pytest.fixture
def cfg(repo: Path) -> HarnessConfig:
    return load_config(repo=repo, env={})


@pytest.fixture
def container_env() -> Environment:
    return Environment(in_container=True, system="Linux", bash="bash", docker=None, devcontainer=True)


CHECK_FNS = {"build", "misra", "no-heap", "unit-tests", "property-tests", "sanitizers", "fuzz", "loopback",
             "budgets", "coverage", "compliance", "diff-policy"}   # check.sh's gate names


class FakeCheck:
    """Stands in for ``bash scripts/check.sh build unit-tests ...``.

    ``decide(gate_fn, repo) -> (status, reason, log)``; default: everything PASSes.
    """

    def __init__(self, cfg: HarnessConfig,
                 decide: Callable[[str, Path], tuple[str, str, str]] | None = None) -> None:
        self.cfg = cfg
        self.decide = decide or (lambda fn, repo: ("PASS", "", ""))
        self.calls: list[list[str]] = []
        self.envs: list[dict[str, str]] = []

    def __call__(self, cmd: list[str], cwd: str | None = None, env: dict[str, str] | None = None,
                 **kw: object) -> subprocess.CompletedProcess:
        self.calls.append(list(cmd))
        self.envs.append(dict(env or {}))
        fns = [c for c in cmd if c in CHECK_FNS]
        lines = []
        rc = 0
        log_dir = self.cfg.check_log_dir
        log_dir.mkdir(parents=True, exist_ok=True)
        for fn in fns:
            status, reason, log = self.decide(fn, self.cfg.repo)
            gid = fn
            (log_dir / f"{gid}.log").write_text(log, encoding="utf-8")
            lines.append(f"{gid:<14} {status:<4} {120:6d} ms (budget {2:3d} s)  {'name-of-' + fn:<22} {reason}")
            if status == "FAIL":
                rc = 1
        lines.append(f"---- ladder: done (logs in {log_dir})")
        return subprocess.CompletedProcess(cmd, rc, "\n".join(lines) + "\n", "")


@pytest.fixture
def fake_check(cfg: HarnessConfig) -> FakeCheck:
    return FakeCheck(cfg)


@pytest.fixture
def gate_runner(cfg: HarnessConfig, container_env: Environment, fake_check: FakeCheck) -> GateRunner:
    return GateRunner(cfg, container_env, runner=fake_check)
