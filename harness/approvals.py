"""Human in the loop: approvals and escalations.

Modes (``--mode``):

- ``interactive`` (default): ``approval: human`` steps ask on the terminal;
  escalations offer retry / abort.
- ``eval``: every human approval passes automatically but is COUNTED
  (interventions metric); an escalation is counted and ends the run, because
  an unattended batch cannot decide for a human -- except where the caller
  names a safe way on (``unattended``: an unaffordable turn on the escalated
  model goes back to the role's own model).
- ``autonomous``: human approvals pass without counting (variant team-autonomous);
  escalations are handled the same way.

No terminal (stdin not a TTY) in interactive mode: approvals are refused and
escalations abort, so a background run never hangs waiting for input.

A ``FileChannel`` takes the terminal's place in interactive mode (eval and autonomous
never use it): the question goes to ``<dir>/pending.json``, the answer comes back as
``<dir>/answer.json`` -- that is how the console answers a run it started. No answer
within the channel's timeout: the approval is refused, the escalation aborts.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import sys
import time
import uuid
from pathlib import Path
from typing import Any, Callable

from .audit import Audit
from .state import RunState

MODES = ("interactive", "eval", "autonomous")
PENDING_FILE = "pending.json"
ANSWER_FILE = "answer.json"
# A reader that has pending.json open (the console's poll, on Windows) makes replacing or deleting it
# fail for a moment: those calls are retried briefly.
BUSY_TRIES = 10
BUSY_PAUSE_S = 0.05


def _retry(action: Callable[[], None], tries: int = BUSY_TRIES) -> None:
    """Run ``action``; an OSError is retried after a short pause, the last one propagates."""
    for attempt in range(tries):
        try:
            action()
            return
        except OSError:
            if attempt + 1 == tries:
                raise
            time.sleep(BUSY_PAUSE_S)


def _remove(path: Path, tries: int = BUSY_TRIES) -> None:
    """Best-effort delete: retried while the file is busy, never raises (it must not cost an answer)."""
    try:
        _retry(lambda: path.unlink(missing_ok=True), tries)
    except OSError:
        pass


class FileChannel:
    """One question at a time through two files in ``directory``.

    ``pending.json``: {"id", "kind": "approval"|"escalation", "step", "question", "details",
    "options", "asked_at"}, replaced atomically. ``answer.json``: {"id", "answer"}; it counts
    only when its id is the pending question's and the answer is one of the offered options --
    anything else (an older question's answer, garbage, another word) is ignored and the wait
    goes on. Both files are removed when ``ask`` returns, whatever the outcome (best effort: a
    file a reader still has open is retried for about half a second and then left, never raised).
    """

    def __init__(self, directory: Path, timeout_s: float = 900, poll_s: float = 0.5) -> None:
        self.dir = Path(directory)
        self.timeout_s = timeout_s
        self.poll_s = poll_s

    def ask(self, kind: str, step: str, question: str, details: str, options: tuple[str, ...]) -> str | None:
        """The chosen option, or None when nobody answered within ``timeout_s``."""
        self.dir.mkdir(parents=True, exist_ok=True)
        pending, answer = self.dir / PENDING_FILE, self.dir / ANSWER_FILE
        ask_id = str(uuid.uuid4())
        question_doc = {
            "id": ask_id, "kind": kind, "step": step, "question": question, "details": details,
            "options": list(options), "asked_at": _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds"),
        }
        tmp = pending.with_name(PENDING_FILE + ".tmp")
        try:
            _remove(answer)                                 # an answer that is already here is not to this question
            tmp.write_text(json.dumps(question_doc, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
            _retry(lambda: os.replace(tmp, pending))        # the console may be reading the last question
            deadline = time.monotonic() + self.timeout_s
            while True:
                chosen = self._answer(answer, ask_id, options)
                if chosen is not None:
                    return chosen
                left = deadline - time.monotonic()
                if left <= 0:
                    return None
                time.sleep(min(self.poll_s, left))
        finally:                                            # cleanup never replaces the result (or the error)
            _remove(tmp, tries=1)
            _remove(pending)
            _remove(answer)

    @staticmethod
    def _answer(path: Path, ask_id: str, options: tuple[str, ...]) -> str | None:
        try:
            data = json.loads(path.read_text(encoding="utf-8"))
        except (OSError, ValueError):                       # not there yet, being replaced, half written
            return None
        if not isinstance(data, dict) or data.get("id") != ask_id:
            return None
        chosen = data.get("answer")
        return chosen if isinstance(chosen, str) and chosen in options else None


class Approver:
    def __init__(self, mode: str, state: RunState, audit: Audit,
                 input_fn: Callable[[str], str] = input, output: Callable[[str], None] = print,
                 isatty: Callable[[], bool] | None = None, channel: FileChannel | None = None) -> None:
        if mode not in MODES:
            raise ValueError(f"mode must be one of {MODES}")
        self.mode = mode
        self.state = state
        self.audit = audit
        self.input = input_fn
        self.output = output
        self.isatty = isatty or (lambda: sys.stdin is not None and sys.stdin.isatty())
        self.channel = channel          # with one, interactive questions go there and the terminal is not consulted

    def approve(self, step: str, question: str, details: str = "") -> bool:
        if self.mode == "eval":
            self.state.count("approvals_counted")
            self._record(step, question, True, "eval-auto")
            return True
        if self.mode == "autonomous":
            self.state.count("approvals_auto")
            self._record(step, question, True, "autonomous")
            return True
        self.state.count("approvals_asked")
        if self.channel is not None:
            answer = self._ask_console("approval", step, question, details, ("approve", "reject"))
            if answer is None:
                self._record(step, question, False, "console-timeout")
                self.output(f"[harness] approval needed at {step}, no answer from the console: refused ({question})")
                return False
            ok = answer == "approve"
            self._record(step, question, ok, "console")
            return ok
        if not self.isatty():
            self._record(step, question, False, "no-terminal")
            self.output(f"[harness] approval needed at {step} but no terminal: refused ({question})")
            return False
        if details:
            self.output(details)
        while True:
            answer = self.input(f"[harness] {step}: {question} [y/n] ").strip().lower()
            if answer in ("y", "yes", "n", "no"):
                ok = answer.startswith("y")
                self._record(step, question, ok, "human")
                return ok

    def escalate(self, step: str, reason: str, options: tuple[str, ...] = ("retry", "abort"),
                 unattended: str = "abort") -> str:
        """Ask a human to choose one of ``options``; counted always. Unattended (eval, autonomous,
        no terminal) the answer is ``unattended``: abort, unless the caller has a safe way on
        (an unaffordable pricier model: back to the role's own model). With a console channel
        (interactive mode) the console is asked instead of the terminal; no answer in time is
        ``abort`` whatever ``unattended`` says: a silent console is not a safe way on."""
        self.state.count("escalations")
        if self.mode == "interactive" and self.channel is not None:
            answer = self._ask_console("escalation", step, reason, "", options)
            if answer is None:                              # a console that went silent is not a safe way on
                self.audit.event("escalation", step=step, reason=reason, choice="abort", by="console-timeout")
                self.output(f"[harness] escalation at {step}: {reason} -> abort (no answer from the console)")
                return "abort"
            self.audit.event("escalation", step=step, reason=reason, choice=answer, by="console")
            return answer
        if self.mode in ("eval", "autonomous") or not self.isatty():
            choice = unattended
            self.audit.event("escalation", step=step, reason=reason, choice=choice, by=self.mode)
            self.output(f"[harness] escalation at {step}: {reason} -> {choice} ({self.mode})")
            return choice
        self.output(f"[harness] ESCALATION at {step}: {reason}")
        while True:
            answer = self.input(f"[harness] choose {'/'.join(options)}: ").strip().lower()
            if answer in options:
                self.audit.event("escalation", step=step, reason=reason, choice=answer, by="human")
                return answer

    def _ask_console(self, kind: str, step: str, question: str, details: str,
                     options: tuple[str, ...]) -> str | None:
        assert self.channel is not None
        self.output(f"[harness] waiting for the console at {step}: {' '.join(question.split())}")
        return self.channel.ask(kind, step, question, details, options)

    def _record(self, step: str, question: str, approved: bool, by: str) -> None:
        entry: dict[str, Any] = {"step": step, "question": question, "approved": approved, "by": by}
        self.state.data.setdefault("approvals", []).append(entry)
        self.audit.event("approval", **entry)
        self.state.save()
