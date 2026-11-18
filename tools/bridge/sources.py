"""Trigger sources -> harness tickets.

A ``Source`` is one triggered work item (a GitHub issue labelled ``agent:fix``
or a Jira issue in a trigger status / with the trigger label) together with the
ticket markdown the harness reads (``# KEY: title``, the fields -- ``**Type:**``
first -- above the first section, then the sections; the harness sanitizes it and
hands it to a model only as untrusted data).

Type: a ``**Type:**`` line or an issue-form ``Type`` field in the body, else the
labels (bug / enhancement / task ...), else Bug for a GitHub ``agent:fix`` issue
and the issue type for Jira. The issue template
``.github/ISSUE_TEMPLATE/agent-fix.md`` has the agent-ready sections.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Optional

from jira_client import adf_to_markdown

_FIELD = re.compile(r"^\*\*(?P<name>[A-Za-z ]+):\*\*\s*(?P<value>.*?)\s*$")   # as harness/ticket.py
TYPE_FIELDS = ("type", "issue type", "ticket type")
NO_RESPONSE = "_No response_"          # what an issue form writes for an empty field
BUG_LABELS = ("bug", "type:bug", "kind/bug", "defect")
FEATURE_LABELS = ("enhancement", "feature", "type:feature", "kind/feature")
TASK_LABELS = ("task", "chore", "type:task", "kind/task")


@dataclass
class Source:
    id: str                         # state key: "gh#12" | "jira:PROJ-7"
    kind: str                       # "github" | "jira"
    key: str                        # ticket key the harness and the commit subject use: GH-12 | PROJ-7
    title: str
    url: str
    ticket_md: str
    issue_number: Optional[int] = None     # GitHub issue
    jira_key: Optional[str] = None
    trigger: str = ""               # what fired: "label:agent:fix" | "status:Agent Ready" | "label:agent"
    labels: list[str] = field(default_factory=list)

    @property
    def acceptance_tests(self) -> list[str]:
        """test_* names of the ticket's "Acceptance tests" section, as the harness reads them."""
        return acceptance_tests(self.ticket_md)


def _split_body(body: str) -> tuple[dict[str, str], str, str]:
    """(fields, description text, sections) of an issue body.

    fields: the ``**Name:** value`` lines above the first ``## `` heading, plus a ``**Type:**``
    line at the top of a section and an issue-form ``### Type`` field (heading + value); all of
    them are removed from the text. description: the other text above the first heading.
    sections: the rest, from the first ``## `` heading on. An issue form renders its fields as
    ``### Label``; without any ``## `` heading those become sections.
    """
    lines = body.split("\n")
    if not any(ln.startswith("## ") for ln in lines):
        lines = ["## " + ln[4:] if ln.startswith("### ") else ln for ln in lines]
    fields: dict[str, str] = {}
    out: list[str] = []
    i, in_sections, section_top, fence = 0, False, False, False
    while i < len(lines):
        line = lines[i]
        stripped = line.strip()
        if stripped.startswith("```") or fence:          # code block: text, never a field or heading
            fence ^= stripped.startswith("```")
            section_top = False
            out.append(line)
            i += 1
            continue
        if line.startswith("## "):
            name = line[3:].strip().lower()
            if name in TYPE_FIELDS:                       # issue form: "### Type" + value
                j = i + 1
                while j < len(lines) and not lines[j].strip():
                    j += 1
                value = lines[j].strip() if j < len(lines) and not lines[j].startswith("#") else ""
                if value and value != NO_RESPONSE:
                    fields.setdefault("type", value)
                i = j + 1 if value else j
                continue
            in_sections, section_top = True, True
            out.append(line)
            i += 1
            continue
        fm = _FIELD.match(stripped)
        name = fm.group("name").strip().lower() if fm else ""
        if fm and (not in_sections or (section_top and name in TYPE_FIELDS)):
            fields.setdefault("type" if name in TYPE_FIELDS else name, fm.group("value"))
            i += 1
            continue
        if stripped:
            section_top = False
        out.append(line)
        i += 1
    text = "\n".join(out)
    first = next((m.start() for m in re.finditer(r"^## ", text, re.M)), len(text))
    return fields, text[:first].strip(), text[first:].strip()


