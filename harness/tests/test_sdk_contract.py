"""The harness against the REAL github-copilot-sdk 1.0.23 types -- without starting the CLI.

These tests pin the API surface the harness relies on: create_session /
resume_session keyword arguments, define_tool, the event classes used for the
audit trail and the credit accounting.
"""

import asyncio
import inspect
from pathlib import Path

import pytest

copilot = pytest.importorskip("copilot")

from harness.audit import Audit, Usage, event_type  # noqa: E402
from harness.config import HarnessConfig  # noqa: E402
from harness.fake import make_event  # noqa: E402
from harness.policy import RolePolicy  # noqa: E402
from harness.runtime import CopilotRuntime, SessionSpec  # noqa: E402
from harness.tools import make_tools, to_sdk_tool  # noqa: E402


def spec_for(cfg: HarnessConfig, role: str) -> SessionSpec:
    rc = cfg.role(role)
    pol = RolePolicy(rc, cfg.policy, cfg.repo)
    tools = make_tools({"write_review": lambda a: f"ok {a['verdict']}", "read_ticket": lambda a: "t",
                        "run_gates": lambda a: "GATES PASS", "write_plan": lambda a: "saved"}, rc.custom_tools)
    return SessionSpec(role=rc, model=rc.model, agent_prompt="role prompt", system_content="rules", tools=tools,
                       hooks=pol.hooks(), permission_handler=pol.permission_handler(), max_ai_credits=3.5,
                       working_directory=str(cfg.repo), remove_sections=["tone"], name='run "x" reviewer')


def test_session_kwargs_match_the_sdk_signatures(cfg: HarnessConfig) -> None:
    rt = CopilotRuntime("copilot", str(cfg.repo))
    kwargs = rt._kwargs(spec_for(cfg, "implementer"))
    create = inspect.signature(copilot.CopilotClient.create_session).parameters
    resume = inspect.signature(copilot.CopilotClient.resume_session).parameters
    for k in kwargs:
        assert k in create, f"create_session has no {k}"
        assert k in resume, f"resume_session has no {k}"
    # no runtime session limit: with one, the model is told its remaining credits and to be
    # frugal; the harness caps are enforced by the orchestrator's credit guard instead
    assert "session_limits" not in kwargs
    big = spec_for(cfg, "implementer")
    big.max_ai_credits = 45.0
    assert "session_limits" not in rt._kwargs(big)
    assert kwargs["agent"] == "implementer"
    agent = kwargs["custom_agents"][0]
    assert agent["name"] == "implementer" and agent["model"] == cfg.role("implementer").model
    assert set(agent) <= set(copilot.session.CustomAgentConfig.__annotations__)
    assert kwargs["available_tools"] == cfg.role("implementer").tools
    assert set(kwargs["hooks"]) <= set(copilot.SessionHooks.__annotations__)
    assert kwargs["system_message"]["mode"] == "customize"
    assert kwargs["system_message"]["sections"] == {"tone": {"action": "remove"}}
    assert {t.name for t in kwargs["tools"]} == {"read_ticket", "run_gates"}
    assert all(isinstance(t, copilot.Tool) for t in kwargs["tools"])


def test_define_tool_wrapper_validates_and_runs(cfg: HarnessConfig) -> None:
    spec = spec_for(cfg, "reviewer")
    (review,) = [t for t in spec.tools if t.name == "write_review"]
    tool = to_sdk_tool(review)
    assert tool.parameters["properties"]["verdict"]["enum"] == ["approve", "reject"]
    ok = asyncio.run(tool.handler(copilot.ToolInvocation(arguments={"verdict": "approve", "score": 4,
                                                                    "summary": "fine"})))
    assert ok.result_type == "success" and ok.text_result_for_llm == "ok approve"
    bad = asyncio.run(tool.handler(copilot.ToolInvocation(arguments={"verdict": "maybe", "score": 9,
                                                                     "summary": "?"})))
    assert bad.result_type == "failure" and "verdict" in bad.text_result_for_llm


def test_async_tool_handlers_are_awaited(cfg: HarnessConfig) -> None:
    async def slow(args: dict) -> str:
        await asyncio.sleep(0)
        return "GATES PASS (unit-tests:PASS)"
    (spec,) = make_tools({"run_gates": slow}, ["run_gates"])
    res = asyncio.run(to_sdk_tool(spec).handler(copilot.ToolInvocation(arguments={})))
    assert res.text_result_for_llm == "GATES PASS (unit-tests:PASS)"


def test_usage_from_sdk_events(tmp_path: Path) -> None:
    usage = Usage()
    events = [
        make_event("assistant.usage", {"model": "claude-haiku-5.5", "inputTokens": 1000, "outputTokens": 50,
                                       "cacheReadTokens": 800, "copilotUsage": {"totalNanoAiu": 3e8}}),
        make_event("assistant.usage", {"model": "claude-haiku-5.5", "inputTokens": 500, "outputTokens": 20,
                                       "copilotUsage": {"totalNanoAiu": 2e8}}),
        make_event("session.usage_checkpoint", {"totalNanoAiu": 6e8}),
    ]
    assert all(isinstance(e, copilot.session_events.SessionEvent) for e in events)
    assert event_type(events[0]) == "assistant.usage"
    for e in events:
        usage.observe("s1", "implementer", "implement", e)
    assert usage.credits == pytest.approx(0.6)            # checkpoint (cumulative) wins over the call sum
    assert usage.tokens()["input_tokens"] == 1500 and usage.tokens()["cache_read_tokens"] == 800
    assert usage.by_role() == {"implementer": pytest.approx(0.6)}
    audit = Audit(tmp_path / "run")
    audit.sdk_event("implement", "implementer", "s1", events[0])
    (rec,) = audit.read_events()
    assert rec["kind"] == "sdk.assistant.usage"
    assert rec["data"]["copilotUsage"]["totalNanoAiu"] == 3e8        # SDK to_dict(): wire format
    restored = Usage(usage.to_dict())
    assert restored.credits == pytest.approx(0.6)


def test_skip_custom_instructions_is_an_sdk_keyword(cfg: HarnessConfig) -> None:
    import dataclasses
    rt = CopilotRuntime("copilot", str(cfg.repo))
    spec = dataclasses.replace(spec_for(cfg, "implementer"), skip_custom_instructions=True)
    kwargs = rt._kwargs(spec)
    assert kwargs["skip_custom_instructions"] is True
    assert "skip_custom_instructions" in inspect.signature(copilot.CopilotClient.create_session).parameters
    assert "skip_custom_instructions" in inspect.signature(copilot.CopilotClient.resume_session).parameters
