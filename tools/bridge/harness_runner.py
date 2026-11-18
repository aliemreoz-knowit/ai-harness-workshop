"""Run the harness for one triggered ticket and collect the outcome.

    worktree (detached at <base>)  ->  python -m harness run --workflow jira-to-pr
        --ticket <file> --mode eval --runtime facilitator [--fake]  ->  summary.json

The ticket file lives NEXT TO the worktree, never inside it (the commit step
refuses paths outside the policy, and gate compliance checks every changed path).
The harness commit step creates the branch (``agent/<key>-<run>``) and one
commit; ``push`` publishes that branch (never in dry-run).

``in_place=True`` (GitHub Actions) runs in the repository checkout itself.
All subprocesses go through ``run_fn`` so tests can script them.
"""

from __future__ import annotations

import json
import re
import subprocess
import sys
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

RunFn = Callable[..., subprocess.CompletedProcess]
_RUN_LINE = re.compile(r"^\[harness\] run (?P<id>\S+): (?P<status>\w+)", re.M)


@dataclass
class RunOutcome:
    status: str                         # done | failed | escalated | aborted | error
    run_id: str = ""
    branch: str = ""
    commit: str = ""
    workdir: str = ""
    summary: dict[str, Any] = field(default_factory=dict)
    gates: str = ""                     # last ladder summary line
    log_tail: str = ""
    error: str = ""
    exit_code: Optional[int] = None

    @property
    def ok(self) -> bool:
        return self.status == "done" and bool(self.branch and self.commit)


def _default_run(cmd: list[str], **kw: Any) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, **kw)  # noqa: S603 - fixed argv, no shell


class HarnessRunner:
    def __init__(self, repo: Path, *, workflow: str = "jira-to-pr", mode: str = "eval",
                 runtime: str = "facilitator", fake: bool = False, base_ref: str = "main",
                 remote: str = "origin", fetch: bool = True, worktree_root: Optional[Path] = None,
                 timeout_s: float = 5400.0, in_place: bool = False, python: str = sys.executable,
                 run_fn: RunFn = _default_run, log: Callable[[str], None] = print) -> None:
        self.repo = Path(repo).resolve()
        self.workflow, self.mode, self.runtime, self.fake = workflow, mode, runtime, fake
        self.base_ref, self.remote, self.fetch = base_ref, remote, fetch
        self.worktree_root = Path(worktree_root) if worktree_root else \
            self.repo.parent / f"{self.repo.name}-bridge" / "worktrees"
        self.timeout_s, self.in_place, self.python = timeout_s, in_place, python
        self.run_fn, self.log = run_fn, log

    # -- git -------------------------------------------------------------------------
    def git(self, cwd: Path, *args: str, timeout: float = 300) -> str:
        cp = self.run_fn(["git", *args], cwd=str(cwd), capture_output=True, text=True, timeout=timeout)
        if cp.returncode != 0:
            raise RuntimeError(f"git {' '.join(args)}: {(cp.stderr or cp.stdout or '').strip()[:400]}")
        return (cp.stdout or "").strip()

    def base(self) -> str:
        if self.fetch and "/" not in self.base_ref:
            self.git(self.repo, "fetch", "--quiet", self.remote, self.base_ref)
            return f"{self.remote}/{self.base_ref}"
        return self.base_ref

    def prepare(self, slug: str) -> Path:
        if self.in_place:
            return self.repo
        wt = self.worktree_root / slug
        if wt.exists():
            raise RuntimeError(f"worktree {wt} exists (a previous run of {slug}?); remove it or rename")
        wt.parent.mkdir(parents=True, exist_ok=True)
        self.git(self.repo, "worktree", "add", "--detach", str(wt), self.base())
        return wt

    def cleanup(self, workdir: Path) -> None:
        if not self.in_place and Path(workdir).resolve() != self.repo:
            self.git(self.repo, "worktree", "remove", "--force", str(workdir))

    def push(self, workdir: Path, branch: str) -> None:
        self.git(Path(workdir), "push", self.remote, f"{branch}:refs/heads/{branch}")

    # -- harness ---------------------------------------------------------------------
    def command(self, ticket_file: Path) -> list[str]:
        cmd = [self.python, "-m", "harness", "run", "--workflow", self.workflow, "--ticket", str(ticket_file),
               "--mode", self.mode, "--runtime", self.runtime]
        return cmd + (["--fake"] if self.fake else [])

    def ticket_file(self, slug: str, ticket_md: str) -> Path:
        base = (self.repo.parent / f"{self.repo.name}-bridge" / "tickets") if self.in_place else self.worktree_root
        path = base / f"{slug}.ticket.md"
        path.parent.mkdir(parents=True, exist_ok=True)
        path.write_text(ticket_md, encoding="utf-8", newline="\n")
        return path

    def run(self, slug: str, ticket_md: str) -> RunOutcome:
        try:
            workdir = self.prepare(slug)
        except (RuntimeError, OSError, subprocess.SubprocessError) as exc:
            return RunOutcome(status="error", error=f"worktree: {exc}")
        ticket = self.ticket_file(slug, ticket_md)
        cmd = self.command(ticket)
        self.log(f"[bridge] {slug}: {' '.join(cmd[1:])} (in {workdir})")
        try:
            cp = self.run_fn(cmd, cwd=str(workdir), capture_output=True, text=True, timeout=self.timeout_s)
        except subprocess.TimeoutExpired:
            return RunOutcome(status="error", workdir=str(workdir), error=f"harness timed out after {self.timeout_s:.0f} s")
        out = (cp.stdout or "") + (cp.stderr or "")
        return self.collect(workdir, out, cp.returncode)

    def collect(self, workdir: Path, output: str = "", exit_code: Optional[int] = None,
                run_id: Optional[str] = None) -> RunOutcome:
        """Read ``summary.json`` of ``run_id`` (or the run named in ``output``, else the newest run)."""
        workdir = Path(workdir)
        tail = "\n".join(output.strip().splitlines()[-15:])
        if not run_id:
            m = _RUN_LINE.search(output)
            run_id = m.group("id") if m else None
        runs = workdir / ".harness" / "runs"
        if not run_id and runs.is_dir():
            done = sorted((p for p in runs.iterdir() if (p / "summary.json").is_file()),
                          key=lambda p: (p / "summary.json").stat().st_mtime)
            run_id = done[-1].name if done else None
        if not run_id:
            return RunOutcome(status="error", workdir=str(workdir), log_tail=tail, exit_code=exit_code,
                              error="the harness did not report a run (see the log tail)")
        try:
            summary = json.loads((runs / run_id / "summary.json").read_text(encoding="utf-8"))
        except (OSError, ValueError) as exc:
            return RunOutcome(status="error", run_id=run_id, workdir=str(workdir), log_tail=tail,
                              exit_code=exit_code, error=f"summary.json: {exc}")
        gates = ""
        try:
            state = json.loads((runs / run_id / "state.json").read_text(encoding="utf-8"))
            gates = str((state.get("last_ladder") or {}).get("summary") or "")
        except (OSError, ValueError):
            pass
        return RunOutcome(status=str(summary.get("status") or "error"), run_id=run_id,
                          branch=str(summary.get("branch") or ""), commit=str(summary.get("commit") or ""),
                          workdir=str(workdir), summary=summary, gates=gates, log_tail=tail,
                          error=str(summary.get("error") or ""), exit_code=exit_code)
