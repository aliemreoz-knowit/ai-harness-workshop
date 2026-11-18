"""The orchestrator behind ``harness.api.run_workflow`` (what the eval runner calls).

    run_workflow(workflow, ticket, settings, mode) -> harness.api.RunResult

One call = one orchestrator run inside ``settings.workdir`` (the eval worktree):

- the harness configuration is ``harness/harness.yaml`` of this package with the
  variant applied: models per role, roles on/off (steps of a switched-off role
  are dropped from the workflow), retry budget, credit cap, prompt template
  (``prompts/<template>/`` overlays the base templates), custom instructions
  on/off (``skip_custom_instructions``), gates on/off (gated steps become plain
  agent steps, the final ladder step is dropped);
- ``mode`` "eval" maps to the orchestrator's eval mode (human approvals pass and
  are COUNTED), ``settings.autonomy == "auto"`` to its autonomous mode (nothing
  counted); escalations end the run;
- the ticket is a file: ``settings.extra["ticket_file"]`` (the runner passes the
  hidden-tests ``tickets/<ID>/ticket.md``), else ``<hidden-tests>/tickets/<ID>/ticket.md``
  ($HARNESS_HIDDEN_TESTS or a sibling ``hidden-tests`` checkout), else the id is
  fetched from Jira;
- the orchestrator's own run folder goes to ``<settings.run_dir>/orchestrator/<run-id>/``;
  its events are translated into the eval schema (harness/api.py) in
  ``<settings.run_dir>/events.jsonl``.

``settings.extra`` knobs: ``escalation`` (false = no role switches to its
``escalation_model``), ``role_overrides`` ({role: {tools: [...], max_reads: N}}: a
smaller tool surface / read budget per role, cheap), ``runtime`` ("copilot" default | "fake": the scripted
FakeRuntime, no model and no credits -- a plumbing check with real gates and
hidden tests), ``runtime_mode`` (facilitator | copilot-user | auto: the caps of harness.yaml
``runtimes.<mode>``; variants.yaml sets facilitator for every eval batch, auto reads the main
checkout's ``harness doctor`` file and falls back to facilitator), ``config`` (another
harness.yaml, e.g. a workshop unit's: host-side keys stay the package's, see ``load_run_config``),
``ticket_file``, ``applied_knobs`` (an overlay variant's list of the settings that apply, see
``applied_knobs``; absent: every setting applies),
``baseline`` (off default: absolute gates as in every earlier batch | auto: the
baseline rule of harness/baseline.py).
Tests inject a runtime and a gate runner through ``make_backend``.
"""

from __future__ import annotations

import asyncio
import copy
import dataclasses
import datetime as _dt
import json
import os
import time
from pathlib import Path
from typing import Any, Callable, Iterable

from . import envdetect
from .api import KNOB_NAMES, RunResult, RunSettings
from .audit import NANO_PER_CREDIT
from .config import DEFAULT_CONFIG, HarnessConfig, StepDef, Workflow, load_config

# eval role names (harness.api.ROLES) -> harness.yaml role names
ROLE_MAP = {"planner": "planner", "implementer": "implementer", "test_writer": "test-writer",
            "reviewer": "reviewer"}
# harness.yaml role -> the eval role that switches it on/off and gives its model
CFG_TO_EVAL = {"planner": "planner", "implementer": "implementer", "test-writer": "test_writer",
               "reviewer": "reviewer", "solo": "implementer"}

RuntimeFactory = Callable[[HarnessConfig, RunSettings], Any]
GateRunnerFactory = Callable[[HarnessConfig, envdetect.Environment], Any]


# ------------------------------------------------------------- configuration ----
def _pin(target: dict[str, Any], package: dict[str, Any], key: str) -> None:
    """``target[key]`` becomes a copy of the package's value; absent there, absent here."""
    if key in package:
        target[key] = copy.deepcopy(package[key])
    else:
        target.pop(key, None)


