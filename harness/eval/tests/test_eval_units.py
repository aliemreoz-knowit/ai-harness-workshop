"""Unit tests: variants, audit parsing, hidden-test output, tickets, aggregation, api discovery."""
from __future__ import annotations

import json
import subprocess
import sys
import textwrap
from pathlib import Path

import pytest

from harness import api, names
from harness.eval import audit, hidden, results, tickets, variants
from harness.eval.fake import run_workflow as fake_run

# ------------------------------------------------------------------ variants --


def test_shipped_matrix_has_every_variant_by_name():
    m = variants.load_matrix(variants.DEFAULT_FILE)
    assert list(m.variants) == list(names.VARIANTS.values())
    v = m.variants
    assert [r for r, on in v["gates"].settings["roles"].items() if on] == ["implementer"]
    assert v["gates-planner"].settings["roles"]["planner"] and not v["gates-planner"].settings["roles"]["reviewer"]
    assert v["gates-reviewer"].settings["roles"]["reviewer"] and not v["gates-reviewer"].settings["roles"]["planner"]
    assert all(v["team"].settings["roles"].values())
    assert v["team-prompt-v2"].settings["prompt_template"] == "v2" and v["team"].settings["prompt_template"] == "v1"
    assert v["team-one-retry"].settings["retry_budget"] == 1 and v["team"].settings["retry_budget"] == 3
    assert set(v["team-small"].settings["models"].values()) == {"claude-haiku-5.5"}
    assert set(v["team-large"].settings["models"].values()) == {"claude-sonnet-5.5"}
    assert v["team-autonomous"].settings["autonomy"] == "auto" and v["team"].settings["autonomy"] == "human"
    assert v["team-no-rules"].settings["custom_instructions"] is False and v["team"].settings["custom_instructions"]
    # inherited from team, not re-stated
    v11 = v["cheap"].settings
    assert set(v11["models"].values()) == {"claude-haiku-5.5"} and v11["prompt_template"] == "cheap"
    assert v11["extra"]["escalation"] is False
    assert v11["extra"]["role_overrides"]["planner"]["max_reads"] == 4
    assert v11["extra"]["role_overrides"]["reviewer"]["tools"] == ["write_review"]
    # cheap caps: run cap 12 (set, not inherited), a session cap per role
    assert v11["max_ai_credits"] == 12 and v["team"].settings["max_ai_credits"] == 40
    caps = {r: o["max_ai_credits_per_session"] for r, o in v11["extra"]["role_overrides"].items()}
    assert caps == {"planner": 2, "implementer": 6, "test_writer": 3, "reviewer": 2}
    # raw: the raw agent of the calibration (no gates, no retries, no AGENTS.md, no run_gates)
    b0 = v["raw"].settings
    assert [r for r, on in b0["roles"].items() if on] == ["implementer"]
    assert (b0["gates"], b0["retry_budget"], b0["custom_instructions"], b0["max_ai_credits"]) == (False, 0, False, 15)
    assert b0["extra"]["escalation"] is False
    assert "run_gates" not in b0["extra"]["role_overrides"]["implementer"]["tools"]
    for name in ("team-prompt-v2", "team-one-retry", "team-small", "team-large", "team-autonomous", "team-no-rules", "cheap"):
        assert all(v[name].settings["roles"].values()), name
    # models only for the roles that are on
    assert set(v["gates"].settings["models"]) == {"implementer"}
    assert m.gate_weights["no-heap"] == 3


def test_block1_matrix_is_the_shipped_one_with_v1_capped():
    full = variants.load_matrix(variants.DEFAULT_FILE)
    b1 = variants.load_matrix(variants.find_variants_file("variants-section1.yaml"))
    assert list(b1.variants) == list(full.variants)
    assert b1.gate_weights == full.gate_weights
    for name, v in full.variants.items():
        want = dict(v.settings)
        if name == "gates":
            assert want["max_ai_credits"] == 40
            want["max_ai_credits"] = 12
        assert b1.variants[name].settings == want, name
        assert b1.variants[name].label == v.label, name


