"""harness.api -> harness/backend.py -> the real orchestrator (fake session, fake check.sh).

The eval runner only knows harness.api.run_workflow. These tests run the real
orchestrator behind it and read the result back the way the runner does:
through the translated audit trail (harness.eval.audit).
"""

import shutil
import sys
import textwrap
from pathlib import Path

from harness import api, backend
from harness.config import load_config
from harness.envdetect import Environment
from harness.eval import audit as eval_audit
from harness.eval import runner as eval_runner
from harness.eval import variants as eval_variants
from harness.fake import FakeRuntime
from harness.gates import GateRunner

from .conftest import FakeCheck, git
from .test_orchestrator import happy_scripts, unit_gate

CONTAINER = Environment(in_container=True, system="Linux", bash="bash", docker=None, devcontainer=True)


def make_backend(scripts=None, runtimes: list | None = None):
    def runtime_factory(cfg, settings):
        rt = FakeRuntime(scripts if scripts is not None else happy_scripts())
        if runtimes is not None:
            runtimes.append(rt)
        return rt

    return backend.make_backend(runtime_factory=runtime_factory,
                                gate_runner_factory=lambda cfg, env: GateRunner(cfg, CONTAINER,
                                                                                runner=FakeCheck(cfg, unit_gate)))


def settings_for(repo: Path, run_dir: Path, ticket_file: Path, **kw) -> api.RunSettings:
    s = api.RunSettings(workdir=str(repo), run_dir=str(run_dir), **kw)
    s.extra["ticket_file"] = str(ticket_file)
    return s


def test_orchestrator_is_the_discovered_backend(monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_BACKEND", raising=False)
    fn = api._discover()
    assert fn.__module__ == "harness.orchestrator" and fn.__name__ == "run_workflow"


def test_full_team_run_through_the_api(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    run_dir = tmp_path / "run"
    runtimes: list = []
    s = settings_for(repo, run_dir, ticket_file, models={"implementer": "claude-haiku-5.5"})
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(runtimes=runtimes))
    assert res.status == "completed" and res.success, res.error
    assert res.approvals == 2 and res.escalations == 0 and res.retries == 2   # 1 gate retry + 1 review reject
    assert res.ai_credits > 0 and res.model_calls > 0 and res.reviewer_score == 4.0
    assert res.head_commit and git(repo, "log", "-1", "--format=%s").strip() == "DEMO-7: Viewer shows wrong numbers"
    # the orchestrator's own run folder lives inside the eval run folder
    (orch_run,) = list((run_dir / "orchestrator").iterdir())
    assert (orch_run / "summary.json").is_file() and (run_dir / "orchestrator.log").is_file()
    # the runner reads the translated trail, not the RunResult
    m = eval_audit.parse_audit(Path(res.audit_path))
    assert m is not None and m.bad_lines == 0
    assert m.approvals == 2 and m.escalations == 0 and m.retries == 2 and m.reviewer_score == 4.0
    assert abs((m.ai_credits or 0) - res.ai_credits) < 1e-6
    assert m.gate_results["unit-tests"] == "PASS" and m.gate_violations == 0 and m.gate_failures_total >= 1
    assert m.end_status == "completed"
    # the default model escalation stays on (implementer model = harness default)
    assert runtimes[0].model_switches


def _summary(run_dir: Path) -> dict:
    import json
    (orch_run,) = list((run_dir / "orchestrator").iterdir())
    return json.loads((orch_run / "summary.json").read_text(encoding="utf-8"))


def test_eval_runs_use_the_facilitator_caps_unless_a_variant_says_otherwise(
        repo: Path, ticket_file: Path, tmp_path: Path, monkeypatch) -> None:
    import json
    from harness import cli
    only_impl = {"planner": False, "implementer": True, "test_writer": False, "reviewer": False}
    main = tmp_path / "main"                       # the "main checkout" of this test: no doctor file yet
    monkeypatch.setattr(backend, "_main_repo", lambda cfg: main)
    # default: the facilitator runtime (an eval batch runs under the facilitator's login)
    s = settings_for(repo, tmp_path / "r1", ticket_file, roles=only_impl)
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend())
    assert res.status == "completed", res.error
    summary = _summary(tmp_path / "r1")
    assert summary["runtime"] == "facilitator" and summary["caps"] == [40.0, 200.0]
    orch_log = (tmp_path / "r1" / "orchestrator.log").read_text(encoding="utf-8")
    assert "runtime facilitator: caps 40 AI credits per session, 200 per run" in orch_log
    # the variant pins the mode
    s = settings_for(repo, tmp_path / "r2", ticket_file, roles=only_impl)
    s.extra["runtime_mode"] = "copilot-user"
    api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend())
    assert _summary(tmp_path / "r2")["caps"] == [15.0, 60.0]
    # auto: the doctor file of the main checkout (never the results dir), else facilitator
    cfg = load_config(repo=repo, env={})
    doctor = cli._doctor_file(cfg, main / ".harness" / "runs")
    doctor.parent.mkdir(parents=True)
    doctor.write_text(json.dumps({"runtime_mode": "copilot-user"}), encoding="utf-8")
    s = settings_for(repo, tmp_path / "r3", ticket_file, roles=only_impl)
    s.extra["runtime_mode"] = "auto"
    api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend())
    assert _summary(tmp_path / "r3")["runtime"] == "copilot-user"
    doctor.unlink()
    s = settings_for(repo, tmp_path / "r4", ticket_file, roles=only_impl)
    s.extra["runtime_mode"] = "auto"
    api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend())
    assert _summary(tmp_path / "r4")["runtime"] == "facilitator"