def load_run_config(path: str | Path | None, repo: str | Path) -> tuple[HarnessConfig, HarnessConfig]:
    """(the configuration of a run, the package's own configuration).

    ``path`` is the harness.yaml to load (None: HARNESS_CONFIG, else the package's own). Another file than
    the package's own is a workshop unit's (``extra.config`` of an overlay variant): its roles, loop, policy,
    ticket, commit and the workflows / gates / prompts it points at are the run's, but the host-side keys come
    from the package's harness/harness.yaml, whatever the unit's file says: the whole ``copilot`` section
    (cli_path, env_passthrough, versions, model_cost, ...), ``paths.bash``, ``paths.check_log``, ``ticket.jira``
    (the MCP server the ticket step talks to), ``runtime`` and ``runtimes`` (the credit caps). The unit's file
    runs under the facilitator's login on the facilitator's machine; it must not pick the binary, the
    environment the CLI inherits, the caps, the shell, the log folder of the gate runs or the Jira endpoint.
    The variant's settings (apply_settings) go on top of the result.

    The unit's gates.yaml (its gate commands) and its hooks are not pinned here: they stay the unit's. What
    they may run on the host is locked where the console saves them (its save rules), not by this function."""
    cfg = load_config(path, repo=repo)
    if cfg.path == DEFAULT_CONFIG:
        return cfg, cfg
    package = load_config(DEFAULT_CONFIG, repo=repo)
    cfg.copilot = copy.deepcopy(package.copilot)
    cfg.runtime = package.runtime
    cfg.runtimes = copy.deepcopy(package.runtimes)
    _pin(cfg.paths, package.paths, "bash")
    _pin(cfg.paths, package.paths, "check_log")
    _pin(cfg.ticket, package.ticket, "jira")
    return cfg, package


def applied_knobs(settings: RunSettings) -> frozenset[str] | None:
    """The settings that apply to this run: None = all of them (a variant without overlay_from); else the
    names in ``settings.extra["applied_knobs"]``, the ones an overlay variant sets itself, while every other
    knob keeps what the unit's harness.yaml and workflow say. Names: retry_budget, custom_instructions,
    prompt_template, autonomy, gates, max_ai_credits (the cap applies whether it is listed or not),
    roles.<role> and models.<role> with the role names of harness.api.ROLES."""
    names = (settings.extra or {}).get("applied_knobs")
    if names is None:
        return None
    if not isinstance(names, (list, tuple)):
        raise ValueError(f"extra.applied_knobs must be a list of knob names, got {names!r}")
    unknown = sorted(str(n) for n in names if n not in KNOB_NAMES)     # one list with harness.eval.variants
    if unknown:
        raise ValueError(f"extra.applied_knobs: unknown knob(s) {', '.join(unknown)}")
    return frozenset(names)


def knob_applies(settings: RunSettings, name: str) -> bool:
    knobs = applied_knobs(settings)
    return knobs is None or name in knobs


