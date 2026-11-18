"""Queue processing, approval (human in the loop) and the Pi target in dry-run mode.

No container and no hardware: builds are stubbed, the shell records commands
and answers git queries from a table.
"""
from __future__ import annotations

import copy
import io
import subprocess
from pathlib import Path

import pytest

import hilcore
import hilqueue
from ghclient import GitHubClient, RecordedTransport

FIX = Path(__file__).resolve().parent / "fixtures" / "github"


class FakeShell(hilcore.Shell):
    """Records every command; git answers come from `refs` (name -> sha)."""

    def __init__(self, root: Path, refs: dict, dry_run: bool = False) -> None:
        super().__init__(root=root, dry_run=dry_run)
        self.refs = refs

    def run(self, cmd, *, remote=False, input=None, check=True, capture=False, timeout=None, binary=False,
            env=None):
        cmd = [str(c) for c in cmd]
        skip = remote and self.dry_run
        self.log.append(("[dry-run] " if skip else "") + " ".join(cmd) + (" <<< (stdin)" if input else ""))
        self.inputs = getattr(self, "inputs", []) + ([input] if input else [])
        out, rc = "", 0
        if cmd[:2] == ["git", "rev-parse"] and "--verify" in cmd:
            ref = cmd[-1].replace("^{commit}", "")
            rc = 0 if ref in self.refs else 1
            out = self.refs.get(ref, "")
        elif cmd[:2] == ["git", "cat-file"]:
            rc = 0 if cmd[3].replace("^{commit}", "") in self.refs.values() else 1
        elif cmd[:2] == ["git", "rev-parse"]:
            out = "station0000"
        if check and rc != 0:
            raise hilcore.HilError("fake: %s" % " ".join(cmd))
        return subprocess.CompletedProcess(cmd, rc, out, "")


@pytest.fixture
def qcfg(cfg, tmp_path):
    c = copy.deepcopy(cfg)
    c["station"]["work_dir"] = str(tmp_path / "work").replace("\\", "/")
    c["queue"]["file"] = str(tmp_path / "queue.txt")
    c["queue"]["state"] = str(tmp_path / "state.json")
    c["github"].update({"enabled": True, "repo": "example-org/tinystream"})
    return c


def fake_run_fn(verdicts):
    calls = []

    def run_fn(cfg, cand, target, shell, approve, checks=None, log=print, receiver=None):
        calls.append(cand)
        ok = approve(cand, target)
        v = verdicts.get(cand.ref, "PASS") if ok else "DECLINED"
        return hilcore.RunResult(verdict=v, run_dir="r", report_path="r/report.md",
                                 report="report for %s" % cand.ref)
    run_fn.calls = calls
    return run_fn


def test_parse_queue():
    q = hilqueue.parse_queue("# comment\n\nmain  golden, projector\nagent/x-1 # trailing\n")
    assert [(c.ref, c.note) for c in q] == [("main", "golden, projector"), ("agent/x-1", "")]


def test_queue_runs_each_commit_once(qcfg, tmp_path):
    Path(qcfg["queue"]["file"]).write_text("main\nagent/a\nmissing-ref\n")
    shell = FakeShell(tmp_path, {"main": "a" * 40, "agent/a": "b" * 40})
    run_fn = fake_run_fn({"agent/a": "FAIL"})
    logs = []
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=logs.append)
    assert [(r["ref"], r["verdict"]) for r in res] == [("main", "PASS"), ("agent/a", "FAIL")]
    assert any("unknown git ref 'missing-ref'" in m for m in logs)
    # second pass: nothing new
    assert hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=logs.append) == []
    # the ref moves: run again
    shell.refs["agent/a"] = "c" * 40
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=logs.append)
    assert [(r["ref"], r["sha"][:1]) for r in res] == [("agent/a", "c")]


def test_declined_is_remembered(qcfg, tmp_path):
    Path(qcfg["queue"]["file"]).write_text("main\n")
    shell = FakeShell(tmp_path, {"main": "a" * 40})
    run_fn = fake_run_fn({})
    res = hilqueue.process(qcfg, "pi", shell, lambda c, t: False, run_fn=run_fn, log=lambda m: None)
    assert res[0]["verdict"] == "DECLINED"
    assert hilqueue.process(qcfg, "pi", shell, lambda c, t: True, run_fn=run_fn, log=lambda m: None) == []


