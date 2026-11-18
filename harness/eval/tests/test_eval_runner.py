"""End-to-end: the runner on a throwaway repo with the fake orchestrator and fake hidden tests."""
from __future__ import annotations

import json
import subprocess
from pathlib import Path

import pytest

from harness import names

from harness import api
from harness.eval import board, cli, results, runner, variants
from harness.eval.fake import FAKE_FIX_MARKER
from harness.eval.fake import run_workflow as fake_run


def _git(cwd: Path, *args: str) -> str:
    return subprocess.run(["git", *args], cwd=cwd, check=True, capture_output=True, text=True).stdout.strip()


def _cfg(repo: Path, hidden: Path, variants_file: Path, tmp_path: Path, **kw) -> runner.EvalConfig:
    m = variants.load_matrix(variants_file)
    base = dict(repo=repo, hidden_dir=hidden, variants=m.select(None), tickets=["EV-1", "EV-2"],
                experiment="t1", results_dir=tmp_path / "results", worktree_root=tmp_path / "wt",
                gate_weights=m.gate_weights, parallel=3, repeats=2, golden_ref="golden",
                hidden_cmd=cli.FAKE_HIDDEN_CMD, backend=fake_run, log=lambda s: None)
    base.update(kw)
    return runner.EvalConfig(**base)


def test_matrix_end_to_end(repo, hidden, variants_file, tmp_path):
    cfg = _cfg(repo, hidden, variants_file, tmp_path)
    recs = runner.Runner(cfg).run()
    assert len(recs) == 2 * 2 * 2
    lines = (tmp_path / "results" / "t1.jsonl").read_text(encoding="utf-8").splitlines()
    assert len(lines) == 8
    for rec in recs:
        assert rec["run_status"] == "done", rec.get("error")
        wt = Path(rec["worktree"])
        # one worktree per (variant, ticket, repeat), on its own branch, from the ticket's seed
        assert wt.is_dir() and _git(wt, "branch", "--show-current") == rec["branch"]
        assert rec["seed_commit"] == _git(repo, "rev-parse", f"ticket/{rec['ticket']}")
        assert rec["ticket"].replace("-", "_").lower() in (wt / "src" / "a.c").read_text()
        # success comes from the hidden tests, which pass exactly when the fake left its fix
        assert rec["success"] == (wt / FAKE_FIX_MARKER).is_file() == rec["workflow_success"]
        assert rec["hidden"]["status"] == ("pass" if rec["success"] else "fail")
        assert rec["metrics_source"] == "audit"
        assert rec["interventions"] == rec["approvals"] + rec["escalations"]
        assert rec["ai_credits"] > 0 and rec["tokens"]["in"] > 0
        assert rec["golden_diff"]["files"] == 1           # seed vs golden ref
        assert rec["path_violations"] == ([".eval-fake-fix"] if rec["success"] else [])
        run_dir = Path(rec["run_dir"])
        for f in ("settings.json", "run_result.json", "events.jsonl", "hidden.log", "record.json"):
            assert (run_dir / f).is_file(), f
        settings = json.loads((run_dir / "settings.json").read_text())
        assert settings["workdir"] == rec["worktree"] and settings["variant"] == rec["variant"]
        assert "root_cause" not in json.dumps(rec)
    # variant settings reach the backend
    b = [r for r in recs if r["variant"] == "B"][0]
    assert b["settings"]["roles"]["planner"] and b["settings"]["models"]["planner"] == "m-sonnet"
    # leaderboard file
    board_doc = json.loads((tmp_path / "results" / "results.json").read_text())
    assert {r["variant"] for r in board_doc["variants"]} == {"A", "B"}
    assert sum(r["runs"] for r in board_doc["variants"]) == 8
    assert len(board_doc["runs"]) == 8


