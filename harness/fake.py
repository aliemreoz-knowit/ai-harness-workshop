"""A scripted stand-in for the Copilot runtime: no model, no network, no credits.

Each role gets a queue of ``Turn``s. A turn performs ``Act``s the way a model
would (tool calls go through the SAME pre-tool-use hook, permission handler,
custom tools and post-tool-use hook as in a real session), then emits usage
events and a final assistant message. When the SDK is importable the events
are real ``copilot.session_events.SessionEvent`` objects built with
``SessionEvent.from_dict``, so the audit and credit code is exercised against
the SDK's own types.

Used by the unit tests and by ``python -m harness run --dry-run``.
"""

from __future__ import annotations

import datetime as _dt
import inspect
import uuid
from dataclasses import dataclass, field
from pathlib import Path
from types import SimpleNamespace
from typing import Any, Callable

from .policy import unquoted
from .runtime import SessionSpec


@dataclass
class Act:
    tool: str
    args: dict[str, Any] = field(default_factory=dict)


def edit(path: str, content: str) -> Act:
    return Act("edit", {"path": path, "content": content})


def call(tool: str, **args: Any) -> Act:
    return Act(tool, args)


def shell(command: str) -> Act:
    return Act("bash", {"command": command})


@dataclass
class Turn:
    reply: str = "done"
    acts: list[Act] = field(default_factory=list)
    nano_aiu: float = 2e8           # 0.2 AI credits per turn
    input_tokens: int = 12000
    output_tokens: int = 400
    cache_read_tokens: int = 8000
    model: str = "fake-model"
    error: Exception | None = None


TurnSource = Turn | Callable[[str, "FakeSession"], Turn]


def _ts() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat()


def make_event(etype: str, data: dict[str, Any], agent_id: str | None = None) -> Any:
    """A real SDK SessionEvent when the SDK is installed, else an attribute-compatible stand-in."""
    wire = {"type": etype, "id": str(uuid.uuid4()), "timestamp": _ts(), "data": data}
    if agent_id:
        wire["agentId"] = agent_id
    try:
        from copilot.session_events import SessionEvent
        return SessionEvent.from_dict(wire)
    except ImportError:
        pass
    return SimpleNamespace(type=SimpleNamespace(value=etype), data=_snake(data), agent_id=agent_id)


def _snake(d: Any) -> Any:
    if isinstance(d, dict):
        out = {}
        for k, v in d.items():
            sk = "".join("_" + c.lower() if c.isupper() else c for c in k)
            out[sk] = _snake(v)
        return SimpleNamespace(**out)
    return d


def _approved(decision: Any) -> tuple[bool, str]:
    kind = getattr(decision, "kind", None)
    if kind is None and isinstance(decision, dict):
        kind = decision.get("kind")
    feedback = getattr(decision, "feedback", None) or (decision.get("feedback") if isinstance(decision, dict) else "")
    return kind in ("approve-once", "approve-for-session", "approved"), str(feedback or "")


