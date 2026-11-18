"""The small interface between the eval runner and the orchestrator.

    run_workflow(workflow, ticket, settings, mode) -> RunResult

The eval runner (harness/eval) only ever calls this function. The orchestrator
(harness/orchestrator, built separately) provides the real implementation; the
eval tests use harness.eval.fake.run_workflow. Which implementation runs is
decided by, in order:

1. set_backend(fn) (tests, the runner's --backend option)
2. the environment variable HARNESS_BACKEND="package.module:function"
3. harness.orchestrator.run_workflow, or harness.orchestrator.api.run_workflow

Contract for an implementation
------------------------------
* It works inside settings.workdir (a git worktree checked out on its own
  branch from the ticket's branch) and leaves its result there: commits on
  that branch and/or changes in the working tree. The hidden tests run against
  the working tree afterwards.
* mode == "eval": approvals are never waited for. A step with approval "human"
  is auto-passed and counted (RunResult.approvals, audit event
  "harness.approval"); settings.autonomy == "auto" turns every approval step
  into an automatic one (nothing is counted). An escalation to a human ends the
  run with status "escalated" and is counted.
* It writes an audit trail: JSON lines in settings.run_dir/events.jsonl (or
  wherever RunResult.audit_path points). The eval runner derives credits,
  tokens, approvals, escalations, retries, gate results and the reviewer score
  from the audit trail and uses the RunResult fields only where the trail is
  silent. One event per line: {"type": str, "data": {...}, "timestamp": str}.

  type                          data keys read by the eval runner
  ----------------------------  ---------------------------------------------
  assistant.usage               model, inputTokens, outputTokens,
                                cacheReadTokens, nanoAiu (optional)
  session.usage_checkpoint      sessionId, totalNanoAiu (cumulative per session),
  (or usage_checkpoint)         optionally totalInputTokens, totalOutputTokens,
                                totalCacheReadTokens
  harness.approval              step, decision ("auto-pass" | "approved" | ...)
  harness.escalation            step, reason
  harness.gate                  gate ("unit-tests"), status ("PASS" | "FAIL" | "SKIP"),
                                attempt (int), violations (int, optional)
  harness.retry                 step, attempt, reason
  harness.review                score (1..5), verdict ("approve" | "reject")
  harness.run_end               status, success

Unknown event types are ignored.
"""
from __future__ import annotations

import dataclasses
import importlib
import os
from dataclasses import dataclass, field
from typing import Any, Callable, Optional

ROLES = ("planner", "implementer", "test_writer", "reviewer")
MODES = ("eval", "interactive")
AUTONOMY = ("human", "auto")
STATUSES = ("completed", "failed", "escalated", "budget_exceeded", "timeout", "error")

# The settings an overlay variant applies only when it names them (harness/eval/variants.py decides what
# is named, harness/backend.py applies it): the scalar knobs, and one knob per role in each ROLE_KNOBS
# group (roles.<role>, models.<role>). CAP_KNOB, the run cap, is no such choice: it always applies.
SCALAR_KNOBS = ("retry_budget", "custom_instructions", "prompt_template", "autonomy", "gates")
ROLE_KNOBS = ("roles", "models")
CAP_KNOB = "max_ai_credits"
KNOB_NAMES = frozenset(SCALAR_KNOBS + (CAP_KNOB,)
                       + tuple(f"{group}.{role}" for group in ROLE_KNOBS for role in ROLES))


