"""Transports and the GitHub / Jira clients against recorded responses."""

from __future__ import annotations

import pytest
from bridge_testlib import FIXTURES

from github_client import GitHubClient, marker
from jira_client import JiraClient, adf_text, adf_to_markdown
from transport import DryRunTransport, HttpError, ReplayTransport


def gh(transport=None) -> GitHubClient:
    return GitHubClient("acme/tinystream", "t0k", transport or ReplayTransport.from_file(FIXTURES / "github.json"))


def jira(transport=None) -> JiraClient:
    return JiraClient("https://acme.atlassian.net", "f@example.invalid", "t0k",
                      transport or ReplayTransport.from_file(FIXTURES / "jira.json"))


# -- transports -----------------------------------------------------------------------
def test_replay_matches_query_in_any_order_and_prefix() -> None:
    t = ReplayTransport([{"method": "GET", "url": "/a?x=1&y=2", "body": 1},
                         {"method": "GET", "url": "/b/*", "body": 2}])
    assert t.request("GET", "https://api.example/a?y=2&x=1").body == 1
    assert t.request("GET", "https://api.example/b/c/d").body == 2
    with pytest.raises(HttpError) as exc:
        t.request("GET", "https://api.example/a?x=1")
    assert exc.value.status == 599


def test_replay_once_sequences_and_error_status() -> None:
    t = ReplayTransport([{"method": "GET", "url": "/s", "body": "first", "once": True},
                         {"method": "GET", "url": "/s", "body": "then"},
                         {"method": "POST", "url": "/w", "status": 422, "body": {"message": "nope"}}])
    assert [t.request("GET", "/s").body for _ in range(3)] == ["first", "then", "then"]
    with pytest.raises(HttpError, match="422: nope"):
        t.request("POST", "/w", body={})
    assert t.writes() == [{"method": "POST", "url": "/w", "body": {}}]


def test_dry_run_sends_reads_and_swallows_writes() -> None:
    inner = ReplayTransport([{"method": "GET", "url": "/r", "body": [1]}])
    logs: list[str] = []
    t = DryRunTransport(inner, logs.append)
    assert t.request("GET", "https://x/r").body == [1]
    res = t.request("POST", "https://x/w", body={"a": 1})
    assert res.body["dry_run"] is True and res.status == 201
    t.request("DELETE", "https://x/w/1")
    assert [w["method"] for w in t.writes] == ["POST", "DELETE"]
    assert inner.writes() == []                       # nothing reached the inner transport
    assert logs[0].startswith("[dry-run] POST /w")


# -- GitHub -----------------------------------------------------------------------------
def test_labeled_issues_excludes_pull_requests() -> None:
    issues = gh().labeled_issues("agent:fix")
    assert [i["number"] for i in issues] == [12]
    assert [p["number"] for p in gh().labeled_pulls("hil-ready")] == [31]


def test_pagination_follows_full_pages() -> None:
    page1 = [{"number": n} for n in range(100)]
    t = ReplayTransport([
        {"method": "GET", "url": "/repos/acme/tinystream/issues?labels=x&state=open&per_page=100&page=1", "body": page1},
        {"method": "GET", "url": "/repos/acme/tinystream/issues?labels=x&state=open&per_page=100&page=2",
         "body": [{"number": 100}]}])
    assert len(gh(t).labeled_issues("x")) == 101


def test_upsert_comment_creates_then_edits_in_place() -> None:
    t = ReplayTransport.from_file(FIXTURES / "github.json")
    c = gh(t)
    c.upsert_comment(12, "issue", "first")           # no comment yet -> POST
    c.upsert_comment(12, "issue", "second")          # marker found (id 501) -> PATCH
    w = t.writes()
    assert [x["method"] for x in w] == ["POST", "PATCH"]
    assert w[0]["body"]["body"].startswith(marker("issue"))
    assert w[1]["url"] == "/repos/acme/tinystream/issues/comments/501"
    assert w[1]["body"]["body"].endswith("second")


def test_remove_label_ignores_missing_label() -> None:
    t = ReplayTransport.from_file(FIXTURES / "github.json")
    gh(t).remove_label(12, "agent:failed")            # recorded 404
    assert t.writes()[0]["url"] == "/repos/acme/tinystream/issues/12/labels/agent%3Afailed"