def test_every_shipped_variant_runs_under_the_facilitator_caps() -> None:
    matrix = eval_variants.load_matrix(eval_variants.DEFAULT_FILE)
    for name, v in matrix.variants.items():
        assert v.settings["extra"].get("runtime_mode") == "facilitator", name
    assert matrix.variants["team-small"].settings["extra"]["escalation"] is False     # merged, not replaced


def test_variant_knobs_reach_the_orchestrator(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    runtimes: list = []
    s = settings_for(repo, tmp_path / "run", ticket_file, autonomy="auto", custom_instructions=False,
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False},
                     models={"implementer": "claude-opus-x"}, retry_budget=1, max_ai_credits=7.0)
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(runtimes=runtimes))
    assert res.status == "completed" and res.success, res.error
    rt = runtimes[0]
    assert {sess.role for sess in rt.sessions} == {"implementer"}           # planner/test-writer/reviewer off
    (sess,) = rt.sessions
    assert sess.spec.model == "claude-opus-x" and sess.spec.skip_custom_instructions
    assert sess.spec.max_ai_credits <= 7.0
    assert rt.model_switches == []          # a variant model is never escalated to another one
    assert res.approvals == 0               # autonomy auto: nothing counted
    m = eval_audit.parse_audit(Path(res.audit_path))
    assert m is not None and m.approvals == 0 and m.reviews == 0


def test_gates_off_and_escalation(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    # the implementer never fixes anything: with gates the retry budget runs out -> escalated
    scripts = {"implementer": []}
    s = settings_for(repo, tmp_path / "a", ticket_file, retry_budget=1,
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False})
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(scripts))
    assert res.status == "escalated" and not res.success and res.escalations == 1
    m = eval_audit.parse_audit(Path(res.audit_path))
    assert m is not None and m.escalations == 1 and m.gate_violations and m.end_status == "escalated"
    # without gates nothing checks the change: the run "completes" but commits nothing
    s2 = settings_for(repo, tmp_path / "b", ticket_file, gates=False,
                      roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False})
    res2 = api.run_workflow("jira-to-pr", "DEMO-7", s2, "eval", backend=make_backend(scripts))
    assert res2.status == "failed" and not res2.success and "nothing to commit" in _reason(res2)


def _reason(res: api.RunResult) -> str:
    import json
    lines = Path(res.audit_path).read_text(encoding="utf-8").splitlines()
    return str(json.loads(lines[-1])["data"].get("reason"))


def test_prompt_template_v2_overlays(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    runtimes: list = []
    s = settings_for(repo, tmp_path / "run", ticket_file, prompt_template="v2",
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False})
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(runtimes=runtimes))
    assert res.status == "completed", res.error
    first = [p for r, p in runtimes[0].prompts if r == "implementer"][0]
    v2 = (Path(backend.__file__).parent / "prompts" / "v2" / "implement.md").read_text(encoding="utf-8")
    assert v2.splitlines()[0].split("$")[0].strip() in first