@dataclass
class RunSettings:
    """Everything a variant can change, plus where the run happens."""

    workdir: str = "."                       # the git worktree the workflow works in
    run_dir: str = ""                        # where the audit trail and artefacts go
    roles: dict[str, bool] = field(default_factory=lambda: {r: True for r in ROLES})
    models: dict[str, str] = field(default_factory=dict)  # role -> model id
    retry_budget: int = 3
    custom_instructions: bool = True         # False = --no-custom-instructions (no AGENTS.md etc.)
    prompt_template: str = "v1"
    autonomy: str = "human"                  # human: approval steps counted in eval mode; auto: none
    gates: bool = True
    max_ai_credits: Optional[float] = None   # hard cap per run
    timeout_s: Optional[float] = None        # wall-clock cap per run
    variant: str = ""                        # bookkeeping only: variant, experiment, repeat index
    experiment: str = ""
    repeat: int = 0
    extra: dict[str, Any] = field(default_factory=dict)  # backend-specific knobs

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, d: dict[str, Any]) -> "RunSettings":
        names = {f.name for f in dataclasses.fields(cls)}
        known = {k: v for k, v in d.items() if k in names}
        extra = dict(known.pop("extra", {}) or {})
        extra.update({k: v for k, v in d.items() if k not in names})
        return cls(**known, extra=extra)


@dataclass
class RunResult:
    """What a workflow run reports. Audit-trail values take precedence in the eval runner."""

    status: str = "completed"                # one of STATUSES
    success: bool = False                    # the workflow's own verdict (gates green, reviewer approved)
    run_id: str = ""
    run_dir: str = ""
    audit_path: str = ""                     # events.jsonl; default <run_dir>/events.jsonl
    duration_s: float = 0.0
    gate_violations: int = 0                 # failed gates on the final attempt
    gate_results: dict[str, str] = field(default_factory=dict)  # gate -> PASS/FAIL/SKIP (final)
    reviewer_score: Optional[float] = None
    approvals: int = 0
    escalations: int = 0
    retries: int = 0
    ai_credits: float = 0.0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    model_calls: int = 0
    head_commit: str = ""
    error: str = ""

    @property
    def interventions(self) -> int:
        return self.approvals + self.escalations

    def to_dict(self) -> dict[str, Any]:
        d = dataclasses.asdict(self)
        d["interventions"] = self.interventions
        return d


WorkflowFn = Callable[[str, str, RunSettings, str], RunResult]


class BackendUnavailable(RuntimeError):
    """No orchestrator implementation could be found."""


_backend: Optional[WorkflowFn] = None


def set_backend(fn: Optional[WorkflowFn]) -> None:
    """Install an implementation (None restores discovery)."""
    global _backend
    _backend = fn


def load_backend(spec: str) -> WorkflowFn:
    """'package.module:function' -> callable."""
    if ":" not in spec:
        raise BackendUnavailable(f"backend spec must be module:function, got {spec!r}")
    mod_name, fn_name = spec.split(":", 1)
    fn = getattr(importlib.import_module(mod_name), fn_name, None)
    if not callable(fn):
        raise BackendUnavailable(f"{spec}: not a callable")
    return fn


def _discover() -> WorkflowFn:
    spec = os.environ.get("HARNESS_BACKEND", "").strip()
    if spec:
        return load_backend(spec)
    for mod_name in ("harness.orchestrator", "harness.orchestrator.api"):
        try:
            mod = importlib.import_module(mod_name)
        except ModuleNotFoundError as exc:
            if exc.name is not None and not mod_name.startswith(exc.name):
                raise
            continue
        fn = getattr(mod, "run_workflow", None)
        if callable(fn):
            return fn
    raise BackendUnavailable(
        "no orchestrator found: expected harness.orchestrator.run_workflow; "
        "set HARNESS_BACKEND=module:function or use the fake backend (--backend fake)")


def run_workflow(workflow: str, ticket: str, settings: RunSettings, mode: str = "eval",
                 backend: Optional[WorkflowFn] = None) -> RunResult:
    """Run one workflow (e.g. "jira-to-pr") on one ticket with the given settings.

    `backend` (optional) bypasses discovery for this one call.
    """
    if mode not in MODES:
        raise ValueError(f"mode must be one of {MODES}, got {mode!r}")
    fn = backend or _backend or _discover()
    result = fn(workflow, ticket, settings, mode)
    if not isinstance(result, RunResult):
        if isinstance(result, dict):
            names = {f.name for f in dataclasses.fields(RunResult)}
            result = RunResult(**{k: v for k, v in result.items() if k in names})
        else:
            raise TypeError(f"backend returned {type(result).__name__}, expected RunResult")
    return result
