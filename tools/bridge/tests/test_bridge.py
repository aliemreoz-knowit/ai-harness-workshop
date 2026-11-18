"""The bridge end to end: labels -> harness run -> PR, comments, check run, HIL queue."""

from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any

from bridge_testlib import FIXTURES, FakeHarness, git

import bridge as bridge_mod
from github_client import GitHubClient, marker
from harness_runner import HarnessRunner
from jira_client import JiraClient
from store import HilQueue, State
from transport import DryRunTransport, ReplayTransport

CLOCK = _dt.datetime(2026, 11, 20, 10, 15, 0)


class Rig:
    def __init__(self, repo: Path, fake: FakeHarness, *, dry: bool = False, use_jira: bool = False,
                 use_github: bool = True, **cfg_over: Any) -> None:
        cfg = bridge_mod._merge(bridge_mod.DEFAULTS, cfg_over)
        cfg["github"]["repo"] = "acme/tinystream"
        self.logs: list[str] = []
        self.gh_replay = ReplayTransport.from_file(FIXTURES / "github.json")
        self.jira_replay = ReplayTransport.from_file(FIXTURES / "jira.json")
        gh_t: Any = DryRunTransport(self.gh_replay, self.logs.append) if dry else self.gh_replay
        jr_t: Any = DryRunTransport(self.jira_replay, self.logs.append) if dry else self.jira_replay
        self.gh_t, self.jr_t = gh_t, jr_t
        self.fake = fake
        runner = HarnessRunner(repo, fake=True, fetch=False, run_fn=fake, log=self.logs.append)
        self.state = State(repo.parent / "state.json")
        self.hilq = HilQueue(repo.parent / "queue.jsonl")
        self.bridge = bridge_mod.Bridge(
            cfg, github=GitHubClient("acme/tinystream", "t0k", gh_t) if use_github else None,
            jira=JiraClient("https://acme.atlassian.net", "f@example.invalid", "t0k", jr_t) if use_jira else None,
            runner=runner, state=self.state, hilq=self.hilq, dry_run=dry, log=self.logs.append, clock=lambda: CLOCK)

    def writes(self) -> list[tuple[str, str]]:
        return [(w["method"], w["url"]) for w in self.gh_replay.writes()]


