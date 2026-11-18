import subprocess
from pathlib import Path

import pytest

from harness.config import HarnessConfig
from harness.envdetect import Environment
from harness.gates import GateContext, GateRunner, disallowed_env_key, parse_check_output, parse_log

from .conftest import FakeCheck, git

CHECK_OUT = """\
build  PASS    812 ms (budget   4 s)  build host+pi
unit-tests  FAIL   1534 ms (budget   2 s)  unit                   see build-check/harness/unit-tests.log
property-tests  SKIP      3 ms (budget   2 s OVER BUDGET)  property               not configured
---- ladder: 1 PASS, 1 FAIL, 1 SKIP in 2349 ms
"""


def test_parse_check_output() -> None:
    r = parse_check_output(CHECK_OUT)
    assert r["build"].status == "PASS" and r["build"].ms == 812
    assert r["unit-tests"].status == "FAIL" and r["unit-tests"].name == "unit" and r["unit-tests"].reason.startswith("see ")
    assert r["property-tests"].over_budget and r["property-tests"].reason == "not configured"


def test_compiler_and_unity_parsers(repo: Path) -> None:
    log = ("/work/src/proto/proto.c:6:5: error: unused variable 'x' [-Werror=unused-variable]\n"
           "\x1b[0m/work/tests/unit/test_proto.c:4:test_parse:\x1b[41mFAIL\x1b[0m: Expected 1 Was 2\n"
           " 10 - test_proto (Failed)\n 11 - test_other (SEGFAULT)\n")
    items = parse_log("ctest", log, "unit-tests", repo, context=1)
    by_rule = {i.rule: i for i in items}
    assert by_rule["-Werror=unused-variable"].file == "src/proto/proto.c"
    assert by_rule["-Werror=unused-variable"].line == 6
    assert ">6 |     return x + 1;" in by_rule["-Werror=unused-variable"].excerpt
    unity = by_rule["test_parse"]
    assert (unity.file, unity.line, unity.message) == ("tests/unit/test_proto.c", 4, "Expected 1 Was 2")
    assert "test_other" in by_rule and "test_proto" not in by_rule   # covered by the Unity line


def test_sanitizer_nm_cppcheck_coverage_parsers(repo: Path) -> None:
    asan = ("==12==ERROR: AddressSanitizer: heap-buffer-overflow on address 0x1\n"
            "    #0 0x55 in __interceptor_memcpy ../sanitizer.inc:1\n"
            "    #1 0x56 in ts_parse /work/src/proto/proto.c:6:12\n"
            "/work/src/proto/proto.c:5:3: runtime error: signed integer overflow\n")
    items = parse_log("sanitizer", asan, "sanitizers", repo)
    assert items[0].rule == "AddressSanitizer:heap-buffer-overflow" and items[0].file == "src/proto/proto.c"
    assert items[0].line == 6 and "ts_parse" in items[0].message
    assert any(i.rule == "UBSan" and i.line == 5 for i in items)
    nm = "build-host/CMakeFiles/tsd.dir/src/codec/codec.c.o:                 U malloc\n"
    (n,) = parse_log("nm", nm, "no-heap", repo)
    assert (n.file, n.rule) == ("src/codec/codec.c", "no-malloc:malloc")
    cpp = "src/proto/proto.c:6:12: style: misra violation (use --rule-texts=<file>) [misra-c2012-10.4]\n"
    (c,) = parse_log("cppcheck", cpp, "misra", repo)
    assert c.rule == "misra-c2012-10.4" and "10.4" in c.message
    cov = "coverage below 80% for txq (src/txq/) from its own tests:\nlines: 41.0% (100 out of 244)\n"
    (cv,) = parse_log("coverage", cov, "coverage", repo)
    assert cv.rule == "coverage:txq" and cv.excerpt.startswith("lines: 41.0%")


