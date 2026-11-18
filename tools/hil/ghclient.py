"""ghclient.py -- the small GitHub REST client of the HIL station.

What the station needs from GitHub, nothing more:
  * open pull requests carrying a label (default `hil-ready`), with head ref and sha
  * a comment with the HIL report on a pull request
  * result labels (`hil-pass` / `hil-fail`) and removal of the trigger label

Reads go to the API; every write (comment, label) is skipped in --dry-run
and recorded instead in <out_dir>/github-dryrun.jsonl, so a dry run shows
exactly what would have been posted. The token comes from an environment
variable named in hil.yaml (default HIL_GITHUB_TOKEN, then GITHUB_TOKEN); the
client never reads other credentials (no `gh auth token`, no git helpers).

The transport is injectable: tests pass RecordedTransport (responses from
JSON files, see tests/fixtures/github/) and never touch the network.
"""
from __future__ import annotations

import json
import os
import urllib.error
import urllib.parse
import urllib.request
from typing import Any, Callable, Optional

Transport = Callable[[str, str, dict, Optional[bytes]], "tuple[int, bytes]"]


class GitHubError(RuntimeError):
    pass


def urllib_transport(method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
    req = urllib.request.Request(url, data=body, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=20) as resp:
            return resp.status, resp.read()
    except urllib.error.HTTPError as exc:
        return exc.code, exc.read()


class RecordedTransport:
    """Answers from recorded responses: {"GET /path?query": {"status": 200, "body": ...}}.

    Unrecorded requests raise. Writes are recorded in `.calls` (a write in a
    test that should be a dry run shows up there and fails the test).
    """

    def __init__(self, responses: dict[str, dict]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, str, Optional[Any]]] = []

    @classmethod
    def from_dir(cls, path: str) -> "RecordedTransport":
        responses: dict[str, dict] = {}
        with open(os.path.join(path, "index.json"), "r", encoding="utf-8") as f:
            index = json.load(f)
        for key, fname in index.items():
            with open(os.path.join(path, fname), "r", encoding="utf-8") as f:
                responses[key] = json.load(f)
        return cls(responses)

    def __call__(self, method: str, url: str, headers: dict, body: Optional[bytes]) -> tuple[int, bytes]:
        parsed = urllib.parse.urlsplit(url)
        key = "%s %s" % (method, parsed.path + (("?" + parsed.query) if parsed.query else ""))
        self.calls.append((method, key, json.loads(body) if body else None))
        if key not in self.responses:
            raise GitHubError("no recorded response for %s" % key)
        r = self.responses[key]
        return int(r.get("status", 200)), json.dumps(r.get("body")).encode()


class GitHubClient:
    def __init__(self, repo: str, token: Optional[str] = None, api: str = "https://api.github.com",
                 dry_run: bool = False, transport: Optional[Transport] = None,
                 out_dir: Optional[str] = None) -> None:
        if not repo or repo.count("/") != 1:
            raise GitHubError("github.repo must be 'owner/name', got '%s'" % repo)
        self.repo = repo
        self.token = token
        self.api = api.rstrip("/")
        self.dry_run = dry_run
        self.transport = transport or urllib_transport
        self.out_dir = out_dir
        self.dry_log: list[dict] = []
        if not dry_run and not token:
            raise GitHubError("no GitHub token: set the variable named in github.token_env (or use --dry-run)")

    @staticmethod
    def token_from_env(names: list[str]) -> Optional[str]:
        for n in names:
            v = os.environ.get(n)
            if v:
                return v
        return None

    # ---- plumbing -----------------------------------------------------------------------
    def _headers(self) -> dict:
        h = {"Accept": "application/vnd.github+json", "X-GitHub-Api-Version": "2022-11-28",
             "User-Agent": "tinystream-hil"}
        if self.token:
            h["Authorization"] = "Bearer " + self.token
        return h

    def _request(self, method: str, path: str, payload: Any = None) -> Any:
        url = self.api + path
        body = json.dumps(payload).encode() if payload is not None else None
        headers = self._headers()
        if body is not None:
            headers["Content-Type"] = "application/json"
        status, data = self.transport(method, url, headers, body)
        if status >= 300:
            raise GitHubError("%s %s -> HTTP %d: %s" % (method, path, status, data[:200]))
        return json.loads(data) if data else None

    def _write(self, method: str, path: str, payload: Any) -> Any:
        if self.dry_run:
            entry = {"method": method, "path": path, "payload": payload}
            self.dry_log.append(entry)
            if self.out_dir:
                os.makedirs(self.out_dir, exist_ok=True)
                with open(os.path.join(self.out_dir, "github-dryrun.jsonl"), "a", encoding="utf-8",
                          newline="\n") as f:
                    f.write(json.dumps(entry) + "\n")
            return {"dry_run": True}
        return self._request(method, path, payload)

    # ---- what the station uses -------------------------------------------------------------
    def labeled_pulls(self, label: str) -> list[dict]:
        """Open PRs with `label`: [{number, title, head_ref, head_sha, head_repo, labels, url}]."""
        q = urllib.parse.urlencode({"state": "open", "labels": label, "per_page": "100"})
        issues = self._request("GET", "/repos/%s/issues?%s" % (self.repo, q)) or []
        out = []
        for it in issues:
            if "pull_request" not in it:
                continue            # a plain issue with the label
            pr = self._request("GET", "/repos/%s/pulls/%d" % (self.repo, int(it["number"])))
            out.append({"number": int(pr["number"]), "title": pr.get("title", ""),
                        "head_ref": pr["head"]["ref"], "head_sha": pr["head"]["sha"],
                        "head_repo": (pr["head"].get("repo") or {}).get("full_name", ""),
                        "labels": [lb["name"] for lb in pr.get("labels", [])],
                        "url": pr.get("html_url", "")})
        return sorted(out, key=lambda p: p["number"])

    def comment(self, number: int, body: str) -> Any:
        return self._write("POST", "/repos/%s/issues/%d/comments" % (self.repo, number), {"body": body})

    def add_labels(self, number: int, labels: list[str]) -> Any:
        return self._write("POST", "/repos/%s/issues/%d/labels" % (self.repo, number), {"labels": labels})

    def remove_label(self, number: int, label: str) -> Any:
        return self._write("DELETE", "/repos/%s/issues/%d/labels/%s" % (self.repo, number,
                                                                        urllib.parse.quote(label)), None)
