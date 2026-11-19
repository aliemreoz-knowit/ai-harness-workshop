"""``python -m harness doctor``: is this machine ready, and which runtime mode applies?

Checks (none of them sends a prompt or spends a credit):

1. Python and the pinned SDK (github-copilot-sdk 1.0.23)
2. Copilot CLI binary and version (pinned 1.0.100)
3. Copilot auth through the SDK (``get_auth_status``) -- starts the CLI, no prompt
4. role models available and enabled (``list_models``)
5. devcontainer, or docker + tinystream-dev image, or neither: then the gates run the Docker-free
   ladder (scripts/lite_gates.py, zig cc) and the gates that need the container SKIP; with
   ``TS_LITE=0`` (as check.sh: never the Docker-free ladder) the doctor waits up to 60 s for
   ``docker info``, and no Docker is a FAIL
6. gates runnable (bash, scripts/check.sh, the toolchain in the image or zig for the lite ladder)
7. repo trusted by the Copilot CLI (hooks do not fire in untrusted folders)
8. MCP config present (Jira server for tickets by key)

Runtime mode: ``copilot-user`` when auth and models are fine, otherwise
``facilitator`` (runs go through the facilitator's central runner).

Depth (``depth()``, ``doctor --depth``): "Full" with the container ladder, "Try" with the
Docker-free one; "(your Copilot)" when the Copilot login works, "(fake model)" otherwise
(``--fake`` runs: no model, no credits). "Look" (a browser only) is the setup scripts' answer
when the doctor cannot run at all.
"""

from __future__ import annotations

import asyncio
import json
import os
import platform
import subprocess
import sys
from dataclasses import asdict, dataclass
from pathlib import Path
from typing import Any, Callable

from . import envdetect, gitops
from .config import HarnessConfig
from .runtime import CopilotRuntime, copilot_env, env_passthrough, sdk_available, sdk_version

OK, WARN, FAIL, SKIP = "OK", "WARN", "FAIL", "SKIP"
LADDER = {"full": "full (container)", "lite": "zig (this laptop)"}     # the doctor --depth "Ladder:" line
LITE_OFF_DOCKER_WAIT_S = 60.0    # TS_LITE=0: how long the doctor waits for `docker info` (check.sh: no bound)


@dataclass
class Check:
    name: str
    status: str
    detail: str


@dataclass
class DoctorReport:
    checks: list[Check]
    runtime_mode: str
    runtime_reason: str
    where: str
    ladder: str = "full"         # full: the gates run in the container | lite: the Docker-free ladder

    @property
    def ok(self) -> bool:
        return all(c.status != FAIL for c in self.checks)

    def to_dict(self) -> dict[str, Any]:
        return {"ok": self.ok, "runtime_mode": self.runtime_mode, "runtime_reason": self.runtime_reason,
                "where": self.where, "ladder": self.ladder, "depth": depth(self),
                "checks": [asdict(c) for c in self.checks]}

    def render(self) -> str:
        width = max(len(c.name) for c in self.checks)
        lines = [f"harness doctor -- {self.where}"]
        for c in self.checks:
            lines.append(f"  {c.status:<4}  {c.name:<{width}}  {c.detail}")
        lines.append("")
        lines.append(f"Runtime mode: {self.runtime_mode} -- {self.runtime_reason}")
        lines.append("Result: " + ("ready" if self.ok else "NOT ready (fix the FAIL lines)"))
        return "\n".join(lines)


def depth(report: DoctorReport) -> str:
    """The participant's depth: Try / Full by the gate ladder, "your Copilot" only when the Copilot
    login works (auth OK); not logged in, not checked (--skip-auth) or broken is the fake model."""
    auth = next((c.status for c in report.checks if c.name == "copilot auth"), SKIP)
    return f"{'Full' if report.ladder == 'full' else 'Try'} ({'your Copilot' if auth == OK else 'fake model'})"


Probe = Callable[[str, HarnessConfig], Any]


