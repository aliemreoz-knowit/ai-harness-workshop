"""Configuration: harness.yaml, gates.yaml and workflows/*.yaml as typed objects.

All three are plain YAML so the workshop can change behaviour without touching
code. ``load_config()`` validates cross references (roles used by steps,
gates used by profiles, jump targets) and applies environment overrides.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Mapping

from . import names, yamlmini

HARNESS_DIR = Path(__file__).resolve().parent
DEFAULT_CONFIG = HARNESS_DIR / "harness.yaml"

STEP_KINDS = ("ticket", "agent", "agent_gated", "gates", "review", "approval", "commit")
APPROVALS = ("human", "auto")
RUNTIMES = ("auto", "facilitator", "copilot-user")
GATE_KINDS = ("check", "python")
CUSTOM_TOOLS = ("run_gates", "read_ticket", "write_plan", "write_review")
TEST_CHANGE_RULES = ("bug", "always")
BASELINE_KINDS = ("tests", "coverage")
BASELINE_MODES = ("auto", "off")


class ConfigError(ValueError):
    """Invalid or inconsistent configuration."""


@dataclass
class RoleConfig:
    name: str
    model: str
    tools: list[str]
    writes: list[str] = field(default_factory=list)
    shell: list[str] = field(default_factory=list)
    escalation_model: str | None = None
    reasoning_effort: str | None = None
    # read budget: at most this many view / grep / glob calls per session (None = no limit)
    max_reads: int | None = None
    # per-role session cap (AI credits); the runtime's max_ai_credits_per_session still bounds it
    max_ai_credits_per_session: float | None = None

    @property
    def custom_tools(self) -> list[str]:
        return [t for t in self.tools if t in CUSTOM_TOOLS]

    @property
    def builtin_tools(self) -> list[str]:
        return [t for t in self.tools if t not in CUSTOM_TOOLS]


@dataclass
class GateDef:
    id: str
    fn: str
    name: str
    kind: str = "check"
    budget_s: float = 0
    timeout_s: float = 300
    parser: str = "none"
    cost: int = 1
    env: dict[str, str] = field(default_factory=dict)
    # a test-type gate judged against the untouched base (harness/baseline.py): tests | coverage
    baseline: str = ""
    # false: a FAIL fails the ladder but the gates after it still run (the agent gets all the feedback at once)
    stops: bool = True


@dataclass
class GatesConfig:
    script: str
    wrapper: str
    stop_on_fail: bool
    gates: dict[str, GateDef]
    profiles: dict[str, list[str]]
    # the script re-executes itself in the container when started on the host
    # (check.sh does, after it has read git on the host: a worktree's .git is a
    # host path the container cannot follow); False = start it through the wrapper
    self_wrapping: bool = False

    def resolve(self, spec: str | list[str]) -> list[GateDef]:
        """A profile name, a single gate id, or a list of gate ids -> gate definitions."""
        if isinstance(spec, str):
            ids = self.profiles.get(spec) or [spec]
        else:
            ids = list(spec)
        out = []
        for gid in ids:
            gid_norm = names.gate(gid)
            if gid_norm not in self.gates:
                raise ConfigError(f"unknown gate or profile: {gid}")
            out.append(self.gates[gid_norm])
        return out


@dataclass
class StepDef:
    id: str
    kind: str
    role: str | None = None
    prompt: str | None = None
    artifact: str | None = None
    gates: str | None = None
    approval: str = "auto"
    retry_budget: int | None = None
    on_exhausted: str = "escalate"
    on_reject: str | None = None
    max_rounds: int | None = None
    # gates: fail | escalate | continue | <earlier step id> (send the failures back there)
    on_fail: str = "fail"
    message: str | None = None
    # agent_gated: the step must add or change a file under tests/ -- "bug" (bug tickets
    # only) or "always"; checked by the orchestrator after the gates pass
    require_test_change: str | None = None


@dataclass
class Workflow:
    name: str
    description: str
    inputs: list[str]
    steps: list[StepDef]
    path: Path | None = None

    def index(self, step_id: str) -> int:
        for i, s in enumerate(self.steps):
            if s.id == step_id:
                return i
        raise ConfigError(f"workflow {self.name}: no step {step_id!r}")

    @property
    def needs_ticket(self) -> bool:
        return "ticket" in self.inputs


@dataclass
class HarnessConfig:
    path: Path
    base_dir: Path
    repo: Path
    runs_dir: Path
    workflows_dir: Path
    prompts_dir: Path
    runtime: str
    runtimes: dict[str, dict[str, Any]]
    copilot: dict[str, Any]
    roles: dict[str, RoleConfig]
    loop: dict[str, Any]
    policy: dict[str, Any]
    ticket: dict[str, Any]
    commit: dict[str, Any]
    paths: dict[str, Any]
    gates: GatesConfig
    raw: dict[str, Any]

    # -- helpers ----------------------------------------------------------------
    def caps(self, runtime: str, role: str | None = None) -> tuple[float, float]:
        """(max credits per session, max credits per run) for a runtime mode; with ``role``, the
        session cap is the smaller of the runtime's and the role's own max_ai_credits_per_session."""
        rt = self.runtimes.get(runtime) or self.runtimes.get("copilot-user") or {}
        session = float(rt.get("max_ai_credits_per_session", 10))
        rc = self.roles.get(role) if role else None
        if rc is not None and rc.max_ai_credits_per_session is not None:
            session = min(session, float(rc.max_ai_credits_per_session))
        return session, float(rt.get("max_ai_credits_per_run", 50))

    def role(self, name: str) -> RoleConfig:
        if name not in self.roles:
            raise ConfigError(f"unknown role {name!r} (known: {', '.join(sorted(self.roles))})")
        return self.roles[name]

    def workflow_names(self) -> list[str]:
        return sorted(p.stem for p in self.workflows_dir.glob("*.yaml"))

    def workflow(self, name: str) -> Workflow:
        path = self.workflows_dir / f"{name}.yaml"
        if not path.is_file():
            raise ConfigError(f"unknown workflow {name!r} (known: {', '.join(self.workflow_names())})")
        wf = parse_workflow(_load_yaml(path), path)
        validate_workflow(wf, self)
        return wf

    @property
    def check_log_dir(self) -> Path:
        return self.repo / str(self.paths.get("check_log", "build-check/harness"))


def _load_yaml(path: Path) -> Any:
    try:
        return yamlmini.load(path.read_text(encoding="utf-8"))
    except (OSError, ValueError) as exc:
        raise ConfigError(f"{path}: {exc}") from None


def _as_list(value: Any) -> list[Any]:
    if value is None:
        return []
    if isinstance(value, list):
        return value
    return [value]


def parse_gates(data: Mapping[str, Any]) -> GatesConfig:
    runner = data.get("runner") or {}
    gates: dict[str, GateDef] = {}
    for gid, g in (data.get("gates") or {}).items():
        if not isinstance(g, Mapping) or "fn" not in g:
            raise ConfigError(f"gates.yaml: gate {gid} needs at least 'fn'")
        kind = str(g.get("kind", "check"))
        if kind not in GATE_KINDS:
            raise ConfigError(f"gates.yaml: gate {gid}: kind must be one of {GATE_KINDS}")
        kind_bl = str(g.get("baseline") or "")
        if kind_bl and kind_bl not in BASELINE_KINDS:
            raise ConfigError(f"gates.yaml: gate {gid}: baseline must be one of {BASELINE_KINDS}")
        gates[names.gate(str(gid))] = GateDef(
            id=names.gate(str(gid)), fn=str(g["fn"]), name=str(g.get("name", gid)), kind=kind,
            budget_s=float(g.get("budget_s", 0)), timeout_s=float(g.get("timeout_s", 300)),
            parser=str(g.get("parser", "none")), cost=int(g.get("cost", 1)),
            env={str(k): str(v) for k, v in (g.get("env") or {}).items()}, baseline=kind_bl,
            stops=str(g.get("stops", True)).lower() not in ("false", "0", "no"))
    profiles: dict[str, list[str]] = {}
    for name, ids in (data.get("profiles") or {}).items():
        ids = [names.gate(str(i)) for i in _as_list(ids)]
        for gid in ids:
            if gid not in gates:
                raise ConfigError(f"gates.yaml: profile {name} uses unknown gate {gid}")
        profiles[str(name)] = ids
    return GatesConfig(script=str(runner.get("script", "scripts/check.sh")),
                       wrapper=str(runner.get("container_wrapper", "scripts/dockerbuild.sh")),
                       stop_on_fail=bool(runner.get("stop_on_fail", False)),
                       gates=gates, profiles=profiles,
                       self_wrapping=bool(runner.get("self_wrapping", False)))


def parse_workflow(data: Mapping[str, Any], path: Path | None = None) -> Workflow:
    if not isinstance(data, Mapping):
        raise ConfigError(f"{path}: a workflow must be a mapping")
    steps = []
    seen: set[str] = set()
    for raw in _as_list(data.get("steps")):
        if not isinstance(raw, Mapping) or "id" not in raw or "kind" not in raw:
            raise ConfigError(f"{path}: every step needs 'id' and 'kind'")
        sid = str(raw["id"])
        if sid in seen:
            raise ConfigError(f"{path}: duplicate step id {sid!r}")
        seen.add(sid)
        kind = str(raw["kind"])
        if kind not in STEP_KINDS:
            raise ConfigError(f"{path}: step {sid}: kind must be one of {STEP_KINDS}")
        approval = str(raw.get("approval", "human" if kind == "approval" else "auto"))
        if approval not in APPROVALS:
            raise ConfigError(f"{path}: step {sid}: approval must be human or auto")
        steps.append(StepDef(
            id=sid, kind=kind, role=raw.get("role"), prompt=raw.get("prompt"),
            artifact=raw.get("artifact"), gates=raw.get("gates"), approval=approval,
            retry_budget=raw.get("retry_budget"), on_exhausted=str(raw.get("on_exhausted", "escalate")),
            on_reject=raw.get("on_reject"), max_rounds=raw.get("max_rounds"),
            on_fail=str(raw.get("on_fail", "fail")), message=raw.get("message"),
            require_test_change=(str(raw["require_test_change"]) if raw.get("require_test_change") else None)))
    if not steps:
        raise ConfigError(f"{path}: workflow has no steps")
    return Workflow(name=str(data.get("name") or (path.stem if path else "workflow")),
                    description=str(data.get("description", "")),
                    inputs=[str(i) for i in _as_list(data.get("inputs"))], steps=steps, path=path)


def validate_workflow(wf: Workflow, cfg: HarnessConfig) -> None:
    ids = {s.id for s in wf.steps}
    for s in wf.steps:
        where = f"workflow {wf.name}, step {s.id}"
        if s.kind in ("agent", "agent_gated", "review"):
            if not s.role:
                raise ConfigError(f"{where}: needs a role")
            cfg.role(s.role)
            if not s.prompt:
                raise ConfigError(f"{where}: needs a prompt template")
            if not (cfg.prompts_dir / f"{s.prompt}.md").is_file():
                raise ConfigError(f"{where}: prompt template prompts/{s.prompt}.md not found")
        if s.kind in ("agent_gated", "gates"):
            if not s.gates:
                raise ConfigError(f"{where}: needs 'gates' (a profile or gate id)")
            cfg.gates.resolve(s.gates)
        if s.on_reject and s.on_reject not in ids:
            raise ConfigError(f"{where}: on_reject target {s.on_reject!r} is not a step")
        if s.on_reject and wf.index(s.on_reject) >= wf.index(s.id):
            raise ConfigError(f"{where}: on_reject must point to an earlier step")
        if s.on_exhausted not in ("escalate", "fail"):
            raise ConfigError(f"{where}: on_exhausted must be escalate or fail")
        if s.require_test_change and s.require_test_change not in TEST_CHANGE_RULES:
            raise ConfigError(f"{where}: require_test_change must be one of {TEST_CHANGE_RULES}")
        if s.require_test_change and s.kind != "agent_gated":
            raise ConfigError(f"{where}: require_test_change needs an agent_gated step")
        if s.on_fail not in ("fail", "escalate", "continue"):
            # a gates step may send its failures back to an earlier agent step (the
            # ladder before the review: the reviewer never sees a red ladder)
            if s.kind != "gates" or s.on_fail not in ids:
                raise ConfigError(f"{where}: on_fail must be fail, escalate, continue or "
                                  "(gates steps) an earlier step id")
            if wf.index(s.on_fail) >= wf.index(s.id):
                raise ConfigError(f"{where}: on_fail must point to an earlier step")


def load_config(path: str | Path | None = None, env: Mapping[str, str] | None = None,
                repo: str | Path | None = None) -> HarnessConfig:
    env = os.environ if env is None else env
    cfg_path = Path(path or env.get("HARNESS_CONFIG") or DEFAULT_CONFIG).resolve()
    data = _load_yaml(cfg_path)
    if not isinstance(data, Mapping):
        raise ConfigError(f"{cfg_path}: expected a mapping")
    base = cfg_path.parent
    paths = dict(data.get("paths") or {})
    repo_path = Path(repo).resolve() if repo else (base / str(paths.get("repo", ".."))).resolve()
    runtime = str(env.get("HARNESS_RUNTIME") or data.get("runtime", "auto"))
    if runtime not in RUNTIMES:
        raise ConfigError(f"runtime must be one of {RUNTIMES}, got {runtime!r}")

    roles: dict[str, RoleConfig] = {}
    for name, r in (data.get("roles") or {}).items():
        if not isinstance(r, Mapping) or "model" not in r:
            raise ConfigError(f"harness.yaml: role {name} needs a model")
        override = env.get("HARNESS_MODEL_" + str(name).upper().replace("-", "_"))
        roles[str(name)] = RoleConfig(
            name=str(name), model=str(override or r["model"]),
            tools=[str(t) for t in _as_list(r.get("tools"))],
            writes=[str(w) for w in _as_list(r.get("writes"))],
            shell=[str(s) for s in _as_list(r.get("shell"))],
            escalation_model=r.get("escalation_model"), reasoning_effort=r.get("reasoning_effort"),
            max_reads=(int(r["max_reads"]) if r.get("max_reads") is not None else None),
            max_ai_credits_per_session=(float(r["max_ai_credits_per_session"])
                                        if r.get("max_ai_credits_per_session") is not None else None))
    if not roles:
        raise ConfigError("harness.yaml: no roles defined")

    loop = dict(data.get("loop") or {})
    if env.get("HARNESS_RETRY_BUDGET"):
        loop["retry_budget"] = int(env["HARNESS_RETRY_BUDGET"])
    if env.get("HARNESS_BASELINE"):
        loop["baseline"] = env["HARNESS_BASELINE"]
    mode = loop.get("baseline")
    loop["baseline"] = "off" if mode is False else str(mode or "auto").lower()
    if loop["baseline"] not in BASELINE_MODES:
        raise ConfigError(f"harness.yaml: loop.baseline must be one of {BASELINE_MODES}, got {loop['baseline']!r}")
    runtimes = {str(k): dict(v or {}) for k, v in (data.get("runtimes") or {}).items()}
    if env.get("HARNESS_MAX_CREDITS_RUN"):
        for rt in runtimes.values():
            rt["max_ai_credits_per_run"] = float(env["HARNESS_MAX_CREDITS_RUN"])

    gates_path = base / str(paths.get("gates", "gates.yaml"))
    gates = parse_gates(_load_yaml(gates_path) or {})
    return HarnessConfig(
        path=cfg_path, base_dir=base, repo=repo_path,
        runs_dir=repo_path / str(paths.get("runs", ".harness/runs")),
        workflows_dir=base / str(paths.get("workflows", "workflows")),
        prompts_dir=base / str(paths.get("prompts", "prompts")),
        runtime=runtime, runtimes=runtimes, copilot=dict(data.get("copilot") or {}),
        roles=roles, loop=loop, policy=dict(data.get("policy") or {}),
        ticket=dict(data.get("ticket") or {}), commit=dict(data.get("commit") or {}),
        paths=paths, gates=gates, raw=dict(data))