def test_variant_settings_become_run_settings():
    m = variants.load_matrix(variants.DEFAULT_FILE)
    s = m.variants["team-no-rules"].run_settings("/w", "/r")
    assert isinstance(s, api.RunSettings)
    assert (s.workdir, s.run_dir, s.variant, s.custom_instructions) == ("/w", "/r", "team-no-rules", False)
    assert s.max_ai_credits == 40


@pytest.mark.parametrize("doc, msg", [
    ({"variants": {"A": {"extends": "B"}, "B": {"extends": "A"}}}, "cycle"),
    ({"variants": {"A": {"extends": "Z"}}}, "unknown variant"),
    ({"variants": {"A": {"rolez": {}}}}, "unknown key"),
    ({"variants": {"A": {"roles": {"implementer": False}}}}, "implementer"),
    ({"variants": {"A": {"roles": {"implementer": True}, "autonomy": "yolo",
                         "models": {"implementer": "m"}}}}, "autonomy"),
    ({"variants": {"A": {"roles": {"implementer": True}}}}, "no model"),
    ({"variants": {}}, "no variants"),
])
def test_matrix_errors(doc, msg):
    with pytest.raises(variants.VariantError, match=msg):
        variants.parse_matrix(doc)


def test_merge_keeps_parent_roles():
    m = variants.parse_matrix({"defaults": {"roles": {"implementer": True}, "models": {"implementer": "h",
                                                                                         "reviewer": "s"}},
                               "variants": {"A": {}, "B": {"extends": "A", "roles": {"reviewer": True}}}})
    assert m.variants["B"].settings["roles"] == {"planner": False, "implementer": True, "test_writer": False,
                                                 "reviewer": True}
    assert m.variants["B"].settings["models"] == {"implementer": "h", "reviewer": "s"}
    with pytest.raises(variants.VariantError, match="unknown variant"):
        m.select(["A", "C"])


# --------------------------------------------------------------------- audit --

def _ev(t, **d):
    return {"type": t, "data": d}


def test_audit_metrics():
    evs = [
        _ev("assistant.usage", model="h", inputTokens=100, outputTokens=10, cacheReadTokens=50),
        _ev("assistant.usage", model="s", inputTokens=200, outputTokens=20, cacheReadTokens=0),
        _ev("session.usage_checkpoint", sessionId="a", totalNanoAiu=1_000_000_000),
        _ev("session.usage_checkpoint", sessionId="a", totalNanoAiu=2_500_000_000),   # cumulative
        _ev("usage_checkpoint", sessionId="b", totalNanoAiu=500_000_000),
        _ev("harness.approval", step="plan"),
        _ev("harness.approval", step="merge"),
        _ev("harness.escalation", step="x"),
        _ev("harness.retry", attempt=1),
        _ev("harness.gate", gate="unit-tests", status="FAIL", attempt=0),
        _ev("harness.gate", gate="sanitizers", status="FAIL", attempt=0),
        _ev("harness.gate", gate="unit-tests", status="PASS", attempt=1),
        _ev("harness.review", score=4, verdict="approve"),
        _ev("assistant.message", content="ignored"),
        None, "junk", {"no": "type"},
    ]
    m = audit.parse_events(evs)
    assert m.model_calls == 2 and m.tokens_in == 300 and m.tokens_out == 30 and m.tokens_cached == 50
    assert m.ai_credits == pytest.approx(3.0)
    assert (m.approvals, m.escalations, m.interventions, m.retries) == (2, 1, 3, 1)
    assert m.gate_results == {"unit-tests": "PASS", "sanitizers": "FAIL"}
    assert m.gate_violations == 1 and m.gate_failures_total == 2
    assert m.reviewer_score == 4.0
    assert m.bad_lines == 3 and m.models == {"h": 1, "s": 1}


