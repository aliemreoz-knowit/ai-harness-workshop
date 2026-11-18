"""A fake orchestrator for tests and dry runs: no model calls, no network.

harness.eval.fake.run_workflow has the signature of harness.api.run_workflow.
The outcome is deterministic per (variant, ticket, repeat, settings) and
follows a toy quality/cost model, so a fake experiment produces a plausible
leaderboard (more roles -> higher pass rate and cost, Sonnet costs ~10x Haiku,
retry budget 1 fails more, no AGENTS.md fails more, autonomy auto counts no
approvals). A successful fake run leaves the marker file FAKE_FIX_MARKER in the
worktree (uncommitted); harness/eval/fake_hidden.py passes exactly then.

settings.extra["fake"] overrides, for tests:
    success: bool        force the outcome
    error: str           raise RuntimeError(error)
    sleep_s: float       real sleep (to exercise parallelism / timeouts)
    no_audit: bool       write no events.jsonl (RunResult fields are then used)
    bad_lines: int       append unparsable lines to the audit trail
"""
from __future__ import annotations

import hashlib
import json
import random
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from ..api import RunResult, RunSettings

FAKE_FIX_MARKER = ".eval-fake-fix"
GATES = ("build", "misra", "no-heap", "unit-tests", "property-tests", "sanitizers", "loopback", "budgets")


def _price(model: str) -> tuple[float, float]:
    """(credits per call low, high) by model family."""
    m = model.lower()
    if "sonnet" in m or "opus" in m:
        return 3.0, 6.0
    if "haiku" in m or "mini" in m:
        return 0.3, 1.0
    return 1.0, 2.5


def _quality(model: str) -> float:
    m = model.lower()
    return 0.12 if ("sonnet" in m or "opus" in m) else -0.06 if "haiku" in m else 0.0


def success_probability(ticket: str, s: RunSettings) -> float:
    roles = s.roles
    p = 0.45
    p += 0.10 if roles.get("planner") else 0.0
    p += 0.06 if roles.get("test_writer") else 0.0
    p += 0.12 if roles.get("reviewer") else 0.0
    p += _quality(s.models.get("implementer", ""))
    p += 0.5 * _quality(s.models.get("reviewer", "")) if roles.get("reviewer") else 0.0
    p += 0.06 if s.prompt_template not in ("", "v1") else 0.0
    p -= 0.15 if s.retry_budget <= 1 else 0.0
    p -= 0.18 if not s.custom_instructions else 0.0
    p -= 0.20 if not s.gates else 0.0
    # tickets differ in difficulty, stable per id
    p -= (int(hashlib.sha256(ticket.encode()).hexdigest()[:4], 16) % 25) / 100.0
    return max(0.03, min(0.97, p))


def _rng(ticket: str, s: RunSettings) -> random.Random:
    key = json.dumps({"t": ticket, "v": s.variant, "r": s.repeat, "x": s.experiment,
                      "roles": s.roles, "models": s.models, "rb": s.retry_budget,
                      "ci": s.custom_instructions, "pt": s.prompt_template, "a": s.autonomy},
                     sort_keys=True)
    return random.Random(int(hashlib.sha256(key.encode()).hexdigest()[:16], 16))


class _Audit:
    def __init__(self, path: Path | None):
        self.path = path
        self.lines: list[str] = []
        self.t = datetime(2026, 11, 20, 12, 15, tzinfo=timezone.utc).timestamp()

    def emit(self, type_: str, **data: Any) -> None:
        self.t += 7.0
        self.lines.append(json.dumps({"type": type_, "data": data, "timestamp":
                                      datetime.fromtimestamp(self.t, timezone.utc).isoformat()}))

    def write(self, bad_lines: int = 0) -> None:
        if self.path is None:
            return
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "w", encoding="utf-8", newline="\n") as fh:
            for line in self.lines:
                fh.write(line + "\n")
            for _ in range(bad_lines):
                fh.write("{not json\n")


