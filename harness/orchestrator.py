"""The orchestrator: runs a workflow step by step, resumable, fully audited.

    ticket -> planner -> implementer <-> gates -> test-writer -> ladder
           (fail -> implementer) -> reviewer (reject -> implementer) -> approval -> branch + commit

Design rules, each one a workshop talking point:

- deterministic first: gates, policy and diff checks are code, not prompts;
  the model only gets structured, excerpted feedback (gate, file, line, rule,
  message, excerpt), never a raw log,
- every loop is bounded: retry budget per gated step, ladder and review rounds,
  credit caps per session and per run; exhausted -> escalation to a human,
- a change is judged by what it changes: before the first agent turn the test
  gates run once on the untouched base; when the base is not green, a test gate
  fails only on new failures and on required tests (baseline.py),
- the reviewer judges a change only after the full ladder passed on it, and a
  stronger (pricier) model is used only when the remaining budget pays for a turn,
- one session per role, kept across retries and review rounds (prompt cache),
- every prompt, tool call, gate result, approval and usage event is written to
  the run folder; ``state.json`` is saved after every change (resume).
"""

from __future__ import annotations

import asyncio
import json
import re
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from . import baseline as bl
from . import gitops
from . import ticket as tk
from .approvals import Approver, FileChannel
from .audit import Audit, Usage
from .config import HarnessConfig, StepDef, Workflow
from .envdetect import Environment
from .gates import GateContext, GateRunner, LadderResult
from .policy import RolePolicy, any_match, protected_hit
from .prompts import Prompts
from .runtime import AgentSession, Runtime, SessionSpec
from .state import RunState, new_run_id
from .tools import make_tools


# ticket types (the **Type:** field / Jira issue type) that count as a bug: such a ticket
# needs a regression test (StepDef.require_test_change "bug")
BUG_TYPES = ("bug", "defect", "regression", "incident")
TESTS_FAIL = "TESTS:FAIL (no new or changed test under tests/)"


class HarnessError(RuntimeError):
    pass


@dataclass
class Outcome:
    kind: str                  # done | jump | stop
    status: str = "done"       # for stop: failed | escalated | aborted
    target: str | None = None  # for jump
    reason: str = ""


DONE = Outcome("done")


def stop(status: str, reason: str) -> Outcome:
    return Outcome("stop", status=status, reason=reason)