def apply_settings(cfg: HarnessConfig, settings: RunSettings) -> HarnessConfig:
    """The variant's knobs on a freshly loaded configuration (mutated and returned). Of an overlay variant only
    the knobs it sets itself (``applied_knobs``) and the run cap: the rest stays as the loaded file has it."""
    defaults = {name: rc.model for name, rc in cfg.roles.items()}
    for cfg_role, eval_role in CFG_TO_EVAL.items():
        if cfg_role not in cfg.roles or not knob_applies(settings, f"models.{eval_role}"):
            continue
        model = (settings.models or {}).get(eval_role)
        if not model:
            continue
        rc = cfg.roles[cfg_role]
        rc.model = str(model)
        # escalate to a stronger model only when the variant keeps the harness default;
        # "all-Haiku" (team-small) must stay all-Haiku
        if rc.escalation_model and rc.model != defaults[cfg_role]:
            rc.escalation_model = None
    extra = settings.extra or {}
    if extra.get("escalation") is False:
        # "none on Sonnet": no retry turn switches to a stronger model either
        for rc in cfg.roles.values():
            rc.escalation_model = None
    for eval_role, over in (extra.get("role_overrides") or {}).items():
        cfg_role = ROLE_MAP.get(str(eval_role), str(eval_role))
        if cfg_role not in cfg.roles or not isinstance(over, dict):
            raise ValueError(f"role_overrides: unknown role {eval_role!r}")
        rc = cfg.roles[cfg_role]
        unknown = set(over) - {"tools", "max_reads", "max_ai_credits_per_session"}
        if unknown:
            raise ValueError(f"role_overrides.{eval_role}: unknown key(s) {', '.join(sorted(unknown))}")
        if "tools" in over:
            rc.tools = [str(t) for t in over["tools"]]
        if "max_reads" in over:
            rc.max_reads = None if over["max_reads"] is None else int(over["max_reads"])
        if "max_ai_credits_per_session" in over:
            cap = over["max_ai_credits_per_session"]
            rc.max_ai_credits_per_session = None if cap is None else float(cap)
    if knob_applies(settings, "retry_budget"):
        cfg.loop["retry_budget"] = int(settings.retry_budget)
    # eval runs keep absolute gates (comparable with every earlier batch; the hidden tests judge
    # the result anyway); a variant opts in to the baseline rule with extra.baseline: auto
    mode = extra.get("baseline", "off")
    cfg.loop["baseline"] = "off" if mode is False else str(mode or "off").lower()
    if cfg.loop["baseline"] not in ("auto", "off"):
        raise ValueError(f"extra.baseline must be auto or off, got {mode!r}")
    if settings.max_ai_credits is not None:
        for rt in cfg.runtimes.values():
            rt["max_ai_credits_per_run"] = float(settings.max_ai_credits)
            rt["max_ai_credits_per_session"] = min(float(rt.get("max_ai_credits_per_session", 10)),
                                                   float(settings.max_ai_credits))
    if knob_applies(settings, "custom_instructions"):
        cfg.copilot["custom_instructions"] = bool(settings.custom_instructions)
    if knob_applies(settings, "prompt_template"):
        cfg.raw["prompt_template"] = settings.prompt_template or "v1"
    if settings.run_dir:
        cfg.runs_dir = Path(settings.run_dir) / "orchestrator"
    return cfg


def workflow_transform(settings: RunSettings) -> Callable[[Workflow], Workflow]:
    # a role, or the gates, that the variant does not set stays as the workflow file has it (on)
    roles = {r: bool(on) for r, on in (settings.roles or {}).items() if knob_applies(settings, f"roles.{r}")}
    gates = bool(settings.gates) or not knob_applies(settings, "gates")

    def keep(step: StepDef) -> bool:
        if step.role:
            eval_role = CFG_TO_EVAL.get(step.role)
            if eval_role and not roles.get(eval_role, True):
                return False
        if step.kind == "gates" and not gates:
            return False
        return True

    def transform(wf: Workflow) -> Workflow:
        steps = []
        for s in wf.steps:
            if not keep(s):
                continue
            if s.kind == "agent_gated" and not gates:
                s = dataclasses.replace(s, kind="agent", gates=None)
            steps.append(s)
        ids = {s.id for s in steps}
        steps = [dataclasses.replace(s, on_reject=s.on_reject if s.on_reject in ids else None,
                                     on_fail=s.on_fail if s.on_fail in ids or s.kind != "gates"
                                     or s.on_fail in ("fail", "escalate", "continue") else "escalate")
                 for s in steps]
        return dataclasses.replace(wf, steps=steps)

    return transform


def find_ticket_file(ticket: str, settings: RunSettings) -> str:
    explicit = (settings.extra or {}).get("ticket_file")
    if explicit:
        return str(explicit)
    if Path(ticket).is_file():
        return ticket
    roots = []
    env = os.environ.get("HARNESS_HIDDEN_TESTS")
    if env:
        roots.append(Path(env))
    here = Path(__file__).resolve().parents[1]
    roots.append(here / "hidden-tests")                       # the workshop repository carries its referee
    roots += [p.parent / "hidden-tests" for p in (here, *here.parents) if p.parent != p]
    for root in roots:
        cand = root / "tickets" / ticket / "ticket.md"
        if cand.is_file():
            return str(cand)
    return ticket          # a Jira key: the ticket step fetches it through MCP