def test_resume_skips_done_runs(repo, hidden, variants_file, tmp_path):
    cfg = _cfg(repo, hidden, variants_file, tmp_path, repeats=1, tickets=["EV-1"])
    calls = []

    def counting(*a):
        calls.append(a[1])
        return fake_run(*a)

    runner.Runner(_cfg(repo, hidden, variants_file, tmp_path, repeats=1, tickets=["EV-1"],
                       variants=variants.load_matrix(variants_file).select(["A"]), backend=counting)).run()
    assert len(calls) == 1
    cfg.resume = True
    cfg.backend = counting
    out = runner.Runner(cfg).run()
    assert len(calls) == 2 and [r["variant"] for r in out] == ["B"]      # A was done already
    assert runner.Runner(cfg).run() == []


def test_errors_are_recorded_and_the_rest_runs(repo, hidden, variants_file, tmp_path):
    def flaky(workflow, ticket, settings, mode):
        if settings.variant == "B":
            raise RuntimeError("model fell over")
        return fake_run(workflow, ticket, settings, mode)

    cfg = _cfg(repo, hidden, variants_file, tmp_path, repeats=1, tickets=["EV-1", "EV-404"], backend=flaky)
    recs = {(r["variant"], r["ticket"]): r for r in runner.Runner(cfg).run()}
    assert recs[("A", "EV-404")]["run_status"] == "setup_error"           # no ticket/EV-404 branch
    assert "ticket/EV-404" in recs[("A", "EV-404")]["error"]
    b = recs[("B", "EV-1")]
    assert b["run_status"] == "done" and b["status"] == "error" and not b["success"]
    assert "model fell over" in b["error"]
    assert (Path(b["run_dir"]) / "workflow_error.txt").is_file()
    assert recs[("A", "EV-1")]["run_status"] == "done"


def test_result_fields_used_without_audit_trail(repo, hidden, variants_file, tmp_path):
    def no_audit(workflow, ticket, settings, mode):
        return api.RunResult(status="completed", success=False, approvals=3, escalations=1, retries=2,
                             ai_credits=1.5, tokens_in=7, gate_results={"no-heap": "FAIL", "unit-tests": "FAIL"},
                             gate_violations=2, reviewer_score=2.0, duration_s=42.0)

    cfg = _cfg(repo, hidden, variants_file, tmp_path, repeats=1, tickets=["EV-1"], backend=no_audit,
               variants=variants.load_matrix(variants_file).select(["A"]))
    (rec,) = runner.Runner(cfg).run()
    assert rec["metrics_source"] == "result"
    assert (rec["interventions"], rec["retries"], rec["ai_credits"], rec["tokens"]["in"]) == (4, 2, 1.5, 7)
    assert rec["gate_violations"] == 2 and rec["gate_violation_score"] == 4.0     # no-heap weighs 3
    assert rec["duration_s"] == 42.0 and rec["reviewer_score"] == 2.0 and not rec["success"]


def test_audit_trail_wins_over_result(repo, hidden, variants_file, tmp_path):
    def liar(workflow, ticket, settings, mode):
        p = Path(settings.run_dir) / "audit" / "events.jsonl"
        p.parent.mkdir(parents=True)
        p.write_text("\n".join(json.dumps(e) for e in [
            {"type": "assistant.usage", "data": {"inputTokens": 1000, "outputTokens": 10}},
            {"type": "session.usage_checkpoint", "data": {"sessionId": "s", "totalNanoAiu": 2_000_000_000}},
            {"type": "harness.approval", "data": {"step": "plan"}},
        ]) + "\n", encoding="utf-8")
        return api.RunResult(status="completed", success=True, approvals=0, ai_credits=0.01,
                             audit_path=str(p))

    cfg = _cfg(repo, hidden, variants_file, tmp_path, repeats=1, tickets=["EV-1"], backend=liar,
               variants=variants.load_matrix(variants_file).select(["A"]))
    (rec,) = runner.Runner(cfg).run()
    assert (rec["ai_credits"], rec["approvals"], rec["tokens"]["in"]) == (2.0, 1, 1000)
    assert (Path(rec["run_dir"]) / "events.jsonl").is_file()                    # trail copied


