"""Fixtures: a throwaway git repo with ticket branches and a fake hidden-tests repo."""
from __future__ import annotations

import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

REPO_ROOT = Path(__file__).resolve().parents[3]
if str(REPO_ROOT) not in sys.path:
    sys.path.insert(0, str(REPO_ROOT))

TICKETS = ("EV-1", "EV-2", "EV-3")


@pytest.fixture(autouse=True)
def _git_identity(monkeypatch: pytest.MonkeyPatch) -> None:
    # commits in the throwaway repos get a test identity from the environment (the devcontainer
    # has no git identity; git config is never touched)
    monkeypatch.setenv("GIT_AUTHOR_NAME", "Eval Test")
    monkeypatch.setenv("GIT_AUTHOR_EMAIL", "eval-test@example.invalid")
    monkeypatch.setenv("GIT_COMMITTER_NAME", "Eval Test")
    monkeypatch.setenv("GIT_COMMITTER_EMAIL", "eval-test@example.invalid")


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """main + golden tag + ticket/<ID> per ticket; commits use the test identity from the environment."""
    r = tmp_path / "repo"
    r.mkdir()
    _git(r, "init", "-q", "-b", "main")
    (r / ".gitignore").write_text(".harness/\nresults/\n", encoding="utf-8")
    (r / "src").mkdir()
    (r / "src" / "a.c").write_text("int a;\n", encoding="utf-8")
    (r / "CMakePresets.json").write_text("{}\n", encoding="utf-8")
    _git(r, "add", "-A")
    _git(r, "commit", "-q", "-m", "golden")
    _git(r, "tag", "golden")
    for t in TICKETS:
        _git(r, "branch", f"ticket/{t}", "main")
        _git(r, "checkout", "-q", f"ticket/{t}")
        (r / "src" / "a.c").write_text(f"int a; /* seed */\nint {t.replace('-', '_').lower()};\n", encoding="utf-8")
        _git(r, "commit", "-q", "-am", "Exercise state")
    _git(r, "checkout", "-q", "main")
    return r


@pytest.fixture
def hidden(tmp_path: Path) -> Path:
    h = tmp_path / "hidden-tests"
    for i, t in enumerate(TICKETS):
        d = h / "tickets" / t
        d.mkdir(parents=True)
        (d / "meta.yaml").write_text(textwrap.dedent(f"""\
            id: {t}
            title: "x"
            eval: {"true" if i < 2 else "false"}
            ticket_branch: ticket/{t}
            modules: [codec]
            allowed_paths:
              - src/**
              - tests/unit/**
            root_cause: >-
              never copied
            """), encoding="utf-8")
        (d / "ticket.md").write_text(f"# {t}: Picture looks odd\n\nbody\n", encoding="utf-8")
    (h / "run.sh").write_text("#!/bin/sh\nexit 2\n", encoding="utf-8")
    return h


@pytest.fixture
def variants_file(tmp_path: Path) -> Path:
    p = tmp_path / "variants.yaml"
    p.write_text(textwrap.dedent("""\
        version: 1
        workflow: jira-to-pr
        defaults:
          roles: {implementer: true}
          models: {planner: m-sonnet, implementer: m-haiku, test_writer: m-haiku, reviewer: m-sonnet}
          retry_budget: 2
        gate_weights: {no-heap: 3}
        variants:
          A: {label: single}
          B: {label: team, roles: {planner: true, reviewer: true}}
        """), encoding="utf-8")
    return p
