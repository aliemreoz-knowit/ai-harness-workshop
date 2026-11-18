"""The gate ladder without Docker: scripts/lite_gates.py, the hand-off in scripts/check.sh,
envdetect.docker_usable, the doctor's depth, gates the lite ladder does not run ("not run here",
never "ladder incomplete") and where envdetect finds the Copilot CLI the setup installed.

Tests marked ``zig`` build and run the real C tests of this checkout with ``zig cc`` (the
``ziglang`` pip package), in a copy of the C tree under tmp_path; they are skipped when
``python -m ziglang version`` fails (conftest.py).
"""

from __future__ import annotations

import asyncio
import json
import os
import re
import shutil
import stat
import subprocess
import sys
import time
from pathlib import Path

import pytest

from harness import baseline, cli, doctor, envdetect
from harness.config import HarnessConfig
from harness.doctor import Check, DoctorReport
from harness.envdetect import Environment
from harness.fake import FakeRuntime, Turn, edit
from harness.gates import (NOT_RUN_HERE, GateContext, GateRunner, LadderResult, disallowed_env_key,
                           parse_check_output)
from harness.orchestrator import Orchestrator
from harness.state import RunState

from .conftest import FakeCheck

REPO = Path(__file__).resolve().parents[2]
LITE = REPO / "scripts" / "lite_gates.py"
NEEDS_DOCKER = "needs Docker (not run here)"
# what the lite build reads: the C tree, the test manifest and the lite scripts
C_TREE = ("src", "include", "tests", "third_party", "tools/tsrecv", "tools/tsnetem")
SCRIPTS = ("lite_gates.py", "lite", "test_manifest.txt")


def clean_env(**extra: str) -> dict[str, str]:
    """This environment without the gate knobs of an outer run (TS_*), plus ``extra``."""
    env = {k: v for k, v in os.environ.items() if not k.startswith("TS_")}
    env.update(extra)
    return env


def copy_c_tree(dst: Path) -> Path:
    ignore = shutil.ignore_patterns("__pycache__", "*.pyc")
    for rel in C_TREE:
        shutil.copytree(REPO / rel, dst / rel, ignore=ignore)
    for rel in SCRIPTS:
        src = REPO / "scripts" / rel
        if src.is_dir():
            shutil.copytree(src, dst / "scripts" / rel, ignore=ignore)
        else:
            (dst / "scripts").mkdir(parents=True, exist_ok=True)
            shutil.copy2(src, dst / "scripts" / rel)
    return dst


def run_lite(tree: Path, *gates: str, logs: Path) -> subprocess.CompletedProcess:
    return subprocess.run([sys.executable, str(tree / "scripts" / "lite_gates.py"), *gates], cwd=tree,
                          env=clean_env(TS_CHECK_LOG=str(logs)), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=900)


def exec_statuses(log: str) -> dict[str, str]:
    parsed = baseline.parse_tests(log) or {"execs": {}}
    return {name: e["status"] for name, e in parsed["execs"].items()}


@pytest.fixture(scope="module")
def lite_run(tmp_path_factory: pytest.TempPathFactory) -> tuple[Path, Path, subprocess.CompletedProcess]:
    """g0 g3 on a copy of this checkout's C tree: (tree, log dir, the finished process)."""
    base = tmp_path_factory.mktemp("lite")
    tree = copy_c_tree(base / "tree")
    logs = base / "logs"
    return tree, logs, run_lite(tree, "build", "unit-tests", logs=logs)


# ------------------------------------------------------------ lite_gates.py ----
@pytest.mark.zig
def test_result_lines_parse(lite_run: tuple) -> None:
    _, _, done = lite_run
    gates = parse_check_output(done.stdout)
    assert gates["unit-tests"].status in {"PASS", "FAIL"}, done.stdout + done.stderr
    assert gates["unit-tests"].budget_s == 2 and gates["build"].budget_s == 4
    assert gates["build"].status == "PASS", done.stdout + done.stderr
    assert "lite (zig)" in gates["build"].reason and "lite (zig)" in gates["unit-tests"].reason
    assert done.returncode == (1 if gates["unit-tests"].status == "FAIL" else 0)
    # exactly check.sh's printf: '%-3s %-4s %6d ms (budget %3d s%s)  %-22s %s'
    line = next(ln for ln in done.stdout.splitlines() if ln.startswith("unit-tests "))
    assert re.match(r"^unit-tests  (PASS|FAIL) [ \d]{6} ms \(budget   2 s( OVER BUDGET)?\)  unit {19}", line), line


