"""The review sees a green ladder, reviews are about concrete defects, a pricier model only
within the budget, and a gate-less variant never reads about gates (fake session, fake check.sh)."""

from pathlib import Path

import pytest

from harness import api
from harness.config import HarnessConfig, load_config
from harness.envdetect import Environment
from harness.eval import variants as eval_variants
from harness.fake import FakeRuntime, Turn, call, edit
from harness.orchestrator import normalize_review
from harness import backend

from .test_backend import make_backend, settings_for
from .test_orchestrator import FIXED, HEADER, events, happy_scripts, make, run, unit_gate

ASAN_LOG = ("==1==ERROR: AddressSanitizer: heap-buffer-overflow\n"
            "    #0 0x1 in ts_parse /work/src/proto/proto.c:5\n")


def asan_gate(fn: str, repo: Path) -> tuple[str, str, str]:
    """unit_gate, and sanitizers fails until proto.c carries the 'bounds checked' fix."""
    if fn == "sanitizers":
        if "bounds checked" in (repo / "src/proto/proto.c").read_text(encoding="utf-8"):
            return "PASS", "", ""
        return "FAIL", "see build-check/harness/sanitizers.log", ASAN_LOG
    return unit_gate(fn, repo)


def approve_turn() -> Turn:
    return Turn("approved", [call("write_review", verdict="approve", score=4, summary="good")])


# ------------------------------------------------------------------ ladder first ----
def test_full_ladder_runs_before_the_review(cfg: HarnessConfig, container_env: Environment,
                                             ticket_file: Path) -> None:
    scripts = happy_scripts()
    scripts["implementer"] = [
        Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)]),
        Turn("asan fixed", [edit("src/proto/proto.c", HEADER + "/* bounds checked */\n" + FIXED)]),
    ]
    scripts["reviewer"] = [approve_turn()]
    rt = FakeRuntime(scripts)
    orch, _ = make(cfg, container_env, rt, decide=asan_gate)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket_file))
    assert state.status == "done", state.data.get("stop_reason")

    impl = [p for r, p in rt.prompts if r == "implementer"]
    assert len(impl) == 2
    assert "full gate ladder" in impl[1] and "sanitizers" in impl[1]       # the ladder's feedback, not a review
    (review_prompt,) = [p for r, p in rt.prompts if r == "reviewer"]   # reviewed once, after a green ladder
    assert "all PASS" in review_prompt and "sanitizers:PASS" in review_prompt and "misra:PASS" in review_prompt
    assert state.counters["ladder_fails"] == 1 and state.counters.get("review_rejects", 0) == 0

    ev = events(state)
    review_at = next(i for i, e in enumerate(ev) if e["kind"] == "review")
    ladders = [i for i, e in enumerate(ev) if e["kind"] == "gates" and e.get("step") == "ladder"]
    assert len(ladders) == 2 and all(i < review_at for i in ladders)
    assert state.data["commit"]


def test_ladder_failures_are_bounded_and_never_reach_the_reviewer(cfg: HarnessConfig, container_env: Environment,
                                                                  ticket_file: Path) -> None:
    scripts = happy_scripts()
    scripts["implementer"] = [Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)])] * 5
    rt = FakeRuntime(scripts)
    orch, _ = make(cfg, container_env, rt, decide=asan_gate)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket_file))
    assert state.status == "escalated" and state.data["stopped_at"] == "ladder"
    assert "ladder failed 3 times (budget 2)" in state.data["stop_reason"]
    assert not [p for r, p in rt.prompts if r == "reviewer"]


def test_review_prompt_says_when_no_gates_ran(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    runtimes: list = []
    scripts = {"implementer": [Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)])],
               "reviewer": [approve_turn()]}
    s = settings_for(repo, tmp_path / "run", ticket_file, gates=False,
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": True})
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(scripts, runtimes=runtimes))
    assert res.status == "completed", res.error
    (review_prompt,) = [p for r, p in runtimes[0].prompts if r == "reviewer"]
    assert "no gates ran" in review_prompt and "never a finding" in review_prompt