def test_github_prs_comment_and_labels_dry_run(qcfg, tmp_path):
    shell = FakeShell(tmp_path, {"head7": "1" * 40}, dry_run=True)   # PR 7 head is local, PR 9 is not
    gh = GitHubClient("example-org/tinystream", dry_run=True, transport=RecordedTransport.from_dir(str(FIX)),
                      out_dir=str(tmp_path))
    logs = []
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, gh=gh, run_fn=fake_run_fn({}),
                           log=logs.append)
    assert [(r["pr"], r["ref"], r["verdict"]) for r in res] == [(7, "refs/hil/pr-7", "PASS")]
    assert any("PR #9" in m and "dry run: no fetch" in m for m in logs)
    assert "[dry-run] git fetch --no-tags origin pull/9/head:refs/hil/pr-9" in shell.log
    assert [(e["method"], e["path"].rsplit("/", 2)[-2:]) for e in gh.dry_log] == [
        ("POST", ["7", "comments"]), ("POST", ["7", "labels"]), ("DELETE", ["labels", "hil-ready"])]
    assert gh.dry_log[0]["payload"]["body"] == "report for refs/hil/pr-7"
    assert gh.dry_log[1]["payload"] == {"labels": ["hil-pass"]}


def test_bridge_queue_entries_run_once_and_get_a_status(qcfg, tmp_path):
    import json
    qcfg["github"]["enabled"] = False
    qcfg["queue"]["bridge"] = "bridge/queue.jsonl"
    bq = tmp_path / "bridge" / "queue.jsonl"
    bq.parent.mkdir()
    lines = [{"op": "enqueue", "id": "pr31-aaaaaaa", "pr": 31, "sha": "a" * 40, "status": "queued", "title": "t"},
             {"op": "enqueue", "id": "pr32-bbbbbbb", "pr": 32, "sha": "b" * 40, "status": "queued"},
             {"op": "enqueue", "id": "pr33-ccccccc", "pr": 33, "sha": "c" * 40, "status": "refused"},
             {"op": "enqueue", "id": "pr34-ddddddd", "pr": 34, "sha": "d" * 40, "status": "queued"},
             {"op": "update", "id": "pr34-ddddddd", "status": "passed"}]
    bq.write_text("".join(json.dumps(x) + "\n" for x in lines), encoding="utf-8")
    shell = FakeShell(tmp_path, {"pr31": "a" * 40, "pr32": "b" * 40})
    run_fn = fake_run_fn({"refs/hil/pr-32": "FAIL"})
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=lambda m: None)
    assert [(r["pr"], r["ref"], r["verdict"]) for r in res] == [(31, "refs/hil/pr-31", "PASS"),
                                                                (32, "refs/hil/pr-32", "FAIL")]
    assert "git update-ref refs/hil/pr-31 " + "a" * 40 in shell.log
    folded = {e["id"]: e for e in hilqueue.BridgeQueue(bq).entries()}
    assert folded["pr31-aaaaaaa"]["status"] == "passed" and folded["pr31-aaaaaaa"]["report"] == "r/report.md"
    assert folded["pr32-bbbbbbb"]["status"] == "failed" and folded["pr32-bbbbbbb"]["verdict"] == "FAIL"
    assert folded["pr33-ccccccc"]["status"] == "refused" and folded["pr34-ddddddd"]["status"] == "passed"
    assert [x["status"] for x in map(json.loads, bq.read_text().splitlines()) if x["id"] == "pr31-aaaaaaa"] == \
        ["queued", "running", "passed"]
    # nothing pending any more: a second pass runs nothing
    assert hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=lambda m: None) == []
    # the bridge queues a commit the station already judged: status from the earlier verdict, no rerun
    with open(bq, "a", encoding="utf-8") as fh:
        fh.write(json.dumps({"op": "enqueue", "id": "pr31-again", "pr": 31, "sha": "a" * 40,
                             "status": "queued"}) + "\n")
    assert hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=run_fn, log=lambda m: None) == []
    assert {e["id"]: e for e in hilqueue.BridgeQueue(bq).entries()}["pr31-again"]["status"] == "passed"
    assert len(run_fn.calls) == 2


def test_bridge_queue_untouched_in_dry_run(qcfg, tmp_path):
    import json
    qcfg["github"]["enabled"] = False
    qcfg["queue"]["bridge"] = "q.jsonl"
    bq = tmp_path / "q.jsonl"
    bq.write_text(json.dumps({"op": "enqueue", "id": "pr7-1", "pr": 7, "sha": "1" * 40, "status": "queued"}) + "\n")
    before = bq.read_text()
    shell = FakeShell(tmp_path, {"head7": "1" * 40}, dry_run=True)
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=fake_run_fn({}), log=lambda m: None)
    assert [r["pr"] for r in res] == [7] and bq.read_text() == before


def test_approver_human_needs_a_terminal(cfg):
    cand = hilcore.Candidate(ref="agent/x", sha="f" * 40, pr=7)
    out = io.StringIO()
    no_tty = io.StringIO("y\n")                       # StringIO.isatty() is False
    assert hilcore.make_approver(cfg, stdin=no_tty, out=out)(cand, "pi") is False
    assert "no terminal: not approved" in out.getvalue()
    assert hilcore.make_approver(cfg, stdin=no_tty, out=out)(cand, "local") is True   # local: approval auto
    assert hilcore.make_approver(cfg, yes=True, stdin=no_tty, out=out)(cand, "pi") is True

    class Tty(io.StringIO):
        def isatty(self):
            return True
    assert hilcore.make_approver(cfg, stdin=Tty("y\n"), out=out)(cand, "pi") is True
    assert hilcore.make_approver(cfg, stdin=Tty("\n"), out=out)(cand, "pi") is False
    assert "deploy PR #7 agent/x" in out.getvalue()


