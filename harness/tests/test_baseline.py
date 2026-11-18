"""Baseline-aware test gates (harness/baseline.py): parsers, the comparison, the gate runner
under the rule and whole runs with the fake runtime.

The fake check.sh below writes ctest/Unity logs shaped like the real ones; the "defects" are
markers in src/proto/proto.c, so the scripted agents fix or break tests by editing it.
"""

from __future__ import annotations

import asyncio
import json
from pathlib import Path

import pytest

from harness import api, backend
from harness import baseline as bl
from harness.config import HarnessConfig, load_config
from harness.envdetect import Environment
from harness.fake import FakeRuntime, Turn, call, edit
from harness.gates import GateContext, GateRunner
from harness.orchestrator import Orchestrator
from harness.state import RunState
from harness.ticket import parse_markdown

from .conftest import FakeCheck, git

HEADER = "/*\n * tinystream v2 -- src/proto/proto.c\n */\n"
BODY = "int ts_parse(int x)\n{\n    return x;\n}\n"
BASE = HEADER + "/* defect-a */\n/* defect-b */\n" + BODY          # two open defects on the branch
FIX_A = HEADER + "/* defect-b */\n" + BODY
FIX_AB = HEADER + BODY

TICKET = """# DEMO-9: The counter is off after a restart

**Type:** Task
**Component:** daemon

## Description

The counter on the board is off by one after every restart.
"""


# ------------------------------------------------------------- fake ctest logs ----
def ctest_log(execs: list[tuple[str, str, list[tuple[str, str]]]]) -> str:
    """(name, pass|fail|segv, [(case, PASS|FAIL)]) -> a ctest --output-on-failure log with Unity output."""
    n = len(execs)
    lines = ["Test project /work/build-host"]
    failed = []
    for i, (name, st, cases) in enumerate(execs, 1):
        lines.append(f"      Start {i:2d}: {name}")
        res = {"pass": "   Passed", "fail": "***Failed", "segv": "***Exception: SegFault"}[st]
        lines.append(f"{i:2d}/{n} Test  #{i}: {name} {'.' * (30 - len(name))}{res}    0.02 sec")
        if st == "pass":
            continue
        failed.append((i, name, "Failed" if st == "fail" else "SEGFAULT"))
        for j, (case, r) in enumerate(cases):
            color = "\x1b[41m" if r == "FAIL" else "\x1b[42m"
            lines.append(f"/work/tests/unit/{name}.c:{10 + j}:{case}:{color}{r}\x1b[0m"
                         + (": Expected 1 Was 2" if r == "FAIL" else ""))
        if st == "fail":
            nf = sum(1 for _, r in cases if r == "FAIL")
            lines += ["", "-----------------------", f"{len(cases)} Tests {nf} Failures 0 Ignored ", "FAIL", ""]
    pct = int(100 * (n - len(failed)) / n)
    lines += ["", f"{pct}% tests passed, {len(failed)} tests failed out of {n}", ""]
    if failed:
        lines.append("The following tests FAILED:")
        lines += [f"\t{i:3d} - {name} ({why})" for i, name, why in failed]
        lines.append("Errors while running CTest")
    return "\n".join(lines) + "\n"


def unit_tests(repo: Path) -> list[tuple[str, str, list[tuple[str, str]]]]:
    text = (repo / "src/proto/proto.c").read_text(encoding="utf-8")
    cases = [("test_parse_a", "FAIL" if "defect-a" in text else "PASS"),
             ("test_parse_b", "FAIL" if "defect-b" in text else "PASS"),
             ("test_parse_c", "FAIL" if "regress" in text else "PASS")]
    proto = ("test_proto", "fail" if any(r == "FAIL" for _, r in cases) else "pass", cases)
    codec = ("test_codec", "segv" if "crash" in text else "pass", [("test_codec_round_trip", "PASS")])
    return [proto, codec]