# ------------------------------------------------------------ concrete findings ----
@pytest.mark.parametrize("verdict, findings, want, notes", [
    ("reject", [{"file": "src/a.c", "line": 3, "severity": "major", "message": "wrong"}], "reject", 0),
    ("reject", [{"file": "", "line": 0, "severity": "major", "message": "loopback not reported"}], "approve", 2),
    ("reject", [{"file": "src/a.c", "line": 0, "severity": "blocker", "message": "vague"}], "approve", 2),
    ("reject", [{"file": "src/a.c", "line": 9, "severity": "minor", "message": "style"}], "approve", 1),
    ("approve", [{"file": "src/a.c", "line": 3, "severity": "blocker", "message": "overflow"}], "reject", 1),
    ("approve", [], "approve", 0),
])
def test_normalize_review(verdict: str, findings: list, want: str, notes: int) -> None:
    got, out, why = normalize_review(verdict, findings)
    assert got == want and len(why) == notes
    assert all(f["severity"] not in ("blocker", "major") or (f["file"] and f["line"] >= 1) for f in out)


def test_reject_without_a_located_defect_counts_as_approve(cfg: HarnessConfig, container_env: Environment,
                                                           ticket_file: Path) -> None:
    scripts = happy_scripts()
    scripts["implementer"] = [Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)])]
    scripts["reviewer"] = [Turn("rejected", [call("write_review", verdict="reject", score=2,
                                                  summary="loopback and coverage not reported",
                                                  findings=[{"severity": "major",
                                                             "message": "gate evidence incomplete"}])])]
    rt = FakeRuntime(scripts)
    orch, _ = make(cfg, container_env, rt)
    state = run(orch, workflow="jira-to-pr", ticket_ref=str(ticket_file))
    assert state.status == "done" and state.data["review"]["verdict"] == "approve"
    assert state.data["review"]["findings"][0]["severity"] == "minor"
    assert "counts as approve" in " ".join(state.data["review"]["harness_notes"])
    assert len([p for r, p in rt.prompts if r == "implementer"]) == 1     # no rework round
    rec = next(e for e in events(state) if e["kind"] == "review")
    assert rec["asked"] == "reject" and rec["verdict"] == "approve"


# ------------------------------------------------------- escalation within budget ----
BROKEN = HEADER + "int ts_parse(int x)\n{\n    return x + 1; /* broken */\n}\n"


def _solo_escalating(cfg: HarnessConfig, session_cap: float) -> None:
    cfg.role("solo").model = "claude-haiku-5.5"
    cfg.role("solo").escalation_model = "claude-sonnet-5.5"
    cfg.runtimes["copilot-user"]["max_ai_credits_per_session"] = session_cap
    cfg.runtimes["copilot-user"]["max_ai_credits_per_run"] = 50


def test_escalation_within_the_budget_switches(cfg: HarnessConfig, container_env: Environment,
                                               ticket_file: Path) -> None:
    _solo_escalating(cfg, 15)
    rt = FakeRuntime({"solo": [Turn("a", [edit("src/proto/proto.c", BROKEN)]),       # 0.2 credits on Haiku
                               Turn("b", [edit("src/proto/proto.c", HEADER + FIXED)])]})
    orch, _ = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    assert state.status == "done"
    assert rt.model_switches == [("solo", "claude-sonnet-5.5")]                  # ~4 credits needed, 14.8 left


def test_unaffordable_escalation_keeps_retrying_on_the_cheap_model(cfg: HarnessConfig, container_env: Environment,
                                                                   ticket_file: Path) -> None:
    _solo_escalating(cfg, 3)
    rt = FakeRuntime({"solo": [Turn("a", [edit("src/proto/proto.c", BROKEN)])]})
    orch, _ = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    # the retry budget is used on Haiku; only then does a human get the run
    assert state.status == "escalated" and state.data["stopped_at"] == "solve"
    assert "after 3 retries" in state.data["stop_reason"]
    assert rt.model_switches == [] and len(rt.prompts) == 4
    skipped = [e for e in events(state) if e["kind"] == "model.escalation_skipped"]
    assert skipped and skipped[0]["need"] == 4.0


def test_unaffordable_escalation_does_not_ask_a_human(cfg: HarnessConfig, container_env: Environment,
                                                      ticket_file: Path) -> None:
    _solo_escalating(cfg, 3)
    rt = FakeRuntime({"solo": [Turn("a", [edit("src/proto/proto.c", BROKEN)]),
                               Turn("b", [edit("src/proto/proto.c", HEADER + FIXED)])]})
    orch, _ = make(cfg, container_env, rt, mode="interactive", answers=["y"], tty=True)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    assert state.status == "done" and rt.model_switches == []
    assert state.counters["escalations"] == 0


def _sonnet_turns() -> FakeRuntime:
    return FakeRuntime({"solo": [Turn("a", [edit("src/proto/proto.c", BROKEN)]),             # Haiku, 0.2
                                 Turn("b", [edit("src/proto/proto.c", BROKEN + "\n")], nano_aiu=3e9),   # Sonnet, 3.0
                                 Turn("c", [edit("src/proto/proto.c", HEADER + FIXED)])]})


