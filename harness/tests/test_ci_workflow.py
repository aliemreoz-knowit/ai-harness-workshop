"""The CI workflow (.github/workflows/ci.yml): on push and pull request, build the host preset and run ctest.

It must stay free of secrets and of Copilot: a fork or a pull request runs it with no credentials.
"""

from __future__ import annotations

import json
import re
from pathlib import Path

import pytest

from harness import yamlmini

REPO = Path(__file__).resolve().parents[2]
WORKFLOW = REPO / ".github" / "workflows" / "ci.yml"
PRESETS = REPO / "CMakePresets.json"


@pytest.fixture(scope="module")
def text() -> str:
    return WORKFLOW.read_text(encoding="utf-8")


@pytest.fixture(scope="module", params=[yamlmini.load, yamlmini.load_mini], ids=["load", "load_mini"])
def doc(request: pytest.FixtureRequest, text: str) -> dict:
    return request.param(text)


def test_runs_on_push_and_pull_request(doc: dict) -> None:
    on = doc.get("on", doc.get(True))                 # PyYAML reads the bare key `on` as True
    assert set(on) == {"push", "pull_request"}


def test_one_ubuntu_job_with_checkout_first(doc: dict) -> None:
    assert doc["name"] == "ci" and len(doc["jobs"]) == 1
    (job,) = doc["jobs"].values()
    assert job["runs-on"] == "ubuntu-latest"
    assert str(job["steps"][0]["uses"]).startswith("actions/checkout@")


def _run_lines(doc: dict) -> list[str]:
    (job,) = doc["jobs"].values()
    return [line.strip() for step in job["steps"] for line in str(step.get("run", "")).splitlines()]


def test_configures_builds_and_tests_the_host_preset_in_that_order(doc: dict) -> None:
    runs = _run_lines(doc)
    steps = [next((i for i, r in enumerate(runs) if r.startswith(prefix)), None)
             for prefix in ("cmake --preset host", "cmake --build --preset host", "ctest --preset host")]
    assert None not in steps and steps == sorted(steps)


def test_the_presets_it_names_exist(doc: dict) -> None:
    presets = json.loads(PRESETS.read_text(encoding="utf-8"))
    runs = "\n".join(_run_lines(doc))
    named = {kind: set(re.findall(rf"{flag}\s+(\w+)", runs)) for kind, flag in
             (("configurePresets", r"cmake --preset"), ("buildPresets", r"cmake --build --preset"),
              ("testPresets", r"ctest --preset"))}
    for kind, names in named.items():
        assert names == {"host"}, kind
        assert names <= {p["name"] for p in presets[kind]}, kind


def test_checkout_does_not_keep_the_token(doc: dict) -> None:
    (job,) = doc["jobs"].values()
    assert job["steps"][0]["with"] == {"persist-credentials": False}


def test_least_privilege_no_secrets_no_copilot(doc: dict) -> None:
    assert doc["permissions"] == {"contents": "read"}
    (job,) = doc["jobs"].values()
    assert "permissions" not in job                   # the workflow-level read-only grant is the only one
    config = json.dumps(doc).lower()                  # the parsed config: the comments may talk about all of it
    assert "secrets." not in config and "copilot" not in config
    assert "github_token" not in config and "github.token" not in config
