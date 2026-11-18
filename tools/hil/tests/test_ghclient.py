"""GitHub client against recorded responses: reads work, writes never leave a dry run."""
from __future__ import annotations

import json
from pathlib import Path

import pytest

from ghclient import GitHubClient, GitHubError, RecordedTransport

FIX = Path(__file__).resolve().parent / "fixtures" / "github"
REPO = "example-org/tinystream"


def test_labeled_pulls_from_recording():
    t = RecordedTransport.from_dir(str(FIX))
    gh = GitHubClient(REPO, dry_run=True, transport=t)
    prs = gh.labeled_pulls("hil-ready")
    assert [p["number"] for p in prs] == [7, 9]            # issue 8 is not a PR
    assert prs[0]["head_sha"] == "1" * 40 and prs[0]["head_ref"] == "agent/unit3-crc-1a2b3c"
    assert all(m == "GET" for m, _, _ in t.calls)


def test_dry_run_records_writes_and_sends_nothing(tmp_path):
    t = RecordedTransport.from_dir(str(FIX))
    gh = GitHubClient(REPO, dry_run=True, transport=t, out_dir=str(tmp_path))
    assert gh.comment(7, "report") == {"dry_run": True}
    gh.add_labels(7, ["hil-pass"])
    gh.remove_label(7, "hil-ready")
    assert t.calls == []                                     # not even a recorded write
    lines = [json.loads(x) for x in (tmp_path / "github-dryrun.jsonl").read_text().splitlines()]
    assert [(x["method"], x["path"]) for x in lines] == [
        ("POST", "/repos/%s/issues/7/comments" % REPO),
        ("POST", "/repos/%s/issues/7/labels" % REPO),
        ("DELETE", "/repos/%s/issues/7/labels/hil-ready" % REPO)]
    assert lines[0]["payload"] == {"body": "report"}


def test_real_mode_needs_a_token_and_sends_it():
    with pytest.raises(GitHubError):
        GitHubClient(REPO, token=None, dry_run=False, transport=RecordedTransport({}))
    seen = []

    def transport(method, url, headers, body):
        seen.append((method, url, headers, json.loads(body) if body else None))
        return 201, b'{"id": 1}'

    gh = GitHubClient(REPO, token="test-token", dry_run=False, transport=transport)
    assert gh.comment(9, "hello") == {"id": 1}
    method, url, headers, body = seen[0]
    assert (method, url) == ("POST", "https://api.github.com/repos/%s/issues/9/comments" % REPO)
    assert headers["Authorization"] == "Bearer test-token" and body == {"body": "hello"}


def test_http_error_and_bad_repo():
    with pytest.raises(GitHubError):
        GitHubClient("no-slash", dry_run=True)
    gh = GitHubClient(REPO, dry_run=True, transport=lambda m, u, h, b: (404, b'{"message": "Not Found"}'))
    with pytest.raises(GitHubError):
        gh.labeled_pulls("hil-ready")


def test_token_from_env(monkeypatch):
    monkeypatch.delenv("HIL_GITHUB_TOKEN", raising=False)
    monkeypatch.setenv("GITHUB_TOKEN", "x")
    assert GitHubClient.token_from_env(["HIL_GITHUB_TOKEN", "GITHUB_TOKEN"]) == "x"
