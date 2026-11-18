"""hilqueue.py -- the HIL queue: candidates from a local file and (optionally) labelled GitHub PRs.

Queue file (hil.yaml queue.file), one candidate per line, '#' starts a comment:

    agent/fix-stripes-1a2b3c   unit 3, stripes
    refs/heads/my-fix

Every candidate is identified by ref + commit: a candidate that already got a
verdict (PASS / FAIL / DECLINED) is not run again until its ref moves to a new
commit. The state lives in queue.state (JSON) next to the runs.

Team bridge (queue.bridge, default .harness/hil/queue.jsonl): the PRs that
tools/bridge queued (label hil-ready) come next. The file is the bridge's
append-only JSON-lines log; an entry's state is the fold of its lines. Entries
with status "queued" are run (head fetched into refs/hil/pr-<n>) and get an
"update" line: running, then passed / failed / skipped (declined). The bridge
queue and the station's own GitHub polling see a PR as the same candidate
(refs/hil/pr-<n> at its head sha), so it runs once.

GitHub (github.enabled): open PRs with github.label are fetched into
refs/hil/pr-<n> and queued after the file and bridge entries; the report goes back as a PR
comment and the result label replaces the trigger label (all writes skipped
and recorded in --dry-run, see ghclient.py). Before every deploy the target's
approval rule applies (hil.yaml targets.<t>.approval: human asks facilitator B).
"""
from __future__ import annotations

import datetime as _dt
import json
from pathlib import Path
from typing import Any, Callable, Optional

from hilcore import Candidate, HilError, Shell, resolve, run_candidate

DONE = ("PASS", "FAIL", "DECLINED")


def parse_queue(text: str) -> list[Candidate]:
    out: list[Candidate] = []
    for raw in text.splitlines():
        line = raw.split("#", 1)[0].strip()
        if not line:
            continue
        parts = line.split(None, 1)
        out.append(Candidate(ref=parts[0], note=parts[1].strip() if len(parts) > 1 else ""))
    return out


class QueueState:
    def __init__(self, path: Path) -> None:
        self.path = path
        self.data: dict[str, dict] = {}
        if path.exists():
            self.data = json.loads(path.read_text(encoding="utf-8")).get("runs", {})

    @staticmethod
    def key(c: Candidate) -> str:
        return "%s@%s" % (c.ref, c.sha)

    def done(self, c: Candidate) -> bool:
        return self.data.get(self.key(c), {}).get("verdict") in DONE

    def record(self, c: Candidate, verdict: str, report: str) -> None:
        self.data[self.key(c)] = {"verdict": verdict, "report": report, "pr": c.pr,
                                  "at": _dt.datetime.now().isoformat(timespec="seconds")}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self.path.write_text(json.dumps({"runs": self.data}, indent=1) + "\n", encoding="utf-8", newline="\n")


def pr_candidate(n: int, sha: str, title: str, cfg: dict, shell: Shell,
                 log: Callable[[str], None] = print) -> Optional[Candidate]:
    """PR head -> refs/hil/pr-<n> (fetched from the remote when the commit is not local)."""
    ref = "refs/hil/pr-%d" % n
    have = shell.run(["git", "cat-file", "-e", sha + "^{commit}"], check=False, capture=True)
    if have.returncode != 0:
        # a read from the PR's own remote; nothing is pushed
        p = shell.run(["git", "fetch", "--no-tags", cfg["github"].get("remote", "origin"),
                       "pull/%d/head:%s" % (n, ref)], remote=True, check=False, capture=True)
        if p.returncode != 0 or shell.dry_run:
            log("hil: PR #%d: head %s not available locally%s, skipped" %
                (n, sha[:10], " (dry run: no fetch)" if shell.dry_run else ""))
            return None
    else:
        shell.run(["git", "update-ref", ref, sha])
    return Candidate(ref=ref, sha=sha, name="pr-%d" % n, note=title, pr=n)


def github_candidates(gh: Any, cfg: dict, shell: Shell, log: Callable[[str], None] = print) -> list[Candidate]:
    out = []
    for pr in gh.labeled_pulls(cfg["github"]["label"]):
        c = pr_candidate(pr["number"], pr["head_sha"], pr.get("title", ""), cfg, shell, log)
        if c is not None:
            out.append(c)
    return out