def _main_repo(package: HarnessConfig) -> Path:
    """The main checkout: where the package's own harness.yaml puts it (paths.repo next to it), not the eval
    worktree that ``settings.workdir`` points at and not the directory of an overlaid harness.yaml, which
    lies inside that worktree. ``package`` is the second value of ``load_run_config``."""
    return (package.base_dir / str(package.paths.get("repo", ".."))).resolve()


def default_runtime(cfg: HarnessConfig, settings: RunSettings) -> Any:
    kind = str((settings.extra or {}).get("runtime", "copilot"))
    if kind == "fake":
        from .fake import FakeRuntime
        return FakeRuntime()
    from .runtime import CopilotRuntime, env_passthrough, sdk_available
    if not sdk_available():
        raise RuntimeError("github-copilot-sdk is not installed (pip install --user github-copilot-sdk==1.0.23)")
    cli = envdetect.find_copilot_cli(str(cfg.copilot.get("cli_path", "") or ""))
    return CopilotRuntime(cli, str(cfg.repo), log_level=str(cfg.copilot.get("log_level", "warning")),
                          passthrough=env_passthrough(cfg))


# ------------------------------------------------------------------ the run ----
def make_backend(runtime_factory: RuntimeFactory | None = None,
                 gate_runner_factory: GateRunnerFactory | None = None,
                 config_path: str | None = None) -> Callable[[str, str, RunSettings, str], RunResult]:
    """A harness.api backend; the factories replace the Copilot runtime / the gate runner (tests)."""

    def backend(workflow: str, ticket: str, settings: RunSettings, mode: str = "eval") -> RunResult:
        return _run(workflow, ticket, settings, mode, runtime_factory or default_runtime,
                    gate_runner_factory, config_path)

    return backend


def run_workflow(workflow: str, ticket: str, settings: RunSettings, mode: str = "eval") -> RunResult:
    return _run(workflow, ticket, settings, mode, default_runtime, None, None)


def run_workflow_fake_runtime(workflow: str, ticket: str, settings: RunSettings, mode: str = "eval") -> RunResult:
    """The real orchestrator, real gates, real hidden tests -- but a scripted runtime: no model, no credits."""
    settings.extra = dict(settings.extra or {}, runtime="fake")
    return run_workflow(workflow, ticket, settings, mode)


