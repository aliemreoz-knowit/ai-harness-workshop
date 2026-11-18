"""Small GitHub REST client for the bridge (issues, labels, comments, PRs, check runs).

Only the calls the bridge needs. Every call goes through a ``Transport``, so
``--dry-run`` (DryRunTransport: no write is sent) and the tests
(ReplayTransport: recorded responses) exercise exactly this code.

Token: ``GITHUB_TOKEN`` or ``GH_TOKEN`` (a fine-grained PAT with Issues, Pull
requests, Contents read/write and -- for check runs -- a GitHub App or the
Actions ``GITHUB_TOKEN``; a PAT cannot create check runs, the client then
falls back to a commit status).
"""

from __future__ import annotations

import urllib.parse
from typing import Any, Optional

from transport import HttpError, Transport

API = "https://api.github.com"


def marker(tag: str) -> str:
    """The hidden HTML comment that identifies the bridge's single updating comment."""
    return f"<!-- tinystream-bridge:{tag} -->"


class GitHubClient:
    def __init__(self, repo: str, token: str, transport: Transport, api: str = API) -> None:
        if repo.count("/") != 1:
            raise ValueError(f"github repo must be owner/name, got {repo!r}")
        self.repo = repo
        self.owner = repo.split("/")[0]
        self.token = token
        self.t = transport
        self.api = api.rstrip("/")

    # -- plumbing ------------------------------------------------------------------
    def _headers(self) -> dict[str, str]:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "tinystream-bridge"}
        if self.token:
            h["Authorization"] = f"Bearer {self.token}"
        return h

    def _url(self, path: str, **query: Any) -> str:
        q = {k: v for k, v in query.items() if v is not None}
        return f"{self.api}{path}" + (f"?{urllib.parse.urlencode(q)}" if q else "")

    def _call(self, method: str, path: str, body: Any = None, **query: Any) -> Any:
        return self.t.request(method, self._url(path, **query), headers=self._headers(), body=body).body

    def _pages(self, path: str, **query: Any) -> list[dict[str, Any]]:
        out: list[dict[str, Any]] = []
        page = 1
        while True:
            items = self._call("GET", path, per_page=100, page=page, **query) or []
            out += items
            if len(items) < 100 or page >= 20:
                return out
            page += 1

    # -- reads ---------------------------------------------------------------------
    def get_repo(self) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repo}")

    def labeled_issues(self, label: str) -> list[dict[str, Any]]:
        """Open issues (not PRs) carrying ``label``."""
        return [i for i in self._pages(f"/repos/{self.repo}/issues", labels=label, state="open")
                if "pull_request" not in i]

    def labeled_pulls(self, label: str) -> list[dict[str, Any]]:
        """Open PRs carrying ``label`` (as issue objects; see get_pull for head/sha)."""
        return [i for i in self._pages(f"/repos/{self.repo}/issues", labels=label, state="open")
                if "pull_request" in i]

    def get_label(self, name: str) -> Optional[dict[str, Any]]:
        try:
            return self._call("GET", f"/repos/{self.repo}/labels/{urllib.parse.quote(name, safe='')}")
        except HttpError as exc:
            if exc.status == 404:
                return None
            raise

    def create_label(self, name: str, color: str, description: str = "") -> dict[str, Any]:
        return self._call("POST", f"/repos/{self.repo}/labels",
                          {"name": name, "color": color, "description": description})

    def get_issue(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repo}/issues/{number}")

    def get_pull(self, number: int) -> dict[str, Any]:
        return self._call("GET", f"/repos/{self.repo}/pulls/{number}")

    def issue_events(self, number: int) -> list[dict[str, Any]]:
        return self._pages(f"/repos/{self.repo}/issues/{number}/events")

    def comments(self, number: int) -> list[dict[str, Any]]:
        return self._pages(f"/repos/{self.repo}/issues/{number}/comments")

    def open_pull_for_branch(self, branch: str) -> Optional[dict[str, Any]]:
        pulls = self._call("GET", f"/repos/{self.repo}/pulls", head=f"{self.owner}:{branch}", state="open") or []
        return pulls[0] if pulls else None

    def labeler(self, number: int, label: str) -> Optional[str]:
        """Login of whoever added ``label`` last (None when unknown)."""
        who = None
        for ev in self.issue_events(number):
            if ev.get("event") == "labeled" and (ev.get("label") or {}).get("name") == label:
                who = (ev.get("actor") or {}).get("login")
        return who

    # -- writes --------------------------------------------------------------------
    def upsert_comment(self, number: int, tag: str, body: str) -> dict[str, Any]:
        """Create the bridge's comment for ``tag`` on issue/PR ``number``, or edit it in place."""
        mark = marker(tag)
        text = f"{mark}\n{body}"
        for c in self.comments(number):
            if mark in str(c.get("body") or ""):
                if c.get("body") == text:
                    return c
                return self._call("PATCH", f"/repos/{self.repo}/issues/comments/{c['id']}", {"body": text})
        return self._call("POST", f"/repos/{self.repo}/issues/{number}/comments", {"body": text})

    def add_labels(self, number: int, labels: list[str]) -> None:
        if labels:
            self._call("POST", f"/repos/{self.repo}/issues/{number}/labels", {"labels": list(labels)})

    def remove_label(self, number: int, label: str) -> None:
        try:
            self._call("DELETE", f"/repos/{self.repo}/issues/{number}/labels/{urllib.parse.quote(label, safe='')}")
        except HttpError as exc:
            if exc.status != 404:      # not set (any more): fine
                raise

    def create_pull(self, title: str, head: str, base: str, body: str, draft: bool = False) -> dict[str, Any]:
        existing = self.open_pull_for_branch(head)
        if existing:
            return self._call("PATCH", f"/repos/{self.repo}/pulls/{existing['number']}", {"body": body})
        return self._call("POST", f"/repos/{self.repo}/pulls",
                          {"title": title, "head": head, "base": base, "body": body, "draft": draft,
                           "maintainer_can_modify": True})

    def check_run(self, sha: str, name: str, *, status: str = "completed", conclusion: Optional[str] = None,
                  title: str = "", summary: str = "", text: str = "", details_url: Optional[str] = None,
                  run_id: Optional[int] = None) -> dict[str, Any]:
        """Create (or update, with ``run_id``) a check run. Falls back to a commit status
        when the token may not write check runs (a PAT: 403; result has "fallback": "status")."""
        body: dict[str, Any] = {"name": name, "head_sha": sha, "status": status,
                                "output": {"title": title or name, "summary": summary[:65000],
                                           **({"text": text[:65000]} if text else {})}}
        if conclusion and status == "completed":
            body["conclusion"] = conclusion
        if details_url:
            body["details_url"] = details_url
        try:
            if run_id:
                return self._call("PATCH", f"/repos/{self.repo}/check-runs/{run_id}", body)
            return self._call("POST", f"/repos/{self.repo}/check-runs", body)
        except HttpError as exc:
            if exc.status not in (403, 404):
                raise
        state = {"success": "success", "failure": "failure", "neutral": "success",
                 "cancelled": "error", "timed_out": "error", "action_required": "failure"}.get(conclusion or "",
                                                                                                "pending")
        res = self._call("POST", f"/repos/{self.repo}/statuses/{sha}",
                         {"state": state, "context": name, "description": (title or name)[:140],
                          **({"target_url": details_url} if details_url else {})})
        return dict(res or {}, fallback="status")