async def _sdk_probe(cli: str, cfg: HarnessConfig, timeout: float = 90) -> dict[str, Any]:
    """Start the CLI through the SDK, read auth status and models, stop. No prompt."""
    rt = CopilotRuntime(cli, str(cfg.repo), log_level="error", passthrough=env_passthrough(cfg))
    out: dict[str, Any] = {}
    try:
        await asyncio.wait_for(rt.start(), timeout)
        auth = await asyncio.wait_for(rt.auth_status(), timeout)
        out["auth"] = {"ok": bool(auth.isAuthenticated), "login": auth.login, "host": auth.host,
                       "type": auth.authType, "message": auth.statusMessage}
        if auth.isAuthenticated:
            models = await asyncio.wait_for(rt.list_models(), timeout)
            out["models"] = {m.id: (m.policy.state if m.policy else "unknown") for m in models}
    except Exception as exc:  # noqa: BLE001
        out["error"] = f"{type(exc).__name__}: {exc}"
    finally:
        try:
            await asyncio.wait_for(rt.stop(), 30)
        except Exception:  # noqa: BLE001
            pass
    return out


def _trusted(repo: Path, env: dict[str, str]) -> tuple[bool | None, str]:
    home = Path(env.get("COPILOT_HOME") or (Path.home() / ".copilot"))
    found_any = False
    for name in ("config.json", "settings.json"):
        path = home / name
        if not path.is_file():
            continue
        found_any = True
        try:
            text = "\n".join(ln for ln in path.read_text(encoding="utf-8").splitlines()
                             if not ln.lstrip().startswith("//"))
            data = json.loads(text) if text.strip() else {}
        except (OSError, ValueError):
            continue
        folders = data.get("trusted_folders") or data.get("trustedFolders") or []
        rp = str(repo.resolve()).replace("\\", "/").lower()
        for f in folders:
            fp = str(f).replace("\\", "/").rstrip("/").lower()
            if rp == fp or rp.startswith(fp + "/"):
                return True, f"trusted via {path.name}: {f}"
    if not found_any:
        return None, f"no Copilot config in {home}"
    return False, f"{repo} is not in trusted_folders ({home / 'config.json'}); hooks in .github/hooks will not fire"


def _mcp_config(cfg: HarnessConfig, env: dict[str, str]) -> tuple[str, str]:
    jira = cfg.ticket.get("jira") or {}
    server = str(jira.get("mcp_server", ""))
    places = [cfg.repo / ".github" / "mcp.json", cfg.repo / ".vscode" / "mcp.json",
              Path(env.get("COPILOT_HOME") or (Path.home() / ".copilot")) / "mcp-config.json"]
    for p in places:
        if p.is_file():
            try:
                data = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                return WARN, f"{p} is not valid JSON"
            servers = data.get("mcpServers") or data.get("servers") or {}
            if server and server in servers:
                return OK, f"{server} configured in {p}"
            return WARN, f"{p} has no {server!r} server ({', '.join(servers) or 'none'})"
    if jira.get("url"):
        return WARN, (f"no MCP config file; the harness uses harness.yaml ticket.jira ({jira.get('url')}) "
                      "-- tickets by Jira key need an OAuth login, local ticket files always work")
    return WARN, "no MCP config: use local ticket files"