def test_runner_batches_cheap_first_and_stops(cfg: HarnessConfig, container_env: Environment) -> None:
    def decide(fn: str, repo: Path) -> tuple[str, str, str]:
        if fn == "unit-tests":
            return "FAIL", "see log", "/work/tests/unit/test_proto.c:4:test_parse:FAIL: Expected 1 Was 2\n"
        return "PASS", "", ""
    fake = FakeCheck(cfg, decide)
    runner = GateRunner(cfg, container_env, runner=fake)
    ladder = runner.run("fast", GateContext(cfg.repo, ["src/**"], cfg.policy))
    assert fake.calls == [["bash", "scripts/check.sh", "diff-policy", "build", "no-heap", "unit-tests", "property-tests"]]   # one container start
    assert fake.envs[0]["TS_CHECK_LOG"] == "build-check/harness"
    assert ladder.summary() == "compliance:PASS style:PASS comments:PASS diff-policy:PASS build:PASS no-heap:PASS unit-tests:FAIL property-tests:PASS"
    assert not ladder.ok
    fb = ladder.feedback(6)
    assert "unit-tests tests/unit/test_proto.c:4 [test_parse] Expected 1 Was 2" in fb
    assert "build" not in fb.split("\n", 1)[1]          # passing gates cost no tokens


def test_python_gate_failure_skips_the_container(cfg: HarnessConfig, container_env: Environment,
                                                  repo: Path) -> None:
    (repo / "include" / "ts_proto.h").write_text("changed\n", encoding="utf-8")
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run("fast", GateContext(repo, ["src/**"], cfg.policy))
    assert fake.calls == []
    assert ladder.summary() == "compliance:FAIL style:NOT_RUN comments:NOT_RUN diff-policy:NOT_RUN build:NOT_RUN no-heap:NOT_RUN unit-tests:NOT_RUN property-tests:NOT_RUN"
    assert "protected-path" in ladder.feedback()


