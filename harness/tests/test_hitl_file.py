"""Approvals and escalations answered through files (FileChannel), no terminal.

A console writes ``answer.json`` next to the harness's ``pending.json``; these tests play the console
with a thread. Poll intervals and timeouts are tiny so the suite stays fast.
"""

import json
import os
import sys
import threading
import time
from pathlib import Path
from typing import Callable

import pytest

from harness import approvals
from harness.approvals import Approver, FileChannel
from harness.audit import Audit
from harness.state import RunState

OPTIONS = ("approve", "reject")


class Console(threading.Thread):
    """Plays the console: calls ``reply(pending)`` once for every question that appears in ``directory``."""

    def __init__(self, directory: Path, reply: Callable[[dict], None]) -> None:
        super().__init__(daemon=True)
        self.directory = directory
        self.reply = reply
        self.asked: list[dict] = []
        self.error: BaseException | None = None
        self._stop_now = threading.Event()

    @property
    def pending(self) -> dict:
        assert self.asked, "no question was asked"
        return self.asked[0]

    def run(self) -> None:
        path = self.directory / "pending.json"
        deadline = time.monotonic() + 60
        try:
            while not self._stop_now.is_set() and time.monotonic() < deadline:
                try:
                    question = json.loads(path.read_text(encoding="utf-8"))
                except (OSError, ValueError):
                    question = None
                if question and question["id"] not in [q["id"] for q in self.asked]:
                    self.asked.append(question)
                    self.reply(question)
                time.sleep(0.005)
        except BaseException as exc:  # noqa: BLE001 - re-raised by finish()
            self.error = exc

    def finish(self) -> None:
        self._stop_now.set()
        self.join(10)
        assert not self.is_alive()
        if self.error is not None:
            raise self.error


def answer(directory: Path, ask_id: str, value: object) -> None:
    tmp = directory / "answer.tmp"
    tmp.write_text(json.dumps({"id": ask_id, "answer": value}), encoding="utf-8")
    os.replace(tmp, directory / "answer.json")


# ------------------------------------------------------------------- FileChannel ----
def test_ask_returns_the_answer_and_removes_both_files(tmp_path: Path) -> None:
    channel = FileChannel(tmp_path, timeout_s=10, poll_s=0.01)
    console = Console(tmp_path, lambda p: answer(tmp_path, p["id"], "approve"))
    console.start()
    assert channel.ask("approval", "plan", "approve the plan?", "1. fix it", OPTIONS) == "approve"
    console.finish()
    p = console.pending
    assert set(p) == {"id", "kind", "step", "question", "details", "options", "asked_at"}
    assert (p["kind"], p["step"], p["question"], p["details"], p["options"]) == (
        "approval", "plan", "approve the plan?", "1. fix it", ["approve", "reject"])
    assert p["id"] and p["asked_at"]
    assert not (tmp_path / "pending.json").exists() and not (tmp_path / "answer.json").exists()


def test_every_question_gets_its_own_id(tmp_path: Path) -> None:
    ids = []
    for _ in range(2):
        console = Console(tmp_path, lambda p: (ids.append(p["id"]), answer(tmp_path, p["id"], "reject")))
        console.start()
        assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "reject"
        console.finish()
    assert len(set(ids)) == 2


def test_a_wrong_id_an_unoffered_answer_and_garbage_are_ignored(tmp_path: Path) -> None:
    def reply(p: dict) -> None:
        settle = 0.15       # many polls of 10 ms: a wrongly accepted file would have been returned by now
        answer(tmp_path, "not-the-id", "approve")
        time.sleep(settle)
        answer(tmp_path, p["id"], "maybe")                      # the right id, not an offered option
        time.sleep(settle)
        answer(tmp_path, p["id"], ["approve"])                  # not even a string
        time.sleep(settle)
        (tmp_path / "answer.json").write_text("{not json", encoding="utf-8")
        time.sleep(settle)
        answer(tmp_path, p["id"], "reject")

    console = Console(tmp_path, reply)
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "reject"
    console.finish()


def test_a_stale_answer_from_an_earlier_question_is_not_taken(tmp_path: Path) -> None:
    answer(tmp_path, "an-earlier-question", "approve")
    channel = FileChannel(tmp_path, timeout_s=0.2, poll_s=0.01)
    assert channel.ask("approval", "s", "q", "", OPTIONS) is None