def run_doctor(cfg: HarnessConfig, skip_auth: bool = False, env: dict[str, str] | None = None,
               probe: Callable[[str, HarnessConfig], dict[str, Any]] | None = None,
               runner: Callable[..., subprocess.CompletedProcess] = subprocess.run) -> DoctorReport:
    env = dict(os.environ if env is None else env)
    checks: list[Check] = []
    e = envdetect.detect(cfg.repo, str(cfg.paths.get("bash", "") or ""), env)

    # 1. python + sdk
    py = platform.python_version()
    checks.append(Check("python", OK if sys.version_info >= (3, 11) else FAIL, f"{py} ({sys.executable})"))
    want_sdk = str(cfg.copilot.get("sdk_version", "1.0.23"))
    have_sdk = sdk_version() if sdk_available() else None
    if have_sdk is None:
        checks.append(Check("copilot sdk", FAIL, f"github-copilot-sdk not importable; pip install --user "
                                                  f"github-copilot-sdk=={want_sdk}"))
    else:
        checks.append(Check("copilot sdk", OK if have_sdk == want_sdk else WARN,
                            f"{have_sdk}" + ("" if have_sdk == want_sdk else f" (pinned {want_sdk})")))

    # 2. cli
    cli = envdetect.find_copilot_cli(str(cfg.copilot.get("cli_path", "") or ""), env)
    want_cli = str(cfg.copilot.get("expected_cli_version", ""))
    if not cli:
        checks.append(Check("copilot cli", FAIL, "not found: winget install GitHub.Copilot (Windows) or the GitHub "
                                                 "install script (Linux, macOS), or set COPILOT_CLI_PATH"))
    else:
        ver = envdetect.cli_version(cli)
        status = OK if ver and (not want_cli or ver == want_cli) else WARN
        checks.append(Check("copilot cli", status, f"{ver or '?'} at {cli}"
                            + ("" if status == OK else f" (pinned {want_cli})")))

    # 3 + 4. auth and models through the SDK
    auth_ok = False
    models_ok = False
    reason = ""
    if skip_auth:
        checks.append(Check("copilot auth", SKIP, "--skip-auth"))
        checks.append(Check("models", SKIP, "--skip-auth"))
        reason = "auth not checked (--skip-auth)"
    elif not cli or have_sdk is None:
        checks.append(Check("copilot auth", FAIL, "needs the SDK and the CLI"))
        checks.append(Check("models", SKIP, "needs auth"))
        reason = "no SDK/CLI on this machine"
    else:
        res = probe(cli, cfg) if probe else asyncio.run(_sdk_probe(cli, cfg))
        if res.get("error") and "auth" not in res:
            checks.append(Check("copilot auth", FAIL, f"CLI did not start through the SDK: {res['error']}"))
            checks.append(Check("models", SKIP, "needs auth"))
            reason = "the Copilot CLI could not be started"
        else:
            auth = res.get("auth") or {}
            auth_ok = bool(auth.get("ok"))
            if auth_ok:
                checks.append(Check("copilot auth", OK, f"logged in as {auth.get('login')} ({auth.get('type')}, "
                                                        f"{auth.get('host')})"))
            else:
                checks.append(Check("copilot auth", WARN, f"not logged in: {auth.get('message') or 'run copilot /login'}"))
                reason = "no Copilot login here"
            models = res.get("models") or {}
            if auth_ok:
                missing = []
                disabled = []
                for role in cfg.roles.values():
                    for m in filter(None, (role.model, role.escalation_model)):
                        if m not in models:
                            missing.append(f"{m} ({role.name})")
                        elif models[m] == "disabled":
                            disabled.append(f"{m} ({role.name})")
                if missing or disabled:
                    detail = ""
                    if missing:
                        detail += "not available: " + ", ".join(sorted(set(missing)))
                    if disabled:
                        detail += ("; " if detail else "") + "disabled by policy: " + ", ".join(sorted(set(disabled)))
                    detail += f" -- {len(models)} models listed: {', '.join(sorted(models)[:12])}"
                    checks.append(Check("models", WARN, detail))
                    reason = "role models are not all available (ask the admin, or set HARNESS_MODEL_<ROLE>)"
                else:
                    models_ok = True
                    checks.append(Check("models", OK, f"all role models available ({len(models)} listed)"))
            else:
                checks.append(Check("models", SKIP, "needs auth"))

    # 5. where gates run
    ladder = "full"
    no_docker = False            # TS_LITE=0 without a usable Docker: no ladder can run here
    if e.in_container:
        checks.append(Check("environment", OK, "inside the devcontainer: gates run directly"))
    else:
        # TS_LITE=0 (stage, central eval): check.sh waits for docker info and never runs lite
        lite_off = env.get("TS_LITE", "") == "0"
        usable, why = (envdetect.docker_usable(timeout=LITE_OFF_DOCKER_WAIT_S) if lite_off
                       else (False, "no Docker needed"))   # only TS_LITE=0 asks for the container
        if not usable and lite_off:
            no_docker = True
            checks.append(Check("environment", FAIL, f"{why}: TS_LITE=0 never runs the Docker-free ladder, the "
                                                     "gates need Docker (start Docker Desktop)"))
        elif not usable:
            ladder = "lite"
            checks.append(Check("environment", OK, "gates run on this machine with zig cc (scripts/lite_gates.py); "
                                                   "fuzz and coverage are not part of it"))
        else:
            img = runner(["docker", "image", "inspect", e.image], capture_output=True, text=True)
            if img.returncode == 0:
                checks.append(Check("environment", OK, f"host ({e.system}) + docker image {e.image}: gates run "
                                                       f"through {cfg.gates.wrapper}"))
            else:
                checks.append(Check("environment", WARN, f"docker present, image {e.image} missing: the first "
                                                         f"gate run builds it from .devcontainer/Dockerfile"))
        checks.append(Check("devcontainer", OK if e.devcontainer else WARN,
                            ".devcontainer/devcontainer.json present" if e.devcontainer else "no devcontainer.json"))

    # 6. gates runnable
    script = cfg.repo / cfg.gates.script
    if not script.is_file():
        checks.append(Check("gates", FAIL, f"{cfg.gates.script} missing in {cfg.repo}"))
    elif not e.bash:
        checks.append(Check("gates", FAIL, "no bash found (Windows: install Git for Windows; WSL bash is not used)"))
    elif no_docker:
        checks.append(Check("gates", FAIL, "the container ladder needs Docker (TS_LITE=0)"))
    elif ladder == "lite":
        # check.sh hands off to scripts/lite_gates.py with this Python (TS_PYTHON): zig must be in it
        try:
            p = runner([sys.executable, "-m", "ziglang", "version"], capture_output=True, text=True, timeout=120,
                       encoding="utf-8", errors="replace")
            out = (p.stdout or "").strip()
            zig = out.splitlines()[0].strip() if p.returncode == 0 and out else ""
        except (OSError, subprocess.TimeoutExpired):
            zig = ""
        if not zig:
            checks.append(Check("gates", FAIL, f"the gates need zig: {sys.executable} -m pip install "
                                               f"ziglang=={envdetect.ZIG_VERSION}"))
        elif zig == envdetect.ZIG_VERSION:
            checks.append(Check("gates", OK, f"zig {zig} via scripts/lite_gates.py"))
        else:
            checks.append(Check("gates", WARN, f"zig {zig} (pinned {envdetect.ZIG_VERSION}) via "
                                               "scripts/lite_gates.py"))
    else:
        probe_cmd = "command -v cmake && command -v ctest && command -v cppcheck && command -v gcovr"
        cmd = ([e.bash, "-c", probe_cmd] if e.in_container
               else [e.bash, cfg.gates.wrapper, "sh", "-c", probe_cmd])
        try:
            p = runner(cmd, cwd=str(cfg.repo), env=e.gate_env(env), capture_output=True, text=True,
                       timeout=300, encoding="utf-8", errors="replace")
            tools = [Path(x.strip()).name for x in (p.stdout or "").splitlines() if x.strip()]
            if p.returncode == 0:
                checks.append(Check("gates", OK, f"toolchain found ({', '.join(tools)}) via "
                                                 f"{'bash' if e.in_container else cfg.gates.wrapper}"))
            else:
                checks.append(Check("gates", FAIL, f"toolchain probe failed ({p.returncode}): "
                                                   f"{(p.stderr or p.stdout or '').strip()[-200:]}"))
        except (OSError, subprocess.TimeoutExpired) as exc:
            checks.append(Check("gates", FAIL, f"cannot run the gate wrapper: {exc}"))
    if gitops.is_repo(cfg.repo):
        checks.append(Check("git repo", OK, f"{gitops.current_branch(cfg.repo)} @ {gitops.head(cfg.repo)[:10]}"))
    else:
        dotgit = cfg.repo / ".git"
        hint = ""
        if dotgit.is_file():
            hint = (" (a git worktree whose .git file points to "
                    f"{dotgit.read_text(encoding='utf-8', errors='replace').strip()[8:]}; "
                    "inside a container open the main checkout instead)")
        checks.append(Check("git repo", FAIL, f"{cfg.repo} is not a git work tree{hint}"))

    # 7. trust
    trusted, detail = _trusted(cfg.repo, env)
    checks.append(Check("repo trust", OK if trusted else WARN, detail))

    # 8. mcp
    status, detail = _mcp_config(cfg, env)
    checks.append(Check("mcp config", status, detail))

    # 9. what the CLI (and every agent shell) inherits: names only, never a value
    kept, dropped = copilot_env(env, env_passthrough(cfg))
    secrets = [n for n in dropped if any(k in n.upper() for k in ("TOKEN", "SECRET", "PASSWORD", "KEY"))]
    checks.append(Check("copilot env", OK, f"{len(kept)} variable(s) reach the CLI, {len(dropped)} stay here"
                        + (f" (among them {', '.join(secrets[:6])})" if secrets else "")
                        + (f"; pass-through: {', '.join(env_passthrough(cfg))}" if env_passthrough(cfg) else "")))

    # runtime mode
    if cfg.runtime != "auto":
        mode, why = cfg.runtime, f"set in harness.yaml / HARNESS_RUNTIME ({reason or 'checks above'})"
    elif auth_ok and models_ok:
        mode, why = "copilot-user", "your own Copilot login works and every role model is available"
    else:
        mode, why = "facilitator", (reason or "own Copilot not usable") + \
            "; your runs go through the facilitator's central runner"
    return DoctorReport(checks, mode, why, e.where, ladder)
