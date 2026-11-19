"""The agent-ready ticket template and the "team" prompt overlay load as the harness reads them."""

from __future__ import annotations

from pathlib import Path

from harness.prompts import Prompts
from harness.ticket import parse_markdown

ROOT = Path(__file__).resolve().parents[2]
SECTIONS = ["Context", "Steps to reproduce", "Inputs", "Expected", "Actual",
            "Acceptance criteria", "Acceptance tests", "Out of scope", "Definition of done"]


def test_ticket_template_parses() -> None:
    t = parse_markdown((ROOT / "tickets" / "TEMPLATE.md").read_text(encoding="utf-8"), "file:TEMPLATE.md")
    assert t.key == "KEY-123"
    assert {"type", "component", "priority"} <= set(t.fields)
    assert list(t.sections) == SECTIONS
    assert t.required_tests == []      # placeholders only: no test name until the section is filled in


def test_team_overlay_replaces_only_implement() -> None:
    team = Prompts(ROOT / "harness" / "prompts", "team")
    base = Prompts(ROOT / "harness" / "prompts")
    out = team.render("implement", flags={"run_gates", "gates"}, ticket_key="KEY-1",
                      ticket_title="t", ticket="T", plan="P", rework="", gates="fast", budget=3)
    assert "Acceptance criteria" in out and "run_gates (profile fast)" in out
    assert team.source("plan") == base.source("plan")


def test_team_overlay_without_gates_never_mentions_them() -> None:
    out = Prompts(ROOT / "harness" / "prompts", "team").render(
        "implement", ticket_key="KEY-1", ticket_title="t", ticket="T", plan="P", rework="")
    assert "run_gates" not in out and "gate-checked" not in out