class Orchestrator:
    # the "progress" part of the baseline rule is judged on the final ladder only (GateContext.final);
    # an agent step's own gates pass on no new failure and the required tests they run
    def __init__(self, cfg: HarnessConfig, runtime: Runtime, env: Environment, mode: str = "interactive",
                 runtime_mode: str = "copilot-user", gate_runner: GateRunner | None = None,
                 input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print,
                 isatty: Callable[[], bool] | None = None,
                 workflow_transform: Callable[[Workflow], Workflow] | None = None,
                 channel: FileChannel | None = None) -> None:
        self.cfg = cfg
        self.runtime = runtime
        self.env = env
        self.mode = mode
        self.runtime_mode = runtime_mode
        self.gates = gate_runner or GateRunner(cfg, env)
        self.prompts = Prompts(cfg.prompts_dir, str(cfg.raw.get("prompt_template") or "v1"))
        # eval variants switch roles / gates off by rewriting the workflow (harness/backend.py)
        self.workflow_transform = workflow_transform
        self.input_fn = input_fn
        self.output = output
        self.isatty = isatty
        self.channel = channel      # interactive approvals and escalations are asked here, not on the terminal
        self.sessions: dict[str, AgentSession] = {}
        # credit guard: the harness cap of each role's session and the reason a turn was aborted
        self.session_caps: dict[str, float] = {}
        self.cap_hit: str = ""
        self.current_step: str = ""
        self.resuming = False
        # set in run()
        self.state: RunState
        self.audit: Audit
        self.usage: Usage
        self.approver: Approver
        self.workflow: Workflow
        self.ticket: tk.Ticket | None = None

    # ================================================================= run ====
    async def run(self, workflow: str | None = None, ticket_ref: str | None = None,
                  resume: str | None = None, run_id: str | None = None) -> RunState:
        self._open(workflow, ticket_ref, resume, run_id)
        t0 = time.monotonic()
        session_cap, run_cap = self.cfg.caps(self.runtime_mode)
        self.audit.event("run.start" if not self.resuming else "run.resume", workflow=self.workflow.name,
                         mode=self.mode, runtime=self.runtime_mode, where=self.env.where,
                         caps={"session": session_cap, "run": run_cap}, cursor=self.state.cursor)
        self.output(f"[harness] runtime {self.runtime_mode}: caps {session_cap:g} AI credits per session, "
                    f"{run_cap:g} per run ({self.mode} mode)")
        try:
            await self.runtime.start()
            await self._loop()
        except KeyboardInterrupt:
            self.state.data["interrupted"] = True
            self.audit.event("run.interrupted", step=self.current_step)
            raise
        except Exception as exc:  # noqa: BLE001 - recorded, run stays resumable
            self.state.status = "failed"
            self.state.data["error"] = f"{type(exc).__name__}: {exc}"
            self.audit.event("run.error", step=self.current_step, error=self.state.data["error"])
        finally:
            await self._close_sessions()
            try:
                await self.runtime.stop()
            except Exception as exc:  # noqa: BLE001
                self.audit.event("runtime.stop_error", error=str(exc))
            self.state.data["duration_s"] = round(float(self.state.data.get("duration_s", 0))
                                                  + time.monotonic() - t0, 1)
            self._save()
            self._write_summary()
        return self.state

    def _open(self, workflow: str | None, ticket_ref: str | None, resume: str | None,
              run_id: str | None = None) -> None:
        repo = self.cfg.repo
        if resume:
            self.state = RunState.load(self.cfg.runs_dir, resume)
            self.workflow = self._transform(self.cfg.workflow(str(self.state.data["workflow"])))
            self.resuming = True
            self.state.data["resumed"] = int(self.state.data.get("resumed", 0)) + 1
            self.state.status = "running"
            self.state.data.pop("error", None)
            if self.state.data.get("ticket"):
                self.ticket = tk.Ticket.from_dict(self.state.data["ticket"])
            self.mode = self.mode if self.mode != "interactive" else str(self.state.data.get("mode", self.mode))
        else:
            if not workflow:
                raise HarnessError("--workflow is required for a new run")
            self.workflow = self._transform(self.cfg.workflow(workflow))
            if self.workflow.needs_ticket and not ticket_ref:
                raise HarnessError(f"workflow {workflow} needs --ticket")
            hint = Path(ticket_ref).stem if ticket_ref else self.workflow.name
            if ticket_ref and hint.lower() == "ticket":   # tickets/<KEY>/ticket.md: name the run after <KEY>
                hint = Path(ticket_ref).resolve().parent.name or hint
            base = gitops.head(repo) if gitops.is_repo(repo) else None
            baseline = gitops.changed_paths(repo) if base else []
            try:
                self.state = RunState.create(
                    self.cfg.runs_dir, run_id or new_run_id(hint), self.workflow.name,
                    [s.id for s in self.workflow.steps],
                    mode=self.mode, runtime=self.runtime_mode, ticket_ref=ticket_ref, base_ref=base,
                    start_branch=gitops.current_branch(repo) if base else None, baseline=baseline,
                    where=self.env.where)
            except FileExistsError as exc:                  # a caller-chosen id that is taken
                raise HarnessError(str(exc)) from exc
        self.audit = Audit(self.state.dir)
        self.usage = Usage(self.state.data.get("usage"))
        self.approver = Approver(self.mode, self.state, self.audit, self.input_fn, self.output, self.isatty,
                                 self.channel)

    async def _loop(self) -> None:
        steps = self.workflow.steps
        while self.state.cursor < len(steps):
            step = steps[self.state.cursor]
            self.current_step = step.id
            st = self.state.step(step.id)
            if step.kind not in ("ticket", "approval") and "test_baseline" not in self.state.data:
                await self._capture_baseline()
            resumed_here = self.resuming and st["status"] in ("running", "failed", "escalated")
            st["status"] = "running"
            st.setdefault("started", time.time())
            self._save()
            self.output(f"[harness] step {step.id} ({step.kind}{', ' + step.role if step.role else ''})")
            self.audit.event("step.start", step=step.id, step_kind=step.kind, role=step.role, resumed=resumed_here)
            outcome = await self._dispatch(step, resumed_here)
            self.resuming = False
            self.audit.event("step.end", step=step.id, outcome=outcome.kind, status=outcome.status,
                             reason=outcome.reason, target=outcome.target)
            if outcome.kind == "done":
                st["status"] = "done"
                self.state.cursor += 1
            elif outcome.kind == "jump":
                target = self.workflow.index(str(outcome.target))
                for s in steps[target:self.state.cursor + 1]:
                    sst = self.state.step(s.id)
                    sst["status"] = "pending"
                    if s.id != step.id:
                        sst["retries"] = 0
                        sst["extra_budget"] = 0
                self.state.cursor = target
            else:
                st["status"] = outcome.status
                st["reason"] = outcome.reason
                self.state.status = outcome.status
                self.state.data["stopped_at"] = step.id
                self.state.data["stop_reason"] = outcome.reason
                self.output(f"[harness] run {outcome.status} at {step.id}: {outcome.reason}")
                self._save()
                return
            self._save()
        self.state.status = "done"
        # a resumed run that finishes is not stopped any more: drop the reason of the earlier stop
        self.state.data.pop("stopped_at", None)
        self.state.data.pop("stop_reason", None)

    async def _dispatch(self, step: StepDef, resumed: bool) -> Outcome:
        handler = {
            "ticket": self._step_ticket, "agent": self._step_agent, "agent_gated": self._step_agent_gated,
            "gates": self._step_gates, "review": self._step_review, "approval": self._step_approval,
            "commit": self._step_commit,
        }[step.kind]
        return await handler(step, resumed)

    # ============================================================ helpers ====
    def _transform(self, wf: Workflow) -> Workflow:
        return self.workflow_transform(wf) if self.workflow_transform else wf

    def _save(self) -> None:
        self.state.data["usage"] = self.usage.to_dict()
        self.state.save()

    def _allowed_writes(self) -> list[str]:
        out: list[str] = []
        for s in self.workflow.steps:
            if s.role:
                for w in self.cfg.role(s.role).writes:
                    if w not in out:
                        out.append(w)
        return out

    def _gate_ctx(self, expect_changes: bool, step: StepDef | None = None) -> GateContext:
        tb = self.state.data.get("test_baseline") or {}
        return GateContext(repo=self.cfg.repo, allowed_writes=self._allowed_writes(), policy=self.cfg.policy,
                           expect_changes=expect_changes, baseline=frozenset(self.state.data.get("baseline") or []),
                           test_baseline=tb if tb.get("active") else None,
                           required_tests=tuple(self.ticket.required_tests if self.ticket else ()),
                           final=bool(step is not None and step.kind == "gates"), run_id=self.state.run_id)

    async def _capture_baseline(self) -> None:
        """Run the workflow's test-type gates once on the untouched base and keep every test result
        in the run state (resumable: a run resumed after this point reuses it)."""
        mode = str(self.cfg.loop.get("baseline", "auto"))
        rec: dict[str, Any] = {"mode": mode, "active": False}
        touched = int(self.state.counters.get("turns", 0)) > 0 or any(
            int(d.get("attempts", 0)) > 0 for d in self.state.data.get("steps", {}).values())
        dirty = sorted(str(p) for p in self.state.data.get("baseline") or [])
        ids = bl.gate_ids(self.cfg, self.workflow) if mode != "off" else []
        if mode == "off":
            rec["reason"] = "loop.baseline is off: every gate is absolute"
        elif touched:
            rec["reason"] = "the run already changed the tree before a base was recorded: absolute gates"
        elif dirty:
            # a base recorded on a dirty tree is not the base: it carries whatever was already edited
            rec["reason"] = (f"the working tree was not clean when the run started ({len(dirty)} path(s) "
                             f"already changed: {bl.short(dirty, 6)}): no untouched base to record, every "
                             "gate is absolute (commit or stash the edits for the baseline rule)")
            rec["dirty"] = dirty
        elif not ids:
            rec["reason"] = "the workflow runs no test-type gate"
        else:
            self.output(f"[harness] baseline: test gates {' '.join(ids)} on the untouched base")
            ctx = GateContext(repo=self.cfg.repo, allowed_writes=self._allowed_writes(), policy=self.cfg.policy,
                              baseline=frozenset(self.state.data.get("baseline") or []), run_id=self.state.run_id)
            ladder = await asyncio.to_thread(self.gates.run, ids, ctx, True)
            self.audit.gates("baseline", 1, ladder, "base")
            gates: dict[str, Any] = {}
            for g in ladder.gates:
                kind = self.cfg.gates.gates[g.id].baseline
                results = g.results if g.status == "FAIL" else None
                if g.status == "FAIL" and g.log:
                    self.audit.copy_log(self.cfg.repo / g.log, f"{g.id}-baseline.log")
                n = (len(bl.failures(results)) if kind == "tests" else
                     sum(1 for m in (results or {}).get("modules", {}).values() if m["state"] != "ok"))
                gates[g.id] = {"status": g.status, "kind": kind, "results": results, "failures": n}
            active = any(v["status"] == "FAIL" and v["results"] is not None for v in gates.values())
            rec.update(gates=gates, summary=ladder.summary(), active=active, duration_ms=ladder.duration_ms)
            if not active:
                rec["reason"] = ("the base is green: absolute gates" if ladder.ok else
                                 "no parsable test results on the base: absolute gates")
        self.state.data["test_baseline"] = rec
        failures = {gid: v["failures"] for gid, v in (rec.get("gates") or {}).items() if v["status"] == "FAIL"}
        self.audit.event("baseline", mode=mode, active=rec["active"], summary=rec.get("summary"),
                         failures=failures, reason=rec.get("reason"), dirty=dirty,
                         required_tests=self.ticket.required_tests if self.ticket else [])
        if rec.get("gates"):
            what = ("on: a test gate fails only on new failures and required tests" if rec["active"]
                    else rec.get("reason", "off"))
            self.output(f"[harness]   baseline {rec.get('summary')} -> rule {what}")
        elif dirty and mode != "off":
            self.output(f"[harness] baseline off: {rec['reason']}")
        self._save()

    async def _run_gates(self, step: StepDef, attempt: int, trigger: str, expect_changes: bool,
                         spec: str | None = None) -> LadderResult:
        ladder = await asyncio.to_thread(self.gates.run, spec or step.gates or "fast",
                                         self._gate_ctx(expect_changes, step))
        self.audit.gates(step.id, attempt, ladder, trigger)
        for g in ladder.failed:
            if g.log:
                self.audit.copy_log(self.cfg.repo / g.log, f"{g.id}-{step.id}-{attempt:02d}.log")
        self.state.data["last_ladder"] = {"step": step.id, "ok": ladder.ok, "summary": ladder.summary(),
                                          "feedback": ladder.feedback(self._max_items())}
        if ladder.lite_note():     # the Docker-free ladder: gates that need the container did not run
            self.state.data["last_ladder"]["not_run_here"] = ladder.lite_note()
        rule = ""
        if ladder.baseline:
            self.state.data["last_ladder"]["baseline"] = delta_digest(ladder.baseline)
            b = ladder.baseline
            if b.get("rule") != "absolute":
                rule = (f" (baseline rule {b['rule']}: {len(b.get('new') or [])} new, "
                        f"{len(b.get('fixed') or [])} fixed, {b.get('pre_existing', 0)} pre-existing)")
        self.output(f"[harness]   gates ({trigger}): {ladder.summary()}{rule}")
        return ladder

    def _max_items(self) -> int:
        return int(self.cfg.loop.get("feedback_max_items", 6))

    def _agent_paths(self) -> list[str]:
        baseline = set(self.state.data.get("baseline") or [])
        try:
            return [p for p in gitops.changed_paths(self.cfg.repo) if p not in baseline]
        except gitops.GitError:
            return []

    def _diff(self, max_chars: int = 12000) -> str:
        paths = self._agent_paths()
        if not paths:
            return "(no changes)"
        try:
            return gitops.diff(self.cfg.repo, paths, max_chars=max_chars)
        except gitops.GitError as exc:
            return f"(diff unavailable: {exc})"

    def _artifact(self, name: str) -> str:
        path = self.state.dir / name
        return path.read_text(encoding="utf-8") if path.is_file() else ""

    def _ticket_type(self) -> str:
        return str((self.ticket.fields.get("type", "") if self.ticket else "")).strip().lower()

    def _needs_test_change(self, step: StepDef) -> bool:
        rule = step.require_test_change
        if rule == "always":
            return True
        return rule == "bug" and self._ticket_type() in BUG_TYPES

    def _test_requirement(self, step: StepDef) -> str:
        if not self._needs_test_change(step):
            return ""
        what = "This is a bug ticket" if step.require_test_change == "bug" else "This step closes a test gap"
        return (f"{what}: add or change at least one test under tests/ that fails without the fix and "
                "passes with it. The harness checks this before the review; without it the step is not done.")

    def _missing_test_change(self, step: StepDef) -> str | None:
        """Structured feedback when the step must touch tests/ and did not, else None."""
        if not self._needs_test_change(step):
            return None
        if any(p.startswith("tests/") for p in self._agent_paths()):
            return None
        kind = "bug ticket" if step.require_test_change == "bug" else "test step"
        return ("GATES FAIL (TESTS:FAIL)\n"
                f"TESTS regression test: the change for this {kind} adds or modifies no file under tests/.\n"
                "- TESTS tests/ [regression-test] add or extend a unit test (tests/unit/test_<module>.c) or a "
                "loopback scenario (tests/loopback/) that reproduces the ticket's symptom: it must fail "
                "without the fix and pass with it. Register a new test file in scripts/test_manifest.txt.")

    def _flags(self, step: StepDef | None, role: str | None) -> set[str]:
        """Line flags of the prompt templates (prompts.select_lines): what this step and role really have."""
        flags: set[str] = set()
        if role and "run_gates" in self.cfg.role(role).tools:
            flags.add("run_gates")
        if step is not None and step.gates:
            flags.add("gates")
        return flags

    def _render(self, step: StepDef, **values: Any) -> str:
        return self.prompts.render(str(step.prompt), flags=self._flags(step, step.role), **self._vars(step, **values))

    def _gate_ids(self, spec: str | None) -> str:
        if not spec:
            return ""
        try:
            return ", ".join(g.id for g in self.cfg.gates.resolve(spec))
        except Exception:  # noqa: BLE001 - only a label in a prompt
            return str(spec)

    def _gate_report(self) -> str:
        """What the reviewer is told about the gates: the last ladder on this very tree, or that none ran."""
        last = self.state.data.get("last_ladder") or {}
        if not last:
            return ("Gate results: no gates ran in this workflow. Judge the diff itself; missing gate evidence "
                    "is never a finding.")
        delta = self._baseline_report(last.get("baseline"))
        if last.get("ok"):
            return (f"Gate results: the harness ran these gates on exactly this tree after the last change, "
                    f"all PASS: {last.get('summary')}. Do not ask for more gate evidence.{delta}")
        return (f"Gate results on exactly this tree: {last.get('summary')}. A failing gate is a finding only "
                f"when this change causes it.{delta}")

    @staticmethod
    def _baseline_report(d: dict[str, Any] | None) -> str:
        """The baseline delta for the reviewer: what the base already failed, what this change fixed."""
        if not d or d.get("rule") in (None, "absolute"):
            return ""
        how = ("the tests the ticket requires pass" if d["rule"] == "required"
               else "at least one of the base failures passes now")
        lines = [f"\nBaseline rule (the branch already failed tests before this change): a test gate passes "
                 f"when no test that passed on the untouched base fails now and {how}.",
                 f"- fixed by this change: {bl.short(d.get('fixed') or [], 10) or 'none'}",
                 f"- new failures: {bl.short(d.get('new') or [], 10) or 'none'}",
                 f"- still failing as on the base (pre-existing, not this change's and not a finding): "
                 f"{d.get('pre_existing', 0)}"]
        if d.get("required"):
            lines.append(f"- required by the ticket: {', '.join(d['required'])}")
        return "\n".join(lines)

    def _vars(self, step: StepDef, **extra: Any) -> dict[str, Any]:
        t = self.ticket
        last = self.state.data.get("last_ladder") or {}
        values: dict[str, Any] = {
            "ticket": t.as_prompt() if t else "(this workflow has no ticket)",
            "ticket_key": t.key if t else "-", "ticket_title": t.title if t else "-",
            "plan": self._artifact("plan.md").strip() or "(no plan for this workflow)",
            "gates": step.gates or "", "gate_ids": self._gate_ids(step.gates),
            "gate_summary": last.get("summary", "(not run yet)"), "gate_report": self._gate_report(),
            "gate_feedback": last.get("feedback", ""), "role": step.role or "",
            "ticket_type": self._ticket_type() or "unknown",
            "test_requirement": self._test_requirement(step),
            "tests_changed": ", ".join(p for p in self._agent_paths() if p.startswith("tests/")) or "none",
        }
        if step.prompt and "context_pack" in self.prompts.source(str(step.prompt)):
            values["context_pack"] = self._context_pack()
        values.update(extra)
        return values

    def _context_pack(self) -> str:
        """A trimmed planning context instead of a tree read (prompt template "cheap", cheap):
        AGENTS.md (when it is not already loaded as custom instructions), the section index of
        PROTOCOL.md and the module map of docs/ARCHITECTURE.md (or the directory list)."""
        repo = self.cfg.repo

        def read(rel: str) -> str:
            try:
                return (repo / rel).read_text(encoding="utf-8")
            except OSError:
                return ""
        parts: list[str] = []
        if self.cfg.copilot.get("custom_instructions", True):
            parts.append("AGENTS.md: loaded as your custom instructions (the rules apply).")
        else:
            parts.append("AGENTS.md:\n" + (read("AGENTS.md").strip() or "(missing)"))
        heads = [ln.rstrip() for ln in read("PROTOCOL.md").splitlines() if ln.startswith("#")]
        parts.append("PROTOCOL.md sections (open one with view and a line range when you need it):\n"
                     + ("\n".join(heads) or "(missing)"))
        arch = read("docs/ARCHITECTURE.md")
        m = re.search(r"^## 1\. Module map\s*\n+```\n(.*?)\n```", arch, re.S | re.M)
        if m:
            modmap = m.group(1)
        else:
            dirs = sorted(str(p.relative_to(repo)).replace("\\", "/") for base in ("src", "tools", "tests")
                          for p in (repo / base).glob("*") if p.is_dir())
            modmap = "\n".join(dirs) or "(no module map)"
        parts.append("Module map:\n" + modmap)
        return "\n\n".join(parts)

    # -- sessions -------------------------------------------------------------
    def _policy_event(self, kind: str, data: dict[str, Any]) -> None:
        if kind == "policy.deny":
            self.state.count("denials")
        self.audit.event(kind, step=self.current_step, **data)

    def _on_event(self, role: str) -> Callable[[Any], None]:
        def handler(event: Any) -> None:
            sid = self.state.data.get("sessions", {}).get(role, "?")
            try:
                self.audit.sdk_event(self.current_step, role, sid, event)
                self.usage.observe(sid, role, self.current_step, event)
                self._guard_credits(role, sid)
            except Exception as exc:  # noqa: BLE001 - auditing must never kill a session
                self.audit.event("audit.error", error=str(exc))
        return handler

    def _run_cap(self) -> float:
        _, run_cap = self.cfg.caps(self.runtime_mode)
        return run_cap + float(self.state.data.get("extra_credits", 0))

    def _guard_credits(self, role: str, sid: str) -> None:
        """Abort the turn in flight once a harness cap is reached.

        The runtime gets no session limit (it would show the model its remaining
        credits and ask it to be frugal), and the run cap spans sessions, so both
        caps are enforced here, while the turn runs, not only between turns."""
        if self.cap_hit:
            return
        s = self.usage.sessions.get(sid)
        spent = Usage.session_credits(s) if s else 0.0
        cap = self.session_caps.get(role)
        if cap is not None and spent >= cap:
            self.cap_hit = f"session credit cap reached ({role}): {spent:.2f} of {cap:.2f} AI credits"
        elif self.usage.credits >= self._run_cap():
            self.cap_hit = f"run credit cap reached: {self.usage.credits:.2f} of {self._run_cap():.2f} AI credits"
        else:
            return
        self.audit.event("credit_guard.abort", step=self.current_step, role=role, reason=self.cap_hit)
        session = self.sessions.get(role)
        abort = getattr(session, "abort", None)
        if abort is None:
            return
        try:
            asyncio.get_running_loop().create_task(abort())
        except RuntimeError:  # no loop in this thread: the check after the turn still stops the run
            pass

    def _spec(self, role: str) -> SessionSpec:
        rc = self.cfg.role(role)
        policy = RolePolicy(rc, self.cfg.policy, self.cfg.repo, audit=self._policy_event,
                            ticket_max_chars=int(self.cfg.ticket.get("max_chars", 12000)))
        handlers: dict[str, Callable[[dict[str, Any]], Any]] = {
            "run_gates": lambda a: self._tool_run_gates(role, a),
            "read_ticket": self._tool_read_ticket,
            "write_plan": self._tool_write_plan,
            "write_review": self._tool_write_review,
        }
        session_cap, run_cap = self.cfg.caps(self.runtime_mode, role)
        cap = max(0.5, min(session_cap, run_cap - self.usage.credits))
        self.session_caps[role] = round(cap, 2) + self._session_spent(role)
        system = self.prompts.system(role=role, writes=", ".join(rc.writes) or "nothing",
                                     protected=(", ".join(policy.protected) or "nothing")
                                     + (f" (except {', '.join(policy.protected_except)})"
                                        if policy.protected_except else ""))
        sm = self.cfg.copilot.get("system_message") or {}
        return SessionSpec(role=rc, model=self._model(role), agent_prompt=self.prompts.role(role, self._flags(None, role)),
                           system_content=system,
                           tools=make_tools(handlers, rc.custom_tools), hooks=policy.hooks(),
                           permission_handler=policy.permission_handler(), max_ai_credits=round(cap, 2),
                           working_directory=str(self.cfg.repo), on_event=self._on_event(role),
                           remove_sections=[str(s) for s in sm.get("remove", [])],
                           name=f"harness {self.state.run_id} {role}",
                           skip_custom_instructions=not bool(self.cfg.copilot.get("custom_instructions", True)))

    async def _session(self, role: str) -> AgentSession:
        if role in self.sessions:
            return self.sessions[role]
        spec = self._spec(role)
        session: AgentSession | None = None
        old = self.state.data.setdefault("sessions", {}).get(role)
        if old:     # a session of an earlier process of this run (resume): continue it, keep its cache
            try:
                session = await self.runtime.resume_session(old, spec)
                self.audit.event("session.resume", role=role, session=old)
            except Exception as exc:  # noqa: BLE001 - fall back to a fresh session
                self.audit.event("session.resume_failed", role=role, session=old, error=str(exc))
        if session is None:
            session = await self.runtime.create_session(spec)
            self.audit.event("session.create", role=role, session=session.session_id, model=spec.model,
                             tools=spec.role.tools, max_ai_credits=spec.max_ai_credits)
        self.state.data["sessions"][role] = session.session_id
        self.audit.prompt(self.current_step, role, f"{spec.system_content}\n\n---\n\n{spec.agent_prompt}\n",
                          kind="system")
        self.sessions[role] = session
        self._save()
        return session

    async def _close_sessions(self) -> None:
        for role, s in list(self.sessions.items()):
            try:
                await s.disconnect()
            except Exception as exc:  # noqa: BLE001
                self.audit.event("session.disconnect_error", role=role, error=str(exc))
        self.sessions.clear()

    async def _send(self, step: StepDef, role: str, session: AgentSession, prompt: str) -> str:
        self.audit.prompt(step.id, role, prompt)
        timeout = float(self.cfg.copilot.get("turn_timeout_s", 1800))
        t0 = time.monotonic()
        before = self.usage.credits
        try:
            reply = await session.send_and_wait(prompt, timeout=timeout)
        except Exception:  # noqa: BLE001
            if not self.cap_hit:
                raise
            reply = ""     # the credit guard aborted this turn; _over_budget stops the run
        spent = self.usage.credits - before
        self.state.data.setdefault("last_turn", {})[role] = {"credits": round(spent, 4), "model": self._model(role)}
        self.audit.event("turn", step=step.id, role=role, seconds=round(time.monotonic() - t0, 1),
                         credits=round(spent, 4), model=self._model(role), reply=reply[:2000])
        self.state.count("turns")
        self._save()
        return reply

    def _session_spent(self, role: str) -> float:
        sid = self.state.data.get("sessions", {}).get(role)
        s = self.usage.sessions.get(sid) if sid else None
        return Usage.session_credits(s) if s else 0.0

    def _over_budget(self, step: StepDef) -> Outcome | None:
        run_cap = self._run_cap()
        if self.usage.credits < run_cap and not self.cap_hit:
            return None
        reason = self.cap_hit or f"run credit cap reached: {self.usage.credits:.2f} of {run_cap:.2f} AI credits"
        choice = self.approver.escalate(step.id, reason, ("continue", "abort"))
        if choice == "continue":
            session_cap, _ = self.cfg.caps(self.runtime_mode)
            self.state.data["extra_credits"] = float(self.state.data.get("extra_credits", 0)) + session_cap
            for r in self.session_caps:
                self.session_caps[r] += session_cap
            self.cap_hit = ""
            return None
        return stop("escalated", reason)

    # ============================================================== tools ====
    async def _tool_run_gates(self, role: str, args: dict[str, Any]) -> str:
        step = self.workflow.steps[self.state.cursor]
        st = self.state.step(step.id)
        st["tool_gate_runs"] = int(st.get("tool_gate_runs", 0)) + 1
        self.state.count("tool_gate_runs")
        ladder = await self._run_gates(step, st["tool_gate_runs"], "tool", expect_changes=False)
        return ladder.feedback(self._max_items())

    def _tool_read_ticket(self, args: dict[str, Any]) -> str:
        if not self.ticket:
            return "(this workflow has no ticket)"
        return self.ticket.as_prompt(args.get("section") or None)

    def _tool_write_plan(self, args: dict[str, Any]) -> str:
        plan = str(args.get("plan", "")).strip()
        if not plan:
            return "error: empty plan"
        plan = plan[:20000]
        self.audit.write("plan.md", plan + "\n")
        self.state.data.setdefault("artifacts", {})["plan"] = "plan.md"
        self.audit.event("artifact", name="plan.md", chars=len(plan))
        return f"plan saved ({len(plan.splitlines())} lines)"

    def _tool_write_review(self, args: dict[str, Any]) -> str:
        verdict = str(args.get("verdict", "")).lower()
        if verdict not in ("approve", "reject"):
            return "error: verdict must be approve or reject"
        asked = verdict
        verdict, findings, notes = normalize_review(verdict, list(args.get("findings") or []))
        review = {"verdict": verdict, "score": int(args.get("score", 0) or 0),
                  "summary": str(args.get("summary", "")), "findings": findings,
                  "round": self.state.step(self.current_step).get("rounds", 0)}
        if notes:
            review["harness_notes"] = notes
        self.state.data["review"] = review
        self.audit.write_json("review.json", review)
        self.audit.write("review.md", render_review(review))
        self.audit.event("review", verdict=verdict, score=review["score"], findings=len(findings),
                         asked=asked, notes=notes)
        self._save()
        return f"review recorded: {verdict}" + (f" ({'; '.join(notes)})" if notes else "")

    # ============================================================== steps ====
    async def _step_ticket(self, step: StepDef, resumed: bool) -> Outcome:
        if self.ticket is None:
            ref = str(self.state.data.get("ticket_ref") or "")
            try:
                self.ticket = await self._load_ticket(ref)
            except (tk.TicketError, RuntimeError, OSError) as exc:
                return stop("failed", f"cannot load ticket {ref!r}: {exc}")
            self.state.data["ticket"] = self.ticket.to_dict()
            self.audit.write_json("ticket.json", self.ticket.to_dict())
            self.audit.write("ticket.md", self.ticket.as_prompt() + "\n")
            self.audit.event("ticket", key=self.ticket.key, source=self.ticket.source, flags=self.ticket.flags,
                             findings=self.ticket.findings, truncated=self.ticket.truncated)
        t = self.ticket
        if t.flags:
            self.output(f"[harness]   sanitizer flagged the ticket: {', '.join(t.flags)}")
        if t.flags and self.cfg.ticket.get("on_injection", "escalate") == "escalate" and step.approval == "human":
            details = "\n".join(f"  - {f['flag']}: {f['excerpt']}" for f in t.findings)
            ok = self.approver.approve(step.id, f"ticket {t.key} contains instruction-like text "
                                       f"({', '.join(t.flags)}). Continue, treating it as data only?", details)
            if not ok:
                return stop("aborted", "flagged ticket not released by a human")
        return DONE

    async def _load_ticket(self, ref: str) -> tk.Ticket:
        max_chars = int(self.cfg.ticket.get("max_chars", 12000))
        for cand in (Path(ref), self.cfg.repo / ref):
            if ref and cand.is_file():
                return tk.load_file(cand, max_chars)
        jira = self.cfg.ticket.get("jira") or {}
        if tk.is_jira_key(ref, str(jira.get("key_pattern", r"^[A-Z][A-Z0-9]+-[0-9]+$"))):
            import os
            server = {"name": jira.get("mcp_server", "atlassian"), "type": "http", "url": jira.get("url", "")}
            args = {"issueIdOrKey": ref}
            cloud = os.environ.get(str(jira.get("cloud_id_env", "JIRA_CLOUD_ID")), "")
            if cloud:
                args["cloudId"] = cloud
            payload = await self.runtime.call_mcp_tool(server, str(jira.get("tool", "getJiraIssue")), args)
            return tk.from_jira_payload(ref, payload, max_chars)
        raise tk.TicketError("not a file and not a Jira key")

    async def _step_agent(self, step: StepDef, resumed: bool) -> Outcome:
        role = str(step.role)
        session = await self._session(role)
        extra: dict[str, Any] = {}
        if step.prompt == "release":
            try:
                extra["git_log"] = gitops.git(self.cfg.repo, "log", "--oneline", "-15").strip()
            except gitops.GitError:
                extra["git_log"] = "(no git history)"
        reply = await self._send(step, role, session, self._render(step, **extra))
        over = self._over_budget(step)
        if over:
            return over
        if step.artifact:
            if not (self.state.dir / step.artifact).is_file():
                self.audit.write(step.artifact, reply.strip() + "\n")
            self.state.data.setdefault("artifacts", {})[step.artifact] = step.artifact
        if step.approval == "human":
            text = self._artifact(step.artifact) if step.artifact else reply
            head = "\n".join(text.splitlines()[:60])
            if not self.approver.approve(step.id, f"approve {step.artifact or 'the result'} of {role}?",
                                         f"----- {step.artifact or 'reply'} -----\n{head}\n-----"):
                return stop("aborted", f"{step.artifact or 'result'} rejected by a human")
        return DONE

    def _rework(self, step: StepDef, round_no: int) -> str:
        """Feedback for a step that runs again: reviewer findings and / or a failed full ladder."""
        parts = []
        review = (self.state.data.get("feedback") or {}).get(step.id)
        if review:
            parts.append(self.prompts.render("rework", round=round_no, review_feedback=review))
        ladder = (self.state.data.get("ladder_feedback") or {}).get(step.id)
        if ladder:
            parts.append(self.prompts.render("ladder", gate_summary=ladder.get("summary", ""),
                                             gate_feedback=ladder.get("feedback", "")))
        return "\n".join(p.strip() for p in parts)

    def _clear_feedback(self, step: StepDef) -> None:
        for key in ("feedback", "ladder_feedback"):
            (self.state.data.get(key) or {}).pop(step.id, None)

    async def _step_agent_gated(self, step: StepDef, resumed: bool) -> Outcome:
        role = str(step.role)
        st = self.state.step(step.id)
        budget = int(step.retry_budget if step.retry_budget is not None else self.cfg.loop.get("retry_budget", 3))
        st.setdefault("retries", 0)
        st.setdefault("extra_budget", 0)
        max_items = self._max_items()

        if resumed and st.get("attempts", 0) > 0:
            # cheap deterministic gates before any model call: maybe the work is already done
            ladder = await self._run_gates(step, int(st["attempts"]), "resume", expect_changes=True)
            missing = self._check_test_change(step, int(st["attempts"])) if ladder.ok else None
            if ladder.ok and not missing:
                st["result"] = "pass"
                self._clear_feedback(step)
                return DONE
            prompt = self._render(step, budget=budget + 1, rework=self._rework(step, self._review_round()))
            prompt += "\n" + self.prompts.render("retry", attempt=st["attempts"], budget=budget + 1,
                                                 gate_feedback=missing or ladder.feedback(max_items))
        else:
            diff = self._diff(8000) if role == "test-writer" else ""
            prompt = self._render(step, budget=budget + 1, diff=diff, rework=self._rework(step, self._review_round()))
        session = await self._session(role)
        while True:
            over = await self._afford_turn(step, role, session)
            if over:
                return over
            st["attempts"] = int(st.get("attempts", 0)) + 1
            self._save()
            await self._send(step, role, session, prompt)
            over = self._over_budget(step)
            if over:
                return over
            ladder = await self._run_gates(step, st["attempts"], "harness", expect_changes=True)
            while ladder.incomplete_only:
                # no gate failed, some could not run: another agent turn cannot help.
                # A human fixes the setup and answers "retry" (the gates run again, no model call).
                skipped = ", ".join(f"{g.id} ({g.reason})" for g in ladder.skipped)
                reason = f"gate ladder incomplete, not an agent error: {skipped}"
                if self.approver.escalate(step.id, reason, ("retry", "abort")) != "retry":
                    return stop("escalated", reason)
                ladder = await self._run_gates(step, st["attempts"], "harness", expect_changes=True)
            missing = self._check_test_change(step, st["attempts"]) if ladder.ok else None
            if ladder.ok and not missing:
                st["result"] = "pass"
                self._clear_feedback(step)
                return DONE
            failing = ladder.summary() if not ladder.ok else TESTS_FAIL
            if st["retries"] >= budget + st["extra_budget"]:
                reason = f"gates still failing after {st['retries']} retries: {failing}"
                if step.on_exhausted == "escalate":
                    if self.approver.escalate(step.id, reason, ("retry", "abort")) == "retry":
                        st["extra_budget"] += 1
                    else:
                        return stop("escalated", reason)
                else:
                    return stop("failed", reason)
            st["retries"] += 1
            self.state.count("retries")
            self.audit.event("retry", step=step.id, attempt=st["attempts"] + 1,
                             reason="gates failed" if not ladder.ok else "no regression test")
            if st["retries"] == 1:
                over = await self._escalate_model(step, role, session)
                if over:
                    return over
            prompt = self.prompts.render("retry", attempt=st["attempts"], budget=budget + 1 + st["extra_budget"],
                                         gate_feedback=ladder.feedback(max_items) if not ladder.ok else missing)

    # -- model escalation within the credit caps ------------------------------
    def _model(self, role: str) -> str:
        """The model the role's session runs on now (its escalation model after a switch)."""
        return str((self.state.data.get("models") or {}).get(role) or self.cfg.role(role).model)

    def _credit_room(self, role: str) -> float:
        """AI credits a next turn of ``role`` may still spend: the smaller of its session cap and the run cap."""
        session_room = self.session_caps.get(role, float("inf")) - self._session_spent(role)
        return max(0.0, min(session_room, self._run_cap() - self.usage.credits))

    def _cost_ratio(self, src: str, dst: str) -> float:
        """Price ratio dst / src per token (copilot.model_cost); unknown models: loop.escalation_cost_ratio."""
        if src == dst:
            return 1.0
        table = self.cfg.copilot.get("model_cost") or {}
        a, b = table.get(src), table.get(dst)
        if a and b:
            return float(b) / float(a)
        return float(self.cfg.loop.get("escalation_cost_ratio", 20))

    def _turn_estimate(self, role: str, model: str) -> float:
        """Credits of the next turn of ``role`` on ``model``: its last turn scaled by the price ratio,
        at least loop.escalation_min_credits."""
        last = (self.state.data.get("last_turn") or {}).get(role) or {}
        floor = float(self.cfg.loop.get("escalation_min_credits", 2))
        if not last:
            return floor
        return max(floor, float(last.get("credits", 0)) * self._cost_ratio(str(last.get("model") or model), model))

    async def _set_model(self, step: StepDef, role: str, session: AgentSession, model: str) -> None:
        try:
            await session.set_model(model)
            self.state.data.setdefault("models", {})[role] = model
            self.audit.event("model.switch", step=step.id, role=role, model=model)
        except Exception as exc:  # noqa: BLE001
            self.audit.event("model.switch_failed", step=step.id, role=role, model=model, error=str(exc))

    def _refuse_model(self, step: StepDef, role: str, model: str, what: str) -> tuple[str, str]:
        need, room = self._turn_estimate(role, model), self._credit_room(role)
        reason = (f"{what} {role} on {model} needs ~{need:.1f} AI credits per turn, "
                  f"{room:.1f} left under the session / run cap")
        self.audit.event("model.escalation_refused", step=step.id, role=role, model=model,
                         need=round(need, 2), room=round(room, 2))
        # unattended (eval, autonomous): back to the role's own model, counted as an escalation
        return reason, self.approver.escalate(step.id, reason, ("retry", "abort"), unattended="retry")

    def _affordable(self, role: str, model: str) -> bool:
        return self._credit_room(role) >= self._turn_estimate(role, model)

    async def _escalate_model(self, step: StepDef, role: str, session: AgentSession) -> Outcome | None:
        """After the first failed attempt: the stronger model if the budget pays for its turns,
        else go on with the current model; a human is asked only when the retry budget is spent."""
        rc = self.cfg.role(role)
        esc = rc.escalation_model
        if not esc or esc == rc.model or self._model(role) == esc:
            return None
        if self._affordable(role, esc):
            await self._set_model(step, role, session, esc)
            return None
        self.audit.event("model.escalation_skipped", step=step.id, role=role, model=esc,
                         need=round(self._turn_estimate(role, esc), 2), room=round(self._credit_room(role), 2))
        return None

    async def _afford_turn(self, step: StepDef, role: str, session: AgentSession) -> Outcome | None:
        """Before every turn on an escalated (pricier) model: does the budget still pay for it?
        No: a human decides (retry = back to the role's own model, abort); unattended (eval,
        autonomous) the run goes back to the role's own model, counted as an escalation."""
        rc = self.cfg.role(role)
        cur = self._model(role)
        if cur == rc.model or self._affordable(role, cur):
            return None
        reason, choice = self._refuse_model(step, role, cur, "the next turn of")
        if choice == "retry":
            await self._set_model(step, role, session, rc.model)
            return None
        return stop("escalated", reason)

    def _check_test_change(self, step: StepDef, attempt: int) -> str | None:
        """The deterministic test check of a ``require_test_change`` step (after green gates): audited, counted."""
        missing = self._missing_test_change(step)
        if self._needs_test_change(step):
            self.audit.event("check", step=step.id, check="test-change", attempt=attempt,
                             ok=missing is None, ticket_type=self._ticket_type())
            self.output(f"[harness]   test check: {'PASS' if missing is None else 'FAIL (nothing changed under tests/)'}")
        if missing:
            self.state.count("test_check_fails")
            self.state.data["last_ladder"] = {"step": step.id, "ok": False, "summary": TESTS_FAIL,
                                              "feedback": missing}
        return missing

    def _review_round(self) -> int:
        for s in self.workflow.steps:
            if s.kind == "review":
                return int(self.state.step(s.id).get("rounds", 0))
        return 0

    async def _step_gates(self, step: StepDef, resumed: bool) -> Outcome:
        st = self.state.step(step.id)
        st["attempts"] = int(st.get("attempts", 0)) + 1
        ladder = await self._run_gates(step, st["attempts"], "harness", expect_changes=False)
        if ladder.ok:
            st["result"] = "pass"
            return DONE
        st["result"] = "fail"
        if step.on_fail == "continue":
            return DONE
        if step.on_fail not in ("fail", "escalate"):
            return self._ladder_back(step, st, ladder)
        if step.on_fail == "escalate":
            if self.approver.escalate(step.id, f"ladder failed: {ladder.summary()}", ("continue", "abort")) == "continue":
                return DONE
            return stop("escalated", f"ladder failed: {ladder.summary()}")
        return stop("failed", f"ladder failed: {ladder.summary()}")

    def _ladder_back(self, step: StepDef, st: dict[str, Any], ladder: LadderResult) -> Outcome:
        """The full ladder failed after the fast gates passed: its structured feedback goes to every
        agent step from ``on_fail`` up to here, bounded by max_rounds (loop.ladder_rounds)."""
        st["fails"] = int(st.get("fails", 0)) + 1
        self.state.count("ladder_fails")
        max_rounds = int(step.max_rounds if step.max_rounds is not None else self.cfg.loop.get("ladder_rounds", 2))
        if st["fails"] > max_rounds + int(st.get("extra_rounds", 0)):
            reason = f"ladder failed {st['fails']} times (budget {max_rounds}): {ladder.summary()}"
            if self.approver.escalate(step.id, reason, ("retry", "abort")) != "retry":
                return stop("escalated", reason)
            st["extra_rounds"] = int(st.get("extra_rounds", 0)) + 1
        target = str(step.on_fail)
        fb = {"summary": ladder.summary(), "feedback": ladder.feedback(self._max_items())}
        for s in self.workflow.steps[self.workflow.index(target):self.workflow.index(step.id)]:
            if s.role and s.kind in ("agent", "agent_gated"):
                self.state.data.setdefault("ladder_feedback", {})[s.id] = fb
        self.audit.event("retry", step=target, attempt=st["fails"] + 1, reason="ladder failed")
        return Outcome("jump", target=target, reason="ladder failed")

    async def _step_review(self, step: StepDef, resumed: bool) -> Outcome:
        role = str(step.role)
        st = self.state.step(step.id)
        st["rounds"] = int(st.get("rounds", 0)) + 1
        self.state.data.pop("review", None)
        self._save()
        session = await self._session(role)
        prompt = self._render(step, diff=self._diff(12000), round=st["rounds"])
        reply = await self._send(step, role, session, prompt)
        if "review" not in self.state.data:
            reply = await self._send(step, role, session,
                                     "You did not record a verdict. Call write_review now, exactly once.")
        over = self._over_budget(step)
        if over:
            return over
        review = self.state.data.get("review") or fallback_review(reply)
        if "review" not in self.state.data:
            self.state.data["review"] = review
            self.audit.write_json("review.json", review)
            self.audit.write("review.md", render_review(review))
        st.setdefault("verdicts", []).append(review["verdict"])
        self.output(f"[harness]   review round {st['rounds']}: {review['verdict']} (score {review.get('score')})")
        if review["verdict"] == "approve":
            return DONE
        self.state.count("review_rejects")
        if not step.on_reject:
            return DONE  # review-only: the review is the product
        max_rounds = int(step.max_rounds if step.max_rounds is not None else self.cfg.loop.get("review_rounds", 2))
        rejects = st["verdicts"].count("reject")
        if rejects > max_rounds + int(st.get("extra_rounds", 0)):
            reason = f"reviewer rejected {rejects} times (budget {max_rounds})"
            if self.approver.escalate(step.id, reason, ("retry", "abort")) == "retry":
                st["extra_rounds"] = int(st.get("extra_rounds", 0)) + 1
            else:
                return stop("escalated", reason)
        # every agent step that runs again sees the findings: the implementer cannot
        # edit tests/, so findings about the tests are the test-writer's to fix
        findings = render_findings(review)
        steps = self.workflow.steps
        for s in steps[self.workflow.index(step.on_reject):self.workflow.index(step.id)]:
            if s.role and s.kind in ("agent", "agent_gated"):
                self.state.data.setdefault("feedback", {})[s.id] = findings
        self.audit.event("retry", step=step.on_reject, attempt=rejects + 1, reason="review rejected")
        return Outcome("jump", target=step.on_reject, reason="review rejected")

    async def _step_approval(self, step: StepDef, resumed: bool) -> Outcome:
        details = self._run_digest()
        if self.approver.approve(step.id, step.message or "continue?", details):
            return DONE
        return stop("aborted", "not approved by a human")

    async def _step_commit(self, step: StepDef, resumed: bool) -> Outcome:
        if self.state.data.get("commit"):
            return DONE  # resumed after a successful commit
        t = self.ticket
        if t is None:
            return stop("failed", "commit needs a ticket (Jira key in the subject)")
        paths = self._agent_paths()
        if not paths:
            return stop("failed", "nothing to commit")
        allowed = self._allowed_writes()
        bad = [p for p in paths if protected_hit(self.cfg.policy, p) or not any_match(allowed, p)]
        if bad:
            return stop("failed", f"refusing to commit paths outside the policy: {', '.join(bad)}")
        fmt = {"key": t.key, "key_lower": t.key_lower, "title": t.title, "run_id": self.state.run_id,
               "run_short": self.state.run_id.rsplit("-", 1)[-1], "workflow": self.workflow.name}
        subject = str(self.cfg.commit.get("subject", "{key}: {title}")).format(**fmt)
        subject = subject if len(subject) <= 72 else subject[:69].rstrip() + "..."
        key_pat = str((self.cfg.ticket.get("jira") or {}).get("key_pattern", r"^[A-Z][A-Z0-9]+-[0-9]+$"))
        if not re.search(key_pat.strip("^$"), subject):
            return stop("failed", f"commit subject {subject!r} carries no Jira key (compliance)")
        review = self.state.data.get("review") or {}
        body = [f"Workflow {self.workflow.name}; gates {(self.state.data.get('last_ladder') or {}).get('summary', '-')}."]
        if review:
            body.append(f"Review: {review.get('verdict')} (score {review.get('score')}).")
        trailers = [str(x).format(**fmt) for x in self.cfg.commit.get("trailers", [])]
        message = subject + "\n\n" + "\n".join(body) + ("\n\n" + "\n".join(trailers) if trailers else "") + "\n"
        branch = str(self.cfg.commit.get("branch", "agent/{key_lower}-{run_short}")).format(**fmt)
        try:
            branch, sha = gitops.commit_on_branch(self.cfg.repo, branch, message, paths)
        except gitops.GitError as exc:
            return stop("failed", f"commit failed: {exc}")
        self.state.data["branch"] = branch
        self.state.data["commit"] = sha
        self.audit.event("commit", branch=branch, sha=sha, paths=paths, subject=subject)
        self.output(f"[harness]   committed {sha[:10]} on {branch} (not pushed)")
        return DONE

    # ============================================================ summary ====
    def _run_digest(self) -> str:
        last = self.state.data.get("last_ladder") or {}
        review = self.state.data.get("review") or {}
        paths = self._agent_paths()
        lines = [f"changed: {', '.join(paths) or '(nothing)'}",
                 f"gates:   {last.get('summary', '(not run)')}",
                 f"review:  {review.get('verdict', '-')} (score {review.get('score', '-')})",
                 f"credits: {self.usage.credits:.2f}"]
        return "\n".join(lines)

    def _write_summary(self) -> None:
        c = self.state.counters
        summary = {
            "run_id": self.state.run_id, "workflow": self.workflow.name, "status": self.state.status,
            "mode": self.mode, "runtime": self.runtime_mode, "caps": list(self.cfg.caps(self.runtime_mode)),
            "where": self.env.where,
            "ticket": (self.ticket.key if self.ticket else None),
            "steps": {s: d.get("status") for s, d in self.state.data["steps"].items()},
            "stopped_at": self.state.data.get("stopped_at"), "reason": self.state.data.get("stop_reason"),
            "error": self.state.data.get("error"),
            "interventions": int(c.get("approvals_counted", 0)) + int(c.get("approvals_asked", 0))
            + int(c.get("escalations", 0)),
            "counters": c, "credits": round(self.usage.credits, 4), "credits_by_role": self.usage.by_role(),
            "tokens": self.usage.tokens(), "duration_s": self.state.data.get("duration_s"),
            "branch": self.state.data.get("branch"), "commit": self.state.data.get("commit"),
            "review": (self.state.data.get("review") or {}).get("verdict"),
        }
        not_run = (self.state.data.get("last_ladder") or {}).get("not_run_here")
        if not_run:               # "lite ladder: misra no-heap sanitizers coverage not run here (needs Docker)"
            summary["not_run_here"] = not_run
        tb = self.state.data.get("test_baseline")
        if tb is not None:
            summary["baseline"] = {k: tb.get(k) for k in ("mode", "active", "summary", "reason", "duration_ms")}
            summary["baseline"]["failures"] = {gid: v.get("failures") for gid, v in (tb.get("gates") or {}).items()
                                               if v.get("status") == "FAIL"}
            summary["baseline_delta"] = (self.state.data.get("last_ladder") or {}).get("baseline")
        self.audit.write_json("summary.json", summary)