def run_workflow(workflow: str, ticket: str, settings: RunSettings, mode: str = "eval") -> RunResult:
    knobs = dict(settings.extra.get("fake") or {})
    if knobs.get("sleep_s"):
        time.sleep(float(knobs["sleep_s"]))
    if knobs.get("error"):
        raise RuntimeError(str(knobs["error"]))
    rng = _rng(ticket, settings)
    run_dir = Path(settings.run_dir) if settings.run_dir else None
    audit = _Audit(None if knobs.get("no_audit") or run_dir is None else run_dir / "events.jsonl")
    res = RunResult(run_id=f"fake-{settings.variant}-{ticket}-r{settings.repeat}",
                    run_dir=str(run_dir or ""), audit_path=str(audit.path or ""))
    sessions: dict[str, float] = {}
    tokens = {"in": 0, "out": 0, "cached": 0}

    def call(role: str) -> None:
        model = settings.models.get(role, "auto")
        lo, hi = _price(model)
        credits = rng.uniform(lo, hi)
        if role in sessions:            # warm call inside the cache window
            credits *= 0.45
        sessions[role] = sessions.get(role, 0.0) + credits
        t_in = rng.randint(18_000, 60_000)
        t_cached = int(t_in * (0.7 if sessions[role] != credits else 0.1))
        t_out = rng.randint(800, 6_000)
        tokens["in"] += t_in
        tokens["out"] += t_out
        tokens["cached"] += t_cached
        res.model_calls += 1
        audit.emit("assistant.usage", model=model, role=role, inputTokens=t_in, outputTokens=t_out,
                   cacheReadTokens=t_cached, nanoAiu=int(credits * 1e9))
        audit.emit("session.usage_checkpoint", sessionId=f"{res.run_id}-{role}",
                   totalNanoAiu=int(sessions[role] * 1e9))

    def approval(step: str) -> None:
        if settings.autonomy == "human":
            res.approvals += 1
            audit.emit("harness.approval", step=step, decision="auto-pass" if mode == "eval" else "approved")

    p = success_probability(ticket, settings)
    will_succeed = bool(knobs["success"]) if "success" in knobs else rng.random() < p
    max_attempts = settings.retry_budget + 1
    # attempts the run needs to get gates green and the reviewer happy (if it succeeds at all)
    needed = 1 + sum(1 for _ in range(settings.retry_budget) if rng.random() < 0.35)
    attempts_used = needed if will_succeed else max_attempts

    if settings.roles.get("planner"):
        call("planner")
        approval("plan")
    duration = rng.uniform(60, 180)
    for attempt in range(attempts_used):
        last = attempt == attempts_used - 1
        call("implementer")
        if settings.roles.get("test_writer") and attempt == 0:
            call("test_writer")
        green = will_succeed and last
        if settings.gates:
            failing = set() if green else set(rng.sample(GATES, rng.randint(1, 2)))
            for g in GATES:
                status = "FAIL" if g in failing else "PASS"
                res.gate_results[g] = status
                audit.emit("harness.gate", gate=g, status=status, attempt=attempt)
        if settings.roles.get("reviewer") and (green or not settings.gates):
            call("reviewer")
            score = rng.choice((4, 5)) if green else rng.choice((1, 2, 3))
            res.reviewer_score = float(score)
            audit.emit("harness.review", score=score, verdict="approve" if green else "reject")
        duration += rng.uniform(150, 420)
        if not last:
            res.retries += 1
            audit.emit("harness.retry", step="implement", attempt=attempt + 1, reason="gate or review feedback")

    if will_succeed:
        approval("merge")
        res.status, res.success = "completed", True
        workdir = Path(settings.workdir)
        if workdir.is_dir():
            (workdir / FAKE_FIX_MARKER).write_text(f"{settings.variant} {ticket} r{settings.repeat}\n",
                                                   encoding="utf-8")
    else:
        res.escalations += 1
        audit.emit("harness.escalation", step="implement", reason="retry budget exhausted")
        res.status, res.success = "escalated", False
    audit.emit("harness.run_end", status=res.status, success=res.success)
    audit.write(int(knobs.get("bad_lines", 0)))

    res.duration_s = round(duration, 1)
    res.ai_credits = round(sum(sessions.values()), 4)
    res.tokens_in, res.tokens_out, res.tokens_cached = tokens["in"], tokens["out"], tokens["cached"]
    res.gate_violations = sum(1 for v in res.gate_results.values() if v == "FAIL")
    return res
