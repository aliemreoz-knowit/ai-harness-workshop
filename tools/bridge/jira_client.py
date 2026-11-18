"""Small Jira Cloud REST v3 client for the bridge (search, comments, labels, transitions).

Auth: basic auth with ``JIRA_EMAIL`` + ``JIRA_API_TOKEN`` (an Atlassian API
token of the facilitator account) on ``JIRA_SITE`` (https://<site>.atlassian.net).
The harness itself reads tickets through the read-only Atlassian MCP server;
the bridge only needs to FIND triggered issues and report back.
"""

from __future__ import annotations

import base64
import urllib.parse
from typing import Any, Optional

from transport import Transport

FIELDS = "summary,description,issuetype,labels,status,updated"
INLINE = ("text", "hardBreak", "mention", "emoji", "inlineCard")


def adf_text(text: str) -> dict[str, Any]:
    """Plain text (paragraphs split on blank lines, lines on newlines) as an ADF document."""
    content = []
    for para in [p for p in text.split("\n\n") if p.strip()]:
        nodes: list[dict[str, Any]] = []
        for i, line in enumerate(para.split("\n")):
            if i:
                nodes.append({"type": "hardBreak"})
            if line:
                nodes.append({"type": "text", "text": line})
        content.append({"type": "paragraph", "content": nodes})
    return {"type": "doc", "version": 1, "content": content}


def adf_to_markdown(node: Any) -> str:
    """Jira's ADF description as simple markdown (headings, lists, code, paragraphs)."""
    if node is None:
        return ""
    if isinstance(node, str):
        return node
    return _blocks(node.get("content") or []).strip() + "\n"


def _inline(nodes: list[dict[str, Any]]) -> str:
    out = []
    for n in nodes:
        t = n.get("type")
        if t == "text":
            s = str(n.get("text", ""))
            marks = {m.get("type") for m in n.get("marks") or []}
            out.append(f"`{s}`" if "code" in marks else s)
        elif t == "hardBreak":
            out.append("\n")
        elif t in ("mention", "emoji"):
            out.append(str((n.get("attrs") or {}).get("text", "")))
        elif t == "inlineCard":
            out.append(str((n.get("attrs") or {}).get("url", "")))
        else:
            out.append(_inline(n.get("content") or []))
    return "".join(out)


def _blocks(nodes: list[dict[str, Any]], indent: str = "") -> str:
    out = []
    for n in nodes:
        t = n.get("type")
        kids = n.get("content") or []
        if t == "heading":
            level = int((n.get("attrs") or {}).get("level", 2))
            out.append(f"{'#' * max(2, level)} {_inline(kids)}\n\n")
        elif t == "paragraph":
            out.append(indent + _inline(kids) + "\n\n")
        elif t in ("bulletList", "orderedList"):
            for i, item in enumerate(kids, 1):
                bullet = f"{i}. " if t == "orderedList" else "- "
                body = _blocks(item.get("content") or [], indent + "   ").strip()
                out.append(f"{indent}{bullet}{body}\n")
            out.append("\n")
        elif t == "codeBlock":
            lang = (n.get("attrs") or {}).get("language") or ""
            out.append(f"```{lang}\n{_inline(kids)}\n```\n\n")
        elif t == "blockquote":
            out.append("> " + _blocks(kids).strip().replace("\n", "\n> ") + "\n\n")
        elif t == "rule":
            out.append("---\n\n")
        elif kids and all(k.get("type") in INLINE for k in kids):
            out.append(indent + _inline(kids) + "\n\n")
        else:                       # panel, table, expand, ...: their blocks
            out.append(_blocks(kids, indent))
    return "".join(out)


class JiraClient:
    def __init__(self, site: str, email: str, token: str, transport: Transport) -> None:
        if not site.startswith("https://"):
            raise ValueError(f"jira site must be https://<site>.atlassian.net, got {site!r}")
        self.site = site.rstrip("/")
        self.t = transport
        self.auth = base64.b64encode(f"{email}:{token}".encode()).decode() if (email or token) else ""

    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/json", "User-Agent": "tinystream-bridge"}
        if self.auth:
            h["Authorization"] = f"Basic {self.auth}"
        return h

    def _call(self, method: str, path: str, body: Any = None, **query: Any) -> Any:
        q = {k: v for k, v in query.items() if v is not None}
        url = f"{self.site}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")
        return self.t.request(method, url, headers=self._headers(), body=body).body

    def browse_url(self, key: str) -> str:
        return f"{self.site}/browse/{key}"

    # -- reads ---------------------------------------------------------------------
    def myself(self) -> dict[str, Any]:
        return self._call("GET", "/rest/api/3/myself")

    def search(self, jql: str, max_results: int = 50) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        token: Optional[str] = None
        for _ in range(20):
            res = self._call("GET", "/rest/api/3/search/jql", jql=jql, fields=FIELDS,
                             maxResults=max_results, nextPageToken=token) or {}
            out += res.get("issues") or []
            token = res.get("nextPageToken")
            if not token or res.get("isLast", True):
                break
        return out

    def comments(self, key: str) -> list[dict[str, Any]]:
        return (self._call("GET", f"/rest/api/3/issue/{key}/comment") or {}).get("comments") or []

    # -- writes --------------------------------------------------------------------
    def upsert_comment(self, key: str, tag: str, text: str, comment_id: Optional[str] = None) -> dict[str, Any]:
        """One bridge comment per issue: edit ``comment_id`` (or the comment carrying the
        ``[tinystream-bridge:<tag>]`` line), else add a new one."""
        line = f"[tinystream-bridge:{tag}]"
        body = {"body": adf_text(f"{text}\n\n{line}")}
        if not comment_id:
            for c in self.comments(key):
                if line in _inline_all(c.get("body")):
                    comment_id = str(c["id"])
                    break
        if comment_id:
            return self._call("PUT", f"/rest/api/3/issue/{key}/comment/{comment_id}", body)
        return self._call("POST", f"/rest/api/3/issue/{key}/comment", body)

    def remove_label(self, key: str, label: str) -> None:
        self._call("PUT", f"/rest/api/3/issue/{key}", {"update": {"labels": [{"remove": label}]}})

    def transition(self, key: str, name: str) -> bool:
        """Move ``key`` through the transition (or to the status) called ``name``; False if none fits."""
        res = self._call("GET", f"/rest/api/3/issue/{key}/transitions") or {}
        want = name.strip().lower()
        for tr in res.get("transitions") or []:
            if str(tr.get("name", "")).lower() == want or str((tr.get("to") or {}).get("name", "")).lower() == want:
                self._call("POST", f"/rest/api/3/issue/{key}/transitions", {"transition": {"id": tr["id"]}})
                return True
        return False


def _inline_all(doc: Any) -> str:
    if isinstance(doc, str):
        return doc
    if isinstance(doc, dict):
        return str(doc.get("text", "")) + "".join(_inline_all(c) for c in doc.get("content") or [])
    return ""