def test_live_poll_runs_the_issue_opens_the_pr_and_queues_hil(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    rep = rig.bridge.poll_once()
    assert rep["errors"] == []
    # HIL: PR 31 queued once, with its sha and branch, for hil.py
    assert [h["status"] for h in rep["hil"]] == ["queued"]
    entry = rig.hilq.pending()[0]
    assert (entry["pr"], entry["branch"], entry["sha"][:7]) == (31, "agent/gh-9-c0de", "1a2b3c4")
    assert entry["check_run_id"] is None                 # the PAT fell back to a commit status
    # fix: issue 12 -> run -> PR 40
    assert rep["fix"] == [{"id": "gh#12", "key": "GH-12", "status": "done", "run_id": fake_harness.run_id,
                           "branch": "agent/gh-12-ab12", "pr": 40, "error": ""}]
    assert "PR without" not in json.dumps(rep)
    assert git(repo.parent / "origin.git", "rev-parse", "agent/gh-12-ab12") == rig.state.get("gh#12")["commit"]
    # the ticket the harness got
    assert fake_harness.harness_calls[0]["ticket"].startswith("# GH-12: Board shows a stale frame counter")
    w = rig.writes()
    assert w[:3] == [("POST", "/repos/acme/tinystream/check-runs"),            # hil check run (queued) ...
                     ("POST", "/repos/acme/tinystream/statuses/1a2b3c4d5e6f708192a3b4c5d6e7f80912345678"),
                     ("POST", "/repos/acme/tinystream/issues/31/comments")]
    assert w[3:] == [
        ("DELETE", "/repos/acme/tinystream/issues/12/labels/agent%3Afix"),      # consumed
        ("POST", "/repos/acme/tinystream/issues/12/labels"),                     # agent:running
        ("POST", "/repos/acme/tinystream/issues/12/comments"),                   # started
        ("POST", "/repos/acme/tinystream/pulls"),
        ("POST", "/repos/acme/tinystream/issues/40/labels"),
        ("POST", "/repos/acme/tinystream/check-runs"),                           # 403 for a PAT ...
        ("POST", f"/repos/acme/tinystream/statuses/{rig.state.get('gh#12')['commit']}"),   # ... commit status
        ("POST", "/repos/acme/tinystream/issues/40/comments"),                   # the PR comment
        ("DELETE", "/repos/acme/tinystream/issues/12/labels/agent%3Arunning"),
        ("POST", "/repos/acme/tinystream/issues/12/labels"),                     # agent:pr-open
        ("PATCH", "/repos/acme/tinystream/issues/comments/501"),                 # the SAME issue comment, edited
    ]
    bodies = rig.gh_replay.writes()
    pr = next(b["body"] for b in bodies if b["url"] == "/repos/acme/tinystream/pulls")
    assert pr["head"] == "agent/gh-12-ab12" and pr["base"] == "main" and "Closes #12" in pr["body"]
    assert pr["title"] == "GH-12: Board shows a stale frame counter after a daemon restart"
    pr_comment = next(b["body"]["body"] for b in bodies if b["url"].endswith("/issues/40/comments"))
    assert pr_comment.startswith(marker("pr")) and "Agent run: PASS" in pr_comment and "unit-tests:PASS" in pr_comment
    final = bodies[-1]["body"]["body"]
    assert "https://github.com/acme/tinystream/pull/40" in final and "hil-ready" in final
    labels = [b["body"]["labels"] for b in bodies if b["url"].endswith("/issues/12/labels")]
    assert labels == [["agent:running"], ["agent:pr-open"]]
    hil_comment = next(b["body"]["body"] for b in bodies if b["url"].endswith("/issues/31/comments"))
    assert "HIL: QUEUED" in hil_comment and "position 1" in hil_comment

    # second poll: nothing new (issue handled, same sha already queued)
    n = len(rig.gh_replay.writes())
    rep2 = rig.bridge.poll_once()
    assert rep2["fix"] == [] and rep2["hil"] == [] and len(rig.gh_replay.writes()) == n
    assert len(fake_harness.harness_calls) == 1


def test_dry_run_sends_no_write_and_pushes_nothing(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness, dry=True)
    rep = rig.bridge.poll_once()
    assert rep["fix"][0]["status"] == "done" and rep["hil"][0]["status"] == "queued"
    assert rig.gh_replay.writes() == []                                  # nothing reached "GitHub"
    methods = [w["method"] + " " + w["url"].split("api.github.com")[-1] for w in rig.gh_t.writes]
    assert "POST /repos/acme/tinystream/pulls" in methods
    assert not any("/issues/900" in m for m in methods)                  # no comment on the fake PR number
    assert any(line.startswith("[dry-run] git push origin agent/gh-12-ab12") for line in rig.logs)
    cp = git(repo.parent / "origin.git", "branch", "--list", "agent/*")
    assert cp == ""                                                      # nothing pushed


def test_failed_run_labels_the_issue_and_opens_no_pr(repo: Path) -> None:
    fake = FakeHarness(status="failed")
    rig = Rig(repo, fake)
    rep = rig.bridge.poll_once()
    assert rep["fix"][0]["status"] == "failed" and rep["fix"][0]["pr"] is None
    urls = [u for _, u in rig.writes()]
    assert "/repos/acme/tinystream/pulls" not in urls and not any("check-runs" in u and "31" in u for u in urls)
    labels = [b["body"]["labels"] for b in rig.gh_replay.writes() if b["url"].endswith("/issues/12/labels")]
    assert labels == [["agent:running"], ["agent:failed"]]
    final = rig.gh_replay.writes()[-1]["body"]["body"]
    assert "Agent run: FAIL" in final and "retry budget exhausted" in final and "--resume" in final
    assert rig.state.get("gh#12")["status"] == "failed"


def test_label_removed_and_re_added_runs_again(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness, dry=True)
    rig.bridge.poll_once()
    assert rig.state.handled("gh#12")
    # the label is gone (the bridge consumed it) -> re-armed on the next successful listing
    rig.state.rearm("github", set())
    assert not rig.state.handled("gh#12")


def test_a_run_the_bridge_lost_is_rearmed_after_the_timeout(repo: Path, fake_harness: FakeHarness) -> None:
    import datetime as dt
    rig = Rig(repo, fake_harness, dry=True)
    rig.state.update("gh#12", kind="github", key="GH-12", status="running")   # a bridge that died mid-run
    rig.state.items["gh#12"]["updated"] = (dt.datetime.now(dt.timezone.utc) - dt.timedelta(seconds=6000)
                                           ).isoformat(timespec="seconds")
    rig.state.save()
    rep = rig.bridge.poll_once()
    assert [f["id"] for f in rep["fix"]] == ["gh#12"]                   # ran again
    assert any("left running for more than 5400 s" in line and "re-armed" in line for line in rig.logs)
    assert rig.state.get("gh#12")["status"] == "done"
    # a run in progress (fresh "running") is left alone
    rig2 = Rig(repo, FakeHarness(), dry=True)
    rig2.state.update("gh#12", kind="github", key="GH-12", status="running")
    assert rig2.bridge.poll_once()["fix"] == []


def test_allowed_actors_refuses_other_labelers(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness, github={"allowed_actors": ["facilitator-a"]})
    rep = rig.bridge.poll_once()
    assert rep["fix"] == [] and fake_harness.harness_calls == []
    assert any("team-red not allowed" in s for s in rep["skipped"])
    assert rig.state.get("gh#12")["status"] == "refused"
    assert rep["hil"][0]["status"] == "refused"                          # team-blue labelled the PR
    assert rig.hilq.pending() == []


def test_hil_refuses_a_fork(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    pull = next(i for i in rig.gh_replay.interactions if i["url"] == "/repos/acme/tinystream/pulls/31")
    pull["body"]["head"]["repo"]["full_name"] = "mallory/tinystream"
    rep = rig.bridge.poll_hil()
    assert rep[0]["status"] == "refused" and rig.hilq.pending() == []
    body = rig.gh_replay.writes()[-1]["body"]["body"]
    assert "HIL: REFUSED" in body and "mallory/tinystream" in body


def test_hil_results_from_the_queue_reach_the_pr(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    rig.bridge.poll_hil()
    (entry,) = rig.hilq.pending()
    n = len(rig.writes())
    assert rig.bridge.sync_hil() == [] and len(rig.writes()) == n          # queued: nothing to report yet
    # hil.py takes the entry, then judges it (the lines tools/hil/hilqueue.py appends)
    rig.hilq.update(entry["id"], status="running")
    assert [r["status"] for r in rig.bridge.sync_hil()] == ["running"]
    report_rel = "build-hil/runs/20261120-1250-pr-31/report.md"
    (repo / report_rel).parent.mkdir(parents=True)
    (repo / report_rel).write_text("<!-- tinystream-hil-report -->\n## HIL: PASS\n| fps | 30 |\n", encoding="utf-8")
    rig.hilq.update(entry["id"], status="passed", verdict="PASS", report=report_rel)
    assert [r["status"] for r in rig.bridge.sync_hil()] == ["passed"]
    w = rig.gh_replay.writes()[n:]
    sha = entry["sha"]
    assert [(x["method"], x["url"]) for x in w] == [
        ("POST", "/repos/acme/tinystream/issues/31/comments"),          # running (fixture: no comment listed)
        ("POST", "/repos/acme/tinystream/check-runs"),                  # 403 for a PAT ...
        ("POST", f"/repos/acme/tinystream/statuses/{sha}"),             # ... pending status
        ("POST", "/repos/acme/tinystream/issues/31/comments"),
        ("POST", "/repos/acme/tinystream/check-runs"),
        ("POST", f"/repos/acme/tinystream/statuses/{sha}")]
    assert "HIL: RUNNING" in w[0]["body"]["body"] and w[2]["body"]["state"] == "pending"
    final = w[3]["body"]["body"]
    assert "HIL: PASS" in final and report_rel in final and "| fps | 30 |" in final and "HIL: QUEUED" not in final
    assert w[4]["body"]["conclusion"] == "success" and w[5]["body"]["state"] == "success"
    # each status change once
    assert rig.bridge.sync_hil() == [] and len(rig.gh_replay.writes()) == n + 6


def test_new_commit_on_a_hil_ready_pr_is_queued_again(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    rig.bridge.poll_hil()
    pull = next(i for i in rig.gh_replay.interactions if i["url"] == "/repos/acme/tinystream/pulls/31")
    pull["body"]["head"]["sha"] = "f" * 40
    out = rig.bridge.poll_hil()
    assert out[0]["position"] == 2 and len(rig.hilq.pending()) == 2


def test_jira_trigger_runs_and_reports_back(repo: Path) -> None:
    fake = FakeHarness(run_id="20261120-101500-proj-7-ab12", branch="agent/proj-7-ab12")
    rig = Rig(repo, fake, use_jira=True, github={"fix_label": "none-such"},
              jira={"enabled": True, "project": "PROJ", "trigger_statuses": ["Agent Ready"],
                    "pr_transition": "In Review"})
    rig.gh_replay.interactions.insert(0, {"method": "GET", "url": "/repos/acme/tinystream/issues?labels=none-such&state=open&per_page=100&page=1",
                                          "body": []})
    rep = rig.bridge.poll_once()
    assert rep["errors"] == [] and rep["fix"][0]["key"] == "PROJ-7" and rep["fix"][0]["pr"] == 40
    assert fake.harness_calls[0]["ticket"].startswith("# PROJ-7: Keyframe request is ignored after a reconnect")
    jira_w = [(w["method"], w["url"]) for w in rig.jira_replay.writes()]
    assert jira_w == [("POST", "/rest/api/3/issue/PROJ-7/comment"),          # started
                      ("PUT", "/rest/api/3/issue/PROJ-7/comment/10001"),     # finished, same comment
                      ("POST", "/rest/api/3/issue/PROJ-7/transitions")]      # -> In Review
    pr = next(w["body"] for w in rig.gh_replay.writes() if w["url"] == "/repos/acme/tinystream/pulls")
    assert "Closes #" not in pr["body"] and "https://acme.atlassian.net/browse/PROJ-7" in pr["body"]
    assert rig.state.get("jira:PROJ-7")["jira_comment_id"] == "10001"
    search = next(c["url"] for c in rig.jira_replay.calls if "search/jql" in c["url"])
    assert "Agent+Ready" in search or "Agent%20Ready" in search


def test_max_runs_per_poll_defers_the_rest(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness, bridge={"max_runs_per_poll": 0})
    rep = rig.bridge.poll_once()
    assert rep["fix"] == [] and rep["skipped"] == ["gh#12 (next poll: max_runs_per_poll=0)"]
    assert not rig.state.handled("gh#12")


def test_an_exception_marks_the_item_and_the_bridge_goes_on(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    rig.gh_replay.interactions = [i for i in rig.gh_replay.interactions if i["url"] != "/repos/acme/tinystream/pulls"]
    rep = rig.bridge.poll_once()
    assert rep["fix"][0]["status"] == "error" and "HttpError" in rep["fix"][0]["error"]
    assert rig.state.get("gh#12")["status"] == "error"


def test_issue_without_acceptance_tests_gets_a_note_and_still_runs(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    rep = rig.bridge.poll_once()
    assert rep["fix"][0]["status"] == "done"                     # informational: the run goes ahead
    comments = [w["body"]["body"] for w in rig.gh_replay.writes()
                if w["url"].endswith(("/issues/12/comments", "/issues/comments/501"))]
    assert len(comments) == 2                                    # started, then the same comment edited
    for body in comments:
        assert "no acceptance tests: the harness accepts any progress; name the tests that must pass" in body
    assert any("no acceptance tests" in line for line in rig.logs)
    assert "Acceptance" not in json.dumps([w["body"] for w in rig.gh_replay.writes() if "/pulls" in w["url"]])


def test_issue_with_acceptance_tests_gets_no_note(repo: Path, fake_harness: FakeHarness) -> None:
    rig = Rig(repo, fake_harness)
    for i in rig.gh_replay.interactions:
        if "labels=agent%3Afix" in i["url"]:
            i["body"][0]["body"] += "\n\n## Acceptance tests\n\n- test_board_counter_after_restart\n"
    rig.bridge.poll_once()
    assert "## Acceptance tests" in fake_harness.harness_calls[0]["ticket"]
    comments = [w["body"]["body"] for w in rig.gh_replay.writes() if "/issues/12/comments" in w["url"]
                or w["url"].endswith("/issues/comments/501")]
    assert comments and not any("no acceptance tests" in b for b in comments)
