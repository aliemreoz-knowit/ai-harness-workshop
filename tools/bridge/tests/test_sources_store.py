"""Sources -> harness tickets; bridge state and the HIL queue file."""

from __future__ import annotations

import json
from pathlib import Path

import pytest
from bridge_testlib import FIXTURES, REPO

import sources
from harness.ticket import parse_markdown
from store import HilQueue, State


def _gh_issue() -> dict:
    data = json.loads((FIXTURES / "github.json").read_text(encoding="utf-8"))
    return next(i for i in data["interactions"] if "labels=agent%3Afix" in i["url"])["body"][0]


def _jira_issue() -> dict:
    data = json.loads((FIXTURES / "jira.json").read_text(encoding="utf-8"))
    return next(i for i in data["interactions"] if "search/jql" in i["url"])["body"]["issues"][0]


def test_github_issue_becomes_a_harness_ticket() -> None:
    src = sources.from_github_issue(_gh_issue(), "GH", "agent:fix")
    assert (src.id, src.key, src.issue_number, src.trigger) == ("gh#12", "GH-12", 12, "label:agent:fix")
    t = parse_markdown(src.ticket_md, "file:test")             # the harness's own parser
    assert t.key == "GH-12"
    assert t.title.startswith("Board shows a stale frame counter")
    assert t.fields.get("type") == "Bug"                        # from the 'bug' label
    assert "Steps to reproduce" in t.sections


def test_issue_type_from_labels_and_existing_type_line_wins() -> None:
    issue = {"number": 3, "title": "x", "labels": [{"name": "enhancement"}], "body": "plain text"}
    md = sources.from_github_issue(issue, "GH", "agent:fix").ticket_md
    assert "**Type:** Feature" in md and "## Description\n\nplain text" in md
    issue = {"number": 4, "title": "x", "labels": [], "body": "**Type:** Bug\n\nbody"}
    md = sources.from_github_issue(issue, "GH", "agent:fix").ticket_md
    assert md.count("**Type:**") == 1
    md = sources.from_github_issue({"number": 5, "title": "x"}, "GH", "l").ticket_md
    assert "**Type:** Bug" in md                                # agent:fix issue without a type label
    md = sources.from_github_issue({"number": 6, "title": "x", "labels": ["task"]}, "GH", "l").ticket_md
    assert "**Type:** Task" in md
    issue = {"number": 7, "title": "x", "labels": [{"name": "bug"}], "body": "**Type:** Feature\n\nbody"}
    assert "**Type:** Feature" in sources.from_github_issue(issue, "GH", "l").ticket_md   # the body wins


# the shape of a real issue body: field lines first, then the sections
FIELDS_FIRST = ("**Type:** Bug  \n**Component:** daemon / receiver  \n**Reported by:** stream operator\n\n"
                "## Description\n\nThe board keeps an old counter.\n\n"
                "## Steps to reproduce\n\n1. Restart the daemon\n2. Relay scenario:\n   ```\n"
                "   **Type:** not a field\n   ## not a heading\n   ```\n")


def _ticket(body: str, labels: list | None = None) -> tuple[str, object]:
    issue = {"number": 1, "title": "Board keeps an old counter", "labels": labels or [], "body": body,
             "html_url": "https://github.com/acme/tinystream/issues/1"}
    md = sources.from_github_issue(issue, "GH", "agent:fix").ticket_md
    return md, parse_markdown(md, "file:test")


def test_issue_fields_go_above_the_sections_where_the_harness_reads_them() -> None:
    md, t = _ticket(FIELDS_FIRST, labels=["enhancement"])
    assert t.fields == {"type": "Bug", "component": "daemon / receiver", "reported by": "stream operator",
                        "source": "https://github.com/acme/tinystream/issues/1"}
    assert md.count("## Description") == 1 and md.count("**Type:**") == 2      # + the one in the code block
    assert md.index("**Component:**") < md.index("**Source:**") < md.index("## Description")
    assert t.sections["Description"] == "The board keeps an old counter."
    assert list(t.sections) == ["Description", "Steps to reproduce"]
    assert "## not a heading" in t.sections["Steps to reproduce"]


def test_type_line_at_the_top_of_a_section_and_free_text_before_the_sections() -> None:
    md, t = _ticket("## Description\n\n**Type:** Feature\n\nAdd a counter.\n\n## Notes\n\nx\n**Type:** stays\n")
    assert t.fields["type"] == "Feature" and t.sections["Description"] == "Add a counter."
    assert "**Type:** stays" in t.sections["Notes"]             # only the top of a section is a field
    md, t = _ticket("**Priority:** High\n\nIt freezes.\n\n## Expected\n\n30 fps\n")
    assert (t.fields["type"], t.fields["priority"]) == ("Bug", "High")
    assert t.sections == {"Description": "It freezes.", "Expected": "30 fps"}


def test_issue_form_fields_become_type_and_sections() -> None:
    body = ("### Type\n\nFeature\n\n### Description\n\nAdd a counter.\n\n"
            "### Acceptance tests\n\n- test_board_shows_fps\n\n### Out of scope\n\n_No response_\n")
    md, t = _ticket(body)
    assert t.fields["type"] == "Feature"
    assert list(t.sections) == ["Description", "Acceptance tests", "Out of scope"]
    assert t.required_tests == ["test_board_shows_fps"]
    md, t = _ticket("### Type\n\n_No response_\n\n### Description\n\nx\n", labels=["enhancement"])
    assert t.fields["type"] == "Feature"                         # empty form field: the label decides