def test_host_starts_the_self_wrapping_check_script(cfg: HarnessConfig) -> None:
    # check.sh reads git on the host, then re-executes itself in the container
    host = Environment(in_container=False, system="Windows", bash="C:/Git/bin/bash.exe", docker="docker",
                       devcontainer=True)
    assert cfg.gates.self_wrapping
    fake = FakeCheck(cfg)
    GateRunner(cfg, host, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == [["C:/Git/bin/bash.exe", "scripts/check.sh", "unit-tests"]]
    assert fake.envs[0]["MSYS_NO_PATHCONV"] == "1"


def test_host_runs_a_plain_script_through_dockerbuild(cfg: HarnessConfig) -> None:
    host = Environment(in_container=False, system="Windows", bash="C:/Git/bin/bash.exe", docker="docker",
                       devcontainer=True)
    cfg.gates.self_wrapping = False
    fake = FakeCheck(cfg)
    GateRunner(cfg, host, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == [["C:/Git/bin/bash.exe", "scripts/dockerbuild.sh", "scripts/check.sh", "unit-tests"]]


def test_fuzz_gate_gets_its_own_batch_and_env(cfg: HarnessConfig, container_env: Environment) -> None:
    fake = FakeCheck(cfg)
    GateRunner(cfg, container_env, runner=fake).run(["unit-tests", "fuzz"], GateContext(cfg.repo, [], cfg.policy))
    assert [c[-1] for c in fake.calls] == ["unit-tests", "fuzz"]
    assert fake.envs[1]["TS_FUZZ"] == "1"
    assert fake.envs[0].get("TS_FUZZ") != "1"


# the keys the harness sets itself, and the one that makes check.sh skip docker on a host
HARNESS_ENV_KEYS = ["TS_IN_CONTAINER", "TS_CONTAINER_NAME", "TS_IMAGE", "TS_CHECK_LOG"]
NOT_TS_KEYS = ["PATH", "BASH_ENV", "LD_PRELOAD", "HARNESS_RUN_ID", "ts_fuzz", "XTS_FUZZ", "TS_", "TS_FUZZ-X",
               "TS_FUZZ\n", "TS_Fuzz", "TS_FUZZ=1", ""]


@pytest.mark.parametrize("key", HARNESS_ENV_KEYS + NOT_TS_KEYS)
def test_a_gate_env_key_that_is_not_allowed_makes_the_gate_an_error(cfg: HarnessConfig, container_env: Environment,
                                                                    key: str) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_FUZZ": "1", key: "1"}
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == []                                  # check.sh never started: nothing is silently dropped
    (g,) = ladder.gates
    assert (g.id, g.status, g.reason) == ("unit-tests", "ERROR", f"gate unit-tests: env {key} is not allowed")
    assert not ladder.ok and "env " in ladder.feedback() and "is not allowed" in ladder.feedback()


def test_an_env_the_harness_sets_itself_is_never_overridden_by_a_gate(cfg: HarnessConfig,
                                                                       container_env: Environment) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_CHECK_LOG": "elsewhere", "TS_FOO_1": "x"}
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == [] and ladder.gates[0].reason == "gate unit-tests: env TS_CHECK_LOG is not allowed"
    cfg.gates.gates["unit-tests"].env = {"TS_FOO_1": "x"}              # any other TS_ key is the gate's to set
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert ladder.ok and fake.envs[0]["TS_FOO_1"] == "x" and fake.envs[0]["TS_CHECK_LOG"] == "build-check/harness"


def test_a_refused_gate_env_stops_the_ladder_like_a_failing_gate(cfg: HarnessConfig,
                                                                 container_env: Environment) -> None:
    cfg.gates.gates["fuzz"].env = {"TS_FUZZ": "1", "TS_IN_CONTAINER": "1"}
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests", "fuzz", "loopback"],
                                                              GateContext(cfg.repo, [], cfg.policy))
    assert [c[-1] for c in fake.calls] == ["unit-tests"]               # the batch before it ran, the one after did not
    assert [(g.id, g.status) for g in ladder.gates] == [("unit-tests", "PASS"), ("fuzz", "ERROR"), ("loopback", "NOT_RUN")]
    assert ladder.gates[1].budget_s == cfg.gates.gates["fuzz"].budget_s
    assert ladder.exit_codes == [0] and len(ladder.commands) == 1      # a batch that did not start has no command


def test_every_gate_of_a_batch_with_a_refused_env_gets_its_own_error(cfg: HarnessConfig,
                                                                     container_env: Environment) -> None:
    for gid in ("unit-tests", "property-tests"):
        cfg.gates.gates[gid].env = {"BASH_ENV": "/tmp/x"}      # the same env: the two gates are one batch
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests", "property-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == []
    assert [(g.id, g.status, g.reason) for g in ladder.gates] == [
        ("unit-tests", "ERROR", "gate unit-tests: env BASH_ENV is not allowed"),
        ("property-tests", "ERROR", "gate property-tests: env BASH_ENV is not allowed")]


def test_every_gate_of_a_batch_is_checked_not_only_the_first(cfg: HarnessConfig, container_env: Environment,
                                                           monkeypatch: pytest.MonkeyPatch) -> None:
    cfg.gates.gates["fuzz"].env = {"PATH": "/x"}
    monkeypatch.setattr(GateRunner, "_batches", lambda self, gates: [list(gates)])      # one batch, whatever the env
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["unit-tests", "fuzz"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == []                                  # the clean gate does not start without its batch
    assert [(g.id, g.status) for g in ladder.gates] == [("unit-tests", "NOT_RUN"), ("fuzz", "ERROR")]
    assert ladder.gates[1].reason == "gate fuzz: env PATH is not allowed"


def test_the_first_refused_key_of_a_gate_is_named(cfg: HarnessConfig, container_env: Environment) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_OK": "1", "PATH": "/x", "TS_IMAGE": "evil"}
    ladder = GateRunner(cfg, container_env, runner=FakeCheck(cfg)).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert ladder.gates[0].reason == "gate unit-tests: env PATH is not allowed"


# _run_check is reachable on its own: the guard is in it, not only in run()
@pytest.mark.parametrize("key", HARNESS_ENV_KEYS + NOT_TS_KEYS)
def test_run_check_on_its_own_refuses_a_gate_env_key_that_is_not_allowed(cfg: HarnessConfig,
                                                                         container_env: Environment,
                                                                         key: str) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_FUZZ": "1", key: "1"}
    fake = FakeCheck(cfg)
    results, rc, cmd, tail = GateRunner(cfg, container_env, runner=fake)._run_check([cfg.gates.gates["unit-tests"]])
    assert fake.calls == []                                  # nothing started, nothing merged into an environment
    assert [(g.id, g.status, g.reason) for g in results] == [("unit-tests", "ERROR", f"gate unit-tests: env {key} is not allowed")]
    assert results[0].budget_s == cfg.gates.gates["unit-tests"].budget_s
    assert (rc, cmd, tail) == (-1, [], "")                   # no command was built, so none is reported


def test_run_check_on_its_own_with_a_context_and_parse_all_refuses_too(cfg: HarnessConfig,
                                                                       container_env: Environment) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_IN_CONTAINER": "1"}
    fake = FakeCheck(cfg)
    ctx = GateContext(cfg.repo, [], cfg.policy, run_id="r1")
    results, rc, cmd, _ = GateRunner(cfg, container_env, runner=fake)._run_check(
        [cfg.gates.gates["unit-tests"]], ctx, parse_all=True)
    assert fake.calls == [] and [g.status for g in results] == ["ERROR"] and (rc, cmd) == (-1, [])


def test_run_check_on_its_own_does_not_count_a_container_for_a_refused_batch(cfg: HarnessConfig) -> None:
    host = Environment(in_container=False, system="Linux", bash="bash", docker="docker", devcontainer=True)
    cfg.gates.gates["unit-tests"].env = {"BASH_ENV": "/tmp/x"}
    fake = FakeCheck(cfg)
    runner = GateRunner(cfg, host, runner=fake)
    runner._run_check([cfg.gates.gates["unit-tests"]])
    assert fake.calls == [] and runner.containers == 0       # the next batch is still container number 1
    cfg.gates.gates["unit-tests"].env = {}
    runner._run_check([cfg.gates.gates["unit-tests"]])
    assert runner.containers == 1 and fake.envs[0]["TS_CONTAINER_NAME"].endswith("-1")


def test_run_check_on_its_own_checks_every_gate_of_the_batch(cfg: HarnessConfig, container_env: Environment) -> None:
    cfg.gates.gates["fuzz"].env = {"TS_FUZZ": "1", "PATH": "/x"}
    fake = FakeCheck(cfg)
    results, rc, cmd, _ = GateRunner(cfg, container_env, runner=fake)._run_check(
        [cfg.gates.gates["unit-tests"], cfg.gates.gates["fuzz"], cfg.gates.gates["loopback"]])
    assert fake.calls == []                                  # the clean gates do not start without their batch
    assert [(g.id, g.status) for g in results] == [("unit-tests", "NOT_RUN"), ("fuzz", "ERROR"), ("loopback", "NOT_RUN")]
    assert results[1].reason == "gate fuzz: env PATH is not allowed"


def test_run_check_on_its_own_still_runs_a_batch_whose_env_is_allowed(cfg: HarnessConfig,
                                                                      container_env: Environment) -> None:
    cfg.gates.gates["unit-tests"].env = {"TS_FOO_1": "x"}
    fake = FakeCheck(cfg)
    results, rc, cmd, _ = GateRunner(cfg, container_env, runner=fake)._run_check([cfg.gates.gates["unit-tests"]])
    assert [(g.id, g.status) for g in results] == [("unit-tests", "PASS")] and rc == 0 and cmd == fake.calls[0]
    assert fake.envs[0]["TS_FOO_1"] == "x" and fake.envs[0]["TS_CHECK_LOG"] == "build-check/harness"


def test_disallowed_env_key_and_the_gates_that_ship(cfg: HarnessConfig) -> None:
    assert disallowed_env_key({}) is None
    assert disallowed_env_key({"TS_FUZZ": "1", "FUZZ_SECS": "x"}) == "FUZZ_SECS"
    assert disallowed_env_key({"TS_A1_B": "1"}) is None
    assert [g.id for g in cfg.gates.gates.values()                                # as shipped
            if g.kind == "check" and disallowed_env_key(g.env) is not None] == []
    assert disallowed_env_key({"": "x"}) == ""           # an empty key is a key, and not allowed


def test_a_python_gate_reads_its_env_itself_under_any_name(cfg: HarnessConfig,
                                                           container_env: Environment) -> None:
    gate = cfg.gates.gates["compliance"]
    assert gate.kind == "python"
    gate.env = {"MAX_LINES": "3"}                         # its own setting: never reaches check.sh
    fake = FakeCheck(cfg)
    ladder = GateRunner(cfg, container_env, runner=fake).run(["compliance"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.calls == [] and ladder.summary() == "compliance:PASS"


def test_timeout_and_missing_result_line(cfg: HarnessConfig, container_env: Environment) -> None:
    def boom(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        raise subprocess.TimeoutExpired(cmd, 1, output="build  PASS 1 ms (budget   4 s)  build\n")
    ladder = GateRunner(cfg, container_env, runner=boom).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert ladder.gates[0].status == "ERROR" and "timed out" in ladder.gates[0].reason
    assert "docker kill" not in ladder.gates[0].reason                   # in the container: no container to kill

    def silent(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        return subprocess.CompletedProcess(cmd, 2, "docker: command not found\n", "")
    ladder = GateRunner(cfg, container_env, runner=silent).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert ladder.gates[0].status == "ERROR" and "docker: command not found" in ladder.gates[0].reason


def test_host_names_the_gate_container_and_kills_it_on_timeout(cfg: HarnessConfig, monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_RUN_ID", raising=False)
    host = Environment(in_container=False, system="Windows", bash="C:/Git/bin/bash.exe", docker="C:/docker.exe",
                       devcontainer=True)
    calls: list[tuple[list[str], dict]] = []

    def runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        calls.append((list(cmd), dict(kw.get("env") or {})))
        if cmd[1:2] == ["kill"]:
            return subprocess.CompletedProcess(cmd, 0, "", "")
        raise subprocess.TimeoutExpired(cmd, 1, output="")
    gr = GateRunner(cfg, host, runner=runner)
    ladder = gr.run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="20261120-101500-ts-104-ab12"))
    assert calls[0][0] == ["C:/Git/bin/bash.exe", "scripts/check.sh", "unit-tests"]
    assert calls[0][1]["TS_CONTAINER_NAME"] == "ts-gate-20261120-101500-ts-104-ab12-1"
    assert calls[0][1]["HARNESS_RUN_ID"] == "20261120-101500-ts-104-ab12"      # the label dockerbuild.sh sets
    assert calls[1][0] == ["C:/docker.exe", "kill", "ts-gate-20261120-101500-ts-104-ab12-1"]
    assert ladder.gates[0].status == "ERROR" and "ts-gate-20261120-101500-ts-104-ab12-1 killed" in ladder.gates[0].reason
    # the next ladder of the same run gets the next number; a failed kill is reported, not raised
    def no_docker(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        if cmd[1:2] == ["kill"]:
            raise OSError("docker not found")
        raise subprocess.TimeoutExpired(cmd, 1, output="")
    gr.runner = no_docker
    ladder = gr.run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="20261120-101500-ts-104-ab12"))
    assert "docker kill ts-gate-20261120-101500-ts-104-ab12-2 failed" in ladder.gates[0].reason


def test_the_watchdog_kills_the_container_while_the_client_still_holds_the_pipes(
        cfg: HarnessConfig, monkeypatch) -> None:
    """The real shape of a timeout on a host: bash is gone but docker run keeps the pipes, so
    subprocess.run does not return until the container exits -- here, when it is killed. The
    batch is a timeout even though the partial output has result lines."""
    import threading
    monkeypatch.delenv("HARNESS_RUN_ID", raising=False)
    host = Environment(in_container=False, system="Windows", bash="bash", docker="docker", devcontainer=True)
    killed = threading.Event()
    seen: list[tuple[list[str], float]] = []

    def runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
        seen.append((list(cmd), float(kw.get("timeout") or 0)))
        if cmd[1:2] == ["kill"]:
            killed.set()
            return subprocess.CompletedProcess(cmd, 0, "", "")
        assert killed.wait(10), "the watchdog never killed the container"
        return subprocess.CompletedProcess(cmd, 137, "unit-tests  PASS     10 ms (budget   2 s)  unit\n", "")
    cfg.gates.gates["unit-tests"].timeout_s = 0.2
    cfg.gates.gates["property-tests"].timeout_s = 0.2
    ladder = GateRunner(cfg, host, runner=runner).run(["unit-tests", "property-tests"], GateContext(cfg.repo, [], cfg.policy,
                                                                                run_id="wd"))
    assert seen[0][1] == 0.4 + 60.0                                   # run() keeps a grace timeout
    assert seen[1][0] == ["docker", "kill", "ts-gate-wd-1"]
    g3, g4 = ladder.gates
    assert g3.status == "PASS"                                         # finished before the timeout
    assert g4.status == "ERROR" and "timed out after 0 s" in g4.reason and "ts-gate-wd-1 killed" in g4.reason
    assert not ladder.ok


def test_container_names_are_unique_per_invocation(cfg: HarnessConfig, monkeypatch) -> None:
    monkeypatch.delenv("HARNESS_RUN_ID", raising=False)
    host = Environment(in_container=False, system="Linux", bash="bash", docker="docker", devcontainer=True)
    fake = FakeCheck(cfg)
    gr = GateRunner(cfg, host, runner=fake)
    gr.run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="r1"))
    gr.run(["unit-tests", "fuzz"], GateContext(cfg.repo, [], cfg.policy, run_id="r1"))
    assert [e["TS_CONTAINER_NAME"] for e in fake.envs] == ["ts-gate-r1-1", "ts-gate-r1-2", "ts-gate-r1-3"]
    assert {e["HARNESS_RUN_ID"] for e in fake.envs} == {"r1"}
    # a container run passes no name (the scripts run directly there), the run id still goes along
    fake2 = FakeCheck(cfg)
    GateRunner(cfg, Environment(in_container=True, system="Linux", bash="bash", docker=None, devcontainer=True),
               runner=fake2).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="r1"))
    assert "TS_CONTAINER_NAME" not in fake2.envs[0] and fake2.envs[0]["HARNESS_RUN_ID"] == "r1"