def test_eval_runner_drives_the_real_orchestrator(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    """The whole block-4 pipeline: worktree from the ticket branch -> api -> orchestrator -> hidden tests."""
    git(repo, "branch", "ticket/DEMO-7")
    hidden = tmp_path / "hidden-tests"
    tdir = hidden / "tickets" / "DEMO-7"
    tdir.mkdir(parents=True)
    (tdir / "meta.yaml").write_text("id: DEMO-7\neval: true\nseed_branch: ticket/DEMO-7\n"
                                    "allowed_paths:\n  - src/**\n  - tests/**\n", encoding="utf-8")
    (tdir / "ticket.md").write_text(ticket_file.read_text(encoding="utf-8"), encoding="utf-8")
    check = tmp_path / "hidden_check.py"
    check.write_text(textwrap.dedent("""\
        import sys, pathlib
        text = (pathlib.Path(sys.argv[1]) / "src" / "proto" / "proto.c").read_text()
        ok = "return x;" in text and "broken" not in text
        print(("PASS" if ok else "FAIL") + "  " + sys.argv[2] + "   test_hidden   0.01 sec")
        sys.exit(0 if ok else 1)
        """), encoding="utf-8")
    vfile = tmp_path / "variants.yaml"
    vfile.write_text(textwrap.dedent("""\
        version: 1
        workflow: jira-to-pr
        defaults:
          roles: {implementer: true}
          models: {planner: claude-sonnet-5.5, implementer: claude-haiku-5.5, test_writer: claude-haiku-5.5,
                   reviewer: claude-sonnet-5.5}
        variants:
          gates: {label: single}
          team: {label: team, roles: {planner: true, test_writer: true, reviewer: true}}
        """), encoding="utf-8")
    m = eval_variants.load_matrix(vfile)
    cfg = eval_runner.EvalConfig(
        repo=repo, hidden_dir=hidden, variants=m.select(None), tickets=["DEMO-7"], experiment="e2e",
        results_dir=tmp_path / "results", worktree_root=tmp_path / "wt", parallel=1, golden_ref=None,
        hidden_cmd=[sys.executable, str(check), "{checkout}", "{ticket}"], backend=make_backend(),
        log=lambda s: None)
    recs = {r["variant"]: r for r in eval_runner.Runner(cfg).run()}
    for v in ("gates", "team"):
        rec = recs[v]
        assert rec["run_status"] == "done", rec.get("error")
        assert rec["success"] is True and rec["workflow_success"] is True, rec
        assert rec["path_violations"] == [] and rec["commits"] >= 1
        assert rec["ai_credits"] > 0 and rec["settings"]["extra"]["ticket_file"].endswith("ticket.md")
    assert recs["team"]["approvals"] == 2 and recs["gates"]["approvals"] == 1      # plan + merge vs merge only
    assert recs["team"]["reviewer_score"] == 4.0 and recs["gates"]["reviewer_score"] is None
    # the ticket text never lands in the worktree
    for rec in recs.values():
        assert not list(Path(rec["worktree"]).rglob("ticket.md"))


def test_overlaid_harness_config_is_what_the_run_loads(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    """overlay_from: harness.yaml, gates.yaml, workflows/ and prompts/ come from the eval worktree (the code
    stays this package's); a variant without overlay_from keeps loading the package's own."""
    pkg = Path(backend.__file__).parent
    git(repo, "branch", "ticket/DEMO-7")
    hidden = tmp_path / "hidden-tests"
    tdir = hidden / "tickets" / "DEMO-7"
    tdir.mkdir(parents=True)
    (tdir / "meta.yaml").write_text("id: DEMO-7\neval: true\nseed_branch: ticket/DEMO-7\n"
                                    "allowed_paths:\n  - src/**\n  - tests/**\n", encoding="utf-8")
    (tdir / "ticket.md").write_text(ticket_file.read_text(encoding="utf-8"), encoding="utf-8")
    unit = tmp_path / "unit-1"                       # a unit's worktree: its own copy of the harness structure
    (unit / "harness").mkdir(parents=True)
    for name in ("harness.yaml", "gates.yaml"):
        shutil.copyfile(pkg / name, unit / "harness" / name)
    shutil.copytree(pkg / "workflows", unit / "harness" / "workflows")
    shutil.copytree(pkg / "prompts", unit / "harness" / "prompts")
    implement = unit / "harness" / "prompts" / "implement.md"
    implement.write_text(implement.read_text(encoding="utf-8") + "\nOVERLAY-MARKER: the unit's own rule.\n",
                         encoding="utf-8")
    vfile = tmp_path / "variants.yaml"
    vfile.write_text(textwrap.dedent(f"""\
        version: 1
        defaults:
          roles: {{implementer: true}}
          models: {{implementer: claude-haiku-5.5}}
        variants:
          P: {{label: package config}}
          O: {{label: unit config, overlay_from: "{unit.as_posix()}"}}
        """), encoding="utf-8")
    runs: dict = {}

    def runtime_factory(cfg, settings):
        rt = FakeRuntime(happy_scripts())
        runs[settings.variant] = (cfg, rt)
        return rt

    backend_fn = backend.make_backend(
        runtime_factory=runtime_factory,
        gate_runner_factory=lambda cfg, env: GateRunner(cfg, CONTAINER, runner=FakeCheck(cfg, unit_gate)))
    m = eval_variants.load_matrix(vfile)
    cfg = eval_runner.EvalConfig(
        repo=repo, hidden_dir=hidden, variants=m.select(None), tickets=["DEMO-7"], experiment="ov",
        results_dir=tmp_path / "results", worktree_root=tmp_path / "wt", parallel=1, golden_ref=None,
        skip_hidden=True, backend=backend_fn, log=lambda s: None)
    recs = {r["variant"]: r for r in eval_runner.Runner(cfg).run()}
    for v in ("P", "O"):
        assert recs[v]["run_status"] == "done" and recs[v]["status"] == "completed", recs[v].get("error")
    pkg_cfg, pkg_rt = runs["P"]
    unit_cfg, unit_rt = runs["O"]
    assert pkg_cfg.base_dir == pkg.resolve()
    assert unit_cfg.base_dir == (Path(recs["O"]["worktree"]) / "harness").resolve()
    assert unit_cfg.prompts_dir.is_relative_to(Path(recs["O"]["worktree"]).resolve())
    assert unit_cfg.workflows_dir.is_relative_to(Path(recs["O"]["worktree"]).resolve())
    first = {v: [p for r, p in rt.prompts if r == "implementer"][0] for v, (c, rt) in runs.items()}
    assert "OVERLAY-MARKER" in first["O"] and "OVERLAY-MARKER" not in first["P"]
    # the run started on a clean tree: the overlay is not the agent's diff
    assert recs["O"]["workflow_committed"] and recs["O"]["path_violations"] == []
    assert not [f for f in recs["O"]["changed_files"] if f.startswith("harness/")]


PKG = Path(backend.__file__).parent
ONLY_IMPLEMENTER = {"planner": False, "implementer": True, "test_writer": False, "reviewer": False}


def make_unit(tmp_path: Path, *edits: tuple[str, str], name: str = "unit") -> Path:
    """A workshop unit's harness/ (a copy of the package's files, harness.yaml with the text edits
    applied); returns its harness.yaml."""
    unit = tmp_path / name / "harness"
    unit.mkdir(parents=True)
    text = (PKG / "harness.yaml").read_text(encoding="utf-8")
    for old, new in edits:
        assert text.count(old) == 1, old                  # an edit that matches nothing would test nothing
        text = text.replace(old, new)
    (unit / "harness.yaml").write_text(text, encoding="utf-8", newline="\n")
    shutil.copyfile(PKG / "gates.yaml", unit / "gates.yaml")
    shutil.copytree(PKG / "workflows", unit / "workflows")
    shutil.copytree(PKG / "prompts", unit / "prompts")
    return unit / "harness.yaml"


def run_overlaid(repo: Path, ticket_file: Path, tmp_path: Path, config_file: Path, **extra) -> dict:
    """An implementer-only run of the real backend on ``extra.config`` = config_file (what an overlay
    variant sets); returns the config, the environment and the runtime the run was given."""
    seen: dict = {}

    def runtime_factory(cfg, settings):
        seen["cfg"], seen["rt"] = cfg, FakeRuntime(happy_scripts())
        return seen["rt"]

    def gate_runner_factory(cfg, env):
        seen["env"] = env
        return GateRunner(cfg, CONTAINER, runner=FakeCheck(cfg, unit_gate))

    s = settings_for(repo, tmp_path / "run", ticket_file, roles=ONLY_IMPLEMENTER)
    s.extra.update(config=str(config_file), **extra)
    seen["res"] = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=backend.make_backend(
        runtime_factory=runtime_factory, gate_runner_factory=gate_runner_factory))
    assert seen["res"].status == "completed", seen["res"].error
    return seen


def test_overlaid_config_never_supplies_the_host_side_keys(repo: Path, ticket_file: Path, tmp_path: Path,
                                                           monkeypatch) -> None:
    """A unit's harness.yaml sets copilot (cli_path, env_passthrough, versions, model_cost), paths.bash,
    runtime and runtimes: the run uses the package's values for all of them."""
    monkeypatch.setattr(backend, "_main_repo", lambda package: tmp_path / "main")      # no doctor file there
    unit_bash = tmp_path / "unit-bash"
    unit_bash.write_text("#!/bin/sh\n", encoding="utf-8")
    unit_cli = tmp_path / "unit-copilot"
    unit_cli.write_text("", encoding="utf-8")
    config = make_unit(
        tmp_path,
        ('  cli_path: ""', f'  cli_path: "{unit_cli.as_posix()}"'),
        ("  expected_cli_version: 1.0.100", "  expected_cli_version: 9.9.9"),
        ("  env_passthrough: []", "  env_passthrough: [UNIT_SECRET*]"),
        ("model_cost: {claude-haiku-5.5: 1, claude-sonnet-5.5: 20}", "model_cost: {claude-haiku-5.5: 1}"),
        ('  bash: ""', f'  bash: "{unit_bash.as_posix()}"'),
        ("\nruntime: auto\n", "\nruntime: copilot-user\n"),
        ("    max_ai_credits_per_session: 40", "    max_ai_credits_per_session: 3"),
        ("    max_ai_credits_per_run: 200", "    max_ai_credits_per_run: 9"))
    pkg = load_config(repo=repo, env={})
    assert pkg.copilot["cli_path"] == "" and pkg.paths["bash"] == "" and pkg.runtime == "auto"

    seen = run_overlaid(repo, ticket_file, tmp_path, config)

    cfg = seen["cfg"]
    assert cfg.base_dir == config.parent.resolve()                      # the overlay is what was loaded
    assert {k: v for k, v in cfg.copilot.items() if k != "custom_instructions"} == pkg.copilot
    assert cfg.paths["bash"] == "" and seen["env"].bash != str(unit_bash)
    assert cfg.runtime == "auto" and cfg.runtimes == pkg.runtimes
    summary = _summary(tmp_path / "run")
    assert summary["runtime"] == "facilitator" and summary["caps"] == [40.0, 200.0]


def test_overlaid_role_tools_still_take_effect(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    implementer = "    escalation_model: claude-sonnet-5.5\n"          # the tools line below it is the implementer's
    package_tools = "    tools: [view, grep, glob, edit, create, bash, read_ticket, run_gates]"
    config = make_unit(tmp_path, (implementer + package_tools,
                                  implementer + "    tools: [view, edit, create, read_ticket]"))

    seen = run_overlaid(repo, ticket_file, tmp_path, config, runtime_mode="facilitator")

    assert seen["cfg"].role("implementer").tools == ["view", "edit", "create", "read_ticket"]
    (impl,) = seen["rt"].sessions_for("implementer")
    assert impl.spec.role.tools == ["view", "edit", "create", "read_ticket"]
    assert load_config(repo=repo, env={}).role("implementer").tools[-1] == "run_gates"     # the package's is unchanged


def test_pinning_the_host_settings_copies_them(repo: Path, tmp_path: Path) -> None:
    config = make_unit(tmp_path, ('  bash: ""', '  bash: "/unit/bash"'))
    cfg, package = backend.load_run_config(str(config), repo)
    assert package.path == (PKG / "harness.yaml").resolve() and cfg.path == config.resolve()
    assert cfg.paths["bash"] == "" and cfg.copilot == package.copilot and cfg.runtimes == package.runtimes
    cfg.copilot["custom_instructions"] = False                       # what apply_settings does to a run's config
    cfg.runtimes["facilitator"]["max_ai_credits_per_run"] = 1.0
    assert "custom_instructions" not in package.copilot
    assert package.runtimes["facilitator"]["max_ai_credits_per_run"] == 200
    own, own_package = backend.load_run_config(str(PKG / "harness.yaml"), repo)    # the package's own: nothing to pin
    assert own is own_package


def test_ticket_source_and_check_log_are_pinned_too(repo: Path, tmp_path: Path) -> None:
    """ticket.jira (the MCP server the ticket step talks to) and paths.check_log (TS_CHECK_LOG of the gate
    runs) are host-side as well; the rest of ticket and paths is the unit's."""
    config = make_unit(tmp_path,
                       ("  max_chars: 12000", "  max_chars: 500"),
                       ("    mcp_server: atlassian", "    mcp_server: unit-mcp"),
                       ("    url: https://mcp.atlassian.com/v1/mcp", "    url: https://unit.example/mcp"),
                       ("  check_log: build-check/harness", "  check_log: unit-logs"),
                       ("  runs: .harness/runs", "  runs: unit-runs"))
    cfg, package = backend.load_run_config(str(config), repo)
    assert package.ticket["jira"]["mcp_server"] == "atlassian"
    assert cfg.ticket["jira"] == package.ticket["jira"]
    assert cfg.paths["check_log"] == "build-check/harness" and cfg.check_log_dir == repo.resolve() / "build-check/harness"
    assert cfg.ticket["max_chars"] == 500 and cfg.paths["runs"] == "unit-runs"      # the unit's own
    cfg.ticket["jira"]["tool"] = "changed"                                          # a copy, not the package's dict
    assert package.ticket["jira"]["tool"] == "getJiraIssue"


def test_a_host_side_key_the_package_lacks_is_dropped_from_the_unit_config(repo: Path, tmp_path: Path,
                                                                           monkeypatch) -> None:
    """paths.bash, paths.check_log and ticket.jira: absent in the package's file, the unit's value must not
    survive (the loader's own default is the package's behaviour)."""
    bare = make_unit(tmp_path, ('  bash: ""', "  # no bash"), ("  check_log: build-check/harness", "  # no check_log"),
                     ("  jira:\n", "  not_jira:\n"), name="bare-package")
    monkeypatch.setattr(backend, "DEFAULT_CONFIG", bare.resolve())
    package = load_config(bare, repo=repo, env={})
    assert not {"bash", "check_log"} & set(package.paths) and "jira" not in package.ticket
    config = make_unit(tmp_path, ('  bash: ""', '  bash: "/unit/bash"'),
                       ("  check_log: build-check/harness", "  check_log: unit-logs"))
    unit = load_config(config, repo=repo, env={})
    assert unit.paths["bash"] == "/unit/bash" and unit.paths["check_log"] == "unit-logs" and "jira" in unit.ticket

    cfg, pinned = backend.load_run_config(str(config), repo)

    assert pinned.path == bare.resolve() and cfg.path == config.resolve()
    assert "bash" not in cfg.paths and "check_log" not in cfg.paths and "jira" not in cfg.ticket
    assert cfg.ticket["max_chars"] == 12000 and cfg.paths["runs"] == ".harness/runs"      # the rest stays the unit's
    assert cfg.check_log_dir == repo.resolve() / "build-check/harness"                    # the loader's default


def test_harness_config_from_the_environment_is_a_unit_config_too(repo: Path, tmp_path: Path, monkeypatch) -> None:
    """No path given: HARNESS_CONFIG names the file, and a file other than the package's gets its host-side
    keys pinned all the same."""
    config = make_unit(tmp_path,
                       ("  expected_cli_version: 1.0.100", "  expected_cli_version: 9.9.9"),
                       ('  bash: ""', '  bash: "/unit/bash"'),
                       ("  check_log: build-check/harness", "  check_log: unit-logs"),
                       ("    mcp_server: atlassian", "    mcp_server: unit-mcp"),
                       ("    max_ai_credits_per_run: 200", "    max_ai_credits_per_run: 9"),
                       ("  retry_budget: 3 ", "  retry_budget: 1 "))
    monkeypatch.setenv("HARNESS_CONFIG", str(config))

    cfg, package = backend.load_run_config(None, repo)

    assert cfg.path == config.resolve() and package.path == (PKG / "harness.yaml").resolve()
    assert cfg.loop["retry_budget"] == 1                                       # the unit's file was loaded ...
    assert cfg.copilot == package.copilot and cfg.copilot["expected_cli_version"] == "1.0.100"
    assert cfg.paths["bash"] == "" and cfg.paths["check_log"] == "build-check/harness"
    assert cfg.ticket["jira"]["mcp_server"] == "atlassian"
    assert cfg.runtimes["facilitator"]["max_ai_credits_per_run"] == 200        # ... and its host keys were not used
    monkeypatch.setenv("HARNESS_CONFIG", str(PKG / "harness.yaml"))
    own, own_package = backend.load_run_config(None, repo)                     # the package's own file: nothing to pin
    assert own is own_package


def test_main_repo_of_an_overlay_variant_is_the_package_checkout(repo: Path, tmp_path: Path) -> None:
    config = make_unit(tmp_path)
    cfg, package = backend.load_run_config(str(config), repo)
    assert backend._main_repo(package) == PKG.resolve().parent          # not tmp/unit, where harness/.. leads
    assert (cfg.base_dir / str(cfg.paths["repo"])).resolve() == config.parent.parent.resolve()


def test_runtime_mode_auto_reads_the_main_checkouts_doctor_file_for_an_overlay_variant(
        repo: Path, ticket_file: Path, tmp_path: Path, monkeypatch) -> None:
    import json
    from harness import cli
    main = tmp_path / "main"                       # this test's package checkout: harness/ with the shipped files
    (main / "harness").mkdir(parents=True)
    for name in ("harness.yaml", "gates.yaml"):
        shutil.copyfile(PKG / name, main / "harness" / name)
    monkeypatch.setattr(backend, "DEFAULT_CONFIG", (main / "harness" / "harness.yaml").resolve())
    doctor = cli._doctor_file(load_config(repo=repo, env={}), main / ".harness" / "runs")
    doctor.parent.mkdir(parents=True)
    doctor.write_text(json.dumps({"runtime_mode": "copilot-user"}), encoding="utf-8")
    config = make_unit(tmp_path)                   # tmp/unit/harness/harness.yaml: its checkout would be tmp/unit

    run_overlaid(repo, ticket_file, tmp_path, config, runtime_mode="auto")

    assert _summary(tmp_path / "run")["runtime"] == "copilot-user"


def test_config_not_shared_between_runs(repo: Path) -> None:
    a = backend.apply_settings(load_config(repo=repo, env={}), api.RunSettings(models={"implementer": "m-a"}))
    b = load_config(repo=repo, env={})
    assert a.role("implementer").model == "m-a" and b.role("implementer").model != "m-a"


def test_v11_cheap_harness(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    """cheap: all Haiku and no escalation, a context pack and a read budget for the planner,
    a diff-only reviewer (no file tools)."""
    from harness.fake import Turn, call, edit

    (repo / "PROTOCOL.md").write_text("# protocol\n\n## 1. Constants\n\nx\n\n### 1.1 Sub\n\ny\n", encoding="utf-8")
    (repo / "docs/ARCHITECTURE.md").write_text("# a\n\n## 1. Module map\n\n```\nsrc/proto/  the parser\n```\n",
                                               encoding="utf-8")
    git(repo, "commit", "-qam", "docs")
    v11 = eval_variants.load_matrix(eval_variants.DEFAULT_FILE).variants["cheap"]
    s = v11.run_settings(str(repo), str(tmp_path / "run"))
    s.extra["ticket_file"] = str(ticket_file)
    views = [call("view", path=p) for p in ("src/proto/proto.c", "include/ts_proto.h", "PROTOCOL.md",
                                            "docs/ARCHITECTURE.md", "tests/unit/test_proto.c")]
    scripts = happy_scripts()
    scripts["planner"] = [Turn("planned", [*views, call("grep", pattern="parse"),
                                           call("write_plan", plan="1. fix ts_parse")])]
    scripts["reviewer"] = [Turn("looked", [call("view", path="src/proto/proto.c"),
                                           call("write_review", verdict="approve", score=4, summary="ok")])]
    scripts["test-writer"] = [Turn("tests", [edit("tests/unit/test_parse_more.c",
                                                  "/* SPDX-License-Identifier: MIT */\n/*\n * tinystream v2 -- "
                                                  "tests/unit/test_parse_more.c\n * More parser cases.\n */\n"
                                                  "void t(void) {}\n")])]
    runtimes: list = []
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(scripts, runtimes=runtimes))
    assert res.status == "completed" and res.success, res.error
    rt = runtimes[0]
    # every session on Haiku, and the failed implementer attempt did not switch to Sonnet
    assert {sess.spec.model for sess in rt.sessions} == {"claude-haiku-5.5"}
    assert rt.model_switches == []
    # planner: the context pack instead of a tree read, 4 reads, then refused; no grep at all
    (plan_prompt,) = [p for r, p in rt.prompts if r == "planner"]
    assert "## 1. Constants" in plan_prompt and "### 1.1 Sub" in plan_prompt
    assert "src/proto/  the parser" in plan_prompt and "AGENTS.md: loaded as your custom instructions" in plan_prompt
    planner_log = [e for e in rt.tool_log if e["role"] == "planner"]
    assert [e["ok"] for e in planner_log if e["tool"] == "view"] == [True, True, True, True, False]
    assert "read budget" in [e for e in planner_log if e["tool"] == "view"][-1]["result"]
    assert not [e for e in planner_log if e["tool"] == "grep"][0]["ok"]          # not offered
    (planner,) = rt.sessions_for("planner")
    assert planner.spec.role.tools == ["view", "read_ticket", "write_plan"] and planner.spec.role.max_reads == 4
    assert "at most 4 files" in planner.spec.agent_prompt
    # reviewer: the diff in the prompt, no file tools
    (reviewer,) = rt.sessions_for("reviewer")
    assert reviewer.spec.role.tools == ["write_review"]
    review_log = [e for e in rt.tool_log if e["role"] == "reviewer"]
    assert [e["ok"] for e in review_log] == [False, True]
    (review_prompt,) = [p for r, p in rt.prompts if r == "reviewer"]
    assert "this is all you get" in review_prompt and "+    return x;" in review_prompt


def test_role_overrides_are_checked(repo: Path) -> None:
    import pytest
    cfg = load_config(repo=repo, env={})
    s = api.RunSettings(workdir=str(repo), extra={"role_overrides": {"auditor": {"tools": []}}})
    with pytest.raises(ValueError, match="unknown role"):
        backend.apply_settings(cfg, s)
    s = api.RunSettings(workdir=str(repo), extra={"role_overrides": {"planner": {"model": "x"}}})
    with pytest.raises(ValueError, match="unknown key"):
        backend.apply_settings(load_config(repo=repo, env={}), s)


def test_role_session_caps_from_role_overrides(repo: Path) -> None:
    cfg = load_config(repo=repo, env={})
    s = api.RunSettings(workdir=str(repo), max_ai_credits=12,
                        extra={"role_overrides": {"planner": {"max_ai_credits_per_session": 2},
                                                  "test_writer": {"max_ai_credits_per_session": 3},
                                                  "implementer": {"max_ai_credits_per_session": 60}}})
    backend.apply_settings(cfg, s)
    assert cfg.roles["planner"].max_ai_credits_per_session == 2
    assert cfg.roles["test-writer"].max_ai_credits_per_session == 3
    for mode in ("copilot-user", "facilitator"):
        assert cfg.caps(mode, "planner") == (2, 12)
        assert cfg.caps(mode, "test-writer") == (3, 12)
        # a role cap above the runtime's session cap does not raise it
        assert cfg.caps(mode, "implementer") == cfg.caps(mode)
        assert cfg.caps(mode, "reviewer") == cfg.caps(mode)


# ------------------------------------------------- knobs of an overlay variant --

def knob_settings(repo: Path, applied: list | None, **kw) -> api.RunSettings:
    s = api.RunSettings(workdir=str(repo), **kw)
    if applied is not None:
        s.extra["applied_knobs"] = applied
    return s


def test_apply_settings_leaves_the_knobs_that_are_not_applied_to_the_loaded_config(repo: Path) -> None:
    cfg = load_config(repo=repo, env={})
    cfg.loop["retry_budget"] = 1                                   # what a unit's harness.yaml would say
    cfg.raw["prompt_template"] = "unit-template"
    cfg.role("implementer").model = "unit-impl"
    package_planner = cfg.role("planner").model
    s = knob_settings(repo, ["models.planner", "max_ai_credits"], retry_budget=3, prompt_template="v2",
                      custom_instructions=False, max_ai_credits=7,
                      models={"planner": "m-plan", "implementer": "m-impl", "test_writer": "m-tests"})

    backend.apply_settings(cfg, s)

    assert cfg.role("planner").model == "m-plan" != package_planner               # applied
    assert cfg.role("implementer").model == "unit-impl"                           # not applied: the unit's
    assert cfg.role("test-writer").model != "m-tests" and cfg.role("solo").model != "m-impl"
    assert cfg.loop["retry_budget"] == 1 and cfg.raw["prompt_template"] == "unit-template"
    assert "custom_instructions" not in cfg.copilot
    assert {rt["max_ai_credits_per_run"] for rt in cfg.runtimes.values()} == {7.0}     # the cap always applies


def test_an_expensive_unit_config_still_stops_at_the_run_cap(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    """A unit's harness.yaml asks for a long, pricey run: 50 retries, 5 ladder rounds, its own caps of 999.
    The overlay variant names no knob, only the cap of the variants file applies (1 credit): the run ends
    budget_exceeded at most one model call (here 0.4 credits) above the cap."""
    from harness.fake import Turn, edit
    from .test_orchestrator import HEADER
    config = make_unit(tmp_path,
                       ("  retry_budget: 3 ", "  retry_budget: 50 "),
                       ("  ladder_rounds: 2 ", "  ladder_rounds: 5 "),
                       ("    max_ai_credits_per_session: 40", "    max_ai_credits_per_session: 999"),
                       ("    max_ai_credits_per_run: 200", "    max_ai_credits_per_run: 999"))
    broken = HEADER + "int ts_parse(int x)\n{\n    return x + 1; /* broken */\n}\n"
    scripts = {"implementer": [Turn(f"try {i}", [edit("src/proto/proto.c", broken + f"/* {i} */\n")], nano_aiu=4e8)
                               for i in range(60)]}
    runtimes: list = []
    s = settings_for(repo, tmp_path / "run", ticket_file, roles=ONLY_IMPLEMENTER, max_ai_credits=1.0)
    s.extra.update(config=str(config), applied_knobs=["max_ai_credits"])        # as variants.py builds it
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(scripts, runtimes=runtimes))
    assert res.status == "budget_exceeded" and not res.success, (res.status, res.error)
    assert 1.0 <= res.ai_credits <= 1.0 + 0.4 + 1e-9
    assert len([p for r, p in runtimes[0].prompts if r == "implementer"]) == 3        # 3 x 0.4, not 51 turns
    summary = _summary(tmp_path / "run")
    # the variant's cap bounds the session as well (min with the runtime's 40): the guard names whichever it
    # checks first, the cap is the variant's 1 credit either way, never the unit's 999
    assert summary["caps"] == [1.0, 1.0] and "credit cap reached" in summary["reason"]
    assert "of 1.00 AI credits" in summary["reason"]


def test_one_list_of_knobs_for_the_variants_file_and_the_backend(repo: Path) -> None:
    """Every name variants.py can put into applied_knobs is one the backend accepts, and nothing else is."""
    assert eval_variants.SCALAR_KNOBS is api.SCALAR_KNOBS and eval_variants.ROLE_KNOBS is api.ROLE_KNOBS
    producible = set(api.SCALAR_KNOBS) | {api.CAP_KNOB} | {f"{g}.{r}" for g in api.ROLE_KNOBS for r in api.ROLES}
    assert producible == api.KNOB_NAMES
    assert backend.applied_knobs(knob_settings(repo, sorted(producible))) == api.KNOB_NAMES
    assert set(backend.ROLE_MAP) == set(api.ROLES)                       # roles.<role> uses the eval names


def test_the_run_cap_applies_with_no_knob_at_all(repo: Path) -> None:
    cfg = load_config(repo=repo, env={})
    backend.apply_settings(cfg, knob_settings(repo, [], max_ai_credits=7))
    assert cfg.runtimes["facilitator"]["max_ai_credits_per_run"] == 7.0 and cfg.loop["retry_budget"] == 3


def test_every_knob_applies_when_no_list_is_given(repo: Path) -> None:
    cfg = load_config(repo=repo, env={})
    cfg.loop["retry_budget"] = 1
    backend.apply_settings(cfg, knob_settings(repo, None, retry_budget=3, prompt_template="v2",
                                              custom_instructions=False, models={"implementer": "m-impl"}))
    assert cfg.loop["retry_budget"] == 3 and cfg.raw["prompt_template"] == "v2"
    assert cfg.copilot["custom_instructions"] is False
    assert cfg.role("implementer").model == "m-impl" and cfg.role("solo").model == "m-impl"


def test_the_implementer_model_knob_reaches_the_solo_role_too(repo: Path) -> None:
    cfg = load_config(repo=repo, env={})
    backend.apply_settings(cfg, knob_settings(repo, ["models.implementer"], models={"implementer": "m-impl"}))
    assert cfg.role("implementer").model == "m-impl" and cfg.role("solo").model == "m-impl"


def test_applied_knobs_are_checked(repo: Path) -> None:
    import pytest
    for bad, msg in ((["retry_budjet"], "unknown knob"), (["models.auditor"], "unknown knob"),
                     ("retry_budget", "must be a list")):
        with pytest.raises(ValueError, match=msg):
            backend.apply_settings(load_config(repo=repo, env={}), knob_settings(repo, bad))
    backend.apply_settings(load_config(repo=repo, env={}),
                           knob_settings(repo, ["gates", "autonomy", "roles.test_writer", "models.reviewer"]))


def step_ids(wf) -> list[str]:
    return [s.id for s in wf.steps]


def test_roles_and_gates_follow_the_applied_knobs(repo: Path) -> None:
    wf = load_config(repo=repo, env={}).workflow("jira-to-pr")
    off = {"planner": False, "implementer": True, "test_writer": False, "reviewer": False}

    def steps(applied, **kw):
        return backend.workflow_transform(knob_settings(repo, applied, **kw))(wf)

    # nothing applied: the unit's workflow as it is, whatever the settings say
    assert step_ids(steps([], roles=off, gates=False)) == step_ids(wf)
    # only the roles the variant switched
    some = steps(["roles.planner", "roles.reviewer"], roles=off, gates=False)
    assert step_ids(some) == [s for s in step_ids(wf) if s not in ("plan", "review")]
    assert any(s.kind == "gates" for s in some.steps)                       # gates not applied: still there
    # gates applied on their own: gated steps become plain ones, the ladder goes
    nogates = steps(["gates"], roles=off, gates=False)
    assert "ladder" not in step_ids(nogates) and {s.kind for s in nogates.steps if s.id == "implement"} == {"agent"}
    assert "plan" in step_ids(nogates)                                      # roles not applied: planner stays
    # no list: every setting applies, as before
    assert step_ids(steps(None, roles=off)) == ["ticket", "implement", "ladder", "merge-approval", "commit"]


def test_an_unapplied_roles_autonomy_and_gates_run_the_units_workflow(repo: Path, ticket_file: Path,
                                                                     tmp_path: Path) -> None:
    """The settings say: implementer only, no gates, autonomous. With nothing applied the real orchestrator runs
    the workflow as the file has it: the full team, the ladder, approvals counted."""
    runtimes: list = []
    s = settings_for(repo, tmp_path / "run", ticket_file, roles=ONLY_IMPLEMENTER, gates=False, autonomy="auto")
    s.extra["applied_knobs"] = []
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(runtimes=runtimes))
    assert res.status == "completed" and res.success, res.error
    assert {sess.role for sess in runtimes[0].sessions} == {"planner", "implementer", "test-writer", "reviewer"}
    assert res.approvals == 2 and res.reviewer_score == 4.0
    m = eval_audit.parse_audit(Path(res.audit_path))
    assert m is not None and m.gate_results["unit-tests"] == "PASS"


def test_an_overlay_variant_runs_the_units_knobs_unless_it_sets_them(repo: Path, ticket_file: Path,
                                                                    tmp_path: Path) -> None:
    """The unit's harness.yaml says retry_budget 1 and its own implementer model; the variants file's defaults
    say 3 and Haiku, with a run cap of 7. A variant without overlay_from gets the defaults; an overlay variant
    keeps the unit's values for everything it does not set itself, and always gets the cap."""
    git(repo, "branch", "ticket/DEMO-7")
    hidden = tmp_path / "hidden-tests"
    tdir = hidden / "tickets" / "DEMO-7"
    tdir.mkdir(parents=True)
    (tdir / "meta.yaml").write_text("id: DEMO-7\neval: true\nseed_branch: ticket/DEMO-7\n"
                                    "allowed_paths:\n  - src/**\n  - tests/**\n", encoding="utf-8")
    (tdir / "ticket.md").write_text(ticket_file.read_text(encoding="utf-8"), encoding="utf-8")
    config = make_unit(tmp_path, ("  retry_budget: 3 ", "  retry_budget: 1 "),
                       ("  implementer:\n    model: claude-haiku-5.5", "  implementer:\n    model: unit-model"))
    unit = config.parent.parent
    vfile = tmp_path / "variants.yaml"
    vfile.write_text(textwrap.dedent(f"""\
        version: 1
        defaults:
          roles: {{implementer: true}}
          models: {{implementer: claude-haiku-5.5}}
          retry_budget: 3
          max_ai_credits: 7
        variants:
          P: {{label: package config}}
          O: {{label: the unit's own, overlay_from: "{unit.as_posix()}"}}
          S: {{label: the unit with retry 2, overlay_from: "{unit.as_posix()}", retry_budget: 2,
              models: {{implementer: variant-model}}}}
        """), encoding="utf-8")
    runs: dict = {}

    def runtime_factory(cfg, settings):
        rt = FakeRuntime(happy_scripts())
        runs[settings.variant] = (cfg, rt)
        return rt

    backend_fn = backend.make_backend(
        runtime_factory=runtime_factory,
        gate_runner_factory=lambda cfg, env: GateRunner(cfg, CONTAINER, runner=FakeCheck(cfg, unit_gate)))
    cfg = eval_runner.EvalConfig(
        repo=repo, hidden_dir=hidden, variants=eval_variants.load_matrix(vfile).select(None), tickets=["DEMO-7"],
        experiment="knobs", results_dir=tmp_path / "results", worktree_root=tmp_path / "wt", parallel=1,
        golden_ref=None, skip_hidden=True, backend=backend_fn, log=lambda s: None)
    recs = {r["variant"]: r for r in eval_runner.Runner(cfg).run()}

    for v in ("P", "O", "S"):
        assert recs[v]["run_status"] == "done" and recs[v]["status"] == "completed", recs[v].get("error")
    assert {v: c.loop["retry_budget"] for v, (c, rt) in runs.items()} == {"P": 3, "O": 1, "S": 2}
    assert {v: c.role("implementer").model for v, (c, rt) in runs.items()} == {
        "P": "claude-haiku-5.5", "O": "unit-model", "S": "variant-model"}
    for v, (c, rt) in runs.items():                                 # the cap of the defaults reaches every run
        assert c.runtimes["facilitator"]["max_ai_credits_per_run"] == 7.0, v
    # the unit's workflow ran as it is for the overlay variants: the full team, not the defaults' implementer
    assert {s.role for s in runs["P"][1].sessions} == {"implementer"}
    assert {s.role for s in runs["O"][1].sessions} == {"planner", "implementer", "test-writer", "reviewer"}
    assert {v: recs[v]["applied_knobs"] for v in recs} == {
        "P": None, "O": ["max_ai_credits"], "S": ["max_ai_credits", "models.implementer", "retry_budget"]}