def test_audit_credits_from_usage_when_no_checkpoint(tmp_path):
    p = tmp_path / "events.jsonl"
    p.write_text("\n".join(json.dumps(e) for e in [
        _ev("assistant.usage", inputTokens=1, nanoAiu=250_000_000),
        _ev("assistant.usage", inputTokens=1, nanoAiu=250_000_000)]) + "\n{torn", encoding="utf-8")
    m = audit.parse_audit(p)
    assert m.ai_credits == pytest.approx(0.5) and m.bad_lines == 1 and m.gate_violations is None
    assert audit.parse_audit(tmp_path / "missing.jsonl") is None
    assert audit.parse_events([_ev("assistant.usage", inputTokens=5)]).ai_credits is None


# -------------------------------------------------------------------- hidden --

RUN_SH_OUTPUT = textwrap.dedent("""\
    [hidden] preset host, tickets: TS-9, tests: test_a test_b
    PASS  TS-9     test_lb_long_stream_100k              6.73 sec
    FAIL  TS-9     test_capture_pipe                  not run
    ---- ctest output ----
    2/2 Test #2: test_capture_pipe ......***Failed    0.00 sec
    [hidden] FAIL (1 of 2)
    """)


def test_parse_run_sh_output():
    tests = hidden.parse_output(RUN_SH_OUTPUT)
    assert tests[0] == {"status": "PASS", "ticket": "TS-9", "name": "test_lb_long_stream_100k", "secs": 6.73}
    assert tests[1]["status"] == "FAIL" and tests[1]["secs"] is None


def _py(code: str) -> list[str]:
    return [sys.executable, "-c", code, "{checkout}", "{preset}", "{ticket}"]


@pytest.mark.parametrize("code, status", [
    ("print('PASS  T-1 test_x 0.1 sec'); print('[hidden] PASS (1 of 1)')", "pass"),
    ("import sys; print('FAIL  T-1 test_x 0.1 sec'); sys.exit(1)", "fail"),
    ("print('[hidden] PASS')", "fail"),                                   # exit 0 without PASS lines
    ("import sys; print('PASS  T-1 test_x 0.1 sec'); sys.exit(1)", "fail"),
    ("import sys; sys.stderr.write('[hidden] build failed'); sys.exit(2)", "error"),
    ("import time; time.sleep(5)", "timeout"),
])
def test_run_hidden_status(tmp_path, code, status):
    r = hidden.run_hidden(tmp_path, tmp_path, "T-1", "host", _py(code), timeout_s=2.0,
                          log_path=tmp_path / "h.log")
    assert r.status == status, r.log_tail
    assert (tmp_path / "h.log").is_file()
    assert r.success == (status == "pass")


def test_run_hidden_passes_arguments(tmp_path):
    code = "import sys; print('PASS  ' + sys.argv[3] + ' ' + sys.argv[2] + ' 0.1 sec')"
    r = hidden.run_hidden(tmp_path, tmp_path, "T-7", "asan", _py(code))
    assert r.status == "pass" and r.tests[0]["name"] == "asan"


def test_run_hidden_missing_program(tmp_path):
    r = hidden.run_hidden(tmp_path, tmp_path, "T-1", "host", [str(tmp_path / "nope.exe")])
    assert r.status == "error"


@pytest.mark.parametrize("ts_lite, docker, lite_py, want", [
    ("", True, True, "run.sh"),
    ("", False, True, "lite.py"),       # no usable Docker: zig cc on this machine, as check.sh does
    ("1", True, True, "lite.py"),
    ("0", False, True, "run.sh"),       # TS_LITE=0: the container or nothing
    ("", False, False, "run.sh"),       # a referee without lite.py
])
def test_default_command_picks_the_container_or_lite(tmp_path, monkeypatch, ts_lite, docker, lite_py, want):
    monkeypatch.setenv("TS_LITE", ts_lite)
    monkeypatch.setattr(hidden, "docker_usable", lambda: docker)
    if lite_py:
        (tmp_path / "lite.py").write_text("", encoding="utf-8")
    assert hidden.default_command(tmp_path)[1].endswith(want)


