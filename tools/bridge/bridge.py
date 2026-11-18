#!/usr/bin/env python3
"""tinystream bridge -- the team-level trigger for the agent harness.

Polls GitHub (and optionally Jira) and turns labels into harness runs:

    GitHub issue labelled ``agent:fix``      -> harness jira-to-pr in a fresh worktree from main,
                                                push the agent branch, open a PR ("Closes #n")
    Jira issue in a trigger status / with    -> the same; the PR goes to the GitHub repo, the
    the label ``agent``                         result is commented on the Jira issue
    PR labelled ``hil-ready``                -> appended to the HIL queue for hil.py

Results: ONE updating comment per issue / PR (hidden marker) and a check run
(``agent-harness``; a PAT falls back to a commit status). Runs use the
facilitator runtime (central login and credit caps).

    python tools/bridge/bridge.py check                        # read-only: tokens, repo, labels, Jira
    python tools/bridge/bridge.py once [--dry-run]             # one poll
    python tools/bridge/bridge.py poll [--interval 60]         # poll until Ctrl-C
    python tools/bridge/bridge.py status                       # handled items + HIL queue
    python tools/bridge/bridge.py labels [--dry-run]           # create missing trigger labels
    python tools/bridge/bridge.py once --fixtures tools/bridge/tests/fixtures/demo   # no network at all

``--dry-run``: reads are real, NO write leaves the machine (no comment, label,
PR, check run, push); the harness runs with the fake runtime unless
``--live-runtime``; state and queue go to ``*.dry-run`` files.
``--fixtures DIR``: GitHub / Jira answered from DIR/github.json and DIR/jira.json
(implies --dry-run).

GitHub Actions (.github/workflows/agent-fix.yml) uses ``ci-start`` / ``ci-finish``
around its own ``python -m harness run`` step.
Environment: GITHUB_TOKEN (or GH_TOKEN), BRIDGE_GITHUB_REPO (owner/name),
JIRA_SITE, JIRA_EMAIL, JIRA_API_TOKEN.
"""

from __future__ import annotations

import argparse
import copy
import datetime as _dt
import json
import os
import subprocess
import sys
import time
from pathlib import Path
from typing import Any, Callable, Optional

HERE = Path(__file__).resolve().parent
REPO = HERE.parents[1]
for _p in (str(HERE), str(REPO)):
    if _p not in sys.path:
        sys.path.insert(0, _p)

import report  # noqa: E402
import sources  # noqa: E402
from github_client import GitHubClient  # noqa: E402
from harness_runner import HarnessRunner, RunOutcome  # noqa: E402
from jira_client import JiraClient  # noqa: E402
from sources import Source  # noqa: E402
from store import HilQueue, State  # noqa: E402
from transport import DryRunTransport, HttpError, ReplayTransport, Transport, UrllibTransport  # noqa: E402

DEFAULTS: dict[str, Any] = {
    "github": {"enabled": True, "repo": "", "api": "https://api.github.com", "base": "main",
               "fix_label": "agent:fix", "hil_label": "hil-ready", "running_label": "agent:running",
               "done_label": "agent:pr-open", "failed_label": "agent:failed", "pr_labels": ["agent"],
               "consume_label": True, "key_prefix": "GH", "allowed_actors": [], "check_name": "agent-harness",
               "hil_check_name": "hil", "draft_pr": False, "same_repo_only": True},
    "jira": {"enabled": False, "site": "", "project": "", "trigger_statuses": [], "trigger_label": "agent",
             "consume_label": True, "jql": "", "pr_transition": ""},
    "harness": {"workflow": "jira-to-pr", "mode": "eval", "runtime": "facilitator", "fake": False,
                "timeout_s": 5400, "remote": "origin", "fetch": True, "worktree_root": "", "cleanup": False},
    "bridge": {"state": ".harness/bridge/state.json", "hil_queue": ".harness/hil/queue.jsonl",
               "interval_s": 60, "max_runs_per_poll": 1},
}
LABEL_COLORS = {"fix_label": ("5319e7", "Run the agent harness on this issue (bridge)"),
                "running_label": ("fbca04", "Agent harness run in progress"),
                "done_label": ("0e8a16", "Agent run finished; PR open"),
                "failed_label": ("b60205", "Agent run did not produce a PR"),
                "hil_label": ("1d76db", "Queue this PR for the hardware-in-the-loop window")}