def test_rerun_resets_worktree_and_cleanup(repo, hidden, variants_file, tmp_path):
    kw = dict(repeats=1, tickets=["EV-1"], variants=variants.load_matrix(variants_file).select(["A"]))
    (first,) = runner.Runner(_cfg(repo, hidden, variants_file, tmp_path, **kw)).run()
    wt = Path(first["worktree"])
    (wt / "stray.txt").write_text("left over", encoding="utf-8")
    (second,) = runner.Runner(_cfg(repo, hidden, variants_file, tmp_path, cleanup=True, **kw)).run()
    assert "stray.txt" not in second["changed_files"]
    assert not wt.exists()
    assert _git(repo, "branch", "--list", first["branch"]) != ""                # branch kept


def test_parallel_runs_overlap(repo, hidden, tmp_path):
    vf = tmp_path / "v.yaml"
    vf.write_text("variants:\n  S:\n    roles: {implementer: true}\n    models: {implementer: m}\n"
                  "    extra: {fake: {sleep_s: 0.6}}\n", encoding="utf-8")
    import time
    cfg = _cfg(repo, hidden, vf, tmp_path, repeats=4, tickets=["EV-1"], parallel=4)
    t0 = time.monotonic()
    recs = runner.Runner(cfg).run()
    assert len(recs) == 4 and time.monotonic() - t0 < 4 * 0.6 + 1.5
    assert len({r["worktree"] for r in recs}) == 4


def test_cli_fake_run_and_leaderboard(repo, hidden, variants_file, tmp_path, capsys):
    rc = cli.main_eval(["--variants", str(variants_file), "--tickets", "eval", "--parallel", "2", "--fake",
                        "--repo", str(repo), "--hidden-tests", str(hidden), "--experiment", "cli",
                        "--golden-ref", "golden", "--results-dir", str(tmp_path / "res"),
                        "--worktree-root", str(tmp_path / "wt")])
    out = capsys.readouterr().out
    assert rc == 0, out
    assert "variant" in out and (tmp_path / "res" / "cli.jsonl").is_file()
    recs = results.load_records([tmp_path / "res" / "cli.jsonl"])
    assert sorted({r["ticket"] for r in recs}) == ["EV-1", "EV-2"]              # eval: true only
    rc = cli.main_leaderboard([str(tmp_path / "res" / "*.jsonl"), "-o", str(tmp_path / "board.json"), "--quiet"])
    assert rc == 0 and json.loads((tmp_path / "board.json").read_text())["runs"]


def test_cli_dry_run_and_errors(repo, hidden, variants_file, tmp_path, capsys):
    rc = cli.main_eval(["--variants", str(variants_file), "--tickets", "EV-1", "--repo", str(repo),
                        "--hidden-tests", str(hidden), "--dry-run", "--repeats", "2"])
    out = capsys.readouterr().out
    assert rc == 0 and out.count("base=ticket/EV-1") == 4 and "A-EV-1-r1" in out
    assert not (repo / "results").exists()
    # a missing ticket branch stops the run before any worktree is created
    rc = cli.main_eval(["--variants", str(variants_file), "--tickets", "EV-1,EV-404", "--repo", str(repo),
                        "--hidden-tests", str(hidden), "--fake", "--results-dir", str(tmp_path / "r"),
                        "--worktree-root", str(tmp_path / "w")])
    assert rc == 2 and "EV-404 -> ticket/EV-404" in capsys.readouterr().err
    assert not (tmp_path / "w").exists()
    assert cli.main_eval(["--tickets", "EV-1", "--resume"]) == 2
    assert cli.main_eval(["--tickets", "bad id", "--hidden-tests", str(hidden)]) == 2
    assert cli.main_eval(["--tickets", "EV-1", "--only", "V99", "--hidden-tests", str(hidden)]) == 2


def test_shipped_variants_with_the_fake(repo, hidden, tmp_path):
    cfg = _cfg(repo, hidden, variants.DEFAULT_FILE, tmp_path, repeats=1, tickets=["EV-1"], parallel=5)
    recs = runner.Runner(cfg).run()
    assert sorted(r["variant"] for r in recs) == sorted(names.VARIANTS.values())
    by = {r["variant"]: r for r in recs}
    assert by["team-autonomous"]["approvals"] == 0
    assert by["team-large"]["ai_credits"] > by["team-small"]["ai_credits"]
    assert by["team-no-rules"]["settings"]["custom_instructions"] is False
    assert by["cheap"]["settings"]["prompt_template"] == "cheap"
    assert by["cheap"]["settings"]["max_ai_credits"] == 12 and by["raw"]["settings"]["gates"] is False


