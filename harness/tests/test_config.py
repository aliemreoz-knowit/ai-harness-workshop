from pathlib import Path

import pytest

from harness.config import ConfigError, HarnessConfig, load_config, parse_workflow, validate_workflow


def test_default_config_and_every_workflow_validate(cfg: HarnessConfig) -> None:
    assert set(cfg.roles) >= {"planner", "implementer", "test-writer", "reviewer", "solo"}
    names = cfg.workflow_names()
    assert set(names) >= {"single-agent", "jira-to-pr", "test-only", "review-only", "release-readiness"}
    for n in names:
        wf = cfg.workflow(n)
        assert wf.steps
    jira = cfg.workflow("jira-to-pr")
    kinds = [s.kind for s in jira.steps]
    assert kinds[0] == "ticket" and kinds[-1] == "commit"
    assert jira.steps[jira.index("review")].on_reject == "implement"
    assert {s.approval for s in jira.steps} == {"human", "auto"}


def test_gate_profiles_are_cheap_first(cfg: HarnessConfig) -> None:
    for name, ids in cfg.gates.profiles.items():
        assert ids[0] == "compliance", name  # pure Python compliance before any container build
    fast = cfg.gates.resolve("fast")
    assert [g.fn for g in fast][1:] == ["harness.form:style", "harness.form:docs", "diff-policy", "build", "no-heap", "unit-tests", "property-tests"]
    assert cfg.gates.resolve(["unit-tests"])[0].id == "unit-tests"
    with pytest.raises(ConfigError):
        cfg.gates.resolve("nope")


def test_caps_and_env_overrides(repo: Path) -> None:
    cfg = load_config(repo=repo, env={"HARNESS_RUNTIME": "facilitator", "HARNESS_MODEL_IMPLEMENTER": "m-x",
                                      "HARNESS_RETRY_BUDGET": "1", "HARNESS_MAX_CREDITS_RUN": "7"})
    assert cfg.runtime == "facilitator"
    assert cfg.role("implementer").model == "m-x"
    assert cfg.loop["retry_budget"] == 1
    assert cfg.caps("facilitator")[1] == 7.0
    with pytest.raises(ConfigError):
        load_config(repo=repo, env={"HARNESS_RUNTIME": "byok"})


def test_workflow_validation_errors(cfg: HarnessConfig) -> None:
    bad_role = parse_workflow({"name": "x", "steps": [{"id": "a", "kind": "agent", "role": "nobody", "prompt": "plan"}]})
    with pytest.raises(ConfigError, match="unknown role"):
        validate_workflow(bad_role, cfg)
    forward = parse_workflow({"name": "x", "steps": [
        {"id": "r", "kind": "review", "role": "reviewer", "prompt": "review", "on_reject": "i"},
        {"id": "i", "kind": "agent_gated", "role": "implementer", "prompt": "implement", "gates": "fast"}]})
    with pytest.raises(ConfigError, match="earlier step"):
        validate_workflow(forward, cfg)
    with pytest.raises(ConfigError, match="kind"):
        parse_workflow({"steps": [{"id": "a", "kind": "deploy"}]})
    with pytest.raises(ConfigError, match="approval"):
        parse_workflow({"steps": [{"id": "a", "kind": "approval", "approval": "maybe"}]})
    no_prompt = parse_workflow({"steps": [{"id": "a", "kind": "agent", "role": "planner", "prompt": "missing"}]})
    with pytest.raises(ConfigError, match="not found"):
        validate_workflow(no_prompt, cfg)
