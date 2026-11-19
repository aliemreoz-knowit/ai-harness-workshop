"""The participant's setup in personal mode (``GET /api/setup``): what ``python -m harness doctor --depth`` says.

The doctor starts the Copilot CLI to read the login (up to 90 s a step), so it runs in a thread of its own and never
holds a request: the first ``state()`` starts it and answers at once with ``state: "checking"``, and the page asks again
every few seconds. A result is kept for ``CACHE_S`` (10 minutes); a ``state()`` after that starts a new check and
answers ``checking`` with the last result meanwhile. ``again()`` ("Check again") starts one now. One check at a time.

The doctor is ``<harness_argv> doctor --depth``, never with ``--skip-auth`` (the depth says "your Copilot" only when the
login was checked), started in the checkout with the environment a run gets (``procs.run_env``: ``COPILOT_*`` passes,
no secret does), in a process group of its own that is killed after ``timeout_s`` (above the doctor's own timeouts).
Of what it prints only the ``Depth:`` and ``Ladder:`` lines and the status of its ``copilot auth`` check are read: the
page gets sentences of this module's, never the doctor's own text (host paths, a login name).

The answer: ``{"state", "depth", "ladder", "reason", "checked_at"}``. ``state`` is ``checking``, ``ready`` or
``error``; ``depth`` one of the doctor's four (``Try (fake model)``, ``Try (your Copilot)``, ``Full (fake model)``,
``Full (your Copilot)``) or ``""``; ``ladder`` ``full``, ``lite`` or ``""``; ``ladder_why`` (lite only) ``ts-lite`` (the
console was started with ``TS_LITE=1``), ``no-docker`` (the setup recorded it) or ``no-image`` (the page words the line
from it); ``reason`` what it means for the
participant; ``checked_at`` the UTC time of the result (``None`` before the first one).
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from console import ladder as ladders
from console import procs
from console.config import ConsoleConfig

__all__ = ["CACHE_S", "DEPTHS", "TIMEOUT_S", "SetupCheck", "read_doctor"]

log = logging.getLogger(__name__)

CACHE_S = 600.0  # how long a result is kept
TIMEOUT_S = 660.0  # the doctor's own worst case: its Copilot probe (3 x 90 s + 30 s), docker info, the gate probe (300 s)
DEPTHS = ("Try (fake model)", "Try (your Copilot)", "Full (fake model)", "Full (your Copilot)")
_DEPTH = re.compile(r"^Depth: (.*?)\s*$", re.M)
_LADDER = re.compile(r"^Ladder: (full|lite|zig) \(", re.M)
_AUTH = re.compile(r"^\s*(OK|WARN|FAIL|SKIP)\s+copilot auth\s", re.M)
_KILL_WAIT_S = 5.0

_COPILOT = {
    "OK": "Your Copilot login works: agent runs use it and count against today's credits.",
    "WARN": "You are not logged in to Copilot here: agent runs use the fake model (no credits). Log in with "
            "copilot /login, then check again.",
    "FAIL": "The Copilot CLI or its SDK does not work here: agent runs use the fake model (no credits). "
            "python -m harness doctor in your checkout says why.",
}  # fmt: skip
_COPILOT_UNKNOWN = "Your Copilot login could not be checked: agent runs use the fake model (no credits)."
_LADDERS = {
    "full": "The gates run in the container ladder (TS_LITE=0).",
    "lite": "The gates run on this laptop with zig cc: build, misra, no-heap, tests, UBSan and the rest.",
}
_LITE = "The gates run on this laptop with zig cc: build, misra, no-heap, tests, UBSan and the rest."
_LITE_WHY = {  # a lite console (``console.ladder``): the same sentence whatever the why; nobody needs Docker
    ladders.TS_LITE: _LITE,
    ladders.NO_DOCKER: _LITE,
    ladders.NO_IMAGE: _LITE,
}  # fmt: skip
_NO_ANSWER = "The setup check gave no answer: python -m harness doctor in your checkout says why."


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def read_doctor(output: str) -> dict[str, str] | None:
    """``{depth, ladder, reason}`` from what ``doctor --depth`` printed; ``None`` without a known ``Depth:`` line."""
    depths = _DEPTH.findall(output)
    depth = depths[-1] if depths else ""
    if depth not in DEPTHS:
        return None
    ladders = _LADDER.findall(output)
    ladder = ladders[-1] if ladders else ("full" if depth.startswith("Full") else "lite")
    ladder = "lite" if ladder == "zig" else ladder  # the doctor says "zig (this laptop)"
    if depth.endswith("(your Copilot)"):
        copilot = _COPILOT["OK"]
    else:  # not logged in (WARN), no CLI or SDK (FAIL); else not checked: the doctor's own words stay out of it
        auth = _AUTH.findall(output)
        status = auth[-1] if auth else ""
        copilot = _COPILOT[status] if status in ("WARN", "FAIL") else _COPILOT_UNKNOWN
    return {"depth": depth, "ladder": ladder, "reason": f"{copilot} {_LADDERS[ladder]}"}


def _run_doctor(argv: list[str], cwd: Path, env: dict[str, str], timeout: float) -> str:
    """Run the doctor and return what it printed (stdout and stderr). ``TimeoutError`` after ``timeout`` seconds, once
    its process tree is killed (the Copilot CLI it started too)."""
    if os.name == "nt":
        detach: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        detach = {"start_new_session": True}
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, **detach)  # fmt: skip
    try:
        out, _ = proc.communicate(timeout=timeout)
    except subprocess.TimeoutExpired:
        procs.kill_tree(proc)
        if proc.poll() is None:
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_KILL_WAIT_S)
        raise TimeoutError(f"the doctor did not finish in {timeout:g} s") from None
    except BaseException:
        proc.kill()
        raise
    return out.decode("utf-8", errors="replace")


class SetupCheck:
    """The doctor's answer for one personal console, checked in the background and kept for ``CACHE_S``.

    ``run(argv, cwd, env, timeout) -> output`` runs the doctor (tests pass their own); ``clock`` says how old a result
    is. Safe to use from many threads.
    """

    def __init__(
        self,
        cfg: ConsoleConfig,
        *,
        run: Callable[[list[str], Path, dict[str, str], float], str] = _run_doctor,
        clock: Callable[[], float] = time.monotonic,
        timeout_s: float = TIMEOUT_S,
    ) -> None:
        self.cfg = cfg
        self._run = run
        self._clock = clock
        self._timeout = timeout_s
        self._lock = threading.Lock()
        self._result: dict[str, Any] = {"state": "ready", "depth": "", "ladder": "", "ladder_why": "", "reason": "", "checked_at": None}
        self._at: float | None = None  # the clock when the result came; None before the first one
        self._checking = False

    def state(self) -> dict[str, Any]:
        """The setup as the page shows it; a check starts when there is no result, or it is older than ``CACHE_S``."""
        with self._lock:
            if not self._checking and (self._at is None or self._clock() - self._at >= CACHE_S):
                self._begin()
            return self._answer()

    def again(self) -> dict[str, Any]:
        """Check now ("Check again"), unless a check is under way already."""
        with self._lock:
            if not self._checking:
                self._begin()
            return self._answer()

    def _answer(self) -> dict[str, Any]:
        return {**self._result, "state": "checking"} if self._checking else dict(self._result)

    def _begin(self) -> None:
        """Start a check (the lock is held)."""
        self._checking = True
        thread = threading.Thread(target=self._check, name="setup-check", daemon=True)
        try:
            thread.start()
        except BaseException:
            self._checking = False
            raise

    def _check(self) -> None:
        argv = [*self.cfg.harness_argv, "doctor", "--depth"]
        # a lite console runs the doctor without Docker (and TS_LITE=1): it would start the image build itself; a full
        # one passes its own TS_LITE (0 on the stage) on
        env = procs.run_env(os.environ, {"PYTHONUTF8": "1", "HARNESS_RUNTIME": self.cfg.runtime,
                                         **ladders.env(self.cfg.ladder)})  # fmt: skip
        try:
            found = read_doctor(self._run(argv, self.cfg.source_repo, env, self._timeout))
        except TimeoutError:
            log.warning("the setup check (harness doctor) did not finish in %s s: stopped", f"{self._timeout:g}")
            found = {"state": "error", "reason": f"The setup check did not finish in {self._timeout:g} s: "
                                                 "python -m harness doctor in your checkout says why."}
        except Exception:
            log.exception("the setup check (harness doctor) could not run")
            found = {"state": "error", "reason": _NO_ANSWER}
        if found is None:
            log.warning("the setup check (harness doctor) printed no Depth line")
            found = {"state": "error", "reason": _NO_ANSWER}
        result = {"state": "ready", "depth": "", "ladder": "", "ladder_why": "", **found, "checked_at": _now()}
        if self.cfg.ladder == "lite" and result["depth"]:  # the console decided: lite, whatever the doctor saw
            if result["depth"].startswith("Full"):
                result["depth"] = "Try" + result["depth"][4:]
            copilot = result["reason"].removesuffix(" " + _LADDERS.get(result["ladder"], _LADDERS["lite"])) if result["ladder"] else ""
            why = self.cfg.ladder_why if self.cfg.ladder_why in _LITE_WHY else ladders.NO_IMAGE
            result.update(ladder="lite", ladder_why=why, reason=f"{copilot} {_LITE_WHY[why]}".strip())
        with self._lock:
            self._result, self._at, self._checking = result, self._clock(), False
