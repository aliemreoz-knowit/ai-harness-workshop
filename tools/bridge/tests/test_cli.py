"""The command line, against the recorded fixtures (``--fixtures`` implies ``--dry-run``)."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bridge_testlib import FIXTURES, FakeHarness

import bridge as bridge_mod


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch: pytest.MonkeyPatch) -> None:
    for var in ("BRIDGE_GITHUB_REPO", "GITHUB_REPOSITORY", "GITHUB_TOKEN", "GH_TOKEN", "JIRA_SITE"):
        monkeypatch.delenv(var, raising=False)


def run(capsys: pytest.CaptureFixture[str], config: Path, repo: Path, *args: str, **kw) -> tuple[int, str]:
    rc = bridge_mod.main(["--config", str(config), "--repo-root", str(repo), "--fixtures", str(FIXTURES), *args], **kw)
    return rc, capsys.readouterr().out


def test_once_with_fixtures_is_a_dry_run(capsys, config_file: Path, repo: Path, fake_harness: FakeHarness) -> None:
    rc, out = run(capsys, config_file, repo, "once", run_fn=fake_harness)
    assert rc == 0, out
    rep = json.loads(out[out.rindex("\n{\n"):])                      # the report printed last
    assert rep["fix"][0]["status"] == "done" and rep["hil"][0]["pr"] == 31
    assert "[dry-run] POST /repos/acme/tinystream/pulls" in out
    assert "--fake" in fake_harness.harness_calls[0]["cmd"]            # dry-run implies the fake runtime
    assert "--runtime" in fake_harness.harness_calls[0]["cmd"]
    assert (repo / ".harness" / "bridge" / "state.json.dry-run").is_file()
    assert not (repo / ".harness" / "bridge" / "state.json").exists()  # the live state is untouched
    rc, out = run(capsys, config_file, repo, "status")
    assert "gh#12" in out and "pr31-1a2b3c4" in out


def test_check_is_read_only(capsys, config_file: Path, repo: Path) -> None:
    config_file.write_text(config_file.read_text(encoding="utf-8").replace("enabled: false", "enabled: true"),
                           encoding="utf-8")
    rc, out = run(capsys, config_file, repo, "check")
    assert rc == 0, out
    assert "github: acme/tinystream ok (owner type Organization" in out
    assert "label 'agent:fix' ok" in out
    assert "jira: https://acme.atlassian.net as Workshop Facilitator" in out
    assert "1 issue(s) match" in out
    assert "[dry-run]" not in out


def test_labels_creates_nothing_that_exists(capsys, config_file: Path, repo: Path) -> None:
    rc, out = run(capsys, config_file, repo, "labels")
    assert rc == 0 and out.count(": exists") == 5


def test_ci_start_and_finish(capsys, config_file: Path, repo: Path, tmp_path: Path, fake_harness: FakeHarness) -> None:
    ticket = tmp_path / "ci" / "ticket.md"
    rc, out = run(capsys, config_file, repo, "ci-start", "--issue", "12", "--ticket-out", str(ticket))
    assert rc == 0 and out.strip().endswith("GH-12")
    assert ticket.read_text(encoding="utf-8").startswith("# GH-12: Board shows a stale frame counter")
    assert "[dry-run] POST /repos/acme/tinystream/issues/12/labels" in out
    # the workflow's own "python -m harness run" step, in the checkout
    fake_harness(["python", "-m", "harness", "run", "--ticket", str(ticket)], cwd=str(repo))
    rc, out = run(capsys, config_file, repo, "ci-finish", "--issue", "12", "--workdir", str(repo))
    res = json.loads(out.strip().splitlines()[-1])
    assert rc == 0 and res["status"] == "done" and res["run_id"] == fake_harness.run_id
    assert "[dry-run] git push origin agent/gh-12-ab12" in out


def test_missing_repo_is_reported(capsys, tmp_path: Path, repo: Path) -> None:
    cfg = tmp_path / "empty.yaml"
    cfg.write_text("harness:\n  fetch: false\n", encoding="utf-8")
    rc, out = run(capsys, cfg, repo, "labels")       # the test repo's origin is a local path, not github.com
    assert rc == 2 and "no GitHub repo" in out


def test_repo_from_remote_parses_github_urls(tmp_path: Path, repo: Path) -> None:
    from bridge_testlib import git
    git(repo, "remote", "set-url", "origin", "git@github.com:acme/tinystream.git")
    assert bridge_mod.repo_from_remote(repo, "origin") == "acme/tinystream"
    git(repo, "remote", "set-url", "origin", "https://github.com/acme/tinystream")
    assert bridge_mod.repo_from_remote(repo, "origin") == "acme/tinystream"


def test_shipped_config_parses_without_pyyaml() -> None:
    from harness.yamlmini import load_mini
    data = load_mini((bridge_mod.HERE / "bridge.yaml").read_text(encoding="utf-8"))
    cfg = bridge_mod._merge(bridge_mod.DEFAULTS, data)
    assert cfg["github"]["fix_label"] == "agent:fix" and cfg["harness"]["runtime"] == "facilitator"
    assert set(data) <= set(bridge_mod.DEFAULTS)
    for section, values in data.items():
        assert set(values) <= set(bridge_mod.DEFAULTS[section]), section
