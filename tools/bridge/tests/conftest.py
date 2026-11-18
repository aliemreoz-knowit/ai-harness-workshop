"""Bridge test fixtures: recorded GitHub/Jira responses, a throw-away git repo with
a bare 'origin', and a scripted stand-in for ``python -m harness run``.

No test touches the network, starts the Copilot CLI or sends a prompt: GitHub
and Jira come from ReplayTransport (tests/fixtures/*), the harness subprocess
is replaced by ``FakeHarness`` (git commands still run for real, in tmp_path).
Helpers live in bridge_testlib.py.
"""

from __future__ import annotations

import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parent))
from bridge_testlib import FakeHarness, git  # noqa: E402


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """tmp/work (main, one commit) with a bare tmp/origin.git as 'origin'."""
    origin = tmp_path / "origin.git"
    git(tmp_path, "init", "--bare", "-q", str(origin))
    work = tmp_path / "work"
    work.mkdir()
    git(work, "init", "-q")
    (work / "README.md").write_text("tinystream (test)\n", encoding="utf-8")
    (work / ".gitignore").write_text(".harness/\n", encoding="utf-8")
    git(work, "add", "-A")
    git(work, "commit", "-q", "-m", "initial")
    git(work, "branch", "-M", "main")
    git(work, "remote", "add", "origin", str(origin))
    git(work, "push", "-q", "origin", "main")
    return work


@pytest.fixture
def fake_harness() -> FakeHarness:
    return FakeHarness()


@pytest.fixture
def config_file(tmp_path: Path) -> Path:
    path = tmp_path / "bridge.yaml"
    path.write_text(
        "github:\n  repo: acme/tinystream\n"
        "jira:\n  enabled: false\n  site: https://acme.atlassian.net\n  project: PROJ\n"
        "  trigger_statuses: [\"Agent Ready\"]\n  pr_transition: In Review\n"
        "harness:\n  fetch: false\n", encoding="utf-8")
    return path