def test_timeout_returns_none_and_leaves_no_question_behind(tmp_path: Path) -> None:
    channel = FileChannel(tmp_path, timeout_s=0.2, poll_s=0.02)
    t0 = time.monotonic()
    assert channel.ask("escalation", "implement", "stuck", "", ("retry", "abort")) is None
    assert time.monotonic() - t0 >= 0.2
    assert not (tmp_path / "pending.json").exists() and not (tmp_path / "answer.json").exists()


def test_the_directory_is_created_when_missing(tmp_path: Path) -> None:
    directory = tmp_path / "runs" / "r-1" / "hitl"
    assert FileChannel(directory, timeout_s=0.05, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) is None
    assert directory.is_dir()


def test_pending_json_is_replaced_atomically(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    replaced: list[tuple[str, str]] = []
    real = os.replace

    def spy(src: object, dst: object) -> None:
        replaced.append((Path(str(src)).name, Path(str(dst)).name))
        real(src, dst)

    monkeypatch.setattr(approvals.os, "replace", spy)
    FileChannel(tmp_path, timeout_s=0.05, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS)
    (src, dst), = replaced
    assert dst == "pending.json" and src != dst              # written under another name, then moved in
    assert list(tmp_path.iterdir()) == []                    # no temp file left


# ---------------------------------------------------------------------- Approver ----
def approver(tmp_path: Path, mode: str = "interactive", channel: FileChannel | None = None,
             **kw: object) -> tuple[Approver, RunState, list[str]]:
    state = RunState.create(tmp_path / "runs", "r-1", "wf", ["a"])
    out: list[str] = []
    kw.setdefault("isatty", lambda: pytest.fail("a run with a channel never asks the terminal"))
    kw.setdefault("input_fn", lambda q: pytest.fail("a run with a channel never reads stdin"))
    return Approver(mode, state, Audit(state.dir), output=out.append, channel=channel, **kw), state, out


def audit_events(state: RunState, kind: str) -> list[dict]:
    lines = (state.dir / "events.jsonl").read_text(encoding="utf-8").splitlines()
    return [e for e in map(json.loads, lines) if e["kind"] == kind]


@pytest.mark.parametrize("given,expected", [("approve", True), ("reject", False)])
def test_an_interactive_approval_asks_the_console(tmp_path: Path, given: str, expected: bool) -> None:
    channel = FileChannel(tmp_path / "hitl", timeout_s=10, poll_s=0.01)
    ap, state, _ = approver(tmp_path, channel=channel)
    console = Console(tmp_path / "hitl", lambda p: answer(tmp_path / "hitl", p["id"], given))
    console.start()
    assert ap.approve("merge-approval", "commit the change?", "digest") is expected
    console.finish()
    assert console.pending["kind"] == "approval" and console.pending["options"] == ["approve", "reject"]
    assert console.pending["details"] == "digest" and console.pending["step"] == "merge-approval"
    (entry,) = state.data["approvals"]
    assert entry == {"step": "merge-approval", "question": "commit the change?", "approved": expected,
                     "by": "console"}
    assert state.counters["approvals_asked"] == 1
    assert [e["by"] for e in audit_events(state, "approval")] == ["console"]


def test_an_approval_nobody_answers_is_refused(tmp_path: Path) -> None:
    ap, state, out = approver(tmp_path, channel=FileChannel(tmp_path / "hitl", timeout_s=0.1, poll_s=0.01))
    assert ap.approve("plan", "approve the plan?") is False
    (entry,) = state.data["approvals"]
    assert entry["approved"] is False and entry["by"] == "console-timeout"
    assert any("refused" in line and "plan" in line for line in out)


def test_an_escalation_asks_the_given_options(tmp_path: Path) -> None:
    channel = FileChannel(tmp_path / "hitl", timeout_s=10, poll_s=0.01)
    ap, state, _ = approver(tmp_path, channel=channel)
    console = Console(tmp_path / "hitl", lambda p: answer(tmp_path / "hitl", p["id"], "continue"))
    console.start()
    assert ap.escalate("solve", "run credit cap reached", ("continue", "abort")) == "continue"
    console.finish()
    p = console.pending
    assert (p["kind"], p["step"], p["question"], p["options"]) == (
        "escalation", "solve", "run credit cap reached", ["continue", "abort"])
    assert state.counters["escalations"] == 1
    (event,) = audit_events(state, "escalation")
    assert event["choice"] == "continue" and event["by"] == "console"


def test_an_escalation_nobody_answers_aborts(tmp_path: Path) -> None:
    ap, state, _ = approver(tmp_path, channel=FileChannel(tmp_path / "hitl", timeout_s=0.1, poll_s=0.01))
    # "unattended" names a safe way on for eval and autonomous runs; a console that went silent is not that
    assert ap.escalate("solve", "stuck", ("retry", "abort"), unattended="retry") == "abort"
    (event,) = audit_events(state, "escalation")
    assert event["choice"] == "abort" and event["by"] == "console-timeout"


class Exploding:
    def ask(self, *a: object, **k: object) -> str:
        raise AssertionError("eval and autonomous runs never ask the console")


@pytest.mark.parametrize("mode,by", [("eval", "eval-auto"), ("autonomous", "autonomous")])
def test_eval_and_autonomous_ignore_the_channel(tmp_path: Path, mode: str, by: str) -> None:
    ap, state, _ = approver(tmp_path, mode=mode, channel=Exploding())  # type: ignore[arg-type]
    assert ap.approve("plan", "approve?") is True
    assert state.data["approvals"][0]["by"] == by
    assert ap.escalate("solve", "stuck", ("retry", "abort"), unattended="retry") == "retry"
    assert audit_events(state, "escalation")[0]["by"] == mode


def test_without_a_channel_nothing_changes(tmp_path: Path) -> None:
    ap, state, out = approver(tmp_path, isatty=lambda: False)
    assert ap.approve("plan", "approve?") is False
    assert state.data["approvals"][0]["by"] == "no-terminal"
    assert ap.escalate("solve", "stuck") == "abort"
    ap2, state2, _ = approver(tmp_path / "other", isatty=lambda: True, input_fn=lambda q: "y")
    assert ap2.approve("plan", "approve?") is True and state2.data["approvals"][0]["by"] == "human"


# ------------------------------------------- cleanup never costs the answer (Windows readers) ----
def flaky_unlink(monkeypatch: pytest.MonkeyPatch, name: str, failures: int | None) -> dict:
    """Path.unlink of ``name`` raises PermissionError (WinError 32 style) ``failures`` times (None: always)."""
    state = {"calls": 0, "left": failures}
    real = Path.unlink

    def unlink(self: Path, missing_ok: bool = False) -> None:
        if self.name == name:
            state["calls"] += 1
            if state["left"] is None or state["left"] > 0:
                if state["left"] is not None:
                    state["left"] -= 1
                raise PermissionError(32, "The process cannot access the file because it is being used")
        real(self, missing_ok=missing_ok)

    monkeypatch.setattr(Path, "unlink", unlink)
    monkeypatch.setattr(approvals, "BUSY_PAUSE_S", 0.001)
    return state


def test_a_busy_pending_file_is_retried_and_the_answer_survives(tmp_path: Path,
                                                                monkeypatch: pytest.MonkeyPatch) -> None:
    state = flaky_unlink(monkeypatch, "pending.json", failures=3)
    console = Console(tmp_path, lambda p: answer(tmp_path, p["id"], "approve"))
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "approve"
    console.finish()
    assert state["left"] == 0 and state["calls"] == 4            # three refusals, then it went
    assert not (tmp_path / "pending.json").exists() and not (tmp_path / "answer.json").exists()


def test_a_pending_file_that_stays_busy_does_not_mask_the_answer_or_the_timeout(
        tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> None:
    state = flaky_unlink(monkeypatch, "pending.json", failures=None)
    console = Console(tmp_path, lambda p: answer(tmp_path, p["id"], "reject"))
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "reject"
    console.finish()
    assert state["calls"] == approvals.BUSY_TRIES                # bounded, then given up on
    assert not (tmp_path / "answer.json").exists()                  # the rest of the cleanup still ran
    other = tmp_path / "other"
    assert FileChannel(other, timeout_s=0.05, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) is None


@pytest.mark.skipif(sys.platform != "win32", reason="an open file cannot be deleted on Windows only")
def test_a_console_still_holding_pending_json_open_delays_the_cleanup_not_the_answer(tmp_path: Path) -> None:
    held: list = []

    def reply(p: dict) -> None:
        held.append(open(tmp_path / "pending.json", "rb"))           # the run queue's poll, mid-read
        answer(tmp_path, p["id"], "approve")
        threading.Timer(0.2, held[0].close).start()

    console = Console(tmp_path, reply)
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "approve"
    console.finish()
    time.sleep(0.3)
    assert not (tmp_path / "pending.json").exists() and not (tmp_path / "answer.json").exists()


# ----------------------------------------------------- a pending question shows in the log ----
def test_the_log_shows_the_question_the_console_is_asked(tmp_path: Path) -> None:
    channel = FileChannel(tmp_path / "hitl", timeout_s=10, poll_s=0.01)
    ap, _, out = approver(tmp_path, channel=channel)
    console = Console(tmp_path / "hitl", lambda p: answer(tmp_path / "hitl", p["id"], p["options"][-1]))
    console.start()
    ap.approve("merge-approval", "commit the change?", "digest")
    ap.escalate("solve", "ladder failed:\n  unit-tests unit\n  sanitizers asan", ("retry", "abort"))
    console.finish()
    assert out == ["[harness] waiting for the console at merge-approval: commit the change?",
                   "[harness] waiting for the console at solve: ladder failed: unit-tests unit sanitizers asan"]


# -------------------------------- writing the question also waits out a reader holding the old file ----
def flaky_replace(monkeypatch: pytest.MonkeyPatch, failures: int | None) -> dict:
    """os.replace onto pending.json raises PermissionError ``failures`` times (None: always)."""
    state = {"calls": 0, "left": failures}
    real = os.replace

    def replace(src: object, dst: object) -> None:
        if Path(str(dst)).name == "pending.json":
            state["calls"] += 1
            if state["left"] is None or state["left"] > 0:
                if state["left"] is not None:
                    state["left"] -= 1
                raise PermissionError(5, "Access is denied")
        real(src, dst)

    monkeypatch.setattr(approvals.os, "replace", replace)
    monkeypatch.setattr(approvals, "BUSY_PAUSE_S", 0.001)
    return state


def test_a_busy_pending_file_is_retried_when_the_question_is_written(tmp_path: Path,
                                                                     monkeypatch: pytest.MonkeyPatch) -> None:
    state = flaky_replace(monkeypatch, failures=3)
    console = Console(tmp_path, lambda p: answer(tmp_path, p["id"], "approve"))
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "approve"
    console.finish()
    assert state["calls"] == 4 and list(tmp_path.iterdir()) == []


def test_a_pending_file_that_never_frees_up_fails_as_before_and_cleans_up(tmp_path: Path,
                                                                          monkeypatch: pytest.MonkeyPatch) -> None:
    state = flaky_replace(monkeypatch, failures=None)
    with pytest.raises(PermissionError):
        FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS)
    assert state["calls"] == approvals.BUSY_TRIES                   # bounded
    assert list(tmp_path.iterdir()) == []                           # the temp file is not left behind


@pytest.mark.skipif(sys.platform != "win32", reason="an open file cannot be replaced on Windows only")
def test_an_old_pending_json_held_open_by_a_reader_delays_the_new_question_not_the_run(tmp_path: Path) -> None:
    stale = tmp_path / "pending.json"
    stale.write_text(json.dumps({"id": "stale", "options": ["approve"]}), encoding="utf-8")
    reader = open(stale, "rb")                                      # the run queue's poll, mid-read
    threading.Timer(0.1, reader.close).start()

    def reply(p: dict) -> None:
        if p["id"] != "stale":
            answer(tmp_path, p["id"], "approve")

    console = Console(tmp_path, reply)
    console.start()
    assert FileChannel(tmp_path, timeout_s=10, poll_s=0.01).ask("approval", "s", "q", "", OPTIONS) == "approve"
    console.finish()
    assert console.asked[-1]["id"] != "stale" and console.asked[-1]["question"] == "q"
