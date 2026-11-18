"""Bridge test helpers (imported by conftest.py and the tests): paths, git, FakeHarness.

A module of its own, not conftest.py: other suites (tools/hil/tests) have a
conftest.py too, and ``from conftest import ...`` would pick whichever was
imported last when both run in one pytest process.

No test touches the network, starts the Copilot CLI or sends a prompt: GitHub
and Jira come from ReplayTransport (tests/fixtures/*), the harness subprocess
is replaced by ``FakeHarness`` (git commands still run for real, in tmp_path).
"""

from __future__ import annotations

import json
import subprocess
import sys
from pathlib import Path
from typing import Any

HERE = Path(__file__).resolve().parent
BRIDGE = HERE.parent
REPO = BRIDGE.parents[1]
for p in (str(BRIDGE), str(REPO)):
    if p not in sys.path:
        sys.path.insert(0, p)

FIXTURES = HERE / "fixtures" / "demo"
GIT_ID = ["-c", "user.name=bridge-test", "-c", "user.email=bridge-test@example.invalid",
          "-c", "commit.gpgsign=false", "-c", "init.defaultBranch=main"]


def git(cwd: Path, *args: str) -> str:
    cp = subprocess.run(["git", *GIT_ID, *args], cwd=str(cwd), capture_output=True, text=True)
    assert cp.returncode == 0, f"git {args}: {cp.stderr}"
    return cp.stdout.strip()


class FakeHarness:
    """run_fn for HarnessRunner: real git, scripted ``python -m harness run``."""

    def __init__(self, status: str = "done", run_id: str = "20261120-101500-gh-12-ab12",
                 branch: str = "agent/gh-12-ab12") -> None:
        self.status, self.run_id, self.branch = status, run_id, branch
        self.harness_calls: list[dict[str, Any]] = []

    def __call__(self, cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
        if cmd[0] == "git":
            return subprocess.run(["git", *GIT_ID, *cmd[1:]], **kw)
        assert cmd[1:4] == ["-m", "harness", "run"], cmd
        cwd = Path(kw["cwd"])
        ticket = Path(cmd[cmd.index("--ticket") + 1])
        self.harness_calls.append({"cmd": cmd, "cwd": cwd, "ticket": ticket.read_text(encoding="utf-8")})
        commit = ""
        if self.status == "done":
            git(cwd, "checkout", "-q", "-b", self.branch)
            (cwd / "fix.txt").write_text("the agent's change\n", encoding="utf-8")
            git(cwd, "add", "fix.txt")
            git(cwd, "commit", "-q", "-m", "GH-12: fix")
            commit = git(cwd, "rev-parse", "HEAD")
        run = cwd / ".harness" / "runs" / self.run_id
        run.mkdir(parents=True)
        summary = {"run_id": self.run_id, "workflow": "jira-to-pr", "status": self.status, "mode": "eval",
                   "runtime": "facilitator", "ticket": "GH-12",
                   "steps": {"ticket": "done", "plan": "done", "implement": "done" if self.status == "done" else "failed"},
                   "stopped_at": None if self.status == "done" else "implement",
                   "reason": None if self.status == "done" else "retry budget exhausted",
                   "error": None, "interventions": 2, "counters": {"retries": 1}, "credits": 0.0,
                   "branch": self.branch if commit else None, "commit": commit or None, "review": "approve"}
        (run / "summary.json").write_text(json.dumps(summary), encoding="utf-8")
        (run / "state.json").write_text(json.dumps({"last_ladder": {"summary": "compliance:PASS diff-policy:PASS build:PASS unit-tests:PASS"}}),
                                        encoding="utf-8")
        line = f"[harness] run {self.run_id}: {self.status}\n"
        return subprocess.CompletedProcess(cmd, 0 if self.status == "done" else 1, stdout=line, stderr="")