@pytest.mark.zig
def test_g3_log_feeds_baseline(lite_run: tuple) -> None:
    _, logs, _ = lite_run
    log = (logs / "unit-tests.log").read_text(encoding="utf-8")
    parsed = baseline.parse_tests(log)
    assert parsed is not None, log[-2000:]
    assert parsed["execs"]["test_codec"]["status"] == "pass"
    assert "test_prop_codec" in parsed["execs"]                   # unit includes the property tests
    assert not any(n.startswith("test_lb_") for n in parsed["execs"])
    if os.name == "nt":                                           # POSIX-only tests: reported, never run
        assert "test_capture" not in parsed["execs"]
        assert re.search(r"^SKIP test_capture: not run on Windows", log, re.M), log[-2000:]
    else:
        assert parsed["execs"]["test_capture"]["status"] == "pass"
    for name, e in parsed["execs"].items():                       # failing tests carry their Unity cases
        if e["status"] == "fail" and e["why"] == "Failed":
            assert e["complete"] and any(s == "fail" for s in e["cases"].values()), name


@pytest.mark.zig
def test_path_with_spaces_and_non_ascii(lite_run: tuple, tmp_path: Path) -> None:
    tree, logs, done = lite_run
    other = copy_c_tree(tmp_path / "Åsa Ström dir")
    logs2 = tmp_path / "logs ö"
    done2 = run_lite(other, "build", "unit-tests", logs=logs2)
    first, second = parse_check_output(done.stdout), parse_check_output(done2.stdout)
    assert {g: r.status for g, r in second.items()} == {g: r.status for g, r in first.items()}, done2.stdout
    assert exec_statuses((logs2 / "unit-tests.log").read_text(encoding="utf-8")) == \
        exec_statuses((logs / "unit-tests.log").read_text(encoding="utf-8"))


def test_unrunnable_gates_skip(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, str(LITE), "misra", "sanitizers", "fuzz", "coverage"], cwd=REPO,
                          env=clean_env(TS_CHECK_LOG=str(tmp_path)), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=120)
    gates = parse_check_output(done.stdout)
    assert sorted(gates) == sorted(["misra", "sanitizers", "fuzz", "coverage"]), done.stdout + done.stderr
    assert {g.status for g in gates.values()} == {"SKIP"}
    assert {g.reason for g in gates.values()} == {NEEDS_DOCKER}
    assert done.returncode == 0
    assert (tmp_path / "misra.log").is_file()