def test_check_run_falls_back_to_commit_status_for_a_pat() -> None:
    t = ReplayTransport.from_file(FIXTURES / "github.json")
    res = gh(t).check_run("ab" * 20, "agent-harness", conclusion="success", title="ok", summary="| a |")
    assert res["fallback"] == "status"
    post = t.writes()[-1]
    assert post["url"] == f"/repos/acme/tinystream/statuses/{'ab' * 20}"
    assert post["body"]["state"] == "success" and post["body"]["context"] == "agent-harness"


def test_labeler_is_the_last_actor_adding_the_label() -> None:
    assert gh().labeler(12, "agent:fix") == "team-red"
    assert gh().labeler(12, "nope") is None


def test_create_pull_reuses_an_open_pr_for_the_branch() -> None:
    t = ReplayTransport([
        {"method": "GET", "url": "/repos/acme/tinystream/pulls?head=acme%3Aagent%2Fx&state=open", "body": [{"number": 5}]},
        {"method": "PATCH", "url": "/repos/acme/tinystream/pulls/5", "body": {"number": 5}}])
    assert gh(t).create_pull("t", "agent/x", "main", "body")["number"] == 5
    assert [w["method"] for w in t.writes()] == ["PATCH"]


def test_auth_header_and_repo_validation() -> None:
    t = ReplayTransport([{"method": "GET", "url": "/repos/acme/tinystream", "body": {}}])
    recorded = {}

    class Spy(ReplayTransport):
        def request(self, method, url, *, headers=None, body=None):
            recorded.update(headers or {})
            return super().request(method, url, headers=headers, body=body)

    gh(Spy(t.interactions)).get_repo()
    assert recorded["Authorization"] == "Bearer t0k"
    assert recorded["X-GitHub-Api-Version"] == "2022-11-28"
    with pytest.raises(ValueError):
        GitHubClient("no-slash", "", t)


# -- Jira -----------------------------------------------------------------------------
def test_adf_to_markdown() -> None:
    issue = jira().search("project = PROJ")[0]
    md = adf_to_markdown(issue["fields"]["description"])
    assert "## Description" in md and "Pressing `k` after a reconnect does nothing." in md
    assert "1. Pull the cable for 3 s\n2. Press k" in md
    assert "```sh\ntsrecv --server 10.42.0.2:5601\n```" in md
    assert adf_to_markdown("plain v2 text") == "plain v2 text"
    assert adf_to_markdown(None) == ""


def test_adf_text_roundtrip_shape() -> None:
    doc = adf_text("line 1\nline 2\n\npara 2")
    assert doc["type"] == "doc" and len(doc["content"]) == 2
    assert doc["content"][0]["content"][1] == {"type": "hardBreak"}


def test_jira_search_paginates_with_next_page_token() -> None:
    t = ReplayTransport([
        {"method": "GET", "url": "/rest/api/3/search/jql*", "once": True,
         "body": {"issues": [{"key": "A-1"}], "nextPageToken": "p2", "isLast": False}},
        {"method": "GET", "url": "/rest/api/3/search/jql*", "body": {"issues": [{"key": "A-2"}], "isLast": True}}])
    assert [i["key"] for i in jira(t).search("x")] == ["A-1", "A-2"]
    assert "nextPageToken=p2" in t.calls[1]["url"]


def test_jira_comment_upsert_and_transition() -> None:
    t = ReplayTransport.from_file(FIXTURES / "jira.json")
    j = jira(t)
    assert j.upsert_comment("PROJ-7", "agent", "started")["id"] == "10001"     # none yet -> POST
    j.upsert_comment("PROJ-7", "agent", "finished")                            # marker found -> PUT
    assert j.transition("PROJ-7", "In Review") is True                         # matched by target status
    assert j.transition("PROJ-7", "Nonexistent") is False
    w = t.writes()
    assert [x["method"] for x in w] == ["POST", "PUT", "POST"]
    assert w[1]["url"] == "/rest/api/3/issue/PROJ-7/comment/10001"
    assert w[2]["body"] == {"transition": {"id": "31"}}


def test_jira_basic_auth_and_site_validation() -> None:
    j = jira()
    assert j._headers()["Authorization"].startswith("Basic ")
    with pytest.raises(ValueError):
        JiraClient("http://insecure", "", "", ReplayTransport([]))