def delta_digest(info: dict[str, Any]) -> dict[str, Any]:
    """The baseline delta of a ladder as the run state, the reviewer and summary.json keep it."""
    keys = ("rule", "final", "required", "fixed", "new", "pre_existing", "progress", "not_found")
    out = {k: info[k] for k in keys if k in info}
    out["gates"] = {gid: {k: v for k, v in d.items() if k in ("rule", "raw_status", "new", "fixed", "required")}
                    | {"pre_existing": len(d.get("pre_existing") or [])}
                    for gid, d in (info.get("gates") or {}).items()}
    return out


BLOCKING = ("blocker", "major")


def normalize_review(verdict: str, findings: list[dict[str, Any]]) -> tuple[str, list[dict[str, Any]], list[str]]:
    """The review policy as code: a blocker / major finding must point at file:line (a concrete
    defect), else it is downgraded to minor; an approve with an open blocking finding is a reject,
    a reject without one is an approve. Returns (verdict, findings, notes on what was changed)."""
    out: list[dict[str, Any]] = []
    notes: list[str] = []
    vague = 0
    for f in findings:
        f = dict(f)
        sev = str(f.get("severity") or "major")
        try:
            line = int(f.get("line") or 0)
        except (TypeError, ValueError):
            line = 0
        if sev in BLOCKING and (not str(f.get("file") or "").strip() or line < 1):
            f["severity"] = "minor"
            f["message"] = f"(was {sev}, no file:line) {f.get('message', '')}"
            vague += 1
        out.append(f)
    if vague:
        notes.append(f"{vague} blocker/major finding(s) without file:line downgraded to minor")
    blocking = [f for f in out if str(f.get("severity") or "major") in BLOCKING]
    if verdict == "approve" and blocking:
        verdict = "reject"
        notes.append("approve with an open blocker/major finding counts as reject")
    elif verdict == "reject" and not blocking:
        verdict = "approve"
        notes.append("reject without a blocker/major finding at file:line counts as approve")
    return verdict, out, notes