def test_g10_is_left_to_the_harness_and_unknown_gates_are_refused(tmp_path: Path) -> None:
    done = subprocess.run([sys.executable, str(LITE), "compliance"], cwd=REPO, env=clean_env(TS_CHECK_LOG=str(tmp_path)),
                          capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    g = parse_check_output(done.stdout)["compliance"]
    assert (g.status, g.reason, done.returncode) == ("SKIP", "harness runs compliance in Python", 0)
    bad = subprocess.run([sys.executable, str(LITE), "unit-tests", "g42"], cwd=REPO, env=clean_env(TS_CHECK_LOG=str(tmp_path)),
                         capture_output=True, text=True, encoding="utf-8", errors="replace", timeout=120)
    assert bad.returncode == 2 and "unknown gate: g42" in bad.stdout + bad.stderr
    assert parse_check_output(bad.stdout) == {}                   # refused before anything runs


def test_the_zig_pin_is_one_value() -> None:
    m = re.search(r'^ZIG_VERSION = "([0-9.]+)"', LITE.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == envdetect.ZIG_VERSION == "0.16.0"


# ----------------------------------------------------- the check.sh hand-off ----
def fake_docker(bin_dir: Path, body_sh: str, body_bat: str) -> dict[str, str]:
    """A ``docker`` first on PATH: a sh script (bash finds it) and a .bat (Python finds it on Windows)."""
    bin_dir.mkdir(parents=True, exist_ok=True)
    sh = bin_dir / "docker"
    sh.write_text("#!/bin/sh\n" + body_sh, encoding="utf-8", newline="\n")
    sh.chmod(sh.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)
    (bin_dir / "docker.bat").write_text("@echo off\r\n" + body_bat, encoding="utf-8", newline="")
    return {"PATH": str(bin_dir) + os.pathsep + os.environ.get("PATH", "")}


NOT_RUNNING_SH = 'echo "Cannot connect to the Docker daemon. Is the docker daemon running?" >&2\nexit 1\n'
NOT_RUNNING_BAT = "echo Cannot connect to the Docker daemon. Is the docker daemon running? 1>&2\r\nexit /b 1\r\n"


def test_docker_installed_not_running(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for k, v in fake_docker(tmp_path / "bin", NOT_RUNNING_SH, NOT_RUNNING_BAT).items():
        monkeypatch.setenv(k, v)
    t0 = time.monotonic()
    assert envdetect.docker_usable() == (False, "Docker installed but not running")
    assert time.monotonic() - t0 < 10


def test_docker_not_installed_and_docker_that_hangs(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setenv("PATH", str(tmp_path / "empty"))
    assert envdetect.docker_usable() == (False, "Docker not installed")
    env = fake_docker(tmp_path / "bin", "sleep 30\n", "ping -n 30 127.0.0.1 >nul\r\n")
    monkeypatch.setenv("PATH", env["PATH"])
    t0 = time.monotonic()
    assert envdetect.docker_usable(timeout=1.0) == (False, "Docker installed but not running")
    assert time.monotonic() - t0 < 10


BASH = envdetect.find_bash()


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.skipif(os.path.exists("/.dockerenv"), reason="inside a container check.sh never hands off")
@pytest.mark.parametrize("why", ["not running", "TS_LITE=1"])
def test_check_sh_hands_off_to_the_lite_ladder(why: str, tmp_path: Path) -> None:
    if why == "TS_LITE=1":     # a docker that would answer: TS_LITE=1 still picks the lite ladder
        env = clean_env(**fake_docker(tmp_path / "bin", "exit 0\n", "exit /b 0\r\n"), TS_LITE="1")
    else:
        env = clean_env(**fake_docker(tmp_path / "bin", NOT_RUNNING_SH, NOT_RUNNING_BAT))
    env.update(TS_PYTHON=sys.executable, TS_CHECK_LOG=str(tmp_path / "logs"), MSYS_NO_PATHCONV="1")
    t0 = time.monotonic()
    done = subprocess.run([str(BASH), "scripts/check.sh", "misra", "sanitizers"], cwd=REPO, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=120)
    assert time.monotonic() - t0 < 10
    gates = parse_check_output(done.stdout)
    assert {g: (r.status, r.reason) for g, r in gates.items()} == \
        {"misra": ("SKIP", NEEDS_DOCKER), "sanitizers": ("SKIP", NEEDS_DOCKER)}, done.stdout + done.stderr
    assert done.returncode == 0


# a docker that takes 9 s for every call (longer than the 8 s probe), then fails
HANGS_SH = "sleep 9\n" + NOT_RUNNING_SH
HANGS_BAT = "ping -n 10 127.0.0.1 >nul\r\n" + NOT_RUNNING_BAT


@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.skipif(os.path.exists("/.dockerenv"), reason="inside a container check.sh never hands off")
@pytest.mark.parametrize("docker", ["not running", "hangs"])
def test_ts_lite_0_never_hands_off_to_the_lite_ladder(docker: str, tmp_path: Path) -> None:
    """TS_LITE=0 (the stage, the central eval batches): check.sh waits for docker and fails like the
    container path when Docker does not work; it never runs the Docker-free ladder."""
    calls = tmp_path / "docker-calls.txt"
    record = f'echo "$*" >> "{calls.as_posix()}"\n'
    body_sh = record + (HANGS_SH if docker == "hangs" else NOT_RUNNING_SH)
    body_bat = f'echo %* >> "{calls}"\r\n' + (HANGS_BAT if docker == "hangs" else NOT_RUNNING_BAT)
    env = clean_env(**fake_docker(tmp_path / "bin", body_sh, body_bat), TS_LITE="0")
    env.update(TS_PYTHON=sys.executable, TS_CHECK_LOG=str(tmp_path / "logs"), MSYS_NO_PATHCONV="1")
    t0 = time.monotonic()
    done = subprocess.run([str(BASH), "scripts/check.sh", "misra", "sanitizers"], cwd=REPO, env=env, capture_output=True,
                          text=True, encoding="utf-8", errors="replace", timeout=90)
    took = time.monotonic() - t0
    out = done.stdout + done.stderr
    assert parse_check_output(done.stdout) == {}, out                   # no lite result line
    assert "(scripts/lite_gates.py, zig cc)" not in out and "lite (zig)" not in out, out    # no hand-off
    assert done.returncode != 0, out
    assert calls.is_file() and calls.read_text(encoding="utf-8").strip(), out     # docker was asked
    assert "TS_LITE=0" in done.stderr, out                              # the failure says why
    if docker == "hangs":
        assert took >= 9, (took, out)                                   # waited past the 8 s probe


# ------------------------------------------------------------- gates.py ----
def test_the_gate_env_carries_the_harness_python(cfg: HarnessConfig, container_env: Environment) -> None:
    fake = FakeCheck(cfg)
    GateRunner(cfg, container_env, runner=fake).run(["unit-tests"], GateContext(cfg.repo, [], cfg.policy))
    assert fake.envs[0]["TS_PYTHON"] == sys.executable
    assert disallowed_env_key({"TS_PYTHON": "x"}) == "TS_PYTHON"     # the harness's own, like TS_IN_CONTAINER
    assert disallowed_env_key({"TS_LITE": "1"}) == "TS_LITE"


def test_the_not_run_here_reason_is_one_value() -> None:
    m = re.search(r'^NEEDS_DOCKER = "([^"]+)"', LITE.read_text(encoding="utf-8"), re.M)
    assert m and m.group(1) == NOT_RUN_HERE == NEEDS_DOCKER


# ------------------------------------- a gate the lite ladder does not run ----
def lite_skips(*not_here: str, other: dict[str, str] | None = None):
    """FakeCheck decide(): the gates in ``not_here`` SKIP as the lite ladder does, ``other`` SKIP with
    the reason given, everything else PASSes."""
    def decide(fn: str, repo: Path) -> tuple[str, str, str]:
        if fn in not_here:
            return "SKIP", NOT_RUN_HERE, ""
        if other and fn in other:
            return "SKIP", other[fn], ""
        return "PASS", "lite (zig)", ""
    return decide


def run_ladder(cfg: HarnessConfig, env: Environment, spec: str, decide) -> LadderResult:
    return GateRunner(cfg, env, runner=FakeCheck(cfg, decide)).run(spec, GateContext(cfg.repo, [], cfg.policy))


def test_a_fast_ladder_with_lite_skips_passes_and_names_the_gates_not_run(cfg: HarnessConfig,
                                                                          container_env: Environment) -> None:
    ladder = run_ladder(cfg, container_env, "fast", lite_skips("no-heap"))
    assert ladder.ok and not ladder.incomplete_only and ladder.skipped == []
    assert [g.id for g in ladder.not_run_here] == ["no-heap"]
    assert ladder.lite_note() == "lite ladder: no-heap not run here (needs Docker)"
    assert ladder.summary() == ("compliance:PASS style:PASS comments:PASS diff-policy:PASS build:PASS no-heap:SKIP unit-tests:PASS property-tests:PASS; "
                                "lite ladder: no-heap not run here (needs Docker)")
    assert ladder.feedback().splitlines()[:2] == ["GATES PASS (compliance:PASS style:PASS comments:PASS diff-policy:PASS build:PASS no-heap:SKIP unit-tests:PASS property-tests:PASS)",
                                                  "lite ladder: no-heap not run here (needs Docker)"]
    assert ladder.to_dict()["not_run_here"] == ["no-heap"] and ladder.to_dict()["ok"] is True
    full = run_ladder(cfg, container_env, "full", lite_skips("misra", "no-heap", "sanitizers", "coverage"))
    assert full.ok and full.lite_note() == "lite ladder: misra no-heap sanitizers coverage not run here (needs Docker)"


def test_a_skip_with_another_reason_still_makes_the_ladder_incomplete(cfg: HarnessConfig,
                                                                      container_env: Environment) -> None:
    zig = f"zig not found: python -m pip install ziglang=={envdetect.ZIG_VERSION}"
    ladder = run_ladder(cfg, container_env, "fast", lite_skips("no-heap", other={"unit-tests": zig}))
    assert not ladder.ok and ladder.incomplete_only and [g.id for g in ladder.skipped] == ["unit-tests"]
    fb = ladder.feedback()
    skipped_lines = [ln for ln in fb.splitlines() if "SKIPPED" in ln]
    assert len(skipped_lines) == 1 and skipped_lines[0].startswith("unit-tests ")                # no-heap is not one of them
    assert skipped_lines[0].endswith(f": SKIPPED ({zig}) -- the ladder is incomplete")
    assert "lite ladder: no-heap not run here (needs Docker)" in fb.splitlines()
    plain = run_ladder(cfg, container_env, "fast", lite_skips(other={"diff-policy": "no git metadata"}))
    assert not plain.ok and plain.incomplete_only and plain.lite_note() == ""
    assert "lite ladder" not in plain.summary() + plain.feedback()
    with_docker = run_ladder(cfg, container_env, "fast", lite_skips())            # every gate ran: unchanged
    assert with_docker.ok and with_docker.summary() == "compliance:PASS style:PASS comments:PASS diff-policy:PASS build:PASS no-heap:PASS unit-tests:PASS property-tests:PASS"
    assert with_docker.feedback() == f"GATES PASS ({with_docker.summary()})"
    assert with_docker.to_dict()["not_run_here"] == []


HEADER = "/*\n * tinystream v2 -- src/proto/proto.c\n */\n"
FIXED = "int ts_parse(int x)\n{\n    return x;\n}\n"


def test_a_run_with_lite_skips_ends_its_gates_step_without_a_human(cfg: HarnessConfig, container_env: Environment,
                                                                    ticket_file: Path) -> None:
    """The orchestrator seam of `harness run --fake` (FakeRuntime, fake check.sh): no escalation, the
    run's state, summary.json and audit trail name the gates not run."""
    rt = FakeRuntime({"solo": [Turn("fixed", [edit("src/proto/proto.c", HEADER + FIXED)])]})
    fake = FakeCheck(cfg, lite_skips("no-heap"))
    out: list[str] = []
    asked: list[str] = []
    orch = Orchestrator(cfg, rt, container_env, mode="eval", runtime_mode="copilot-user",
                        gate_runner=GateRunner(cfg, container_env, runner=fake),
                        input_fn=lambda q: asked.append(q) or "abort", output=out.append, isatty=lambda: False)
    state = asyncio.run(orch.run(workflow="single-agent", ticket_ref=str(ticket_file)))
    assert state.status == "done", state.data.get("stop_reason")
    assert state.counters.get("escalations", 0) == 0 and asked == [] and state.counters.get("retries", 0) == 0
    assert state.data["steps"]["solve"]["status"] == "done"
    note = "lite ladder: no-heap not run here (needs Docker)"
    assert state.data["last_ladder"]["not_run_here"] == note
    assert any(note in line for line in out)
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert summary["status"] == "done" and summary["not_run_here"] == note
    gate_events = [json.loads(x) for x in (state.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
                   if json.loads(x)["kind"] == "gates"]
    solve = [e for e in gate_events if e["step"] == "solve"]
    assert solve and solve[-1]["ok"] is True and solve[-1]["not_run_here"] == note and note in solve[-1]["summary"]
    trail = json.loads((state.dir / solve[-1]["file"]).read_text(encoding="utf-8"))
    assert trail["not_run_here"] == ["no-heap"] and trail["ok"] is True


@pytest.mark.zig
@pytest.mark.skipif(BASH is None, reason="no bash")
@pytest.mark.skipif(envdetect.in_container(), reason="inside a container check.sh never hands off")
def test_harness_run_fake_with_ts_lite_reaches_the_end_without_a_human(tmp_path: Path, ticket_file: Path,
                                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    """`python -m harness run --fake` (single-agent) with TS_LITE=1 on a copy of this checkout's C tree:
    the real check.sh hands off to lite_gates.py; no-heap is not run here and the run still ends (eval mode)."""
    tree = copy_c_tree(tmp_path / "tree")
    for name in ("check.sh", "protected_paths.txt"):
        shutil.copy2(REPO / "scripts" / name, tree / "scripts" / name)
    (tree / ".gitignore").write_text("build-*/\n.harness/\n", encoding="utf-8", newline="\n")
    subprocess.run(["git", "init", "-q", "-b", "main", str(tree)], check=True)
    subprocess.run(["git", "-C", str(tree), "add", "-A"], check=True)
    subprocess.run(["git", "-C", str(tree), "commit", "-q", "-m", "seed"], check=True)
    proto = (tree / "src" / "proto" / "proto.c").read_text(encoding="utf-8")
    script = {"solo": [Turn("done", [edit("src/proto/proto.c", proto + "/* checked without Docker */\n")])]}
    real = FakeRuntime
    monkeypatch.setattr("harness.fake.FakeRuntime", lambda: real(script))
    for k in [k for k in os.environ if k.startswith(("TS_", "HARNESS_"))]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("TS_LITE", "1")
    rc = cli.main(["--repo", str(tree), "run", "--workflow", "single-agent", "--ticket", str(ticket_file),
                   "--mode", "eval", "--fake", "--run-id", "lite-1"])
    state = RunState.load(tree / ".harness" / "runs", "lite-1")
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    assert rc == 0 and summary["status"] == "done", (summary, state.data.get("last_ladder"))
    assert summary["counters"].get("escalations", 0) == 0 and summary["steps"]["solve"] == "done"
    assert summary["not_run_here"] == "lite ladder: no-heap not run here (needs Docker)"
    assert state.data["branch"] and summary["commit"]


# --------------------------------------------------------------- doctor ----
def report(auth: str, ladder: str) -> DoctorReport:
    return DoctorReport([Check("python", "OK", "3.12"), Check("copilot auth", auth, "")], "facilitator", "", "host",
                        ladder=ladder)


def test_depth_names() -> None:
    assert doctor.depth(report("WARN", "lite")) == "Try (fake model)"
    assert doctor.depth(report("OK", "lite")) == "Try (your Copilot)"
    assert doctor.depth(report("SKIP", "full")) == "Full (fake model)"
    assert doctor.depth(report("OK", "full")) == "Full (your Copilot)"
    assert doctor.depth(report("WARN", "full")) == "Full (fake model)"     # not logged in
    assert doctor.depth(report("SKIP", "lite")) == "Try (fake model)"      # --skip-auth: auth not known
    assert doctor.depth(report("FAIL", "lite")) == "Try (fake model)"


def ok_runner(cmd: list[str], **kw: object) -> subprocess.CompletedProcess:
    return subprocess.CompletedProcess(cmd, 0, "0.16.0\n", "")


def host_env(tmp_path: Path) -> dict[str, str]:
    env = clean_env(COPILOT_HOME=str(tmp_path / "home"), HARNESS_BASH=sys.executable)
    env.pop("COPILOT_CLI_PATH", None)
    return env


def test_doctor_without_docker_reports_the_lite_ladder(cfg: HarnessConfig, tmp_path: Path,
                                                       monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envdetect, "find_copilot_cli", lambda configured="", env=None: None)
    monkeypatch.setattr(envdetect, "docker_usable", lambda timeout=8.0: (False, "Docker installed but not running"))
    r = doctor.run_doctor(cfg, skip_auth=True, env=host_env(tmp_path), runner=ok_runner)
    st = {c.name: c for c in r.checks}
    assert r.ladder == "lite" and st["environment"].status == "WARN"
    assert st["environment"].detail.startswith("Docker installed but not running")
    assert st["gates"].status == "OK" and "zig 0.16.0" in st["gates"].detail
    assert r.to_dict()["ladder"] == "lite" and r.to_dict()["depth"] == "Try (fake model)"
    no_zig = lambda cmd, **kw: subprocess.CompletedProcess(cmd, 1, "", "No module named ziglang")
    r = doctor.run_doctor(cfg, skip_auth=True, env=host_env(tmp_path), runner=no_zig)
    gates = next(c for c in r.checks if c.name == "gates")
    assert gates.status == "FAIL" and "pip install ziglang==0.16.0" in gates.detail
    monkeypatch.setattr(envdetect, "docker_usable", lambda timeout=8.0: (True, ""))
    r = doctor.run_doctor(cfg, skip_auth=True, env=host_env(tmp_path), runner=ok_runner)
    assert r.ladder == "full" and doctor.depth(r) == "Full (fake model)"


def test_doctor_under_ts_lite_0_never_reports_the_lite_ladder(cfg: HarnessConfig, tmp_path: Path,
                                                              monkeypatch: pytest.MonkeyPatch) -> None:
    """TS_LITE=0: check.sh waits for Docker and never runs lite, so the doctor waits as long as its own
    bound for `docker info` and reports the container ladder; without Docker that is a FAIL."""
    monkeypatch.setattr(envdetect, "find_copilot_cli", lambda configured="", env=None: None)
    waits: list[float] = []

    def answers(timeout: float = 8.0) -> tuple[bool, str]:
        waits.append(timeout)
        return True, ""
    monkeypatch.setattr(envdetect, "docker_usable", answers)
    env = dict(host_env(tmp_path), TS_LITE="0")
    r = doctor.run_doctor(cfg, skip_auth=True, env=env, runner=ok_runner)
    assert r.ladder == "full" and doctor.LADDER[r.ladder] == "full (Docker)"
    assert doctor.depth(r) == "Full (fake model)" and waits and waits[-1] > 8.0
    monkeypatch.setattr(envdetect, "docker_usable", lambda timeout=8.0: (False, "Docker installed but not running"))
    r = doctor.run_doctor(cfg, skip_auth=True, env=env, runner=ok_runner)
    st = {c.name: c for c in r.checks}
    assert r.ladder == "full"
    assert st["environment"].status == "FAIL" and "TS_LITE=0" in st["environment"].detail
    assert st["gates"].status == "FAIL" and "Docker" in st["gates"].detail
    waits.clear()                                          # unset: today's 8 s probe
    monkeypatch.setattr(envdetect, "docker_usable", answers)
    doctor.run_doctor(cfg, skip_auth=True, env=host_env(tmp_path), runner=ok_runner)
    assert waits == [8.0]


def test_doctor_cli_hint_names_winget_and_the_install_script(cfg: HarnessConfig, tmp_path: Path,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    monkeypatch.setattr(envdetect, "find_copilot_cli", lambda configured="", env=None: None)
    monkeypatch.setattr(envdetect, "docker_usable", lambda timeout=8.0: (False, "Docker not installed"))
    r = doctor.run_doctor(cfg, skip_auth=True, env=host_env(tmp_path), runner=ok_runner)
    cli_check = next(c for c in r.checks if c.name == "copilot cli")
    assert "winget install GitHub.Copilot (Windows) or the GitHub install script (Linux, macOS)" in cli_check.detail


def test_doctor_depth_flag_prints_the_two_lines_and_exits_0(repo: Path, tmp_path: Path, capsys: pytest.CaptureFixture,
                                                            monkeypatch: pytest.MonkeyPatch) -> None:
    for k in [k for k in os.environ if k.startswith("TS_")]:
        monkeypatch.delenv(k)
    monkeypatch.setenv("COPILOT_HOME", str(tmp_path / "home"))
    monkeypatch.setattr(envdetect, "find_copilot_cli", lambda configured="", env=None: None)   # a FAIL line
    monkeypatch.setattr(envdetect, "docker_usable", lambda timeout=8.0: (False, "Docker not installed"))
    assert cli.main(["--repo", str(repo), "doctor", "--depth", "--skip-auth"]) == 0
    lines = capsys.readouterr().out.splitlines()
    assert "Depth: Try (fake model)" in lines and "Ladder: lite (no Docker)" in lines
    assert lines[-2:] == ["Depth: Try (fake model)", "Ladder: lite (no Docker)"]
    assert cli.main(["--repo", str(repo), "doctor", "--skip-auth"]) == 1        # without --depth: the usual exit
    assert not any(ln.startswith(("Depth:", "Ladder:")) for ln in capsys.readouterr().out.splitlines())
    # with Docker (a report as run_doctor makes it; the real probe would start the container)
    monkeypatch.setattr(doctor, "run_doctor", lambda cfg, skip_auth=False: report("OK", "full"))
    assert cli.main(["--repo", str(repo), "doctor", "--depth", "--skip-auth"]) == 0
    assert capsys.readouterr().out.splitlines()[-2:] == ["Depth: Full (your Copilot)", "Ladder: full (Docker)"]


# ------------------------------------------------- where the setup puts the CLI ----
def no_cli_on_path(monkeypatch: pytest.MonkeyPatch, tmp_path: Path, system: str) -> None:
    """No copilot on PATH, no npm global install, and envdetect sees ``system``."""
    empty = tmp_path / "empty-path"
    empty.mkdir(exist_ok=True)
    monkeypatch.setenv("PATH", str(empty))
    monkeypatch.setattr(envdetect, "NPM_GLOBAL_ROOTS", ())
    monkeypatch.setattr(envdetect, "LOCAL_CLI_DIR", tmp_path / "no-pinned-cli")
    monkeypatch.setattr(envdetect.platform, "system", lambda: system)


def native(path: Path) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(b"MZ native")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)
    return path


def test_the_cli_is_found_in_wingets_links_and_packages_folders(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    no_cli_on_path(monkeypatch, tmp_path, "Windows")
    local = tmp_path / "Local AppData Åsa"
    env = {"LOCALAPPDATA": str(local)}
    assert envdetect.find_copilot_cli("", env) is None
    pkg = native(local / "Microsoft" / "WinGet" / "Packages" / "GitHub.Copilot_Microsoft.Winget.Source_8wekyb3d8bbwe"
                 / "copilot.exe")
    assert envdetect.find_copilot_cli("", env) == str(pkg)
    link = native(local / "Microsoft" / "WinGet" / "Links" / "copilot.exe")
    assert envdetect.find_copilot_cli("", env) == str(link)                    # the link folder first
    pkg.unlink()
    link.unlink()
    nested = native(pkg.parent / "bin" / "copilot.exe")
    assert envdetect.find_copilot_cli("", env) == str(nested)
    shim = local / "Microsoft" / "WinGet" / "Links" / "copilot.exe"            # a script is never the CLI
    shim.write_bytes(b"#!/bin/sh\n")
    assert envdetect.find_copilot_cli("", env) == str(nested)


def test_the_cli_is_found_in_local_bin_on_linux_and_macos(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    for system in ("Linux", "Darwin"):
        no_cli_on_path(monkeypatch, tmp_path, system)
        home = tmp_path / f"home-{system}"
        env = {"HOME": str(home)}
        assert envdetect.find_copilot_cli("", env) is None
        cli_bin = native(home / ".local" / "bin" / "copilot")
        assert envdetect.find_copilot_cli("", env) == str(cli_bin)


def test_copilot_cli_path_and_path_keep_priority(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    no_cli_on_path(monkeypatch, tmp_path, "Linux" if os.name != "nt" else "Windows")
    exe = "copilot.exe" if os.name == "nt" else "copilot"
    local, home = tmp_path / "local", tmp_path / "home"
    native(local / "Microsoft" / "WinGet" / "Links" / exe)
    native(home / ".local" / "bin" / exe)
    env = {"LOCALAPPDATA": str(local), "HOME": str(home)}
    on_path = native(tmp_path / "on-path" / exe)
    monkeypatch.setenv("PATH", str(on_path.parent))
    assert envdetect.find_copilot_cli("", env) == shutil.which("copilot")       # PATH before the setup's places
    assert Path(envdetect.find_copilot_cli("", env) or "").parent == on_path.parent
    pinned = native(tmp_path / "pinned" / exe)
    assert envdetect.find_copilot_cli("", dict(env, COPILOT_CLI_PATH=str(pinned))) == str(pinned)


def test_the_pinned_cli_in_the_repository_comes_first(tmp_path: Path, monkeypatch) -> None:
    import platform
    osname = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}[platform.system()]
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}[platform.machine().lower()]
    exe = "copilot.exe" if platform.system() == "Windows" else "copilot"
    local = tmp_path / "node_modules" / "@github" / f"copilot-{osname}-{arch}" / exe
    local.parent.mkdir(parents=True)
    local.write_bytes(b"MZ binary")
    monkeypatch.setattr(envdetect, "LOCAL_CLI_DIR", tmp_path)
    assert envdetect.find_copilot_cli("", {}) == str(local)
    assert envdetect.find_copilot_cli("", {"COPILOT_CLI_PATH": str(tmp_path / "missing")}) == str(local)