def world(fn: str, repo: Path) -> tuple[str, str, str]:
    """g3 / g5: the unit tests above; g4 / g7: always green; everything else PASS."""
    if fn in ("unit-tests", "sanitizers"):
        execs = unit_tests(repo)
        bad = any(st != "pass" for _, st, _ in execs)
        return ("FAIL" if bad else "PASS", f"see build-check/harness/{fn.upper()}.log" if bad else "", ctest_log(execs))
    if fn in ("property-tests", "loopback"):
        return "PASS", "", ctest_log([("test_prop_proto", "pass", [])])
    return "PASS", "", ""


def seed_defects(repo: Path, text: str = BASE) -> None:
    (repo / "src/proto/proto.c").write_text(text, encoding="utf-8", newline="\n")
    git(repo, "commit", "-q", "-am", "two open defects")


def make(cfg: HarnessConfig, env: Environment, runtime: FakeRuntime, decide=world,
         mode: str = "eval") -> tuple[Orchestrator, FakeCheck]:
    fake = FakeCheck(cfg, decide)
    out: list[str] = []
    orch = Orchestrator(cfg, runtime, env, mode=mode, runtime_mode="copilot-user",
                        gate_runner=GateRunner(cfg, env, runner=fake), input_fn=lambda q: "abort",
                        output=out.append, isatty=lambda: False)
    orch.lines = out  # type: ignore[attr-defined]
    return orch, fake


def run(orch: Orchestrator, **kw) -> RunState:
    return asyncio.run(orch.run(**kw))


def ticket(tmp_path: Path, extra: str = "") -> Path:
    p = tmp_path / "DEMO-9.md"
    p.write_text(TICKET + extra, encoding="utf-8")
    return p


def base_calls(fake: FakeCheck) -> list[list[str]]:
    return [c for c in fake.calls if c == ["bash", "scripts/check.sh", "unit-tests", "property-tests"]]


# ------------------------------------------------------------------- parsers ----
REAL_G3 = """\
 1/13 Test  #2: test_clients .....................***Failed    0.02 sec
/work/tests/unit/test_clients.c:514:test_clients_init_and_accept_until_full:\x1b[42mPASS\x1b[0m
/work/tests/unit/test_clients.c:151:test_clients_same_ip_two_ports_are_two_clients:\x1b[41mFAIL\x1b[0m: Expected 1 Was 0

-----------------------
2 Tests 1 Failures 0 Ignored \n\x1b[41mFAIL\x1b[0m

 2/13 Test  #4: test_harness .....................   Passed    0.02 sec
      Start 13: test_txq
 5/13 Test  #6: test_net .........................***Exception: SegFault  0.05 sec

13/13 Test #25: test_lb_tight_link ...............***Failed    0.04 sec
/work/tests/loopback/test_lb_tight_link.c:91:test_tight_link_5pct_loss_keeps_frame_rate:\x1b[41mFAIL\x1b[0m: Expected 1 Was 7:daemon not running
\x1b[41mFAIL\x1b[0m

69% tests passed, 3 tests failed out of 13
"""

REAL_G9 = """\
[49/93] Building C object CMakeFiles/test_clients.dir/tests/unit/test_clients.c.o
proto lines 100.0% from 2 own test(s) (min 85%)
tests of txq failed in the coverage build
coverage below 50% for capture (src/capture/) from its own tests:
lines: 41.0% (100 out of 244)
capture lines 41.0% from 1 own test(s) (min 50%)
no unit tests for net (tests/unit/test_net.c)
info: a test failed in the all-tests coverage run (see build-check/harness/g9-ctest-all.txt)
"""