class BridgeQueue:
    """The team bridge's HIL queue (the tools/bridge store.HilQueue format), read and appended here."""

    STATUS = {"PASS": "passed", "FAIL": "failed", "DECLINED": "skipped"}

    def __init__(self, path: Path) -> None:
        self.path = path

    def entries(self) -> list[dict]:
        folded: dict[str, dict] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if isinstance(rec, dict) and "id" in rec:
                folded.setdefault(rec["id"], {}).update({k: v for k, v in rec.items() if k != "op"})
        return list(folded.values())

    def pending(self) -> list[dict]:
        return [e for e in self.entries() if e.get("status") == "queued"]

    def update(self, entry_id: str, **fields: Any) -> None:
        rec = {"op": "update", "id": entry_id,
               "updated_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"), **fields}
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")


def bridge_candidates(bq: BridgeQueue, cfg: dict, shell: Shell,
                      log: Callable[[str], None] = print) -> list[tuple[Candidate, str]]:
    out = []
    for e in bq.pending():
        if not isinstance(e.get("pr"), int) or not e.get("sha"):
            log("hil: bridge queue: entry %s without pr / sha, skipped" % e.get("id"))
            continue
        c = pr_candidate(e["pr"], str(e["sha"]), str(e.get("title", "")), cfg, shell, log)
        if c is not None:
            out.append((c, str(e["id"])))
    return out


def process(cfg: dict, target: str, shell: Shell, approve: Callable[[Candidate, str], bool],
            gh: Any = None, queue_file: Optional[str] = None, checks: Optional[list[str]] = None,
            run_fn: Callable[..., Any] = run_candidate, log: Callable[[str], None] = print,
            receiver: Optional[str] = None) -> list[dict]:
    """One pass over the queue. Returns [{ref, sha, pr, verdict, report}] for the candidates run now."""
    root = shell.root
    qpath = root / (queue_file or cfg["queue"]["file"])
    state = QueueState(root / cfg["queue"]["state"])
    cands = parse_queue(qpath.read_text(encoding="utf-8")) if qpath.exists() else []
    for c in cands:
        try:
            c.sha = resolve(shell, c.ref)
        except HilError as exc:
            log("hil: queue: %s" % exc)
            c.sha = ""
    cands = [c for c in cands if c.sha]
    bridge_ids: dict[str, str] = {}
    bq_path = cfg["queue"].get("bridge")
    bq = BridgeQueue(root / bq_path) if bq_path else None
    if bq is not None:
        for c, entry_id in bridge_candidates(bq, cfg, shell, log):
            bridge_ids[QueueState.key(c)] = entry_id
            cands.append(c)
    if gh is not None:
        cands += github_candidates(gh, cfg, shell, log)
    write_bq = bq is not None and not shell.dry_run
    results = []
    seen = set()
    for c in cands:
        key = QueueState.key(c)
        if key in seen:
            continue
        seen.add(key)
        entry_id = bridge_ids.get(key)
        if state.done(c):
            if entry_id and write_bq:   # this commit got its verdict before the bridge queued it
                prev = state.data[key]
                bq.update(entry_id, status=BridgeQueue.STATUS[prev["verdict"]], verdict=prev["verdict"],
                          report=prev.get("report", ""))
            continue
        if entry_id and write_bq:
            bq.update(entry_id, status="running")
        res = run_fn(cfg, c, target, shell, approve, checks=checks, log=log, receiver=receiver)
        report_path = getattr(res, "report_path", "")
        if entry_id and write_bq:
            bq.update(entry_id, status=BridgeQueue.STATUS.get(res.verdict, "failed"), verdict=res.verdict,
                      report=report_path)
        state.record(c, res.verdict, report_path)
        results.append({"ref": c.ref, "sha": c.sha, "pr": c.pr, "verdict": res.verdict, "report": report_path})
        if gh is not None and c.pr is not None and res.verdict in ("PASS", "FAIL", "DRY-RUN"):
            g = cfg["github"]
            if g.get("comment", True) and res.report:
                gh.comment(c.pr, res.report)
            labels = g.get("result_labels", {})
            lab = labels.get("pass" if res.verdict == "PASS" else "fail") if res.verdict != "DRY-RUN" else None
            if lab:
                gh.add_labels(c.pr, [lab])
            if g.get("remove_trigger_label", True) and res.verdict != "DRY-RUN":
                gh.remove_label(c.pr, g["label"])
    return results