def test_lite_py_refuses_what_needs_the_container(tmp_path):
    lite = Path(__file__).resolve().parents[3] / "hidden-tests" / "lite.py"
    if not lite.is_file():
        pytest.skip("no hidden-tests/lite.py in this tree")
    (tmp_path / "CMakePresets.json").write_text("{}", encoding="utf-8")
    tree = str(tmp_path)
    for args, says in (([tree, "asan", "TS-104"], "needs Docker"), ([tree, "TS-999"], "unknown ticket"), ([], "usage")):
        done = subprocess.run([sys.executable, str(lite), *args], capture_output=True, text=True, encoding="utf-8")
        assert done.returncode == 2 and says in done.stderr, (args, done.stderr)


def test_default_command_uses_run_sh(tmp_path, monkeypatch):
    monkeypatch.setenv("HARNESS_BASH", "/x/bash")
    argv = hidden.build_command(hidden.DEFAULT_CMD, tmp_path / "ht", tmp_path / "wt", "host", "T-1")
    assert argv[0] == "/x/bash" and argv[1].endswith("/ht/run.sh") and argv[3:] == ["host", "T-1"]
    assert "\\" not in argv[1] and "\\" not in argv[2]


# ------------------------------------------------------------------- tickets --

def test_ticket_meta_and_eval_set(hidden):
    t = tickets.load_ticket(hidden, "EV-1")
    assert (t.ticket_branch, t.eval, t.has_meta) == ("ticket/EV-1", True, True)
    assert tickets.load_ticket(hidden, "EV-99").ticket_branch == "ticket/EV-99"
    assert tickets.eval_set(hidden) == ["EV-1", "EV-2"]
    assert tickets.parse_ticket_list("EV-3,eval,EV-1", hidden) == ["EV-3", "EV-1", "EV-2"]
    with pytest.raises(ValueError):
        tickets.parse_ticket_list("EV-1,../x", hidden)
    with pytest.raises(ValueError):
        tickets.parse_ticket_list("eval", None)
    assert not hasattr(t, "root_cause")


@pytest.mark.parametrize("path, pattern, ok", [
    ("tests/unit/test_x.c", "tests/unit/**", True),
    ("tests/unit/deep/x.c", "tests/unit/**", True),
    ("tests/loopback/x.c", "tests/unit/**", False),
    ("src/codec/rle.c", "src/codec/*.c", True),
    ("src/codec/sub/rle.c", "src/codec/*.c", False),
    ("src/a/b/c.h", "src/**/*.h", True),
    ("src/c.h", "src/**/*.h", True),
    ("scripts/test_manifest.txt", "scripts/test_manifest.txt", True),
])
def test_glob(path, pattern, ok):
    assert tickets._glob_match(path, pattern) is ok


def test_path_violations():
    assert tickets.path_violations(["src/a.c", "include/x.h"], ["src/**"]) == ["include/x.h"]
    assert tickets.path_violations(["anything"], []) == []


# ------------------------------------------------------------------- results --

def test_wilson():
    lo, hi = results.wilson(8, 10)
    assert 0.49 < lo < 0.5 and 0.94 < hi < 0.95
    assert results.wilson(0, 0) == (0.0, 0.0)


def _rec(v, t, k, ok, credits, exp="e"):
    return {"schema": 1, "experiment": exp, "variant": v, "ticket": t, "repeat": k, "success": ok,
            "run_status": "done", "ai_credits": credits, "interventions": 2, "retries": 1, "duration_s": 10.0,
            "gate_violations": 0, "reviewer_score": None, "tokens": {"in": 10, "out": 1, "cached": 5}}