def test_parse_tests_from_a_real_shaped_log() -> None:
    r = bl.parse_tests(REAL_G3)
    assert r is not None
    e = r["execs"]
    assert e["test_harness"] == {"status": "pass", "why": "Passed", "complete": True, "cases": {}}
    assert e["test_clients"]["complete"] and e["test_clients"]["cases"] == {
        "test_clients_init_and_accept_until_full": "pass", "test_clients_same_ip_two_ports_are_two_clients": "fail"}
    assert e["test_net"]["status"] == "fail" and e["test_net"]["why"] == "SegFault" and not e["test_net"]["complete"]
    # a Unity run without its summary line aborted: unknown how far it got
    assert not e["test_lb_tight_link"]["complete"]
    assert bl.failures(r) == ["test_clients:test_clients_same_ip_two_ports_are_two_clients", "test_net",
                              "test_lb_tight_link:test_tight_link_5pct_loss_keeps_frame_rate", "test_lb_tight_link"]
    assert set(bl.segments(REAL_G3)) == {"test_clients", "test_harness", "test_net", "test_lb_tight_link"}
    assert bl.parse_tests("ninja: build stopped: subcommand failed.\n") is None   # build failure: no results


def test_parse_coverage() -> None:
    r = bl.parse_coverage(REAL_G9)
    assert r is not None and r["all_failed"]
    m = r["modules"]
    assert m["proto"] == {"pct": 100.0, "min": 85.0, "state": "ok"}
    assert m["txq"]["state"] == "tests-fail" and m["net"]["state"] == "no-tests"
    assert m["capture"]["pct"] == 41.0
    assert bl.parse_coverage("no coverage lines\n") is None


def test_compare_tests_new_pre_existing_fixed() -> None:
    base = {"execs": {
        "test_proto": {"status": "fail", "why": "Failed", "complete": True,
                       "cases": {"a": "fail", "b": "fail", "c": "pass"}},
        "test_net": {"status": "fail", "why": "SegFault", "complete": False, "cases": {}},
        "test_ok": {"status": "pass", "why": "Passed", "complete": True, "cases": {}}}}
    now = {"execs": {
        "test_proto": {"status": "fail", "why": "Failed", "complete": True,
                       "cases": {"a": "pass", "b": "fail", "c": "fail"}},
        # no longer crashes; its case failures were hidden behind the crash on the base: not new
        "test_net": {"status": "fail", "why": "Failed", "complete": True, "cases": {"x": "fail", "y": "pass"}},
        "test_ok": {"status": "pass", "why": "Passed", "complete": True, "cases": {}},
        "test_new": {"status": "fail", "why": "Failed", "complete": True, "cases": {"n": "fail"}}}}
    d = bl.compare_tests(base, now)
    assert d["new"] == ["test_proto:c", "test_new:n"]
    assert d["pre_existing"] == ["test_proto:b", "test_net:x"]
    assert d["fixed"] == ["test_proto:a", "test_net"]
    # a test that ran to its end on the base and aborts now is a new failure
    now["execs"]["test_proto"] = {"status": "fail", "why": "SegFault", "complete": False, "cases": {"a": "pass"}}
    assert "test_proto" in bl.compare_tests(base, now)["new"]
    # a test green on the base fails now
    now["execs"]["test_ok"] = {"status": "fail", "why": "Failed", "complete": True, "cases": {"z": "fail"}}
    assert "test_ok:z" in bl.compare_tests(base, now)["new"]


def test_compare_coverage_floor_is_the_lower_of_base_and_threshold() -> None:
    base = {"modules": {"txq": {"pct": None, "min": None, "state": "tests-fail"},
                        "proto": {"pct": 100.0, "min": 85.0, "state": "ok"},
                        "capture": {"pct": 41.0, "min": 50.0, "state": "ok"},
                        "rate": {"pct": 90.0, "min": 80.0, "state": "ok"},
                        "net": {"pct": None, "min": None, "state": "tests-fail"}},
            "all_failed": True}
    now = {"modules": {"txq": {"pct": 61.0, "min": 80.0, "state": "ok"},       # base had no number: fine
                       "proto": {"pct": 86.0, "min": 85.0, "state": "ok"},     # >= min(100, 85)
                       "capture": {"pct": 41.0, "min": 50.0, "state": "ok"},   # = base (below threshold): ok
                       "rate": {"pct": None, "min": None, "state": "tests-fail"},   # new
                       "net": {"pct": None, "min": None, "state": "tests-fail"}},   # pre-existing
           "all_failed": True}
    d = bl.compare_coverage(base, now)
    assert [u.split()[0] for u in d["new"]] == ["rate"]
    assert d["fixed"] == ["txq"]
    assert any(u.startswith("net") for u in d["pre_existing"])
    assert any(u.startswith("all-tests") for u in d["pre_existing"])
    now["modules"]["capture"]["pct"] = 40.0
    assert "capture" in [u.split()[0] for u in bl.compare_coverage(base, now)["new"]]


