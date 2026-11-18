"""HarnessRunner: worktree, command line, summary collection, push."""

from __future__ import annotations

import subprocess
from pathlib import Path

from bridge_testlib import FakeHarness, git

from harness_runner import HarnessRunner


def test_command_uses_the_facilitator_runtime_and_eval_mode(tmp_path: Path) -> None:
    r = HarnessRunner(tmp_path, python="py")
    assert r.command(Path("t.md")) == ["py", "-m", "harness", "run", "--workflow", "jira-to-pr", "--ticket", "t.md",
                                       "--mode", "eval", "--runtime", "facilitator"]
    assert HarnessRunner(tmp_path, python="py", fake=True).command(Path("t.md"))[-1] == "--fake"


def test_run_in_a_fresh_worktree_from_origin_main(repo: Path, fake_harness: FakeHarness) -> None:
    git(repo, "commit", "--allow-empty", "-q", "-m", "local only")       # not on origin: must not be the base
    r = HarnessRunner(repo, fake=True, fetch=True, run_fn=fake_harness, log=lambda m: None)
    o = r.run("gh-12-20261120-101500", "# GH-12: t\n\n**Type:** Bug\n")
    assert o.ok, o
    wt = Path(o.workdir)
    assert wt == repo.parent / "work-bridge" / "worktrees" / "gh-12-20261120-101500"
    call = fake_harness.harness_calls[0]
    assert call["cwd"] == wt and "--fake" in call["cmd"] and "--runtime" in call["cmd"]
    ticket = Path(call["cmd"][call["cmd"].index("--ticket") + 1])
    assert wt.resolve() not in ticket.resolve().parents                 # never inside the worktree
    assert call["ticket"].startswith("# GH-12: t")
    assert git(wt, "log", "-1", "--format=%s", "HEAD~1") == "initial"  # based on origin/main
    assert (o.status, o.branch, o.gates) == ("done", "agent/gh-12-ab12", "compliance:PASS diff-policy:PASS build:PASS unit-tests:PASS")
    r.push(wt, o.branch)
    assert git(repo.parent / "origin.git", "rev-parse", o.branch) == o.commit
    r.cleanup(wt)
    assert not wt.exists()


def test_failed_run_is_reported_without_branch(repo: Path) -> None:
    r = HarnessRunner(repo, fetch=False, run_fn=FakeHarness(status="failed"), log=lambda m: None)
    o = r.run("gh-12-x", "# GH-12: t\n")
    assert (o.status, o.ok, o.branch) == ("failed", False, "")
    assert o.summary["stopped_at"] == "implement"
    assert "[harness] run" in o.log_tail


def test_existing_worktree_and_missing_summary_are_errors(repo: Path) -> None:
    def silent(cmd, **kw):
        if cmd[0] == "git":
            return subprocess.run(cmd, **kw)
        return subprocess.CompletedProcess(cmd, 2, stdout="", stderr="config error: boom")

    r = HarnessRunner(repo, fetch=False, run_fn=silent, log=lambda m: None)
    o = r.run("dup", "# GH-1: t\n")
    assert o.status == "error" and "did not report a run" in o.error and "config error" in o.log_tail
    o2 = r.run("dup", "# GH-1: t\n")
    assert o2.status == "error" and o2.error.startswith("worktree:")


def test_in_place_mode_uses_the_checkout(repo: Path, fake_harness: FakeHarness) -> None:
    r = HarnessRunner(repo, in_place=True, fetch=False, run_fn=fake_harness, log=lambda m: None)
    o = r.run("gh-12", "# GH-12: t\n")
    assert Path(o.workdir) == repo.resolve() and o.ok
    again = r.collect(repo)                      # newest run in .harness/runs (ci-finish without --run-id)
    assert again.run_id == fake_harness.run_id and again.commit == o.commit
