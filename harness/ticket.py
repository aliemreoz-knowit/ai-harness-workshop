"""Tickets: a local markdown file or a Jira issue read through the Jira MCP server.

A local ticket follows the agent-ready template::

    # KEY-123: short title
    **Type:** Bug
    ## Description
    ...
    ## Steps to reproduce
    ...
    ## Acceptance tests
    - test_txq_index_and_seq_wrap

The ``**Name:** value`` lines above the first section are the ticket's fields
(``type`` decides whether a bug fix must come with a regression test). Older
bridge tickets put them at the top of the first section (``## Description``,
then ``**Type:** Bug``): field lines at the top of a section are read as fields
too (a field above the sections wins), and a section heading that appears twice
keeps both bodies.

The test_* names under "Acceptance tests" (or "Required tests") are the tests a
change must make pass; the baseline rule of the test gates (``baseline.py``)
requires them instead of "any progress" when the branch already has failures.

Whatever the source, the body is sanitized (``sanitize.py``) and only ever
handed to a model inside an ``<untrusted-data>`` block.
"""

from __future__ import annotations

import json
import re
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any

from . import sanitize as san

_TITLE = re.compile(r"^#\s+(?P<key>[A-Z][A-Z0-9]+-\d+)\s*[:\-\u2013\u2014]\s*(?P<title>.+?)\s*$")
_FIELD = re.compile(r"^\*\*(?P<name>[A-Za-z ]+):\*\*\s*(?P<value>.*?)\s*$")


class TicketError(ValueError):
    pass


@dataclass
class Ticket:
    key: str
    title: str
    body: str                       # sanitized body (without the title line)
    source: str                     # "file:<path>" | "jira:<key>"
    fields: dict[str, str] = field(default_factory=dict)
    sections: dict[str, str] = field(default_factory=dict)
    flags: list[str] = field(default_factory=list)
    findings: list[dict[str, str]] = field(default_factory=list)
    truncated: bool = False

    def as_prompt(self, section: str | None = None) -> str:
        """The ticket as a model may see it: wrapped, flagged, never as instructions."""
        if section:
            want = section.strip().lower()
            body = next((v for k, v in self.sections.items() if k.lower() == want), None)
            if body is None:
                return f"(no section {section!r}; sections: {', '.join(self.sections) or 'none'})"
        else:
            body = self.body
        head = f"{self.key}: {self.title}"
        return san.wrap(f"{head}\n\n{body}".strip(), self.source, self.flags)

    def to_dict(self) -> dict[str, Any]:
        return asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> Ticket:
        return cls(**data)

    @property
    def key_lower(self) -> str:
        return self.key.lower()

    @property
    def required_tests(self) -> list[str]:
        """test_* names of the "Acceptance tests" / "Required tests" section (baseline rule)."""
        from .baseline import required_tests
        return required_tests(self.sections)


def parse_markdown(text: str, source: str, max_chars: int = 12000, key_hint: str | None = None) -> Ticket:
    s = san.sanitize(text, max_chars)
    lines = s.text.split("\n")
    key, title, start = key_hint, "", 0
    for i, line in enumerate(lines):
        if not line.strip():
            continue
        m = _TITLE.match(line.strip())
        if m:
            key, title, start = m.group("key"), m.group("title"), i + 1
        elif line.startswith("# "):
            title, start = line[2:].strip(), i + 1
        break
    if not key:
        raise TicketError(f"{source}: no ticket key; the first line must be '# KEY-123: title'")
    body = "\n".join(lines[start:]).strip()
    fields: dict[str, str] = {}
    sections: dict[str, str] = {}
    lifted: dict[str, str] = {}     # field lines at the top of a section (older ticket form)
    current: str | None = None
    buf: list[str] = []

    def close() -> None:
        if current is None:
            return
        text = _lift_fields(buf, lifted)
        old = sections.get(current, "")
        sections[current] = f"{old}\n\n{text}" if old and text else (text or old)

    for line in body.split("\n"):
        fm = _FIELD.match(line.strip())
        if fm and current is None:
            fields[fm.group("name").strip().lower()] = fm.group("value")
            continue
        if line.startswith("## "):
            close()
            current, buf = line[3:].strip(), []
            continue
        if current is not None:
            buf.append(line)
    close()
    for name, value in lifted.items():
        fields.setdefault(name, value)
    return Ticket(key=key, title=title or key, body=body, source=source, fields=fields,
                  sections=sections, flags=s.flags, findings=s.findings, truncated=s.truncated)


def _lift_fields(lines: list[str], into: dict[str, str]) -> str:
    """The section text; its leading ``**Name:** value`` lines also go to ``into`` (the first
    value of a name wins). The text keeps them: a section may consist of such lines only."""
    for line in lines:
        stripped = line.strip()
        fm = _FIELD.match(stripped)
        if fm:
            into.setdefault(fm.group("name").strip().lower(), fm.group("value"))
        elif stripped:
            break
    return "\n".join(lines).strip()


def load_file(path: Path, max_chars: int = 12000) -> Ticket:
    try:
        text = path.read_text(encoding="utf-8")
    except OSError as exc:
        raise TicketError(f"cannot read ticket {path}: {exc}") from None
    return parse_markdown(text, source=f"file:{path.name}", max_chars=max_chars)


def is_jira_key(ref: str, pattern: str = r"^[A-Z][A-Z0-9]+-[0-9]+$") -> bool:
    return re.match(pattern, ref) is not None


def from_jira_payload(key: str, payload: Any, max_chars: int = 12000) -> Ticket:
    """Build a Ticket from a Jira MCP ``getJiraIssue`` result (JSON text or dict)."""
    data: Any = payload
    if isinstance(payload, str):
        try:
            data = json.loads(payload)
        except ValueError:
            data = {"fields": {"summary": key, "description": payload}}
    if isinstance(data, dict) and "content" in data and isinstance(data["content"], list):
        # MCP CallToolResult: [{"type": "text", "text": "..."}]
        texts = [c.get("text", "") for c in data["content"] if isinstance(c, dict)]
        return from_jira_payload(key, "\n".join(texts), max_chars)
    fields = (data or {}).get("fields", {}) if isinstance(data, dict) else {}
    summary = str(fields.get("summary") or key)
    description = fields.get("description") or ""
    if isinstance(description, dict):  # Atlassian Document Format
        description = _adf_to_text(description)
    issue_type = (fields.get("issuetype") or {}).get("name", "") if isinstance(fields.get("issuetype"), dict) else ""
    md = f"# {key}: {summary}\n"
    if issue_type:
        md += f"**Type:** {issue_type}\n"
    md += "\n" + str(description)
    return parse_markdown(md, source=f"jira:{key}", max_chars=max_chars, key_hint=key)


def _adf_to_text(node: Any) -> str:
    if isinstance(node, dict):
        if node.get("type") == "text":
            return str(node.get("text", ""))
        inner = "".join(_adf_to_text(c) for c in node.get("content", []) or [])
        if node.get("type") == "heading":
            level = int((node.get("attrs") or {}).get("level", 2))
            return "\n" + "#" * max(2, level) + " " + inner + "\n"
        if node.get("type") in ("paragraph", "listItem", "codeBlock"):
            return inner + "\n"
        return inner
    if isinstance(node, list):
        return "".join(_adf_to_text(c) for c in node)
    return ""