def test_board_files(hidden, tmp_path):
    d = tmp_path / "board"
    board.init(d, hidden, 3, None, False)
    t = json.loads((d / "tickets.json").read_text())
    assert [x["id"] for x in t["tickets"]] == ["EV-1", "EV-2", "EV-3"]
    assert t["tickets"][0]["title"] == "Picture looks odd" and "root_cause" not in json.dumps(t)
    with pytest.raises(SystemExit):
        board.init(d, hidden, 3, None, False)
    board.set_ticket(d, "EV-2", "review", "U2", "reviewer asked for a test")
    board.set_unit(d, "U2", 3, "EV-2", "team", None)
    assert board.main(["--dir", str(d), "ticket", "EV-9", "hil_ready"]) == 0
    t = json.loads((d / "tickets.json").read_text())
    assert {x["id"]: x["state"] for x in t["tickets"]}["EV-2"] == "review"
    assert t["tickets"][-1]["id"] == "EV-9"
    u = json.loads((d / "units.json").read_text())
    assert u["units"][1] == {"id": "U2", "name": "Unit 2", "level": 3, "ticket": "EV-2", "variant": "team"}
    with pytest.raises(SystemExit):
        board.set_unit(d, "U1", 9, None, None, None)


def _committing_backend(commit: bool):
    """Like the harness: an uncommitted fix, or a commit on its own agent/<key>-<id> branch."""
    def backend(workflow, ticket, settings, mode="eval"):
        wt = Path(settings.workdir)
        (wt / "src" / "a.c").write_text("int a; /* fixed */\n", encoding="utf-8")
        (wt / "src" / "new.c").write_text("int n;\n", encoding="utf-8")
        if commit:
            _git(wt, "checkout", "-q", "-b", f"agent/{ticket.lower()}-abcd")
            _git(wt, "add", "src")
            _git(wt, "commit", "-q", "-m", f"{ticket}: fix")
        return api.RunResult(status="completed", success=commit, run_id="r1", ai_credits=0.5)
    return backend


@pytest.mark.parametrize("commit", [True, False])
def test_commit_branch_patch_and_cleanup(repo, hidden, variants_file, tmp_path, commit):
    kw = dict(repeats=1, tickets=["EV-1"], variants=variants.load_matrix(variants_file).select(["A"]),
              backend=_committing_backend(commit), cleanup=True)
    (rec,) = runner.Runner(_cfg(repo, hidden, variants_file, tmp_path, **kw)).run()
    assert rec["run_status"] == "done", rec.get("error")
    assert rec["workflow_committed"] is commit and rec["commits"] == (1 if commit else 0)
    assert rec["hidden_pass"] == rec["success"]
    # the record's branch carries the commit; the harness's agent branch is gone
    assert rec["branch"] == "eval/t1/A-EV-1-r0"
    tip = _git(repo, "rev-parse", rec["branch"])
    assert (tip != rec["seed_commit"]) is commit
    if commit:
        assert rec["agent_branch"] == "agent/ev-1-abcd" and rec["commit"] == tip
        assert _git(repo, "log", "-1", "--format=%s", rec["branch"]) == "EV-1: fix"
    assert _git(repo, "branch", "--list", "agent/*") == ""
    # the full patch survives the worktree: tracked change and the new file, committed or not
    assert not Path(rec["worktree"]).exists()
    patch = (Path(rec["run_dir"]) / "diff.patch").read_text(encoding="utf-8")
    assert "+int a; /* fixed */" in patch and "src/new.c" in patch and "+int n;" in patch
    # leaderboard: pass rate and commit rate side by side
    board_doc = json.loads((tmp_path / "results" / "results.json").read_text())
    (row,) = board_doc["variants"]
    assert row["commits"] == (1 if commit else 0) and row["commit_rate"] == (1.0 if commit else 0.0)
    assert board_doc["runs"][0]["workflow_committed"] is commit and "hidden_pass" in board_doc["runs"][0]