def test_required_tests_from_the_ticket_and_locate(repo: Path) -> None:
    t = parse_markdown(TICKET + "\n## Acceptance tests\n\n- `test_parse_b` passes\n- test_lb_seq_wrap_repair, "
                       "test_parse_b\n\n## Out of scope\n\n- test_other\n", "file:t.md")
    assert t.required_tests == ["test_parse_b", "test_lb_seq_wrap_repair"]
    t2 = parse_markdown(TICKET + "\n## Required tests\n\ntest_x\n", "file:t.md")
    assert t2.required_tests == ["test_x"]
    assert parse_markdown(TICKET, "file:t.md").required_tests == []
    (repo / "tests/unit/test_proto.c").write_text("void test_parse_b(void)\n{\n}\n", encoding="utf-8")
    assert bl.locate("test_proto", repo) == "test_proto"
    assert bl.locate("test_parse_b", repo) == "test_proto"
    assert bl.locate("test_nowhere", repo) is None


# ------------------------------------------------------------ the gate runner ----
def capture(runner: GateRunner, cfg: HarnessConfig) -> dict:
    ladder = runner.run(["unit-tests", "property-tests"], GateContext(cfg.repo, [], {}), parse_all=True)
    gates = {g.id: {"status": g.status, "kind": "tests", "results": g.results if g.status == "FAIL" else None}
             for g in ladder.gates}
    return {"mode": "auto", "active": True, "gates": gates}


def test_gate_passes_with_pre_existing_failures_and_lists_only_new_ones(
        cfg: HarnessConfig, container_env: Environment, repo: Path) -> None:
    seed_defects(repo)
    runner = GateRunner(cfg, container_env, runner=FakeCheck(cfg, world))
    tb = capture(runner, cfg)
    assert tb["gates"]["unit-tests"]["status"] == "FAIL" and tb["gates"]["property-tests"]["status"] == "PASS"
    ctx = GateContext(cfg.repo, [], {}, test_baseline=tb)

    (repo / "src/proto/proto.c").write_text(FIX_A, encoding="utf-8")              # fixes a, b stays
    ladder = runner.run(["unit-tests", "property-tests"], ctx)
    g3 = ladder.gates[0]
    assert ladder.ok and g3.status == "PASS" and g3.delta["raw_status"] == "FAIL"
    assert g3.delta["fixed"] == ["test_proto:test_parse_a"] and g3.delta["pre_existing"] == ["test_proto:test_parse_b"]
    assert ladder.baseline["rule"] == "progress" and ladder.baseline["progress"] is True
    fb = ladder.feedback()
    assert fb.startswith("GATES PASS") and "PASS by the baseline rule" in fb and "test_parse_a" in fb

    (repo / "src/proto/proto.c").write_text(FIX_A.replace(BODY, "/* regress */\n" + BODY), encoding="utf-8")
    ladder = runner.run(["unit-tests", "property-tests"], ctx)
    g3 = ladder.gates[0]
    assert not ladder.ok and g3.status == "FAIL" and g3.delta["new"] == ["test_proto:test_parse_c"]
    assert [it.rule for it in g3.items] == ["test_parse_c"]                       # not test_parse_b
    assert g3.items[0].file == "tests/unit/test_proto.c" and g3.items[0].message == "Expected 1 Was 2"
    fb = ladder.feedback()
    assert "test_parse_c" in fb and "test_parse_b" not in fb
    assert "1 other failure(s) in unit-tests were already failing on the untouched base" in fb

    (repo / "src/proto/proto.c").write_text(FIX_A + "/* crash */\n", encoding="utf-8")   # a green test aborts
    g3 = runner.run(["unit-tests"], ctx).gates[0]
    assert g3.status == "FAIL" and g3.delta["new"] == ["test_codec"]
    assert g3.items[0].rule == "test_codec" and "passed on the untouched base" in g3.items[0].message