def test_acceptance_tests_as_the_harness_reads_them() -> None:
    src = sources.from_github_issue({"number": 2, "title": "x", "body": FIELDS_FIRST}, "GH", "agent:fix")
    assert src.acceptance_tests == []
    body = FIELDS_FIRST + "\n## Acceptance tests\n\n- test_board_counter_after_restart\n- test_board_counter_reset\n"
    src = sources.from_github_issue({"number": 2, "title": "x", "body": body}, "GH", "agent:fix")
    assert src.acceptance_tests == ["test_board_counter_after_restart", "test_board_counter_reset"]


def test_the_issue_template_is_an_agent_ready_ticket() -> None:
    text = (REPO / ".github" / "ISSUE_TEMPLATE" / "agent-fix.md").read_text(encoding="utf-8")
    front, body = text.split("---\n", 2)[1:]                   # GitHub strips the front matter
    assert "name: Agent-ready fix" in front and "agent:fix" not in front.split("labels:")[1]
    md, t = _ticket(body)
    assert t.fields["type"] == "Bug" and t.flags == []         # no hidden comment, nothing flagged
    assert list(t.sections) == ["Description", "Steps to reproduce", "Expected", "Actual", "Environment",
                                "Acceptance criteria", "Acceptance tests", "Out of scope"]
    assert t.required_tests == []                               # the placeholder names no test
    filled = body.replace("- one test per line", "- test_board_counter_after_restart\n- one test per line")
    assert _ticket(filled)[1].required_tests == ["test_board_counter_after_restart"]


def test_jira_issue_becomes_a_harness_ticket() -> None:
    issue = _jira_issue()
    trig = sources.jira_trigger(issue, ["agent ready"], "agent")
    assert trig == "status:Agent Ready"
    src = sources.from_jira_issue(issue, "https://acme.atlassian.net/browse/PROJ-7", trig)
    t = parse_markdown(src.ticket_md, "file:test")
    assert (t.key, t.fields.get("type")) == ("PROJ-7", "Bug")
    assert "Pressing `k` after a reconnect" in t.body
    assert sources.jira_trigger({"fields": {"labels": ["agent"], "status": {"name": "To Do"}}}, [], "agent") == "label:agent"


def test_default_jql() -> None:
    jql = sources.default_jql("PROJ", ["Agent Ready"], "agent")
    assert jql == ('project = "PROJ" AND statusCategory != Done AND (status in ("Agent Ready") OR labels = "agent") '
                   "ORDER BY updated ASC")
    with pytest.raises(ValueError):
        sources.default_jql("PROJ", [], "")


def test_state_rearms_items_that_stopped_matching(tmp_path: Path) -> None:
    st = State(tmp_path / "s" / "state.json")
    st.update("gh#1", kind="github", status="done")
    st.update("gh#2", kind="github", status="running")
    st.update("jira:A-1", kind="jira", status="failed")
    assert st.rearm("github", set()) == ["gh#1"]               # a run in progress is never re-armed
    assert State(tmp_path / "s" / "state.json").handled("jira:A-1")   # persisted
    assert not st.handled("gh#1")


def test_items_left_running_past_the_timeout_are_rearmed(tmp_path: Path) -> None:
    import datetime as dt
    st = State(tmp_path / "state.json")
    st.update("gh#2", kind="github", status="running")
    st.update("gh#3", kind="github", status="running")
    st.update("gh#4", kind="github", status="done")
    old = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=7200)).isoformat(timespec="seconds")
    st.items["gh#3"]["updated"] = old                           # the bridge died two hours ago
    st.items["gh#4"]["updated"] = old                           # done is done, however old
    assert st.stale("github", 5400) == ["gh#3"]
    assert st.stale("github", None) == []
    # still labelled (matching) or not: a run that never ended is not handled
    assert st.rearm("github", {"gh#2", "gh#3", "gh#4"}, stale_s=5400) == ["gh#3"]
    assert st.handled("gh#2") and st.handled("gh#4") and not st.handled("gh#3")
    assert not State(tmp_path / "state.json").handled("gh#3")


def test_pr_sections_are_kept_per_pr(tmp_path: Path) -> None:
    st = State(tmp_path / "state.json")
    st.pr_section(40, "agent", "A")
    assert st.pr_section(40, "hil", "H") == {"agent": "A", "hil": "H"}


def test_hil_queue_fold_and_position(tmp_path: Path) -> None:
    q = HilQueue(tmp_path / "hil" / "queue.jsonl")
    a = q.enqueue(pr=31, sha="a" * 40, branch="agent/a")
    b = q.enqueue(pr=32, sha="b" * 40, branch="agent/b")
    assert (a["id"], q.position(b["id"])) == ("pr31-aaaaaaa", 2)
    q.update(a["id"], status="running", started_by="hil.py")
    assert q.position(b["id"]) == 1
    assert q.find(31, "a" * 40)["status"] == "running"
    assert q.find(31, "c" * 40) is None
    (q.path).open("a", encoding="utf-8").write("not json\n")   # a torn line is skipped
    assert len(q.entries()) == 2
