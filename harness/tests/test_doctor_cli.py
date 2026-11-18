import json
import subprocess
from pathlib import Path

import pytest

from harness import cli, level1_runner
from harness.config import HarnessConfig
from harness.doctor import run_doctor


def ok_runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, "/usr/bin/cmake\n/usr/bin/ctest\n/usr/bin/cppcheck\n/usr/bin/gcovr\n", "")


def models_for(cfg: HarnessConfig) -> dict[str, str]:
    return {m: "enabled" for r in cfg.roles.values() for m in (r.model, r.escalation_model) if m}


@pytest.fixture
def container(monkeypatch: pytest.MonkeyPatch, tmp_path: Path) -> dict[str, str]:
    cli_bin = tmp_path / "copilot"
    cli_bin.write_text("", encoding="utf-8")
    return {"TS_IN_CONTAINER": "1", "COPILOT_CLI_PATH": str(cli_bin), "COPILOT_HOME": str(tmp_path / "home"),
            "HARNESS_BASH": __import__("sys").executable}


def checks(report) -> dict[str, str]:
    return {c.name: c.status for c in report.checks}


def test_doctor_picks_copilot_user_when_auth_and_models_work(cfg: HarnessConfig, container: dict) -> None:
    probe = lambda cli, c: {"auth": {"ok": True, "login": "someone", "type": "user", "host": "https://github.com"},
                            "models": models_for(cfg)}
    report = run_doctor(cfg, env=container, probe=probe, runner=ok_runner)
    assert report.runtime_mode == "copilot-user"
    st = checks(report)
    assert st["copilot auth"] == "OK" and st["models"] == "OK" and st["environment"] == "OK"
    assert st["gates"] == "OK" and st["git repo"] == "OK"
    assert "Runtime mode: copilot-user" in report.render()


def test_doctor_falls_back_to_facilitator(cfg: HarnessConfig, container: dict) -> None:
    no_login = lambda cli, c: {"auth": {"ok": False, "message": "not logged in"}}
    r = run_doctor(cfg, env=container, probe=no_login, runner=ok_runner)
    assert r.runtime_mode == "facilitator" and checks(r)["copilot auth"] == "WARN"
    missing = lambda cli, c: {"auth": {"ok": True, "login": "x"}, "models": {"gpt-x": "enabled"}}
    r = run_doctor(cfg, env=container, probe=missing, runner=ok_runner)
    assert r.runtime_mode == "facilitator" and checks(r)["models"] == "WARN"
    assert "not available" in [c.detail for c in r.checks if c.name == "models"][0]
    broken = lambda cli, c: {"error": "FileNotFoundError: copilot"}
    r = run_doctor(cfg, env=container, probe=broken, runner=ok_runner)
    assert checks(r)["copilot auth"] == "FAIL" and not r.ok


def test_doctor_reports_trust_and_mcp(cfg: HarnessConfig, container: dict, repo: Path) -> None:
    home = Path(container["COPILOT_HOME"])
    home.mkdir()
    (home / "config.json").write_text("// comment\n" + json.dumps({"trusted_folders": [str(repo)]}), encoding="utf-8")
    (repo / ".github").mkdir()
    (repo / ".github" / "mcp.json").write_text(json.dumps({"mcpServers": {"atlassian": {"type": "http"}}}),
                                               encoding="utf-8")
    r = run_doctor(cfg, env=container, skip_auth=True, runner=ok_runner)
    st = checks(r)
    assert st["repo trust"] == "OK" and st["mcp config"] == "OK" and st["copilot auth"] == "SKIP"


def test_cli_workflows_and_runs(capsys: pytest.CaptureFixture, repo: Path) -> None:
    assert cli.main(["--repo", str(repo), "workflows"]) == 0
    out = capsys.readouterr().out
    assert "jira-to-pr" in out and "review[reviewer]" in out
    assert cli.main(["--repo", str(repo), "runs"]) == 0
    assert "no runs" in capsys.readouterr().out
    assert cli.main(["--repo", str(repo), "run"]) == 2


def test_level1_runner_parses_jsonl_defensively(tmp_path: Path) -> None:
    seen = {}

    def fake(cmd, **kw):
        seen["cmd"] = cmd
        usage = Path(cmd[cmd.index("--usage-output-file") + 1])
        usage.write_text(json.dumps({"totalNanoAiu": 4e8}), encoding="utf-8")
        out = "\n".join(["Copilot banner, not JSON",
                         json.dumps({"type": "tool.execution_complete", "data": {"toolCallId": "1", "success": True}}),
                         json.dumps({"type": "tool.execution_complete", "data": {"toolCallId": "2", "success": False}}),
                         '{"type": "assistant.mess',            # truncated line
                         json.dumps({"type": "assistant.message", "data": {"content": "done"}})])
        return subprocess.CompletedProcess(cmd, 0, out, "")

    res = level1_runner.run("implementer", "fix it", allow=("view", "edit"), credits=2, runner=fake)
    assert res == {"answer": "done", "tool_calls": 2, "tool_failures": 1, "usage": {"totalNanoAiu": 4e8},
                   "exit_code": 0}
    cmd = seen["cmd"]
    for flag in ("-p", "--output-format", "-s", "--no-ask-user", "--agent", "--max-ai-credits"):
        assert flag in cmd
    assert "--allow-tool=edit" in cmd and cmd[cmd.index("--agent") + 1] == "implementer"
    # the CLI refuses a cap below 30 credits: the runner never asks for less
    assert cmd[cmd.index("--max-ai-credits") + 1] == "30"
    level1_runner.run("implementer", "fix it", credits=45, runner=fake)
    assert seen["cmd"][seen["cmd"].index("--max-ai-credits") + 1] == "45"
    level1_runner.run("implementer", "fix it", runner=fake)
    assert seen["cmd"][seen["cmd"].index("--max-ai-credits") + 1] == "30"


def test_level1_runner_is_small() -> None:
    src = Path(level1_runner.__file__).read_text(encoding="utf-8").splitlines()
    code = [ln for ln in src if ln.strip() and not ln.strip().startswith(("#", '"""'))]
    assert len(code) <= 40