def test_aggregate_and_board(tmp_path):
    recs = [_rec("gates-planner", "A", 0, True, 1.0), _rec("gates-planner", "B", 0, False, 3.0),
            _rec("team-no-rules", "A", 0, True, 1.0), _rec("team-no-rules", "B", 0, True, 1.0),
            _rec("team-no-rules", "B", 0, False, 9.0, exp="other"),
            {"schema": 1, "experiment": "e", "variant": "gates-reviewer", "ticket": "A", "repeat": 0,
             "run_status": "setup_error", "success": False}]
    rows = results.aggregate(recs[:4])
    assert [r["variant"] for r in rows] == ["team-no-rules", "gates-planner"]
    assert rows[0]["pass_rate"] == 1.0 and rows[1]["credits_per_pass"] == 4.0
    assert rows[1]["per_ticket"] == {"A": {"n": 1, "pass": 1}, "B": {"n": 1, "pass": 0}}
    board = results.write_board(recs, tmp_path / "out" / "results.json")
    assert json.loads((tmp_path / "out" / "results.json").read_text())["schema"] == 1
    assert board["experiments"] == ["e", "other"] and len(board["runs"]) == 6
    v10 = next(r for r in board["variants"] if r["variant"] == "team-no-rules")
    assert v10["runs"] == 3 and v10["passes"] == 2
    assert "gates-reviewer" not in [r["variant"] for r in board["variants"]]      # setup errors are not scored
    assert "team-no-rules" in results.format_table(board["variants"])


def test_latest_replaces_rerun():
    a, b = _rec("gates", "A", 0, False, 1.0), _rec("gates", "A", 0, True, 2.0)
    assert results.latest([a, b]) == [b]


def test_load_records_skips_torn_lines(tmp_path):
    p = tmp_path / "x.jsonl"
    p.write_text(json.dumps(_rec("gates", "A", 0, True, 1)) + "\n{\"schema\": 1, \"tor", encoding="utf-8")
    assert len(results.load_records([p, tmp_path / "missing.jsonl"])) == 1


# ----------------------------------------------------------------------- api --

def test_api_backend_selection(monkeypatch):
    seen = []

    def backend(workflow, ticket, settings, mode):
        seen.append((workflow, ticket, mode))
        return {"status": "completed", "success": True, "approvals": 2, "escalations": 1, "bogus": 1}

    api.set_backend(backend)
    try:
        r = api.run_workflow("jira-to-pr", "T-1", api.RunSettings(), "eval")
    finally:
        api.set_backend(None)
    assert isinstance(r, api.RunResult) and r.interventions == 3 and seen == [("jira-to-pr", "T-1", "eval")]
    with pytest.raises(ValueError):
        api.run_workflow("w", "T-1", api.RunSettings(), "chaos", backend=backend)
    monkeypatch.setenv("HARNESS_BACKEND", "harness.eval.fake:run_workflow")
    assert api._discover() is fake_run
    monkeypatch.setenv("HARNESS_BACKEND", "no.such.module:fn")
    with pytest.raises(ModuleNotFoundError):
        api._discover()
    monkeypatch.delenv("HARNESS_BACKEND")
    try:
        import harness.orchestrator  # noqa: F401
    except ModuleNotFoundError:
        with pytest.raises(api.BackendUnavailable):
            api._discover()


def test_settings_from_dict_keeps_unknown_keys_in_extra():
    s = api.RunSettings.from_dict({"retry_budget": 1, "custom": 2, "extra": {"a": 1}})
    assert s.retry_budget == 1 and s.extra == {"a": 1, "custom": 2}


# ------------------------------------------------------------------- boards --