def ticket_markdown(key: str, title: str, body: str, issue_type: str, url: str) -> str:
    """The harness ticket: title line, fields (Type first), then the sections.

    The Type comes from the body (a ``**Type:**`` line or an issue-form ``Type`` field), else
    ``issue_type`` (labels / the Jira issue type / the default)."""
    title = " ".join(title.split()) or "(no title)"
    fields, description, rest = _split_body((body or "").replace("\r\n", "\n").strip())
    head = [f"# {key}: {title}", "", f"**Type:** {fields.pop('type', '') or issue_type}  "]
    fields.pop("source", None)
    head += [f"**{n[:1].upper() + n[1:]}:** {v}  " for n, v in fields.items()]
    head += [f"**Source:** {url}", ""]
    parts = []
    if description or not rest:
        parts.append("## Description\n\n" + (description or "(no description)"))
    if rest:
        parts.append(rest)
    return "\n".join(head) + "\n" + "\n\n".join(parts) + "\n"


def type_from_labels(labels: list[str], default: str = "Task") -> str:
    low = [x.lower() for x in labels]
    if any(x in BUG_LABELS for x in low):
        return "Bug"
    if any(x in FEATURE_LABELS for x in low):
        return "Feature"
    if any(x in TASK_LABELS for x in low):
        return "Task"
    return default


def acceptance_tests(ticket_md: str) -> list[str]:
    """The test_* names the harness will require (its own parser and rule)."""
    from harness.ticket import parse_markdown
    return parse_markdown(ticket_md, "file:bridge").required_tests


def from_github_issue(issue: dict[str, Any], key_prefix: str, trigger_label: str) -> Source:
    """An ``agent:fix`` issue: Bug unless a label (or the body's Type) says otherwise."""
    number = int(issue["number"])
    labels = [str(lb.get("name") if isinstance(lb, dict) else lb) for lb in issue.get("labels") or []]
    key = f"{key_prefix}-{number}"
    url = str(issue.get("html_url") or "")
    return Source(id=f"gh#{number}", kind="github", key=key, title=str(issue.get("title") or ""), url=url,
                  ticket_md=ticket_markdown(key, str(issue.get("title") or ""), str(issue.get("body") or ""),
                                            type_from_labels(labels, default="Bug"), url),
                  issue_number=number, trigger=f"label:{trigger_label}", labels=labels)


def from_jira_issue(issue: dict[str, Any], browse_url: str, trigger: str) -> Source:
    f = issue.get("fields") or {}
    key = str(issue["key"])
    issue_type = str((f.get("issuetype") or {}).get("name") or "Task")
    labels = [str(x) for x in f.get("labels") or []]
    return Source(id=f"jira:{key}", kind="jira", key=key, title=str(f.get("summary") or ""), url=browse_url,
                  ticket_md=ticket_markdown(key, str(f.get("summary") or ""), adf_to_markdown(f.get("description")),
                                            issue_type, browse_url),
                  jira_key=key, trigger=trigger, labels=labels)


def jira_trigger(issue: dict[str, Any], statuses: list[str], label: str) -> str:
    f = issue.get("fields") or {}
    status = str((f.get("status") or {}).get("name") or "")
    if any(status.lower() == s.lower() for s in statuses):
        return f"status:{status}"
    if label and label in (f.get("labels") or []):
        return f"label:{label}"
    return ""


def default_jql(project: str, statuses: list[str], label: str) -> str:
    parts = []
    if statuses:
        parts.append("status in (" + ", ".join(f'"{s}"' for s in statuses) + ")")
    if label:
        parts.append(f'labels = "{label}"')
    if not parts:
        raise ValueError("jira: set trigger_statuses and/or trigger_label (or an explicit jql)")
    scope = f'project = "{project}" AND ' if project else ""
    return f"{scope}statusCategory != Done AND ({' OR '.join(parts)}) ORDER BY updated ASC"