def _run(workflow: str, ticket: str, settings: RunSettings, mode: str, runtime_factory: RuntimeFactory,
         gate_runner_factory: GateRunnerFactory | None, config_path: str | None) -> RunResult:
    from .cli import resolve_runtime_mode
    from .orchestrator import Orchestrator

    t0 = time.monotonic()
    run_dir = Path(settings.run_dir) if settings.run_dir else None
    if run_dir:
        run_dir.mkdir(parents=True, exist_ok=True)
    result = RunResult(run_dir=str(run_dir or ""))
    extra = settings.extra or {}
    cfg, package = load_run_config(config_path or extra.get("config"), settings.workdir)
    cfg = apply_settings(cfg, settings)
    env = envdetect.detect(cfg.repo, str(cfg.paths.get("bash", "") or ""))
    # the variant's extra.runtime_mode, else HARNESS_RUNTIME / harness.yaml, else (auto) the doctor
    # file of the main checkout -- the worktree has none and the runs folder is the results dir --
    # else the facilitator runtime: eval batches run under the facilitator's login and caps
    doctor_dir = _main_repo(package) / str(package.paths.get("runs", ".harness/runs"))
    runtime_mode = resolve_runtime_mode(cfg, extra.get("runtime_mode") or None, doctor_dir=doctor_dir,
                                        default="facilitator")
    autonomous = settings.autonomy == "auto" and knob_applies(settings, "autonomy")
    orch_mode = "autonomous" if autonomous else ("eval" if mode == "eval" else "interactive")
    log_path = (run_dir / "orchestrator.log") if run_dir else None

    def output(line: str) -> None:
        if log_path is None:
            return
        with open(log_path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")

    def no_input(prompt: str) -> str:
        raise RuntimeError("eval runs never wait for terminal input")

    orch = Orchestrator(cfg, runtime_factory(cfg, settings), env, mode=orch_mode, runtime_mode=runtime_mode,
                        gate_runner=gate_runner_factory(cfg, env) if gate_runner_factory else None,
                        input_fn=no_input, output=output, isatty=lambda: False,
                        workflow_transform=workflow_transform(settings))
    ticket_ref = find_ticket_file(ticket, settings)
    timed_out = False
    try:
        coro = orch.run(workflow=workflow, ticket_ref=ticket_ref)
        if settings.timeout_s:
            asyncio.run(asyncio.wait_for(coro, timeout=float(settings.timeout_s)))
        else:
            asyncio.run(coro)
    except (asyncio.TimeoutError, TimeoutError):
        timed_out = True
    except Exception as exc:  # noqa: BLE001 - reported in the result, the experiment goes on
        result.status, result.error = "error", f"{type(exc).__name__}: {exc}"
    state = getattr(orch, "state", None)
    result.duration_s = round(time.monotonic() - t0, 1)
    if state is None:
        if not result.error:
            result.status, result.error = "error", "the orchestrator did not start"
        return result
    result.run_id = state.run_id
    status, success = classify(state.data, timed_out, orch.workflow)
    if result.status != "error":
        result.status = status
    result.success = success and result.status == "completed"
    result.error = result.error or str(state.data.get("error") or "")
    _fill_from_state(result, state.data, orch.usage)
    events = translate_events(state.dir, state.data, result)
    if run_dir is not None:
        out = run_dir / "events.jsonl"
        with open(out, "w", encoding="utf-8", newline="\n") as fh:
            for ev in events:
                fh.write(json.dumps(ev, ensure_ascii=False) + "\n")
        result.audit_path = str(out)
    return result


def classify(data: dict[str, Any], timed_out: bool, workflow: Workflow) -> tuple[str, bool]:
    """Orchestrator state -> (api status, the workflow's own verdict)."""
    st = str(data.get("status"))
    reason = str(data.get("stop_reason") or "")
    if timed_out:
        return "timeout", False
    if data.get("error"):
        return "error", False
    if "credit cap" in reason:
        return "budget_exceeded", False
    if st in ("escalated", "aborted"):
        return "escalated", False
    if st != "done":
        return "failed", False
    ladder_ok = bool((data.get("last_ladder") or {}).get("ok", True))
    has_review = any(s.kind == "review" for s in workflow.steps)
    review_ok = (not has_review) or (data.get("review") or {}).get("verdict") == "approve"
    return "completed", ladder_ok and review_ok


def _fill_from_state(result: RunResult, data: dict[str, Any], usage: Any) -> None:
    c = data.get("counters") or {}
    result.approvals = int(c.get("approvals_counted", 0))
    result.escalations = int(c.get("escalations", 0))
    result.retries = int(c.get("retries", 0)) + int(c.get("review_rejects", 0))
    result.ai_credits = round(float(usage.credits), 4)
    tok = usage.tokens()
    result.tokens_in = int(tok.get("input_tokens", 0))
    result.tokens_out = int(tok.get("output_tokens", 0))
    result.tokens_cached = int(tok.get("cache_read_tokens", 0))
    result.model_calls = sum(int(s.get("calls", 0)) for s in usage.sessions.values())
    result.head_commit = str(data.get("commit") or "")
    review = data.get("review") or {}
    if review.get("score"):
        result.reviewer_score = float(review["score"])


# --------------------------------------------------------- audit translation ----
def _get(d: Any, *names: str, default: Any = None) -> Any:
    if not isinstance(d, dict):
        return default
    for n in names:
        if n in d and d[n] is not None:
            return d[n]
    return default


def _read_events(run_dir: Path) -> Iterable[dict[str, Any]]:
    path = run_dir / "events.jsonl"
    if not path.is_file():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            continue
    return out


def translate_events(run_dir: Path, data: dict[str, Any], result: RunResult) -> list[dict[str, Any]]:
    """The orchestrator's events.jsonl ("kind" records) -> the eval schema of harness/api.py."""
    out: list[dict[str, Any]] = []
    ladders = 0
    final_gates: dict[str, str] = {}

    def emit(etype: str, ts: str, **d: Any) -> None:
        out.append({"type": etype, "data": d, "timestamp": ts})

    for rec in _read_events(run_dir):
        kind = str(rec.get("kind", ""))
        ts = str(rec.get("ts") or _dt.datetime.now(_dt.timezone.utc).isoformat())
        d = rec.get("data") if isinstance(rec.get("data"), dict) else {}
        if kind == "sdk.assistant.usage":
            cu = _get(d, "copilot_usage", "copilotUsage", default={}) or {}
            nano = _get(cu, "total_nano_aiu", "totalNanoAiu")
            payload = {"model": _get(d, "model", default="unknown"), "role": rec.get("role"),
                       "inputTokens": int(_get(d, "input_tokens", "inputTokens", default=0) or 0),
                       "outputTokens": int(_get(d, "output_tokens", "outputTokens", default=0) or 0),
                       "cacheReadTokens": int(_get(d, "cache_read_tokens", "cacheReadTokens", default=0) or 0)}
            if nano is not None:
                payload["nanoAiu"] = float(nano)
            emit("assistant.usage", ts, **payload)
        elif kind == "sdk.session.usage_checkpoint":
            nano = _get(d, "total_nano_aiu", "totalNanoAiu")
            if nano is not None:
                emit("session.usage_checkpoint", ts, sessionId=str(rec.get("session") or "_"),
                     totalNanoAiu=float(nano))
        elif kind == "approval":
            if rec.get("by") == "eval-auto":
                emit("harness.approval", ts, step=rec.get("step"), decision="auto-pass")
            elif rec.get("by") == "human":
                emit("harness.approval", ts, step=rec.get("step"),
                     decision="approved" if rec.get("approved") else "rejected")
        elif kind == "escalation":
            emit("harness.escalation", ts, step=rec.get("step"), reason=rec.get("reason"))
        elif kind == "retry":
            emit("harness.retry", ts, step=rec.get("step"), attempt=rec.get("attempt"), reason=rec.get("reason"))
        elif kind == "review":
            emit("harness.review", ts, score=rec.get("score"), verdict=rec.get("verdict"))
        elif kind == "gates":
            ladders += 1
            try:
                ladder = json.loads((run_dir / str(rec.get("file"))).read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            for g in ladder.get("gates") or []:
                status = str(g.get("status", ""))
                if status not in ("PASS", "FAIL", "SKIP", "ERROR"):
                    continue          # NOT_RUN: the gate keeps its earlier result
                status = "FAIL" if status == "ERROR" else status
                final_gates[str(g.get("id"))] = status
                emit("harness.gate", ts, gate=g.get("id"), status=status, attempt=ladders,
                     violations=len(g.get("items") or []), step=rec.get("step"), trigger=rec.get("trigger"))
    result.gate_results = final_gates
    result.gate_violations = sum(1 for s in final_gates.values() if s == "FAIL")
    emit("harness.run_end", _dt.datetime.now(_dt.timezone.utc).isoformat(),
         status=result.status, success=result.success, orchestrator_status=data.get("status"),
         stopped_at=data.get("stopped_at"), reason=data.get("stop_reason"),
         credits=round(result.ai_credits, 4), nano_total=round(result.ai_credits * NANO_PER_CREDIT))
    return out
