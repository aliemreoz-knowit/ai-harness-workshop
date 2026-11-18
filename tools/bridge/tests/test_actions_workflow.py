"""The optional Actions workflow: permissions, the inert guard, and that it drives the bridge + harness."""

from __future__ import annotations

import re

import pytest
from bridge_testlib import REPO

WORKFLOW = REPO / ".github" / "workflows" / "agent-fix.yml"


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def doc(text: str) -> dict:
    yaml = pytest.importorskip("yaml")
    return yaml.safe_load(text)


def test_triggers_and_permissions(doc: dict) -> None:
    on = doc.get("on", doc.get(True))                 # PyYAML reads the bare key `on` as True
    assert on["issues"]["types"] == ["labeled"] and "workflow_dispatch" in on
    assert doc["permissions"] == {"contents": "write", "pull-requests": "write", "issues": "write",
                                  "checks": "write", "copilot-requests": "write"}


def test_job_is_inert_outside_an_org_and_without_opt_in(doc: dict) -> None:
    cond = " ".join(doc["jobs"]["fix"]["if"].split())
    assert "github.event.repository.owner.type == 'Organization'" in cond
    assert "vars.AGENT_FIX_ENABLED == 'true'" in cond
    assert "github.event.label.name == 'agent:fix'" in cond
    guards, trigger = cond.rsplit("&&", 1)              # owner && opt-in && (dispatch || label)
    assert "Organization" in guards and "AGENT_FIX_ENABLED" in guards and "||" not in guards
    assert "||" in trigger


def test_steps_install_the_pinned_cli_and_run_the_harness(doc: dict) -> None:
    runs = "\n".join(s.get("run", "") for s in doc["jobs"]["fix"]["steps"])
    assert "@github/copilot@1.0.100" in runs and "github-copilot-sdk==1.0.23" in runs
    assert re.search(r"python -m harness run --workflow jira-to-pr .*--mode eval --runtime", runs)
    assert "bridge.py ci-start" in runs and "bridge.py ci-finish" in runs
    finish = next(s for s in doc["jobs"]["fix"]["steps"] if "ci-finish" in s.get("run", ""))
    assert finish.get("if") == "always()"


def test_no_secret_other_than_the_job_token(text: str) -> None:
    assert "secrets." not in text                      # GITHUB_TOKEN only; no PAT, no model key
    assert re.findall(r"\$\{\{\s*github\.token\s*\}\}", text)