class FakeSession:
    def __init__(self, runtime: FakeRuntime, spec: SessionSpec, session_id: str) -> None:
        self.runtime = runtime
        self.spec = spec
        self.session_id = session_id
        self.prompts: list[str] = []
        self.tool_log: list[dict[str, Any]] = []
        self.models: list[str] = [spec.model]
        self._nano_total = 0.0
        self.disconnected = False
        self.aborts = 0

    @property
    def role(self) -> str:
        return self.spec.role.name

    def _emit(self, etype: str, data: dict[str, Any]) -> None:
        if self.spec.on_event:
            self.spec.on_event(make_event(etype, data))

    async def send_and_wait(self, prompt: str, timeout: float) -> str:
        if self.disconnected:
            raise RuntimeError("Session is disconnected")
        self.prompts.append(prompt)
        self.runtime.prompts.append((self.role, prompt))
        src = self.runtime.next_turn(self.role)
        turn = src(prompt, self) if callable(src) else src
        if inspect.isawaitable(turn):
            turn = await turn
        if turn.error is not None:
            raise turn.error
        for act in turn.acts:
            await self._act(act)
        self._nano_total += turn.nano_aiu
        self._emit("assistant.usage", {"model": self.models[-1] or turn.model, "inputTokens": turn.input_tokens,
                                       "outputTokens": turn.output_tokens, "cacheReadTokens": turn.cache_read_tokens,
                                       "copilotUsage": {"totalNanoAiu": turn.nano_aiu}})
        self._emit("assistant.message", {"content": turn.reply, "messageId": str(uuid.uuid4())})
        self._emit("session.usage_checkpoint", {"totalNanoAiu": self._nano_total})
        self._emit("session.idle", {})
        return turn.reply

    async def _act(self, act: Act) -> None:
        call_id = "call-" + uuid.uuid4().hex[:8]
        name, args = act.tool, dict(act.args)
        self._emit("tool.execution_start", {"toolCallId": call_id, "toolName": name, "arguments": args})
        ok, result = await self._run_tool(name, args)
        entry = {"tool": name, "args": args, "ok": ok, "result": result}
        self.tool_log.append(entry)
        self.runtime.tool_log.append({"role": self.role, **entry})
        data: dict[str, Any] = {"toolCallId": call_id, "success": ok}
        if ok:
            data["result"] = {"content": str(result)}
        else:
            data["error"] = {"message": str(result)}
        self._emit("tool.execution_complete", data)

    async def _hook(self, name: str, payload: dict[str, Any]) -> Any:
        fn = (self.spec.hooks or {}).get(name)
        if not fn:
            return None
        out = fn(payload, {"session_id": self.session_id})
        if inspect.isawaitable(out):
            out = await out
        return out

    async def _permission(self, request: Any) -> tuple[bool, str]:
        dec = self.spec.permission_handler(request, {"session_id": self.session_id})
        if inspect.isawaitable(dec):
            dec = await dec
        return _approved(dec)

    async def _run_tool(self, name: str, args: dict[str, Any]) -> tuple[bool, str]:
        offered = set(self.spec.role.tools)
        is_mcp = "-" in name
        if not is_mcp and name not in offered:
            return False, f"tool {name} does not exist in this session"
        pre = await self._hook("on_pre_tool_use", {"toolName": name, "toolArgs": args,
                                                    "sessionId": self.session_id, "timestamp": _ts(),
                                                    "workingDirectory": self.spec.working_directory})
        if isinstance(pre, dict) and pre.get("permissionDecision") == "deny":
            return False, f"denied by hook: {pre.get('permissionDecisionReason', '')}"
        custom = {t.name: t for t in self.spec.tools}
        if name in custom:
            ok, why = await self._permission(SimpleNamespace(kind="custom-tool", tool_name=name,
                                                             tool_description="", args=args))
            if not ok:
                return False, f"permission denied: {why}"
            try:
                result: Any = custom[name].call(args)
                if inspect.isawaitable(result):
                    result = await result
            except Exception as exc:  # noqa: BLE001 - like define_tool's wrapper
                return False, f"tool error: {exc}"
        elif name in ("edit", "create"):
            path = str(args.get("path", ""))
            ok, why = await self._permission(SimpleNamespace(kind="write", file_name=path, diff="",
                                                             intention="edit", can_offer_session_approval=False))
            if not ok:
                return False, f"permission denied: {why}"
            target = Path(path) if Path(path).is_absolute() else Path(self.spec.working_directory) / path
            target.parent.mkdir(parents=True, exist_ok=True)
            target.write_text(str(args.get("content", "")), encoding="utf-8", newline="\n")
            result = f"wrote {path}"
        elif name == "bash":
            cmd = str(args.get("command", ""))
            # the CLI's own flag comes from its shell parser: a > inside quotes is not a redirection
            ok, why = await self._permission(SimpleNamespace(
                kind="shell", full_command_text=cmd, has_write_file_redirection=">" in unquoted(cmd),
                possible_urls=[], commands=[], intention="run", possible_paths=[], can_offer_session_approval=False))
            if not ok:
                return False, f"permission denied: {why}"
            result = f"(fake shell output of {cmd!r})"
        elif is_mcp:
            server, _, tool = name.partition("-")
            ok, why = await self._permission(SimpleNamespace(kind="mcp", server_name=server, tool_name=tool,
                                                             tool_title=tool, read_only=bool(args.pop("_read_only", True)),
                                                             args=args))
            if not ok:
                return False, f"permission denied: {why}"
            result = self.runtime.mcp_results.get(tool, "")
        else:
            result = f"(fake {name} result)"
        post = await self._hook("on_post_tool_use", {"toolName": name, "toolArgs": args, "toolResult": result,
                                                      "sessionId": self.session_id, "timestamp": _ts(),
                                                      "workingDirectory": self.spec.working_directory})
        if isinstance(post, dict) and "modifiedResult" in post:
            result = post["modifiedResult"]
        return True, str(result)

    async def set_model(self, model: str) -> None:
        self.models.append(model)
        self.runtime.model_switches.append((self.role, model))

    async def disconnect(self) -> None:
        self.disconnected = True

    async def abort(self) -> None:
        self.aborts += 1


class FakeRuntime:
    name = "fake"

    def __init__(self, scripts: dict[str, list[TurnSource]] | None = None,
                 mcp_results: dict[str, Any] | None = None, default_reply: str = "(no scripted turn)") -> None:
        self.scripts = {k: list(v) for k, v in (scripts or {}).items()}
        self.mcp_results = dict(mcp_results or {})
        self.default_reply = default_reply
        self.sessions: list[FakeSession] = []
        self.prompts: list[tuple[str, str]] = []
        self.tool_log: list[dict[str, Any]] = []
        self.model_switches: list[tuple[str, str]] = []
        self.resumed: list[str] = []
        self.mcp_calls: list[tuple[str, str, dict[str, Any]]] = []
        self.started = False
        self.stopped = False

    def next_turn(self, role: str) -> TurnSource:
        queue = self.scripts.get(role) or []
        if queue:
            return queue.pop(0)
        return Turn(reply=self.default_reply, nano_aiu=0)

    async def start(self) -> None:
        self.started = True

    async def stop(self) -> None:
        self.stopped = True

    async def create_session(self, spec: SessionSpec) -> FakeSession:
        s = FakeSession(self, spec, f"fake-{spec.role.name}-{len(self.sessions) + 1}")
        self.sessions.append(s)
        return s

    async def resume_session(self, session_id: str, spec: SessionSpec) -> FakeSession:
        self.resumed.append(session_id)
        s = FakeSession(self, spec, session_id)
        self.sessions.append(s)
        return s

    async def call_mcp_tool(self, server: dict[str, Any], tool: str, arguments: dict[str, Any]) -> Any:
        self.mcp_calls.append((str(server.get("name")), tool, arguments))
        if tool not in self.mcp_results:
            raise RuntimeError(f"fake MCP: no result for {tool}")
        return self.mcp_results[tool]

    def sessions_for(self, role: str) -> list[FakeSession]:
        return [s for s in self.sessions if s.role == role]