@pytest.fixture
def no_builds(monkeypatch):
    def prepare(shell, cfg, cand):
        cand.sha, cand.name, cand.dir = "d" * 40, hilcore.slug(cand.ref), "build-hil/cand/x"
        return cand
    monkeypatch.setattr(hilcore, "prepare", prepare)
    monkeypatch.setattr(hilcore, "build", lambda *a, **k: None)
    monkeypatch.setattr(hilcore, "ensure_station_tools", lambda shell, cfg: "build-host")
    monkeypatch.setattr(hilcore, "ensure_clip", lambda shell, cfg: "build-hil/poster_30f.y8")


def test_pi_dry_run_lists_the_deploy(qcfg, tmp_path, no_builds):
    shell = FakeShell(tmp_path, {}, dry_run=True)
    res = hilcore.run_candidate(qcfg, hilcore.Candidate(ref="agent/x"), "pi", shell, lambda c, t: True,
                                checks=["file", "camera"], log=lambda m: None)
    assert res.verdict == "DRY-RUN"
    log = "\n".join(shell.log)
    dest = "hil@10.42.0.2"
    assert "[dry-run] scp" in log and "build-hil/cand/x/build-pi/tinystreamd %s:/tmp/tinystreamd.hil" % dest in log
    assert "sudo install -m 0755 /tmp/tinystreamd.hil /usr/local/bin/tinystreamd" in log
    assert "sudo systemctl restart tinystreamd" in log
    assert "measure.py --spec" in log
    for line in shell.log:                            # nothing reached the Pi
        if line.replace("[dry-run] ", "").split()[0] in ("ssh", "scp"):
            assert line.startswith("[dry-run] ")
    envs = [i for i in shell.inputs if "TSD_SOURCE" in i]
    assert [e.split("TSD_SOURCE=")[1].split()[0] for e in envs] == ["file", "camera", "camera"]  # + leave_source
    assert "--seq-start %d" % hilcore.seq_start_of(qcfg["checks"]["file"]) in envs[0]
    md = (tmp_path / "work").glob("runs/*/report.md")
    text = next(md).read_text()
    assert "Dry run" in text and "HIL DRY-RUN" in text


def test_pi_declined_deploys_nothing(qcfg, tmp_path, no_builds):
    shell = FakeShell(tmp_path, {}, dry_run=False)
    res = hilcore.run_candidate(qcfg, hilcore.Candidate(ref="agent/x"), "pi", shell, lambda c, t: False,
                                log=lambda m: None)
    assert res.verdict == "DECLINED"
    assert not any(line.startswith(("ssh", "scp")) for line in shell.log)


def test_pi_camera_check_pauses_rpicam(qcfg, tmp_path):
    shell = FakeShell(tmp_path, {}, dry_run=True)
    t = hilcore.PiTarget(qcfg, shell)
    run_dir = "%s/runs/x" % qcfg["station"]["work_dir"]
    r = t.run_check(hilcore.Candidate(ref="x", dir="c"), "camera", run_dir, "clip.y8", "build-host")
    ev = r["spec"]["events"]
    assert [(e["at"], e["cmd"][-1]) for e in ev] == [(4.0, "sudo pkill -STOP -x rpicam-vid"),
                                                     (7.0, "sudo pkill -CONT -x rpicam-vid")]
    assert r["spec"]["mode"] == "remote" and r["spec"]["server"] == "10.42.0.2" and "ref" not in r["spec"]


def test_bridge_queue_format_matches_the_bridge_store(qcfg, tmp_path):
    # the bridge's own writer (tools/bridge/store.py) and this reader agree on the format
    import importlib.util
    store_py = Path(hilcore.__file__).resolve().parents[1] / "bridge" / "store.py"
    spec = importlib.util.spec_from_file_location("bridge_store_for_hil_test", store_py)
    store = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(store)
    path = tmp_path / ".harness" / "hil" / "queue.jsonl"
    q = store.HilQueue(path)
    e = q.enqueue(pr=41, sha="e" * 40, branch="agent/gh-9-c0de", repo="o/r", title="fix", check_run_id=5)
    qcfg["github"]["enabled"] = False
    qcfg["queue"]["bridge"] = ".harness/hil/queue.jsonl"
    shell = FakeShell(tmp_path, {"pr41": "e" * 40})
    res = hilqueue.process(qcfg, "local", shell, lambda c, t: True, run_fn=fake_run_fn({}), log=lambda m: None)
    assert [(r["pr"], r["verdict"]) for r in res] == [(41, "PASS")]
    (done,) = q.entries()
    assert done["id"] == e["id"] and done["status"] == "passed" and done["check_run_id"] == 5
    assert q.pending() == [] and q.position(e["id"]) == 0