def render_findings(review: dict[str, Any]) -> str:
    lines = [f"Verdict: {review.get('verdict')} (score {review.get('score')}). {review.get('summary', '')}".strip()]
    for f in review.get("findings") or []:
        loc = f"{f.get('file')}:{f.get('line')}" if f.get("file") else "-"
        lines.append(f"- [{f.get('severity', 'major')}] {loc} {f.get('message', '')}")
    return "\n".join(lines)


def render_review(review: dict[str, Any]) -> str:
    return f"# Review (round {review.get('round', '?')})\n\n" + render_findings(review) + "\n"


def fallback_review(reply: str) -> dict[str, Any]:
    """The reviewer answered without write_review: read 'VERDICT: approve|reject' from the text."""
    m = re.search(r"verdict\W{0,3}(approve|reject)", reply, re.I)
    verdict = m.group(1).lower() if m else "reject"
    summary = reply.strip()[:600] or "(empty review)"
    findings = [] if m else [{"file": "", "line": 0, "severity": "major",
                              "message": "the reviewer recorded no verdict; treat as rejected"}]
    return {"verdict": verdict, "score": 0, "summary": summary, "findings": findings, "round": 0}


def load_events(run_dir: Path) -> list[dict[str, Any]]:
    path = Path(run_dir) / "events.jsonl"
    return [json.loads(x) for x in path.read_text(encoding="utf-8").splitlines() if x.strip()] if path.exists() else []


def run_workflow(workflow: str, ticket: str, settings: Any, mode: str = "eval") -> Any:
    """harness.api backend (the eval runner finds it here): see harness/backend.py."""
    from .backend import run_workflow as _run

    return _run(workflow, ticket, settings, mode)