def log(msg: str) -> None:
    print(f"{_dt.datetime.now():%H:%M:%S} {msg}", flush=True)


# ------------------------------------------------------------------ config ----
def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in (over or {}).items():
        out[k] = _merge(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else v
    return out


def load_config(path: Optional[Path]) -> dict[str, Any]:
    path = path or HERE / "bridge.yaml"
    data: dict[str, Any] = {}
    if path.is_file():
        from harness.yamlmini import load
        data = load(path.read_text(encoding="utf-8")) or {}
    cfg = _merge(DEFAULTS, data)
    gh, jr = cfg["github"], cfg["jira"]
    gh["repo"] = os.environ.get("BRIDGE_GITHUB_REPO") or gh["repo"] or os.environ.get("GITHUB_REPOSITORY", "")
    jr["site"] = os.environ.get("JIRA_SITE") or jr["site"]
    return cfg


def repo_from_remote(repo_root: Path, remote: str) -> str:
    try:
        url = subprocess.run(["git", "remote", "get-url", remote], cwd=str(repo_root), capture_output=True,
                             text=True, timeout=20).stdout.strip()
    except (OSError, subprocess.SubprocessError):
        return ""
    for prefix in ("https://github.com/", "git@github.com:", "ssh://git@github.com/"):
        if url.startswith(prefix):
            return url[len(prefix):].removesuffix(".git")
    return ""


# ------------------------------------------------------------------ bridge ----
class Bridge:
    def __init__(self, cfg: dict[str, Any], *, github: Optional[GitHubClient], jira: Optional[JiraClient],
                 runner: HarnessRunner, state: State, hilq: HilQueue, dry_run: bool = False,
                 log: Callable[[str], None] = log, clock: Callable[[], _dt.datetime] = _dt.datetime.now) -> None:
        self.cfg, self.gh, self.jira, self.runner = cfg, github, jira, runner
        self.state, self.hilq, self.dry_run, self.log, self.clock = state, hilq, dry_run, log, clock
        self.g, self.j, self.h = cfg["github"], cfg["jira"], cfg["harness"]

    # -- polling ---------------------------------------------------------------------
    def poll_once(self) -> dict[str, Any]:
        rep: dict[str, Any] = {"hil": [], "hil_results": [], "fix": [], "skipped": [], "errors": []}
        if self.gh:
            try:
                rep["hil"] = self.poll_hil()
            except HttpError as exc:
                rep["errors"].append(f"github hil: {exc}")
            try:
                rep["hil_results"] = self.sync_hil()
            except HttpError as exc:
                rep["errors"].append(f"github hil results: {exc}")
        triggered: list[Source] = []
        if self.gh:
            try:
                triggered += self.github_triggers(rep)
            except HttpError as exc:
                rep["errors"].append(f"github issues: {exc}")
        if self.jira:
            try:
                triggered += self.jira_triggers()
            except HttpError as exc:
                rep["errors"].append(f"jira: {exc}")
        limit = int(self.cfg["bridge"]["max_runs_per_poll"])
        for src in triggered[:limit]:
            rep["fix"].append(self.handle(src))
        rep["skipped"] += [f"{s.id} (next poll: max_runs_per_poll={limit})" for s in triggered[limit:]]
        for e in rep["errors"]:
            self.log(f"[bridge] error: {e}")
        return rep

    def github_triggers(self, rep: dict[str, Any]) -> list[Source]:
        assert self.gh is not None
        label = self.g["fix_label"]
        issues = self.gh.labeled_issues(label)
        self._rearm("github", {f"gh#{i['number']}" for i in issues})
        out = []
        for issue in issues:
            src = sources.from_github_issue(issue, self.g["key_prefix"], label)
            if self.state.handled(src.id):
                continue
            allowed = [a.lower() for a in self.g.get("allowed_actors") or []]
            if allowed:
                who = self.gh.labeler(src.issue_number or 0, label)
                if (who or "").lower() not in allowed:
                    self.state.update(src.id, kind="github", key=src.key, status="refused", labeler=who)
                    self._gh_comment(src.issue_number, "issue",
                                     f"### Agent run: REFUSED\n\n`{label}` was added by `{who}`, who is not in the "
                                     "bridge's `allowed_actors`. Ask a facilitator to add the label.")
                    rep["skipped"].append(f"{src.id} (labeler {who} not allowed)")
                    continue
            out.append(src)
        return out

    def jira_triggers(self) -> list[Source]:
        assert self.jira is not None
        statuses = list(self.j.get("trigger_statuses") or [])
        label = str(self.j.get("trigger_label") or "")
        jql = self.j.get("jql") or sources.default_jql(str(self.j.get("project") or ""), statuses, label)
        issues = self.jira.search(jql)
        self._rearm("jira", {f"jira:{i['key']}" for i in issues})
        out = []
        for issue in issues:
            trig = sources.jira_trigger(issue, statuses, label) or "jql"
            src = sources.from_jira_issue(issue, self.jira.browse_url(issue["key"]), trig)
            if not self.state.handled(src.id):
                out.append(src)
        return out

    def _rearm(self, kind: str, matching: set[str]) -> list[str]:
        """Forget handled items that stopped matching, and items left running longer than
        harness.timeout_s (a run that never ended: the bridge was restarted or the harness hung)."""
        stale_s = float(self.h.get("timeout_s") or 0) or None
        for i in self.state.stale(kind, stale_s):
            self.log(f"[bridge] {i}: left running for more than {stale_s:.0f} s (bridge restarted or run "
                     "timed out): re-armed")
        return self.state.rearm(kind, matching, stale_s=stale_s)

    # -- one fix run -----------------------------------------------------------------
    def slug(self, src: Source) -> str:
        return f"{src.key.lower()}-{self.clock():%Y%m%d-%H%M%S}"

    def handle(self, src: Source) -> dict[str, Any]:
        self.log(f"[bridge] {src.id}: {src.key} '{src.title}' ({src.trigger}) -> {self.h['workflow']}")
        self.state.update(src.id, kind=src.kind, key=src.key, title=src.title, trigger=src.trigger,
                          status="running", url=src.url)
        result: dict[str, Any] = {"id": src.id, "key": src.key}
        o = RunOutcome(status="error", error="not started")
        pr: Optional[dict[str, Any]] = None
        try:
            self.notify_start(src)
            o = self.runner.run(self.slug(src), src.ticket_md)
            if o.ok:
                pr = self.publish(src, o)
            self.notify_end(src, o, pr)
        except Exception as exc:  # noqa: BLE001 - one bad item must not stop the bridge
            err = f"{type(exc).__name__}: {exc}"
            o.error = f"{o.error}; {err}" if o.error and o.error != "not started" else err
            pr = None
            self.log(f"[bridge] {src.id}: {err}")
        finally:
            if o.ok and (pr or not self.gh) and not o.error:
                status = "done"
            else:
                status = o.status if o.status != "done" else "error"
            self.state.update(src.id, status=status, run_id=o.run_id, branch=o.branch, commit=o.commit,
                              workdir=o.workdir, error=o.error,
                              pr=(pr or {}).get("number"), pr_url=(pr or {}).get("html_url"))
            result.update(status=status, run_id=o.run_id, branch=o.branch, pr=(pr or {}).get("number"),
                          error=o.error)
            if o.ok and self.h.get("cleanup") and o.workdir:
                try:
                    self.runner.cleanup(Path(o.workdir))
                except RuntimeError as exc:
                    self.log(f"[bridge] cleanup: {exc}")
        self.log(f"[bridge] {src.id}: {result['status']}" + (f", PR #{result['pr']}" if result.get("pr") else ""))
        return result

    def notify_start(self, src: Source) -> None:
        text = report.started(src.key, src.title, self.h["workflow"], self.h["runtime"], self.runner.fake, src.trigger)
        text = self._with_notes(src, text, log=True)
        if src.kind == "github" and self.gh and src.issue_number:
            if self.g.get("consume_label"):
                self.gh.remove_label(src.issue_number, self.g["fix_label"])
            for lb in (self.g["done_label"], self.g["failed_label"]):
                if lb in src.labels:
                    self.gh.remove_label(src.issue_number, lb)
            self.gh.add_labels(src.issue_number, [self.g["running_label"]])
            self._gh_comment(src.issue_number, "issue", text)
        elif src.kind == "jira" and self.jira and src.jira_key:
            if self.j.get("consume_label") and src.trigger.startswith("label:"):
                self.jira.remove_label(src.jira_key, src.trigger.split(":", 1)[1])
            self._jira_comment(src, text)

    def publish(self, src: Source, o: RunOutcome) -> Optional[dict[str, Any]]:
        """Push the agent branch, open (or update) the PR, check run, PR comment."""
        if not self.gh:
            self.log(f"[bridge] {src.id}: no GitHub repo configured; branch {o.branch} stays local")
            return None
        if self.dry_run:
            self.log(f"[dry-run] git push {self.runner.remote} {o.branch}")
        else:
            self.runner.push(Path(o.workdir), o.branch)
        closes = src.issue_number if src.kind == "github" else None
        pr = self.gh.create_pull(f"{src.key}: {src.title}", o.branch, self.g["base"],
                                 report.pr_body(src.key, src.title, src.url, o, closes), bool(self.g.get("draft_pr")))
        number = int(pr.get("number") or 0)
        if pr.get("dry_run"):
            number = 0           # no such PR: nothing to label or comment on
            self.log(f"[dry-run] PR comment:\n{report.finished(src.key, src.title, o, src.url)}")
        if number and self.g.get("pr_labels"):
            self.gh.add_labels(number, list(self.g["pr_labels"]))
        self.gh.check_run(o.commit, self.g["check_name"], conclusion=report.conclusion(o),
                          title=f"{src.key}: {o.status} ({o.gates or 'gates -'})",
                          summary=report.outcome_table(o), details_url=pr.get("html_url") or src.url)
        if number:
            sections = self.state.pr_section(number, "agent", report.finished(src.key, src.title, o, src.url))
            self.gh.upsert_comment(number, "pr", report.compose(sections))
        return pr

    def notify_end(self, src: Source, o: RunOutcome, pr: Optional[dict[str, Any]]) -> None:
        pr_url = (pr or {}).get("html_url")
        text = self._with_notes(src, report.finished(src.key, src.title, o, pr_url))
        if src.kind == "github" and self.gh and src.issue_number:
            self.gh.remove_label(src.issue_number, self.g["running_label"])
            self.gh.add_labels(src.issue_number, [self.g["done_label"] if pr else self.g["failed_label"]])
            self._gh_comment(src.issue_number, "issue", text)
        elif src.kind == "jira" and self.jira and src.jira_key:
            self._jira_comment(src, text)
            if pr and self.j.get("pr_transition"):
                if not self.jira.transition(src.jira_key, str(self.j["pr_transition"])):
                    self.log(f"[bridge] {src.jira_key}: no transition '{self.j['pr_transition']}'")

    def _with_notes(self, src: Source, text: str, log: bool = False) -> str:
        """The issue comment plus the notes on the ticket (no acceptance tests: the harness
        accepts any progress on a red branch). Never blocks the run."""
        try:
            notes = report.ticket_notes(src.acceptance_tests)
        except Exception as exc:  # noqa: BLE001 - a note must never stop a run
            self.log(f"[bridge] {src.id}: ticket notes: {type(exc).__name__}: {exc}")
            return text
        if notes and log:
            self.log(f"[bridge] {src.id}: no acceptance tests in the ticket (the harness accepts any progress)")
        return f"{text}\n\n{notes}" if notes else text

    def _gh_comment(self, number: Optional[int], tag: str, body: str) -> None:
        if self.gh and number:
            self.gh.upsert_comment(number, tag, body + "\n\n<sub>tinystream bridge -- this comment is edited in place.</sub>")

    def _jira_comment(self, src: Source, text: str) -> None:
        assert self.jira is not None and src.jira_key
        prev = (self.state.get(src.id) or {}).get("jira_comment_id")
        res = self.jira.upsert_comment(src.jira_key, "agent", text, comment_id=prev)
        if isinstance(res, dict) and res.get("id") and not res.get("dry_run"):
            self.state.update(src.id, jira_comment_id=str(res["id"]))

    # -- HIL queue -------------------------------------------------------------------
    def poll_hil(self) -> list[dict[str, Any]]:
        assert self.gh is not None
        label = self.g["hil_label"]
        out = []
        for item in self.gh.labeled_pulls(label):
            number = int(item["number"])
            pull = self.gh.get_pull(number)
            head = pull.get("head") or {}
            sha, branch = str(head.get("sha") or ""), str(head.get("ref") or "")
            if pull.get("state") != "open" or not sha or self.hilq.find(number, sha):
                continue
            head_repo = ((head.get("repo") or {}).get("full_name") or "")
            reason = ""
            if self.g.get("same_repo_only", True) and head_repo != self.gh.repo:
                reason = f"the PR comes from `{head_repo or 'a deleted fork'}`; HIL runs only branches of {self.gh.repo}."
            allowed = [a.lower() for a in self.g.get("allowed_actors") or []]
            if not reason and allowed:
                who = self.gh.labeler(number, label)
                if (who or "").lower() not in allowed:
                    reason = f"`{label}` was added by `{who}`, who is not in the bridge's `allowed_actors`."
            if reason:
                entry = self.hilq.enqueue(pr=number, sha=sha, branch=branch, repo=self.gh.repo, status="refused",
                                          reason=reason)
                sections = self.state.pr_section(number, "hil", report.hil_refused(reason))
                self.gh.upsert_comment(number, "pr", report.compose(sections))
                out.append({"pr": number, "sha": sha, "status": "refused", "id": entry["id"]})
                continue
            cr = self.gh.check_run(sha, self.g["hil_check_name"], status="queued", title="HIL: queued",
                                   summary="Queued for the hardware-in-the-loop window (hil.py).",
                                   details_url=pull.get("html_url"))
            entry = self.hilq.enqueue(pr=number, sha=sha, branch=branch, repo=self.gh.repo,
                                      url=pull.get("html_url"), title=pull.get("title"),
                                      check_run_id=None if cr.get("fallback") or cr.get("dry_run") else cr.get("id"),
                                      comment_tag="pr", dry_run=self.dry_run)
            pos = self.hilq.position(entry["id"])
            sections = self.state.pr_section(number, "hil", report.hil_queued(sha, pos, entry["id"]))
            self.gh.upsert_comment(number, "pr", report.compose(sections))
            self.log(f"[bridge] PR #{number} {sha[:10]} -> HIL queue (position {pos})")
            out.append({"pr": number, "sha": sha, "status": "queued", "id": entry["id"], "position": pos})
        return out

    HIL_CONCLUSION = {"passed": "success", "failed": "failure", "skipped": "neutral"}

    def sync_hil(self) -> list[dict[str, Any]]:
        """What hil.py appended to the queue (running, passed, failed, skipped) -> the PR comment's
        `hil` section and the `hil` check (or commit status). Each status change is written once."""
        assert self.gh is not None
        synced = self.state.data.setdefault("hil_synced", {})
        out = []
        for e in self.hilq.entries():
            st = e.get("status")
            if st not in ("running", *self.HIL_CONCLUSION) or synced.get(e["id"]) == st:
                continue
            pr, sha = int(e["pr"]), str(e["sha"])
            sections = self.state.pr_section(pr, "hil", report.hil_result(e, self._hil_report(e)))
            self.gh.upsert_comment(pr, "pr", report.compose(sections))
            name, run_id = self.g["hil_check_name"], e.get("check_run_id")
            if st == "running":
                self.gh.check_run(sha, name, status="in_progress", title="HIL: running",
                                  summary="On the hardware-in-the-loop station now (hil.py).",
                                  details_url=e.get("url"), run_id=run_id)
            else:
                verdict = str(e.get("verdict") or st).upper()
                self.gh.check_run(sha, name, status="completed", conclusion=self.HIL_CONCLUSION[st],
                                  title=f"HIL: {verdict}", summary=f"hil.py verdict {verdict} (entry {e['id']}).",
                                  details_url=e.get("url"), run_id=run_id)
            synced[e["id"]] = st
            self.state.save()
            self.log(f"[bridge] PR #{pr} {sha[:10]} HIL {st}")
            out.append({"pr": pr, "sha": sha, "status": st, "id": e["id"]})
        return out

    def _hil_report(self, entry: dict[str, Any]) -> str:
        """hil.py's markdown report of the entry, when it was written on this machine."""
        rel = str(entry.get("report") or "")
        if not rel:
            return ""
        path = Path(rel) if Path(rel).is_absolute() else self.runner.repo / rel
        try:
            return path.read_text(encoding="utf-8")
        except OSError:
            return ""

    # -- CI (GitHub Actions) ---------------------------------------------------------
    def ci_start(self, issue_number: int, ticket_out: Path) -> Source:
        assert self.gh is not None
        src = sources.from_github_issue(self.gh.get_issue(issue_number), self.g["key_prefix"], self.g["fix_label"])
        ticket_out.parent.mkdir(parents=True, exist_ok=True)
        ticket_out.write_text(src.ticket_md, encoding="utf-8", newline="\n")
        self.notify_start(src)
        return src

    def ci_finish(self, issue_number: int, workdir: Path, run_id: Optional[str] = None) -> dict[str, Any]:
        assert self.gh is not None
        src = sources.from_github_issue(self.gh.get_issue(issue_number), self.g["key_prefix"], self.g["fix_label"])
        o = self.runner.collect(workdir, run_id=run_id)
        pr = self.publish(src, o) if o.ok else None
        self.notify_end(src, o, pr)
        return {"id": src.id, "status": o.status, "run_id": o.run_id, "pr": (pr or {}).get("number")}


# --------------------------------------------------------------------- CLI ----
def _paths(cfg: dict[str, Any], repo_root: Path, dry_run: bool) -> tuple[Path, Path]:
    def p(rel: str) -> Path:
        path = Path(rel) if Path(rel).is_absolute() else repo_root / rel
        return path.with_name(path.name + ".dry-run") if dry_run else path
    return p(cfg["bridge"]["state"]), p(cfg["bridge"]["hil_queue"])


def build(args: argparse.Namespace, *, transport: Optional[Transport] = None,
          jira_transport: Optional[Transport] = None, run_fn: Any = None) -> Bridge:
    cfg = load_config(Path(args.config) if args.config else None)
    repo_root = Path(args.repo_root).resolve() if args.repo_root else REPO
    dry = bool(args.dry_run or args.fixtures)
    gh_t: Transport = transport or (ReplayTransport.from_file(Path(args.fixtures) / "github.json")
                                    if args.fixtures else UrllibTransport())
    jr_t: Transport = jira_transport or (ReplayTransport.from_file(Path(args.fixtures) / "jira.json")
                                         if args.fixtures and (Path(args.fixtures) / "jira.json").is_file()
                                         else UrllibTransport())
    if dry:
        gh_t, jr_t = DryRunTransport(gh_t, log), DryRunTransport(jr_t, log)
    h = cfg["harness"]
    github = None
    if cfg["github"]["enabled"]:
        repo = cfg["github"]["repo"] or repo_from_remote(repo_root, h["remote"])
        if repo:
            github = GitHubClient(repo, os.environ.get("GITHUB_TOKEN") or os.environ.get("GH_TOKEN") or "",
                                  gh_t, cfg["github"]["api"])
        else:
            log("[bridge] no GitHub repo (github.repo, BRIDGE_GITHUB_REPO or an 'origin' remote on github.com)")
    jira = None
    if cfg["jira"]["enabled"]:
        if not cfg["jira"]["site"]:
            raise SystemExit("jira.enabled but no site (jira.site or JIRA_SITE)")
        jira = JiraClient(cfg["jira"]["site"], os.environ.get("JIRA_EMAIL", ""), os.environ.get("JIRA_API_TOKEN", ""),
                          jr_t)
    fake = bool(h.get("fake") or getattr(args, "fake_runtime", False)
                or (dry and not getattr(args, "live_runtime", False)))
    kw: dict[str, Any] = {"run_fn": run_fn} if run_fn else {}
    runner = HarnessRunner(repo_root, workflow=h["workflow"], mode=h["mode"], runtime=h["runtime"], fake=fake,
                           base_ref=cfg["github"]["base"], remote=h["remote"], fetch=bool(h["fetch"]),
                           worktree_root=Path(h["worktree_root"]) if h.get("worktree_root") else None,
                           timeout_s=float(h["timeout_s"]), in_place=bool(getattr(args, "in_place", False)),
                           log=log, **kw)
    state_path, queue_path = _paths(cfg, repo_root, dry)
    return Bridge(cfg, github=github, jira=jira, runner=runner, state=State(state_path), hilq=HilQueue(queue_path),
                  dry_run=dry)


def parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python tools/bridge/bridge.py", description=__doc__.split("\n\n")[0])
    p.add_argument("--config", help="bridge.yaml (default tools/bridge/bridge.yaml)")
    p.add_argument("--repo-root", help="tinystream checkout the bridge works from (default: this one)")
    p.add_argument("--dry-run", action="store_true", help="no write leaves the machine; fake runtime")
    p.add_argument("--fixtures", help="answer GitHub/Jira from DIR/github.json, DIR/jira.json (implies --dry-run)")
    p.add_argument("--fake-runtime", action="store_true", help="harness --fake: scripted runtime, no model")
    p.add_argument("--live-runtime", action="store_true", help="with --dry-run: still use the real runtime")
    sub = p.add_subparsers(dest="cmd", required=True)
    sub.add_parser("check", help="read-only: GitHub repo/labels, Jira search")
    sub.add_parser("once", help="poll once")
    pl = sub.add_parser("poll", help="poll until Ctrl-C")
    pl.add_argument("--interval", type=float, default=None)
    sub.add_parser("status", help="handled items and the HIL queue")
    sub.add_parser("labels", help="create missing trigger labels on GitHub")
    cs = sub.add_parser("ci-start", help="(Actions) issue -> ticket file, labels, start comment")
    cs.add_argument("--issue", type=int, required=True)
    cs.add_argument("--ticket-out", required=True)
    cf = sub.add_parser("ci-finish", help="(Actions) run summary -> push, PR, check run, comments")
    cf.add_argument("--issue", type=int, required=True)
    cf.add_argument("--workdir", default=".")
    cf.add_argument("--run-id", default=None)
    for sp in (cs, cf):
        sp.set_defaults(in_place=True)
    return p


def cmd_check(b: Bridge) -> int:
    ok = True
    if b.gh:
        try:
            repo = b.gh.get_repo()
            owner = (repo.get("owner") or {}).get("type")
            print(f"github: {b.gh.repo} ok (owner type {owner}, default branch {repo.get('default_branch')}, "
                  f"push={((repo.get('permissions') or {}).get('push'))})")
            if owner != "Organization":
                print("github: personal repository -> the Actions workflow stays inert (Copilot requests need an "
                      "org-owned repo); the polling bridge works")
            for key in LABEL_COLORS:
                name = b.g[key]
                print(f"github: label {name!r} " + ("ok" if b.gh.get_label(name) else "MISSING (bridge.py labels)"))
        except HttpError as exc:
            ok = False
            print(f"github: {exc}")
    else:
        print("github: disabled or no repo")
    if b.jira:
        try:
            me = b.jira.myself()
            print(f"jira: {b.jira.site} as {me.get('displayName') or me.get('emailAddress')}")
            j = b.j
            jql = j.get("jql") or sources.default_jql(str(j.get("project") or ""), list(j.get("trigger_statuses") or []),
                                                      str(j.get("trigger_label") or ""))
            print(f"jira: {len(b.jira.search(jql))} issue(s) match: {jql}")
        except (HttpError, ValueError) as exc:
            ok = False
            print(f"jira: {exc}")
    print(f"harness: workflow {b.h['workflow']}, mode {b.h['mode']}, runtime {b.h['runtime']}"
          + (" (fake runtime)" if b.runner.fake else "") + f", worktrees in {b.runner.worktree_root}")
    return 0 if ok else 1


def cmd_status(b: Bridge) -> int:
    print(f"state: {b.state.path}")
    for i, it in sorted(b.state.items.items()):
        print(f"  {i:<16} {it.get('status', '-'):<10} {it.get('run_id') or '-':<40} "
              f"{('PR #' + str(it['pr'])) if it.get('pr') else ''}")
    print(f"HIL queue: {b.hilq.path}")
    for e in b.hilq.entries():
        print(f"  {e['id']:<20} {e.get('status', '-'):<9} PR #{e.get('pr')} {str(e.get('sha', ''))[:10]} "
              f"{e.get('branch', '')}")
    return 0


def cmd_labels(b: Bridge) -> int:
    if not b.gh:
        print("labels: no GitHub repo", file=sys.stderr)
        return 2
    for key, (color, desc) in LABEL_COLORS.items():
        name = b.g[key]
        if b.gh.get_label(name):
            print(f"label {name!r}: exists")
        else:
            b.gh.create_label(name, color, desc)
            print(f"label {name!r}: created" + (" (dry-run)" if b.dry_run else ""))
    return 0


def main(argv: Optional[list[str]] = None, **build_kw: Any) -> int:
    args = parser().parse_args(argv)
    b = build(args, **build_kw)
    if args.cmd == "check":
        return cmd_check(b)
    if args.cmd == "status":
        return cmd_status(b)
    if args.cmd == "labels":
        return cmd_labels(b)
    if args.cmd == "once":
        rep = b.poll_once()
        print(json.dumps(rep, indent=2))
        return 1 if rep["errors"] or any(r["status"] not in ("done",) for r in rep["fix"]) else 0
    if args.cmd == "poll":
        interval = args.interval or float(b.cfg["bridge"]["interval_s"])
        log(f"[bridge] polling every {interval:.0f} s" + (" (dry-run)" if b.dry_run else "") + "; Ctrl-C stops")
        try:
            while True:
                rep = b.poll_once()
                if rep["fix"] or rep["hil"]:
                    log(json.dumps({k: v for k, v in rep.items() if v}))
                time.sleep(interval)
        except KeyboardInterrupt:
            return 0
    if args.cmd == "ci-start":
        src = b.ci_start(args.issue, Path(args.ticket_out))
        print(src.key)
        return 0
    if args.cmd == "ci-finish":
        res = b.ci_finish(args.issue, Path(args.workdir), args.run_id)
        print(json.dumps(res))
        return 0 if res["pr"] else 1
    return 2


if __name__ == "__main__":
    sys.exit(main())