def test_an_outer_run_id_labels_the_containers_but_names_stay_per_run(cfg: HarnessConfig, monkeypatch) -> None:
    # a console that starts the harness sets HARNESS_RUN_ID: every container carries that label
    # (tinystream.run=<id>), while names stay unique per orchestrator run (parallel eval runs)
    monkeypatch.setenv("HARNESS_RUN_ID", "console-7")
    host = Environment(in_container=False, system="Linux", bash="bash", docker="docker", devcontainer=True)
    fake = FakeCheck(cfg)
    GateRunner(cfg, host, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="run/a b"))
    GateRunner(cfg, host, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy, run_id="run-b"))
    assert [e["HARNESS_RUN_ID"] for e in fake.envs] == ["console-7", "console-7"]
    assert [e["TS_CONTAINER_NAME"] for e in fake.envs] == ["ts-gate-run-a-b-1", "ts-gate-run-b-1"]


def test_dockerbuild_names_and_labels_the_container() -> None:
    text = (Path(__file__).resolve().parents[2] / "scripts" / "dockerbuild.sh").read_text(encoding="utf-8")
    assert 'NAME="${TS_CONTAINER_NAME:-}"' in text and '"--name" ]' in text
    assert '--label "tinystream.run=$HARNESS_RUN_ID"' in text
    assert "TS_(IN_CONTAINER|CONTAINER_NAME)" in text           # the name is not forwarded into the container


