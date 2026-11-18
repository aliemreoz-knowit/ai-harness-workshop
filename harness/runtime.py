"""The model runtime behind the orchestrator: Copilot SDK sessions.

``Runtime`` is the seam the orchestrator talks to; ``CopilotRuntime`` drives
the pinned Copilot CLI through ``github-copilot-sdk`` (JSON-RPC over stdio),
``harness.fake.FakeRuntime`` replays scripted turns in the unit tests.

One role = one SDK session, configured with:

- ``custom_agents=[{name, prompt, tools, model}]`` + ``agent=<role>``: the role
  prompt is the custom agent's prompt (stable prefix, prompt-cache friendly),
- ``available_tools``: exactly the role's tools (built-ins + harness tools),
- ``tools``: the harness custom tools (``@define_tool``),
- ``hooks``: ``on_pre_tool_use`` / ``on_post_tool_use`` from the role policy,
- ``on_permission_request``: the role policy, never a human prompt,
- no ``session_limits``: the credit caps are enforced by the orchestrator (see below),
- ``system_message``: ``customize`` mode, unhelpful sections removed,
  harness rules appended.

The CLI process gets an allowlisted environment (``copilot_env``: PATH, HOME, the
CLI's and the harness's own variables, TLS / proxy settings, plus
``copilot.env_passthrough`` from harness.yaml), never the harness's whole
environment: the bridge and HIL start the harness with their Jira / GitHub
tokens set, and an agent's shell inherits whatever the CLI has.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import Any, Callable, Iterable, Mapping, Protocol

from .config import RoleConfig
from .envdetect import NO_AUTO_UPDATE
from .tools import ToolSpec, to_sdk_tool

# No session_limits on the session: with a limit set, the Copilot runtime (CLI 1.0.100)
# shows the model a <session_limits_status> message after every tool round
# ("Remaining session limits: N AI credits. ... Be frugal; avoid optional
# exploration ..."), and agents cut their work short to save credits. The
# runtime also refuses limits below 30 credits. The harness caps are enforced by
# the orchestrator instead: it watches the usage events and aborts the turn in
# flight when a session or run cap is reached (Orchestrator._guard_credits).


@dataclass
class SessionSpec:
    role: RoleConfig
    model: str
    agent_prompt: str
    system_content: str
    tools: list[ToolSpec]
    hooks: dict[str, Any]
    permission_handler: Callable[[Any, Any], Any]
    max_ai_credits: float
    working_directory: str
    on_event: Callable[[Any], None] | None = None
    mcp_servers: dict[str, Any] = field(default_factory=dict)
    remove_sections: list[str] = field(default_factory=list)
    name: str | None = None
    # True = do not load AGENTS.md / .github/copilot-instructions.md (variant team-no-rules, CLI --no-custom-instructions)
    skip_custom_instructions: bool = False


class AgentSession(Protocol):
    session_id: str

    async def send_and_wait(self, prompt: str, timeout: float) -> str: ...
    async def set_model(self, model: str) -> None: ...
    async def disconnect(self) -> None: ...


class Runtime(Protocol):
    name: str

    async def start(self) -> None: ...
    async def stop(self) -> None: ...
    async def create_session(self, spec: SessionSpec) -> AgentSession: ...
    async def resume_session(self, session_id: str, spec: SessionSpec) -> AgentSession: ...
    async def call_mcp_tool(self, server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any: ...


def sdk_available() -> bool:
    try:
        import copilot  # noqa: F401
    except ImportError:
        return False
    return True


def sdk_version() -> str | None:
    try:
        from importlib.metadata import version
        return version("github-copilot-sdk")
    except Exception:  # noqa: BLE001
        return None


def _uses_env_token(env: Mapping[str, str]) -> bool:
    """True when the Copilot CLI should authenticate from GITHUB_TOKEN:
    in GitHub Actions (copilot-requests: write) or when HARNESS_COPILOT_ENV_TOKEN=1."""
    return env.get("GITHUB_ACTIONS") == "true" or env.get("HARNESS_COPILOT_ENV_TOKEN") == "1"


# The environment the Copilot CLI gets -- and with it every agent shell. The SDK hands this
# dict to the CLI process as its whole environment, so it is an allowlist (name or prefix*,
# case-insensitive: Windows upper-cases os.environ), never a copy of the harness's own
# environment: the bridge and HIL run the harness with their Jira / GitHub tokens set.
ENV_ALLOW = (
    # process basics (Windows needs SystemRoot, ComSpec, PATHEXT, APPDATA for node and the CLI)
    "PATH", "HOME", "USERPROFILE", "TEMP", "TMP", "TMPDIR", "SystemRoot", "SystemDrive", "ComSpec", "PATHEXT",
    "APPDATA", "LOCALAPPDATA", "ProgramData", "ProgramFiles", "ProgramFiles(x86)", "ProgramW6432", "windir",
    "USERNAME", "USER", "LOGNAME", "SHELL", "LANG", "LC_*", "TERM", "TZ", "XDG_*", "MSYS*",
    # TLS and proxies (corporate networks)
    "SSL_CERT_*", "REQUESTS_CA_BUNDLE", "CURL_CA_BUNDLE", "NODE_EXTRA_CA_CERTS",
    "HTTP_PROXY", "HTTPS_PROXY", "NO_PROXY", "http_proxy", "https_proxy", "no_proxy",
    # the CLI, the harness, the gate scripts
    "COPILOT_*", "GITHUB_COPILOT_*", "TS_*", "HARNESS_*", "GITHUB_ACTIONS",
)
# Never, whatever copilot.env_passthrough says: credentials of the bridge, HIL and the MCP
# servers belong to those processes, not to an agent's shell; so does the place where a console
# answers approvals (HARNESS_HITL_DIR): HARNESS_* is allowed above, this takes it back.
ENV_NEVER = ("JIRA_*", "GH_TOKEN", "HIL_*", "GITHUB_MCP_TOKEN", "HARNESS_HITL_*")


def env_passthrough(cfg: Any) -> list[str]:
    """harness.yaml ``copilot.env_passthrough``: extra names (or prefix*) the CLI may inherit."""
    raw = (getattr(cfg, "copilot", None) or {}).get("env_passthrough") or []
    return [str(x) for x in (raw if isinstance(raw, list) else [raw])]


def _env_match(name: str, patterns: Iterable[str]) -> bool:
    low = name.lower()
    for p in patterns:
        p = str(p).lower()
        if p.endswith("*"):
            if low.startswith(p[:-1]):
                return True
        elif low == p:
            return True
    return False


def copilot_env(base: Mapping[str, str], passthrough: Iterable[str] = ()) -> tuple[dict[str, str], list[str]]:
    """(the environment for the CLI process, the names that were dropped).

    ENV_ALLOW plus ``passthrough`` (harness.yaml copilot.env_passthrough: names or prefix*),
    minus ENV_NEVER; GITHUB_TOKEN only when the CLI authenticates from it (_uses_env_token)."""
    allow = tuple(ENV_ALLOW) + tuple(str(p) for p in passthrough)
    keep: dict[str, str] = {}
    dropped: list[str] = []
    token_ok = _uses_env_token(base)
    for name, value in base.items():
        if _env_match(name, ENV_NEVER):
            ok = False
        elif name.upper() == "GITHUB_TOKEN":
            ok = token_ok
        else:
            ok = _env_match(name, allow)
        if ok:
            keep[name] = value
        else:
            dropped.append(name)
    keep.update(NO_AUTO_UPDATE)      # the pinned CLI, never the newer one it downloads by itself
    return keep, sorted(dropped)


class CopilotSessionAdapter:
    """Thin wrapper: ``send_and_wait`` returns the final assistant text."""

    def __init__(self, session: Any) -> None:
        self._s = session
        self.session_id: str = session.session_id

    async def send_and_wait(self, prompt: str, timeout: float) -> str:
        event = await self._s.send_and_wait(prompt, timeout=timeout)
        if event is None:
            return ""
        return str(getattr(event.data, "content", "") or "")

    async def set_model(self, model: str) -> None:
        await self._s.set_model(model)

    async def disconnect(self) -> None:
        await self._s.disconnect()

    async def abort(self) -> None:
        """Abort the turn in flight (the session stays usable)."""
        await self._s.abort()

    @property
    def raw(self) -> Any:
        return self._s


class CopilotRuntime:
    name = "copilot"

    def __init__(self, cli_path: str | None, working_directory: str, log_level: str = "warning",
                 env: Mapping[str, str] | None = None, passthrough: Iterable[str] = ()) -> None:
        self.cli_path = cli_path
        self.working_directory = working_directory
        self.log_level = log_level
        # the allowlisted environment (copilot_env): the bridge's and HIL's tokens, GH_TOKEN
        # (it would outrank the stored Copilot login) and everything unlisted stay here
        self.env, self.env_dropped = copilot_env(os.environ if env is None else env, passthrough)
        # never let the SDK download or update a runtime behind our back
        self.env.setdefault("COPILOT_AUTO_UPDATE", "false")
        self.env.setdefault("COPILOT_SKIP_CLI_DOWNLOAD", "1")
        self.client: Any = None

    async def start(self) -> None:
        from copilot import CopilotClient, RuntimeConnection

        if not self.cli_path:
            raise RuntimeError("Copilot CLI not found: set copilot.cli_path in harness.yaml or COPILOT_CLI_PATH")
        self.client = CopilotClient(connection=RuntimeConnection.for_stdio(path=self.cli_path),
                                    working_directory=self.working_directory, log_level=self.log_level,
                                    env=self.env, use_logged_in_user=True)
        await self.client.start()

    async def stop(self) -> None:
        if self.client is not None:
            try:
                await self.client.stop()
            finally:
                self.client = None

    def _kwargs(self, spec: SessionSpec) -> dict[str, Any]:
        tool_names = list(dict.fromkeys(spec.role.tools))
        agent = {"name": spec.role.name, "display_name": f"harness {spec.role.name}",
                 "description": f"tinystream harness role {spec.role.name}", "prompt": spec.agent_prompt,
                 "tools": tool_names, "infer": False, "model": spec.model}
        if spec.role.reasoning_effort:
            agent["reasoning_effort"] = spec.role.reasoning_effort
        system_message: dict[str, Any] = {"mode": "customize", "content": spec.system_content}
        if spec.remove_sections:
            system_message["sections"] = {s: {"action": "remove"} for s in spec.remove_sections}
        kwargs: dict[str, Any] = {
            "on_permission_request": spec.permission_handler,
            "model": spec.model,
            "tools": [to_sdk_tool(t) for t in spec.tools],
            "available_tools": tool_names,
            "system_message": system_message,
            "hooks": spec.hooks,
            "working_directory": spec.working_directory,
            "custom_agents": [agent],
            "agent": spec.role.name,
            "streaming": False,
            "on_event": spec.on_event,
        }
        if spec.role.reasoning_effort:
            kwargs["reasoning_effort"] = spec.role.reasoning_effort
        if spec.skip_custom_instructions:
            kwargs["skip_custom_instructions"] = True
        if spec.mcp_servers:
            kwargs["mcp_servers"] = spec.mcp_servers
            kwargs["mcp_oauth_token_storage"] = "persistent"
        return kwargs

    async def create_session(self, spec: SessionSpec) -> CopilotSessionAdapter:
        kwargs = self._kwargs(spec)
        if spec.name:
            kwargs["name"] = spec.name[:100].replace('"', "'")
        return CopilotSessionAdapter(await self.client.create_session(**kwargs))

    async def resume_session(self, session_id: str, spec: SessionSpec) -> CopilotSessionAdapter:
        return CopilotSessionAdapter(await self.client.resume_session(session_id, **self._kwargs(spec)))

    async def call_mcp_tool(self, server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        """Execute one MCP tool deterministically (no prompt, no model call).

        A throw-away session gets only that MCP server; ``session.rpc.tools.execute``
        runs the tool through the runtime's native invocation pipeline.
        """
        from copilot.generated.rpc import ToolsExecuteRequest

        name = str(server.get("name", "mcp"))
        cfg = {k: v for k, v in server.items() if k != "name"}
        cfg.setdefault("tools", [tool])
        session = await self.client.create_session(
            on_permission_request=lambda req, inv: _only_this_mcp_tool(req, tool),
            mcp_servers={name: cfg}, available_tools=[f"{name}-{tool}", tool],
            mcp_oauth_token_storage="persistent", streaming=False)
        try:
            last: Exception | None = None
            for candidate in (f"{name}-{tool}", tool):
                try:
                    return await session.rpc.tools.execute(ToolsExecuteRequest(arguments=arguments, name=candidate))
                except Exception as exc:  # noqa: BLE001 - try the next naming scheme
                    last = exc
            raise RuntimeError(f"MCP tool {name}/{tool} failed: {last}")
        finally:
            await session.disconnect()

    # -- doctor helpers (no prompt is ever sent) ----------------------------------
    async def auth_status(self) -> Any:
        return await self.client.get_auth_status()

    async def list_models(self) -> list[Any]:
        return await self.client.list_models()


def _only_this_mcp_tool(request: Any, tool: str) -> Any:
    """The throw-away MCP session may call the one configured tool, nothing else."""
    from copilot.generated.rpc import PermissionDecisionApproveOnce, PermissionDecisionReject

    if getattr(request, "kind", None) == "mcp" and getattr(request, "tool_name", None) == tool:
        return PermissionDecisionApproveOnce()
    return PermissionDecisionReject(feedback=f"harness: only the MCP tool {tool} may run here")