def test_progress_rule_and_required_tests(cfg: HarnessConfig, container_env: Environment, repo: Path) -> None:
    seed_defects(repo)
    runner = GateRunner(cfg, container_env, runner=FakeCheck(cfg, world))
    tb = capture(runner, cfg)
    # nothing fixed, between steps: no new failure is enough; the base failures are not a to-do list
    (repo / "src/proto/proto.c").write_text(BASE + "/* tidy */\n", encoding="utf-8")
    ladder = runner.run(["unit-tests", "property-tests"], GateContext(cfg.repo, [], {}, test_baseline=tb))
    assert ladder.ok and ladder.baseline["progress"] is False and ladder.baseline["final"] is False
    fb = ladder.feedback()
    assert fb.startswith("GATES PASS") and "2 failure(s) were already failing" in fb and "progress" not in fb
    # nothing fixed, on the final ladder: no progress, the base failures named as context only
    ladder = runner.run(["unit-tests", "property-tests"], GateContext(cfg.repo, [], {}, test_baseline=tb, final=True))
    g3 = ladder.gates[0]
    assert g3.status == "FAIL" and "no progress" in g3.reason and ladder.baseline["progress"] is False
    assert g3.items[-1].rule == "progress" and "test_proto:test_parse_a" in g3.items[-1].message
    assert "context, not a list to fix" in g3.items[-1].message and "Work from the ticket" in g3.items[-1].message
    # the ticket requires test_parse_b: fixing a alone is not enough ...
    ctx = GateContext(cfg.repo, [], {}, test_baseline=tb, required_tests=("test_parse_b",))
    (repo / "src/proto/proto.c").write_text(FIX_A, encoding="utf-8")
    g3 = runner.run(["unit-tests", "property-tests"], ctx).gates[0]
    assert g3.status == "FAIL" and g3.delta["required"] == {"test_parse_b": "fail"}
    assert "required test(s) not passing: test_parse_b" in g3.reason
    assert [it.rule for it in g3.items] == ["test_parse_b"] and "(required by the ticket)" in g3.items[0].message
    # ... fixing b alone is (a stays failing as on the base)
    (repo / "src/proto/proto.c").write_text(HEADER + "/* defect-a */\n" + BODY, encoding="utf-8")
    ladder = runner.run(["unit-tests", "property-tests"], ctx)
    assert ladder.ok and ladder.gates[0].delta["required"] == {"test_parse_b": "pass"}
    # a required test no gate of the final ladder runs fails the ladder
    ctx_final = GateContext(cfg.repo, [], {}, test_baseline=tb, required_tests=("test_parse_b", "test_missing"),
                            final=True)
    ladder = runner.run(["unit-tests", "property-tests"], ctx_final)
    assert not ladder.ok and ladder.baseline["not_found"] == ["test_missing"]
    assert any(it.rule == "test_missing" for it in ladder.gates[0].items)


def test_build_failure_stays_absolute(cfg: HarnessConfig, container_env: Environment, repo: Path) -> None:
    seed_defects(repo)
    runner = GateRunner(cfg, container_env, runner=FakeCheck(cfg, world))
    tb = capture(runner, cfg)
    broken = FakeCheck(cfg, lambda fn, r: ("FAIL", "build", "/work/src/proto/proto.c:5:3: error: expected ';'\n")
                       if fn == "unit-tests" else ("PASS", "", ""))
    g3 = GateRunner(cfg, container_env, runner=broken).run(["unit-tests"], GateContext(cfg.repo, [], {}, test_baseline=tb)).gates[0]
    assert g3.status == "FAIL" and g3.delta["rule"] == "absolute" and g3.items[0].rule == "error"