def test_baseline_dirt_is_not_blamed_on_the_agent(cfg: HarnessConfig, container_env: Environment,
                                                  repo: Path) -> None:
    (repo / "notes.txt").write_text("user's own scratch file\n", encoding="utf-8")
    ctx = GateContext(repo, ["src/**"], cfg.policy, baseline=frozenset({"notes.txt"}))
    ladder = GateRunner(cfg, container_env, runner=FakeCheck(cfg)).run(["compliance"], ctx)
    assert ladder.ok
    git(repo, "status", "--short")


def test_dp_log_becomes_items(repo: Path) -> None:
    log = ("protected list: include/ src/pool/\n"
           "protected: the change touches protected paths: include/ts_proto.h scripts/check.sh\n"
           "calls: new process creation / program loading:\n"
           "  src/net/net.c\t    system(cmd);\n"
           "tlv: new TLV type(s): TLV_CTRL_SHELL (the control channel is closed, PROTOCOL.md section 5)\n")
    items = parse_log("dp", log, "diff-policy", repo)
    got = {(i.rule, i.file) for i in items}
    assert ("protected-path", "include/ts_proto.h") in got and ("protected-path", "scripts/check.sh") in got
    assert ("process-call", "src/net/net.c") in got
    assert ("new-tlv", "") in got and any("TLV_CTRL_SHELL" in i.message for i in items)


def test_check_output_parses_dp_line() -> None:
    from harness.gates import parse_check_output
    r = parse_check_output("diff-policy  FAIL     12 ms (budget   1 s)  diff policy            protected: include/x.h\n")
    assert r["diff-policy"].status == "FAIL" and r["diff-policy"].reason.startswith("protected")


def test_a_style_finding_fails_the_ladder_but_the_build_and_tests_still_run(cfg: HarnessConfig, container_env) -> None:
    from harness.gates import GateContext, GateRunner
    src = cfg.repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "int trailing = 1;   \n", encoding="utf-8")
    ladder = GateRunner(cfg, container_env, runner=FakeCheck(cfg)).run("fast", GateContext(cfg.repo, ["src/**"], cfg.policy))
    status = {r.id: r.status for r in ladder.gates}
    assert status["style"] == "FAIL" and not ladder.ok
    assert all(status[g] != "NOT_RUN" for g in ("diff-policy", "build", "unit-tests")), status