def test_boards_are_self_contained_and_carry_no_ticket_ids():
    import re
    board_dir = Path(__file__).resolve().parents[3] / "tools" / "board"
    pages = sorted(board_dir.glob("*.html"))
    assert [p.name for p in pages] == ["leaderboard.html", "mission.html"]
    for p in pages:
        text = p.read_text(encoding="utf-8")
        assert not re.search(r"TS-[0-9]{3}", text), p           # misra scans tools/
        assert not re.search(r"""<(script|link)[^>]+(src|href)=["']?https?:""", text), p   # no external deps
        assert "\r\n" not in text


# ---------------------------------------------------------------------- fake --

def test_fake_is_deterministic_and_respects_settings(tmp_path):
    base = variants.load_matrix(variants.DEFAULT_FILE).variants

    def run(name, rep=0):
        d = tmp_path / f"{name}-{rep}"
        d.mkdir(exist_ok=True)
        s = base[name].run_settings(str(d), str(d / "run"))
        s.repeat = rep
        return fake_run("jira-to-pr", "T-1", s, "eval"), d

    r1, _ = run("team")
    r2, _ = run("team")
    assert r1.to_dict() == r2.to_dict()
    r9, _ = run("team-autonomous")
    assert r9.approvals == 0
    v4_runs = [run("team", k)[0] for k in range(1, 6)]
    assert all(r.approvals >= 1 for r in v4_runs if r.success)
    r7, _ = run("team-small", 9)
    r8, _ = run("team-large", 9)
    assert r8.ai_credits > r7.ai_credits
    m = audit.parse_audit(Path(r1.audit_path))
    assert m.ai_credits == pytest.approx(r1.ai_credits, rel=1e-3)
    assert m.approvals == r1.approvals and m.retries == r1.retries


def test_base_tag_is_the_tag_the_seed_is_built_on(tmp_path, monkeypatch):
    import subprocess
    from harness.eval import worktree as wt
    for k, v in {"GIT_AUTHOR_NAME": "t", "GIT_AUTHOR_EMAIL": "t@example.invalid",
                 "GIT_COMMITTER_NAME": "t", "GIT_COMMITTER_EMAIL": "t@example.invalid"}.items():
        monkeypatch.setenv(k, v)
    repo = tmp_path / "r"

    def g(*a):
        subprocess.run(["git", "-C", str(repo), *a], check=True, capture_output=True)
    subprocess.run(["git", "init", "-q", "-b", "main", str(repo)], check=True)
    (repo / "a.c").write_text("golden\n", encoding="utf-8")
    g("add", "-A"); g("commit", "-qm", "golden"); g("tag", "v2.0.0-golden")
    (repo / "a.c").write_text("harness\n", encoding="utf-8")
    g("commit", "-qam", "harness"); g("tag", "v9.1.0-harness")
    g("checkout", "-qb", "ticket/X-1"); (repo / "a.c").write_text("seed\n", encoding="utf-8")
    g("commit", "-qam", "Exercise state")
    assert wt.base_tag(repo, "ticket/X-1") == "v9.1.0-harness"
    assert wt.base_tag(repo, "v2.0.0-golden") == "v2.0.0-golden"     # no harness tag below it
    assert wt.base_tag(repo, "ticket/X-1", ("nothing-*",)) is None


# --------------------------------------------------- "experiment start" alias --
def test_experiment_start_is_the_eval_runner(monkeypatch, capsys):
    import harness.__main__ as entry

    seen = []
    monkeypatch.setattr(entry, "_call", lambda target, argv: seen.append((target, argv)) or 0)
    assert entry.main(["experiment", "start", "--tickets", "eval", "--only", "team"]) == 0
    assert entry.main(["experiment", "leaderboard", "--quiet"]) == 0
    assert seen == [(entry.EVAL_COMMANDS["eval"], ["--tickets", "eval", "--only", "team"]),
                    (entry.EVAL_COMMANDS["leaderboard"], ["--quiet"])]
    assert entry.main(["experiment"]) == 2
    assert entry.main(["experiment", "--help"]) == 0
    assert "experiment {start,leaderboard}" in capsys.readouterr().err