# ----------------------------------------------------------------- whole runs ----
def test_run_on_a_red_base_reaches_green_gates(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                               tmp_path: Path) -> None:
    seed_defects(repo)
    rt = FakeRuntime({"solo": [
        Turn("fixed a, broke c", [edit("src/proto/proto.c", FIX_A.replace(BODY, "/* regress */\n" + BODY))]),
        Turn("fixed c", [edit("src/proto/proto.c", FIX_A)]),
    ]})
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "done", state.data.get("stop_reason") or state.data.get("error")
    assert fake.calls[0] == ["bash", "scripts/check.sh", "unit-tests", "property-tests"]                # the base, before any turn
    tb = state.data["test_baseline"]
    assert tb["active"] and tb["gates"]["unit-tests"]["failures"] == 2 and tb["gates"]["property-tests"]["status"] == "PASS"
    retry = [p for r, p in rt.prompts if r == "solo"][1]
    assert "test_parse_c" in retry and "[test_parse_b]" not in retry
    assert "1 other failure(s) in unit-tests were already failing on the untouched base" in retry
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["baseline"]["active"] and summary["baseline"]["failures"] == {"unit-tests": 2}
    delta = summary["baseline_delta"]
    assert delta["rule"] == "progress" and delta["fixed"] == ["unit-tests test_proto:test_parse_a"] and delta["new"] == []
    assert delta["pre_existing"] == 1
    kinds = [json.loads(x)["kind"] for x in (state.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    assert kinds.index("baseline") < kinds.index("turn")
    assert (state.dir / "gates" / "baseline-01-base.json").is_file()
    assert (state.dir / "logs" / "unit-tests-baseline.log").is_file()
    assert git(repo, "log", "-1", "--format=%s").strip().startswith("DEMO-9:")


def test_run_with_acceptance_tests_needs_them(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                              tmp_path: Path) -> None:
    seed_defects(repo)
    rt = FakeRuntime({"solo": [
        Turn("fixed a", [edit("src/proto/proto.c", FIX_A)]),
        Turn("fixed b", [edit("src/proto/proto.c", HEADER + "/* defect-a */\n" + BODY)]),
    ]})
    orch, _ = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent",
                ticket_ref=str(ticket(tmp_path, "\n## Acceptance tests\n\n- test_parse_b\n")))
    assert state.status == "done", state.data.get("stop_reason")
    retry = [p for r, p in rt.prompts if r == "solo"][1]
    assert "required test(s) not passing: test_parse_b" in retry
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["baseline_delta"]["rule"] == "required"
    assert summary["baseline_delta"]["required"] == ["test_parse_b"]


def test_reviewer_gets_the_baseline_delta(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                          tmp_path: Path) -> None:
    seed_defects(repo)
    rt = FakeRuntime({
        "planner": [Turn("planned", [call("write_plan", plan="1. fix the parser")])],
        "implementer": [Turn("fixed a", [edit("src/proto/proto.c", FIX_A)])],
        "test-writer": [Turn("no test needed", [])],
        "reviewer": [Turn("ok", [call("write_review", verdict="approve", score=4, summary="good")])],
    })
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "done", state.data.get("stop_reason") or state.data.get("error")
    # one base run for every test gate the workflow uses (fast + ladder), cheap first
    assert fake.calls[0] == ["bash", "scripts/check.sh", "unit-tests", "property-tests", "sanitizers", "loopback"]
    review = [p for r, p in rt.prompts if r == "reviewer"][0]
    assert "Baseline rule" in review and "fixed by this change: unit-tests test_proto:test_parse_a" in review
    assert "still failing as on the base (pre-existing, not this change's and not a finding): 2" in review


def test_off_keeps_absolute_gates(repo: Path, container_env: Environment, tmp_path: Path) -> None:
    seed_defects(repo)
    cfg = load_config(repo=repo, env={"HARNESS_BASELINE": "off", "HARNESS_RETRY_BUDGET": "0"})
    rt = FakeRuntime({"solo": [Turn("fixed a", [edit("src/proto/proto.c", FIX_A)])]})
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "escalated" and "unit-tests:FAIL" in state.data["stop_reason"]
    assert base_calls(fake) == [] and state.data["test_baseline"]["mode"] == "off"
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["baseline"]["active"] is False and summary["baseline_delta"] is None


def test_dirty_worktree_at_start_keeps_absolute_gates(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                                      tmp_path: Path) -> None:
    seed_defects(repo)
    # someone's uncommitted edits: a base recorded now would not be the untouched base
    (repo / "src/proto/scratch.c").write_text("/* experiment */\n", encoding="utf-8")
    (repo / "notes.txt").write_text("scratch\n", encoding="utf-8")
    cfg.loop["retry_budget"] = 0
    rt = FakeRuntime({"solo": [Turn("fixed a", [edit("src/proto/proto.c", FIX_A)])]})
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket(tmp_path)))
    # no base recorded, absolute gates: fixing a is not enough, unit-tests still fails on defect-b
    assert base_calls(fake) == []
    assert state.status == "escalated" and "unit-tests:FAIL" in state.data["stop_reason"]
    tb = state.data["test_baseline"]
    assert tb["active"] is False and tb["dirty"] == ["notes.txt", "src/proto/scratch.c"]
    assert "not clean when the run started" in tb["reason"] and "src/proto/scratch.c" in tb["reason"]
    trail = [json.loads(x) for x in (state.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()]
    ev = next(e for e in trail if e["kind"] == "baseline")
    assert ev["active"] is False and ev["dirty"] == ["notes.txt", "src/proto/scratch.c"] and "not clean" in ev["reason"]
    assert any("baseline off" in line and "not clean" in line for line in orch.lines)  # type: ignore[attr-defined]
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["baseline"]["active"] is False and "not clean" in summary["baseline"]["reason"]


def test_green_base_behaves_as_before(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                      tmp_path: Path) -> None:
    rt = FakeRuntime({"solo": [Turn("broke it", [edit("src/proto/proto.c", HEADER + "/* defect-a */\n" + BODY)]),
                               Turn("fixed", [edit("src/proto/proto.c", HEADER + "/* checked */\n" + BODY)])]})
    (repo / "src/proto/proto.c").write_text(HEADER + BODY, encoding="utf-8")
    git(repo, "commit", "-q", "-am", "green")
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "done", state.data.get("stop_reason")
    assert state.data["test_baseline"]["active"] is False
    assert "green" in state.data["test_baseline"]["reason"]
    retry = [p for r, p in rt.prompts if r == "solo"][1]
    assert "[test_parse_a]" in retry and "baseline" not in retry.lower()


def test_resume_reuses_the_recorded_base(cfg: HarnessConfig, container_env: Environment, repo: Path,
                                         tmp_path: Path) -> None:
    seed_defects(repo)
    cfg.loop["retry_budget"] = 0
    rt = FakeRuntime({"solo": [Turn("broke c", [edit("src/proto/proto.c", BASE + "/* regress */\n")])]})
    orch, fake = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "escalated" and "test_parse_c" in json.dumps(state.data["last_ladder"])
    rt2 = FakeRuntime({"solo": [Turn("fixed a", [edit("src/proto/proto.c", FIX_A)])]})
    orch2, fake2 = make(cfg, container_env, rt2)
    state2 = run(orch2, resume=state.run_id)
    assert state2.status == "done", state2.data.get("stop_reason")
    assert len(base_calls(fake)) == 1 and base_calls(fake2) == []


def sanitizer_world(fn: str, repo: Path) -> tuple[str, str, str]:
    """world, plus sanitizers with a test of its own that fails while proto.c carries 'asan-bug'."""
    if fn == "sanitizers":
        text = (repo / "src/proto/proto.c").read_text(encoding="utf-8")
        bad = "asan-bug" in text
        execs = [("test_sanitizer_proto", "fail" if bad else "pass", [("test_no_overflow", "FAIL" if bad else "PASS")])]
        return ("FAIL" if bad else "PASS", "see build-check/harness/sanitizers.log" if bad else "", ctest_log(execs))
    return world(fn, repo)


def test_a_fix_that_shows_only_in_the_full_ladder_is_not_a_failed_step(
        cfg: HarnessConfig, container_env: Environment, repo: Path, tmp_path: Path) -> None:
    """Two unrelated unit-test failures on the base (unit-tests); the ticket's defect fails sanitizers only. The
    implementer's fast gates (unit-tests, property-tests) cannot see the fix: they pass on "no new failure", the final
    ladder sees the progress in sanitizers. No retry, no escalation, no feedback about the base failures."""
    seed_defects(repo, BASE + "/* asan-bug */\n")
    rt = FakeRuntime({
        "planner": [Turn("planned", [call("write_plan", plan="1. fix the overflow")])],
        "implementer": [Turn("fixed the overflow", [edit("src/proto/proto.c", BASE)])],
        "test-writer": [Turn("covered", [])],
        "reviewer": [Turn("ok", [call("write_review", verdict="approve", score=4, summary="good")])],
    })
    orch, fake = make(cfg, container_env, rt, decide=sanitizer_world)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "done", state.data.get("stop_reason") or state.data.get("error")
    tb = state.data["test_baseline"]
    assert tb["gates"]["unit-tests"]["failures"] == 2 and tb["gates"]["sanitizers"]["failures"] == 1
    assert state.counters.get("retries", 0) == 0 and state.counters.get("escalations", 0) == 0
    assert len([p for r, p in rt.prompts if r == "implementer"]) == 1
    impl_prompt = [p for r, p in rt.prompts if r == "implementer"][0]
    assert "test_parse_a" not in impl_prompt and "progress" not in impl_prompt
    steps = state.data["steps"]
    assert steps["implement"]["result"] == "pass" and steps["ladder"]["result"] == "pass"
    delta = state.data["last_ladder"]["baseline"]
    assert delta["final"] is True and delta["progress"] is True
    assert delta["fixed"] == ["sanitizers test_sanitizer_proto:test_no_overflow"] and delta["pre_existing"] == 2
    review = [p for r, p in rt.prompts if r == "reviewer"][0]
    assert "fixed by this change: sanitizers test_sanitizer_proto:test_no_overflow" in review


def test_no_progress_on_the_final_ladder_goes_back_to_the_implementer(
        cfg: HarnessConfig, container_env: Environment, repo: Path, tmp_path: Path) -> None:
    seed_defects(repo, BASE + "/* asan-bug */\n")
    cfg.loop["ladder_rounds"] = 1
    rt = FakeRuntime({
        "planner": [Turn("planned", [call("write_plan", plan="1. tidy")])],
        "implementer": [Turn("tidied", [edit("src/proto/proto.c", BASE + "/* asan-bug */\n/* tidy */\n")]),
                        Turn("fixed", [edit("src/proto/proto.c", BASE)])],
        "test-writer": [Turn("nothing", []), Turn("nothing", [])],
        "reviewer": [Turn("ok", [call("write_review", verdict="approve", score=4, summary="good")])],
    })
    orch, _ = make(cfg, container_env, rt, decide=sanitizer_world)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket(tmp_path)))
    assert state.status == "done", state.data.get("stop_reason") or state.data.get("error")
    assert state.counters.get("retries", 0) == 0 and state.counters["ladder_fails"] == 1
    impl = [p for r, p in rt.prompts if r == "implementer"]
    assert len(impl) == 2 and "no progress" in impl[1] and "context, not a list to fix" in impl[1]
    assert "Work from the ticket" in impl[1]


def test_eval_backend_keeps_absolute_gates_unless_a_variant_opts_in(repo: Path) -> None:
    cfg = backend.apply_settings(load_config(repo=repo, env={}), api.RunSettings())
    assert cfg.loop["baseline"] == "off"
    cfg = backend.apply_settings(load_config(repo=repo, env={}), api.RunSettings(extra={"baseline": "auto"}))
    assert cfg.loop["baseline"] == "auto"
    with pytest.raises(ValueError):
        backend.apply_settings(load_config(repo=repo, env={}), api.RunSettings(extra={"baseline": "always"}))
    assert load_config(repo=repo, env={}).loop["baseline"] == "auto"