def test_unattended_run_goes_back_to_the_cheap_model_beyond_the_cap(cfg: HarnessConfig, container_env: Environment,
                                                                     ticket_file: Path) -> None:
    # after the Sonnet turn 2.8 credits are left and the next Sonnet turn is estimated at 3.0:
    # an eval batch cannot ask, so the role goes back to Haiku (counted as an escalation) and finishes
    _solo_escalating(cfg, 6)
    rt = _sonnet_turns()
    orch, _ = make(cfg, container_env, rt)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    assert state.status == "done", state.data.get("stop_reason")
    assert len(rt.prompts) == 3
    assert rt.model_switches == [("solo", "claude-sonnet-5.5"), ("solo", "claude-haiku-5.5")]
    assert state.counters["escalations"] == 1
    esc = [e for e in events(state) if e["kind"] == "escalation"]
    assert len(esc) == 1 and esc[0]["choice"] == "retry" and esc[0]["by"] == "eval"
    assert "the next turn of solo on claude-sonnet-5.5 needs ~3.0" in esc[0]["reason"]
    assert [e for e in events(state) if e["kind"] == "model.switch"][-1]["model"] == "claude-haiku-5.5"


def test_autonomous_run_goes_back_to_the_cheap_model_too(cfg: HarnessConfig, container_env: Environment,
                                                         ticket_file: Path) -> None:
    _solo_escalating(cfg, 6)
    rt = _sonnet_turns()
    orch, _ = make(cfg, container_env, rt, mode="autonomous")
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    assert state.status == "done" and state.counters["escalations"] == 1
    assert rt.model_switches[-1] == ("solo", "claude-haiku-5.5")


def test_a_human_may_still_stop_the_run_beyond_the_cap(cfg: HarnessConfig, container_env: Environment,
                                                       ticket_file: Path) -> None:
    _solo_escalating(cfg, 6)
    rt = _sonnet_turns()
    orch, _ = make(cfg, container_env, rt, mode="interactive", answers=["y", "abort"], tty=True)
    state = run(orch, workflow="single-agent", ticket_ref=str(ticket_file))
    assert state.status == "escalated" and len(rt.prompts) == 2
    assert rt.model_switches == [("solo", "claude-sonnet-5.5")]
    assert "the next turn of solo on claude-sonnet-5.5 needs ~3.0" in state.data["stop_reason"]


def test_v7_all_haiku_has_no_escalation(repo: Path) -> None:
    v7 = eval_variants.load_matrix(eval_variants.DEFAULT_FILE).variants["team-small"]
    s = v7.run_settings(str(repo), "")
    assert s.extra.get("escalation") is False
    cfg = backend.apply_settings(load_config(repo=repo, env={}), s)
    assert all(rc.escalation_model is None for rc in cfg.roles.values())


# ------------------------------------------------------------- gate-less prompts ----
def test_gateless_variant_never_mentions_gates(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    runtimes: list = []
    scripts = {"implementer": [Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)])]}
    s = settings_for(repo, tmp_path / "b0", ticket_file, gates=False, retry_budget=0, custom_instructions=False,
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False})
    s.extra["role_overrides"] = {"implementer": {"tools": ["view", "grep", "glob", "edit", "create", "bash",
                                                           "read_ticket"]}}
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(scripts, runtimes=runtimes))
    assert res.status == "completed", res.error
    rt = runtimes[0]
    (impl,) = rt.sessions_for("implementer")
    (prompt,) = [p for r, p in rt.prompts if r == "implementer"]
    for text in (impl.spec.agent_prompt, prompt):
        assert "run_gates" not in text and "Gate profile" not in text and "gate-checked" not in text


def test_gated_variant_names_its_profile(repo: Path, ticket_file: Path, tmp_path: Path) -> None:
    runtimes: list = []
    s = settings_for(repo, tmp_path / "v1", ticket_file,
                     roles={"planner": False, "implementer": True, "test_writer": False, "reviewer": False})
    res = api.run_workflow("jira-to-pr", "DEMO-7", s, "eval", backend=make_backend(runtimes=runtimes))
    assert res.status == "completed", res.error
    rt = runtimes[0]
    first = [p for r, p in rt.prompts if r == "implementer"][0]
    assert "Gate profile for this step: fast (compliance, style, comments, diff-policy, build, no-heap, unit-tests, property-tests)" in first
    assert "Check your change with run_gates" in first
    assert "Use run_gates after a change" in rt.sessions_for("implementer")[0].spec.agent_prompt
