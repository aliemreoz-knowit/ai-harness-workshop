"""What the Copilot CLI -- and so every agent shell -- inherits from the harness process."""

from harness import backend
from harness.config import HarnessConfig, load_config
from harness.runtime import CopilotRuntime, copilot_env, env_passthrough

BRIDGE_ENV = {
    "PATH": "x", "HOME": "/h", "USERPROFILE": "C:/u", "TEMP": "t", "TMP": "t", "TMPDIR": "t",
    "SYSTEMROOT": "C:/Windows", "COMSPEC": "cmd.exe", "PATHEXT": ".EXE", "APPDATA": "a", "LOCALAPPDATA": "l",
    "LANG": "C.UTF-8", "LC_ALL": "C", "TERM": "xterm", "SSL_CERT_FILE": "ca.pem", "REQUESTS_CA_BUNDLE": "ca.pem",
    "HTTPS_PROXY": "p", "NO_PROXY": "n", "COPILOT_HOME": "ch", "TS_IMAGE": "img", "HARNESS_RUNTIME": "auto",
    # the bridge's and HIL's own credentials, and whatever else the shell had
    "GITHUB_TOKEN": "repo", "GH_TOKEN": "repo", "JIRA_API_TOKEN": "jt", "JIRA_EMAIL": "e", "JIRA_SITE": "s",
    "JIRA_CLOUD_ID": "c", "HIL_GITHUB_TOKEN": "ht", "GITHUB_MCP_TOKEN": "mt", "AWS_SECRET_ACCESS_KEY": "aws",
    "BRIDGE_GITHUB_REPO": "o/r", "GITHUB_REPOSITORY": "o/r",
}


def test_only_the_allowlist_reaches_the_copilot_cli():
    rt = CopilotRuntime(cli_path="copilot", working_directory=".", env=BRIDGE_ENV)
    for name in ("GITHUB_TOKEN", "GH_TOKEN", "JIRA_API_TOKEN", "JIRA_EMAIL", "JIRA_SITE", "JIRA_CLOUD_ID",
                 "HIL_GITHUB_TOKEN", "GITHUB_MCP_TOKEN", "AWS_SECRET_ACCESS_KEY", "BRIDGE_GITHUB_REPO",
                 "GITHUB_REPOSITORY"):
        assert name not in rt.env, name
        assert name in rt.env_dropped
    for name in ("PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SYSTEMROOT", "COMSPEC", "PATHEXT",
                 "APPDATA", "LOCALAPPDATA", "LANG", "LC_ALL", "TERM", "SSL_CERT_FILE", "REQUESTS_CA_BUNDLE",
                 "HTTPS_PROXY", "NO_PROXY", "COPILOT_HOME", "TS_IMAGE", "HARNESS_RUNTIME"):
        assert rt.env[name] == BRIDGE_ENV[name], name
    assert rt.env["COPILOT_AUTO_UPDATE"] == "false" and rt.env["COPILOT_SKIP_CLI_DOWNLOAD"] == "1"


def test_actions_keeps_the_job_token_but_never_gh_token():
    rt = CopilotRuntime(cli_path="copilot", working_directory=".",
                        env={"GITHUB_TOKEN": "job", "GH_TOKEN": "job", "GITHUB_ACTIONS": "true", "PATH": "x"})
    assert rt.env["GITHUB_TOKEN"] == "job" and rt.env["GITHUB_ACTIONS"] == "true"
    assert "GH_TOKEN" not in rt.env


def test_explicit_opt_in_keeps_the_env_token():
    rt = CopilotRuntime(cli_path="copilot", working_directory=".",
                        env={"GITHUB_TOKEN": "t", "GH_TOKEN": "t", "HARNESS_COPILOT_ENV_TOKEN": "1"})
    assert rt.env["GITHUB_TOKEN"] == "t" and "GH_TOKEN" not in rt.env


def test_passthrough_adds_names_but_never_the_other_tools_tokens():
    env = dict(BRIDGE_ENV, NODE_OPTIONS="--x", MY_MCP_KEY="k", MY_MCP_URL="u")
    kept, dropped = copilot_env(env, ["NODE_OPTIONS", "MY_MCP_*", "JIRA_API_TOKEN", "GH_TOKEN", "GITHUB_MCP_TOKEN",
                                      "HIL_GITHUB_TOKEN"])
    assert kept["NODE_OPTIONS"] == "--x" and kept["MY_MCP_KEY"] == "k" and kept["MY_MCP_URL"] == "u"
    for name in ("JIRA_API_TOKEN", "GH_TOKEN", "GITHUB_MCP_TOKEN", "HIL_GITHUB_TOKEN"):
        assert name not in kept and name in dropped
    assert copilot_env({"path": "p", "Home": "h"}, [])[0] == {"path": "p", "Home": "h",      # case-insensitive names
                                                              "COPILOT_AUTO_UPDATE": "false"}


def test_the_cli_never_updates_itself_under_the_harness():
    # a CLI that runs a newer version than the pinned one speaks another SDK protocol
    assert copilot_env({"PATH": "p", "COPILOT_AUTO_UPDATE": "true"})[0]["COPILOT_AUTO_UPDATE"] == "false"


def test_passthrough_comes_from_harness_yaml(cfg: HarnessConfig, monkeypatch) -> None:
    assert env_passthrough(cfg) == []
    assert "env_passthrough" in cfg.copilot                         # the key is documented in harness.yaml
    cfg.copilot["env_passthrough"] = ["MY_MCP_*"]
    seen: dict = {}

    class Recorder:
        def __init__(self, cli, wd, log_level="warning", passthrough=()):
            seen["passthrough"] = list(passthrough)

    monkeypatch.setattr("harness.runtime.CopilotRuntime", Recorder)
    monkeypatch.setattr("harness.runtime.sdk_available", lambda: True)
    monkeypatch.setattr("harness.envdetect.find_copilot_cli", lambda *a, **k: "copilot")
    backend.default_runtime(cfg, backend.RunSettings())
    assert seen["passthrough"] == ["MY_MCP_*"]


def test_doctor_reports_what_stays_behind(cfg: HarnessConfig, tmp_path, monkeypatch) -> None:
    from harness.doctor import run_doctor
    import subprocess

    def ok(cmd, **kw):
        return subprocess.CompletedProcess(cmd, 0, "/usr/bin/cmake\n", "")
    env = dict(BRIDGE_ENV, TS_IN_CONTAINER="1", HARNESS_BASH=__import__("sys").executable)
    env.pop("COPILOT_HOME")
    report = run_doctor(cfg, env=env, skip_auth=True, runner=ok)
    (check,) = [c for c in report.checks if c.name == "copilot env"]
    assert check.status == "OK" and "JIRA_API_TOKEN" in check.detail and "jt" not in check.detail
    assert "GH_TOKEN" in check.detail and "HIL_GITHUB_TOKEN" in check.detail


def test_default_config_has_no_passthrough() -> None:
    assert load_config(env={}).copilot.get("env_passthrough") == []
