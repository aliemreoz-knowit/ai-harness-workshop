"""Runs: a queue of harness runs on lanes, a live watcher per run, approvals, stop, finalize and a console restart.

A unit asks for a run with ``Runner.submit``. A ``workflow`` run goes to the ``agent`` lane, a ``gates`` or ``warm``
run (``harness gates --profile fast --json``) to the ``gates`` lane; a lane runs at most ``cfg.lanes[lane]`` runs at
a time, the others wait in the order they came. A unit has at most one run queued or going. A workflow run needs at
least 1 credit left, when it is asked for and again when it starts, and gets ``cap = min(run_cap, left)`` as it
starts (a top-up while it waits counts); gates and warm runs spend no credits. In personal mode a workflow run may be
``fake`` (the harness's scripted fake model, ``--fake``): it needs no credits left, its cap is ``run_cap``, and it is
charged 0 credits whatever its files count.

A worker starts the run in the unit worktree (``command``: on ``cfg.runtime``, ``facilitator`` in the room,
``copilot-user`` in personal mode) with ``procs.run_env`` and the values the console sets itself (``HARNESS_RUNTIME`` =
``cfg.runtime``, ``HARNESS_MAX_CREDITS_RUN`` = the cap, ``HARNESS_HITL_DIR``, ``HARNESS_RUN_ID``, ``PYTHONUTF8``; in
personal mode what the ladder decided at start adds: ``TS_LITE=1`` and Docker hidden in lite, the console's own
``TS_LITE`` in full), and holds the unit lock (``UnitStore.lock``) until the run is
finalized. With the unit locked and before the process starts, the run's record gets ``weakened``: the guardrails the
unit's harness files have given up (``structure.weakening``), as they are when the run starts, not when it was asked
for.

Files under ``<data>/runs/``::

    <id>.json                 the run record (``RunStore``: one file per run, written by atomic replace)
    <id>/hitl/                pending.json / answer.json of the harness's FileChannel. Outside the worktree, so an
                              agent can never answer its own approval
    <id>/console.log          the run's stdout and stderr
    <id>/console-diff.patch   what the run left uncommitted, saved at finalize

The harness keeps the run's audit trail in ``<worktree>/.harness/runs/<id>/`` (``run_dir``). That folder is in the
worktree, where an agent can write: its files are read only as plain files (``worktree_ops.open_plain``: no link, no
hard link), with a size limit, and only fields the console knows are kept from ``summary.json``, type-checked.

A watcher thread per active run, every ``tick_s``: new audit events (at most 4 MB a tick) become readable lines on
``run:<id>`` (SSE id = the event's ``seq``), and the credits of its ``turn`` events are counted as they come; a
repeated tool call sets ``loop_flag`` and publishes ``loop``; ``pending.json`` appearing makes the run ``waiting``
and publishes ``approval``, its going makes it ``running`` again; the end of the process finalizes the run. The
watcher is the only thing that finalizes a started run: ``stop`` kills the process (and the run's gate containers)
and waits for it. ``shutdown`` leaves running processes alone; the next ``start`` watches those that still run and
finalizes as ``stopped`` those whose process is gone (or whose start time was never known).

Finalize, once per run, never over a status already settled: the process tree and the gate containers are swept;
the status (a stop or a restart: ``stopped``; else summary.json, for gates runs the exit code); the credits; then
``worktree_ops.wrap_up`` (branch, commit, a best-effort patch, ``unit/NN`` back on the commit it had at the start,
and the restore to it, always). A unit whose worktree could not be put back is held as ``needs-reset:<run id>``
(``UnitStore.hold_for_reset``) before the final record is written; otherwise it is unlocked after the credits are
charged. Last a final ``end`` line and the record are published. A charge that fails (``units.json`` refused for longer
than its retries) leaves the run its status and result: the record says ``charged: false``, and the charge is made at
the unit's next launch, before its budget is looked at, or at a restart. A finalize that fails half way still settles:
the status, summary and result it had worked out (an ``error`` status when it failed before the result; never over a
settled one), at least the credits the watcher saw (cap + ``OVERSHOOT`` if it saw none), the unit freed or held.

Credits treat the run's files as hostile: a workflow run is charged the most of summary.json's ``credits`` and the
sum of its ``turn`` events, taking per seq the most that any line of the trail, or the watcher live, gave (a line
written ahead with a coming seq cannot lower the real one). That total is believable when it is finite and at most
the run's ceiling: its cap, plus ``extension`` for each continue the facilitator gave it at a credit cap
(``extensions``: the harness then raises its own cap by its facilitator session cap), plus ``OVERSHOOT`` (the harness
looks at its cap between model calls, so a run ends at most one call past it). A believable total is charged in full.
Anything else is an anomaly: summary.json or the trail refused (not a plain file, too large, not JSON), a credit
amount that is not a finite number of 0 or more, a seq out of range, a total past the ceiling. The run is then charged
its ceiling, the most an honest run can spend (so never less than any believable part of its count), and its record
says ``accounting: "capped"``. No number from a run file can make a finalize or a relay fail.

A workflow run may take another unit's harness structure (``overlay_from="unit-04"``, a handover run). Before the run
starts: ``handover.collect`` reads that unit's overlay files from the tip of its branch ``unit/NN`` (one full commit id,
``overlay_source_commit`` on the record), never from its worktree, which may hold a run of its own or an agent's
half-done edits; only files ``cfg.overlay`` and ``cfg.editable`` both allow, as blobs of that commit. ``validate.check``
runs on each; a file that fails, or no file at all, refuses the run and nothing has changed. Then, with the unit lock
held and under the running unit's worktree lock, ``files.commit_files`` makes the commit ``console: harness of unit-04``
(trailer ``Console-Unit``) with ``unit/NN`` as its parent, from bytes (no file is written by path), ``run/<id>`` is
made at it and checked out, and the harness starts there; ``overlay_commit`` and ``overlay_skipped`` (what was left
out, listed) are on the record. The run's diff and patch start at the overlay commit, so they hold what the agent
changed and not the overlay. The end is any run's: ``unit/NN`` is put back (it was never moved) and ``run/<id>``
stays. A run that fails after the checkout began (``_Active.touched``) is finalized as one that started, so the
worktree is put back; so is a handover run that ends early while a console that died left its worktree on
``run/<id>`` (``_left_over``: a refusal, a stop while queued). ``keep`` refuses a handover run (its commit holds the
other unit's files, not only the agent's change).

A unit can never extend a run: ``continue`` on an escalation of only ``continue`` and ``abort`` is the facilitator's
answer (``allowed_options``; ``answer`` raises ``FacilitatorOnly`` for anyone else), unless the question is a failed
gate ladder (``ladder failed: ...``: going on costs no credits more). Any other such question counts as a credit cap
(the run's, or a role's session cap; an unknown one too: it fails closed). The facilitator's continue there goes on
the record (``extensions``) before the answer reaches the harness, and only once per question.

Topics: ``run:<id>`` gets ``line`` (SSE id = seq), ``loop``, ``approval`` and ``run`` (``RunRecord.view``: no pid, host
paths or raw summary; the text of a question, like the progress, plan.md and review.md, goes through ``events.scrub``);
``global`` is public (no login) and gets ``run`` with ``RunRecord.public`` fields only. A browser
that comes back reads what it missed from the trail (``lines``) and then the topic; ``settled`` tells it when the
topic has nothing more to say.

Locks, in this order only:

0. The answer lock, held by ``answer`` alone around one question: its record (read, ``extensions`` written), its HITL
   files, and the runner's lock for a moment to raise the watcher's ceiling. Nothing else takes it.
1. The runner's lock guards its queues and the runs it holds in memory (``_open``: the unit's queued or going run;
   ``_owed``: ended runs whose charge failed, charged at the unit's next launch or by a restart).
   While it is held, only single records of the ``RunStore`` are read or written (that store holds its own lock for
   one file at a time): never a ``UnitStore`` call, a scan of all records, or a wait on anything else.
2. ``UnitStore``'s lock, held by its own calls only (a catch-up reset or a restore holds it across its git calls).
3. ``files.worktree_lock``, taken only while a run (or a keep) holds the unit lock, with the unit's git location
   looked up before (``UnitStore.git_for``), so nothing waits on the ``UnitStore`` lock while holding it. A reset or a
   restore takes the store lock and then the worktree lock, and refuses a locked unit: the two never wait on each
   other. A handover run takes no lock of the unit it takes the harness from: it reads git, not that unit's worktree.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import json
import logging
import math
import os
import re
import subprocess
import tempfile
import threading
import time
from collections import deque
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path, PurePosixPath
from typing import Any, BinaryIO, Callable, Iterator, Mapping

from console import events, files, handover, ladder, procs, structure, validate, worktree_ops
from console.config import ConsoleConfig
from console.files import worktree_lock
from console.sse import Broker
from console.units import NEEDS_RESET, Busy, Unit, UnitStore

__all__ = [
    "ACTIVE", "FINAL", "KINDS", "MODES", "OVERSHOOT", "RUN_ID", "FacilitatorOnly", "RunRecord", "RunStore", "Runner",
    "allowed_options", "extension_credits",
]  # fmt: skip

log = logging.getLogger(__name__)

KINDS = ("workflow", "gates", "warm")
MODES = ("interactive", "autonomous")
ACTIVE = frozenset({"running", "waiting"})
FINAL = frozenset({"done", "failed", "escalated", "stopped", "refused", "error"})
MIN_LEFT = 1.0  # credits a unit needs left to start a workflow run
OVERSHOOT = 1.0  # credits a run may end past its cap: one model call (the harness looks at its cap between calls)
RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")  # what a run id looks like (nothing else becomes a path)

_FROM_SUMMARY = {"done": "done", "failed": "failed", "escalated": "escalated", "aborted": "stopped"}
_HOST_ONLY = frozenset(
    {"pid", "started_time", "run_dir", "hitl_dir", "log", "patch", "summary", "live_credits", "live_seq", "charged"}
)  # not in what a browser sees
_WORKFLOW = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
_TICKET = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")  # no "..", no option
_TICKET_MAX = 200
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_UNIT_NAME = re.compile(r"unit-[0-9]{2}")  # what a unit is called (``UnitStore.init``); nothing else is kept as a name
_ERROR_MAX = 1000
_RUN_FOLDER = (".harness", "runs")  # where the harness keeps a run in the worktree
_PENDING_KEYS = ("id", "kind", "step", "question", "details", "options", "asked_at")
_PENDING_MAX = 1024 * 1024
_SUMMARY_MAX = 1024 * 1024
_TICK_BYTES = 4 * 1024 * 1024  # audit trail a watcher reads per tick
_TRAIL_MAX = 256 * 1024 * 1024  # audit trail finalize reads for the credits
_LOG_TAIL = 4 * 1024 * 1024  # end of a gates run's log searched for its JSON
_TEXT_MAX = 500
_ITEMS_MAX = 100
_SEQ_MAX = 2**53  # a seq (or a whole number kept as one) the JSON of a record can carry exactly
_REPLAY_MAX = 64 * 1024 * 1024  # audit trail read for a browser (its lines, its progress); real ones are under 8 MB
_ARTIFACT_MAX = 200 * 1024  # plan.md, review.md
_DIFF_MAX = 500 * 1024
_ARTIFACTS = ("plan.md", "review.md")
_STOP_WAIT_S = 30.0  # how long stop() waits for the watcher to finalize
_JOIN_S = 30.0
_LOCK_TRIES, _LOCK_WAIT_S = 20, 0.25  # a keep may hold the unit for a moment when its next run starts
_REPLACE_TRIES, _REPLACE_WAIT_S = 50, 0.05  # Windows refuses os.replace while a reader has the target open: ~2.5 s
_UNREADABLE = object()  # pending.json is there but cannot be read just now



def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")


# --- records ----------------------------------------------------------------------------------------------------


@dataclass(frozen=True)
class RunRecord:
    """One run as ``<data>/runs/<id>.json`` keeps it. Times are UTC ISO 8601; paths are the facilitator's."""

    id: str
    unit: str
    kind: str  # workflow | gates | warm
    lane: str  # agent | gates
    workflow: str | None = None
    ticket: str | None = None
    mode: str = "interactive"
    overlay_from: str | None = None  # the unit whose harness structure the run takes (a handover run)
    status: str = "queued"
    queued_at: str = ""
    started_at: str | None = None
    ended_at: str | None = None
    pid: int | None = None
    started_time: float | None = None  # procs.start_time(pid): tells the run's process from a later one with its pid
    run_dir: str = ""  # the harness's run folder in the worktree; "" for gates and warm runs
    hitl_dir: str = ""  # "" for gates and warm runs
    log: str = ""
    cap: float = 0.0
    credits: float = 0.0
    summary: dict = field(default_factory=dict)  # summary.json, or a gates run's JSON; plus exit_code when known
    branch: str | None = None
    commit: str | None = None
    patch: str | None = None
    kept: bool | None = None
    loop_flag: str | None = None
    pending: dict | None = None
    error: str = ""
    cli: str = ""
    unit_head: str | None = None  # the commit unit/NN had when the run started
    overlay_commit: str | None = None  # "console: harness of <unit>" on run/<id>: where the agent's work starts
    overlay_skipped: list = field(default_factory=list)  # paths of that harness left out (not plain, not text ...)
    overlay_source_commit: str | None = None  # the commit the other unit's branch was on: what "its harness" was
    accounting: str | None = None  # "capped": the run's files could not be trusted, so its ceiling was charged
    extensions: int = 0  # continues the facilitator gave at a credit cap: each raises the ceiling by ``extension``
    live_credits: float = 0.0  # what the watcher has counted so far (kept here so a restart does not lose it)
    live_seq: int = 0  # the highest audit seq the watcher had seen when it last counted a turn
    charged: bool = False  # the credits are on the unit's spent (set in the same write as the final status)
    weakened: list = field(default_factory=list)  # the flags of the harness the run started with (structure.weakening)
    fake: bool = False  # personal mode: the harness's scripted fake model (``--fake``): no model, charged 0 credits

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)

    @classmethod
    def from_dict(cls, data: dict[str, Any]) -> "RunRecord":
        return cls(**{key: value for key, value in data.items() if key in _FIELDS})

    def view(self, by: str | None = None, personal: bool = False) -> dict[str, Any]:
        """The record as a browser sees it: no pid, host paths or raw summary; ``has_patch`` for the patch, and
        ``not_run_here`` for the gates the Docker-free ladder did not run (the harness's line, ``""`` when the full
        ladder ran).

        A pending question also says which of its options ``by`` may answer (``allowed_options``): a unit's unless
        ``by`` is ``admin`` (the run's own topic, which every watcher shares, gets a unit's).
        """
        out = {key: value for key, value in self.to_dict().items() if key not in _HOST_ONLY}
        out["has_patch"] = bool(self.patch)
        lite = self.summary.get("not_run_here") if isinstance(self.summary, dict) else None
        out["not_run_here"] = lite if isinstance(lite, str) else ""
        if self.pending is not None:
            out["pending"] = {**self.pending, "allowed_options": allowed_options(self.pending, by, personal)}
        return out

    def public(self, position: int | None = None) -> dict[str, Any]:
        """The fields the public ``global`` topic may carry; ``position``: the place in the lane's queue (1 = next)."""
        return {"id": self.id, "unit": self.unit, "kind": self.kind, "status": self.status, "position": position,
                "credits": self.credits}


_FIELDS = frozenset(f.name for f in dataclasses.fields(RunRecord))


_CONTINUE_OR_ABORT = (["continue", "abort"], ["abort", "continue"])
_LADDER = "ladder failed:"  # how the harness starts the question when a gate ladder fails (orchestrator._step_gates)


class FacilitatorOnly(ValueError):
    """An answer only the facilitator may give: one that would let a run go on past its cap."""


def _credit_cap_question(pending: dict) -> bool:
    """An escalation of only ``continue`` and ``abort`` whose ``continue`` gives the run more credits: the harness's
    question at the run's credit cap (``run credit cap reached: ...``) or a role's (``session credit cap reached
    (<role>): ...``). Every such escalation but a failed gate ladder counts as one (fail closed)."""
    question = pending.get("question")
    ladder = isinstance(question, str) and question.startswith(_LADDER)
    return pending.get("kind") == "escalation" and pending.get("options") in _CONTINUE_OR_ABORT and not ladder


def allowed_options(pending: dict, by: str | None, personal: bool = False) -> list[str]:
    """The options of a pending question ``by`` may answer: every one for ``admin``; for a unit (or anyone else) not
    ``continue`` at a credit cap. In personal mode (``personal``) there is no facilitator: the participant answers it."""
    raw = pending.get("options")
    options = [option for option in raw if isinstance(option, str)] if isinstance(raw, list) else []
    if by != "admin" and not personal and _credit_cap_question(pending):
        options = [option for option in options if option != "continue"]
    return options


def extension_credits(harness: Mapping[str, Any], runtime: str | None = None) -> float:
    """What a continue at a credit cap gives a run. With ``runtime`` (personal mode: ``cfg.runtime``, the participant's
    own ``copilot-user``): that runtime's session cap, as the harness adds it (``HarnessConfig.caps``: the ``copilot-user``
    one when the runtime has none). Without it: what a facilitator's continue at a credit cap gives a run: the session cap of the facilitator runtime in
    ``harness`` (harness.yaml), which the harness adds to its run cap (``HarnessConfig.caps``: the ``copilot-user``
    runtime when there is no ``facilitator`` one, 10 when the value is not set). ``ValueError`` when it is no number
    of 0 or more."""
    runtimes = harness.get("runtimes")
    runtimes = runtimes if isinstance(runtimes, Mapping) else {}
    if runtime is not None:
        chosen = runtimes.get(runtime) or runtimes.get("copilot-user") or {}
    else:
        chosen = runtimes.get("facilitator") or runtimes.get("copilot-user") or {}
    raw = chosen.get("max_ai_credits_per_session", 10) if isinstance(chosen, Mapping) else 10
    try:
        value = float(raw)
    except (TypeError, ValueError, OverflowError):
        value = math.nan
    if not math.isfinite(value) or value < 0:
        raise ValueError(f"harness.yaml: max_ai_credits_per_session of the facilitator runtime is not usable: {raw!r}")
    return value


_store_locks: dict[str, threading.Lock] = {}  # one per records folder, however many RunStores use it
_store_locks_guard = threading.Lock()


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` by atomic replace (retried briefly while a reader has it open on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT_S)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


class RunStore:
    """Run records, one JSON file per run in ``dir``. Safe to share between threads."""

    def __init__(self, dir: Path) -> None:
        self.dir = Path(dir)
        key = os.path.normcase(os.path.abspath(self.dir))
        with _store_locks_guard:
            self._lock = _store_locks.setdefault(key, threading.Lock())

    def _path(self, run_id: object) -> Path:
        if not isinstance(run_id, str) or not RUN_ID.fullmatch(run_id):
            raise KeyError(run_id)  # nothing that is not a run id becomes a path
        return self.dir / f"{run_id}.json"

    def create(self, rec: RunRecord) -> RunRecord:
        """Store a new record; ``FileExistsError`` when a run with its id exists."""
        path = self._path(rec.id)
        with self._lock:
            if path.exists():
                raise FileExistsError(f"run {rec.id} exists already")
            _write_json(path, rec.to_dict())
        return rec

    def get(self, run_id: str) -> RunRecord:
        """The record; ``KeyError`` when there is no such run."""
        path = self._path(run_id)
        with self._lock:
            return self._read(path, run_id)

    def update(self, run_id: str, **fields: Any) -> RunRecord:
        """Change fields of a record (any but ``id``); returns the new record."""
        bad = sorted((set(fields) - _FIELDS) | ({"id"} & set(fields)))
        if bad:
            raise ValueError(f"cannot update {', '.join(bad)}")
        path = self._path(run_id)
        with self._lock:
            rec = dataclasses.replace(self._read(path, run_id), **fields)
            _write_json(path, rec.to_dict())
        return rec

    def list(self, unit: str | None = None) -> list[RunRecord]:
        """Every record (only ``unit``'s when given), newest first by ``queued_at``. Unreadable files are skipped."""
        records = []
        with self._lock:
            for path in sorted(self.dir.glob("*.json")) if self.dir.is_dir() else ():
                try:
                    records.append(RunRecord.from_dict(json.loads(path.read_text(encoding="utf-8"))))
                except (OSError, ValueError, TypeError):
                    log.warning("run record %s cannot be read; skipped", path.name)
        if unit is not None:
            records = [rec for rec in records if rec.unit == unit]
        return sorted(records, key=lambda rec: (rec.queued_at, rec.id), reverse=True)

    @staticmethod
    def _read(path: Path, run_id: str) -> RunRecord:
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise KeyError(run_id) from None
        return RunRecord.from_dict(json.loads(text))


# --- what the console takes from files a run wrote --------------------------------------------------------------


def _finite(value: Any) -> float | None:
    """``value`` as a float when it is a finite number. Never raises: a ``bool`` is no number, nor is an int too large
    for a float (JSON allows any number of digits)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except (OverflowError, ValueError, TypeError):
        return None
    return number if math.isfinite(number) else None


def _credit(value: Any) -> float | None:
    """A credit amount: a finite number, 0 or more."""
    number = _finite(value)
    return None if number is None or number < 0 else round(number, 4)


def _number(value: Any) -> int | float | None:
    number = _finite(value)
    if number is None:
        return None
    return value if isinstance(value, int) and abs(value) < _SEQ_MAX else number


def _flag(value: Any) -> bool | None:
    return value if isinstance(value, bool) else None


def _text(value: Any) -> str | None:
    return value[:_TEXT_MAX] if isinstance(value, str) else None


def _mapping(value: Any, item: Callable[[Any], Any]) -> dict | None:
    """A small mapping of short text keys to values that pass ``item``."""
    if not isinstance(value, dict):
        return None
    out = {}
    for key, raw in list(value.items())[:_ITEMS_MAX]:
        kept = item(raw)
        if isinstance(key, str) and len(key) <= 100 and kept is not None:
            out[key] = kept
    return out


def _numbers(value: Any) -> dict | None:
    return _mapping(value, _number)


def _texts(value: Any) -> dict | None:
    return _mapping(value, _text)


def _gate_list(value: Any) -> list | None:
    if not isinstance(value, list):
        return None
    fields = {"id": _text, "name": _text, "status": _text, "reason": _text, "ms": _number, "over_budget": _flag}
    return [_pick(gate, fields) for gate in value[:_ITEMS_MAX] if isinstance(gate, dict)]


_GATE_ID = re.compile(r"[a-z][a-z0-9]*(?:-[a-z0-9]+)*|G[0-9]{1,3}|diff-policy|diff-size")


def _lite_note(value: Any) -> str | None:
    """The gates the Docker-free ladder did not run (they need Docker), as the harness words it in summary.json
    (``lite ladder: misra no-heap sanitizers coverage not run here (needs Docker)``); ``harness gates --json`` gives their ids as a list,
    which gets the same words. ``None`` for none, or for anything else."""
    if isinstance(value, str):
        return _text(value) or None
    if isinstance(value, list) and value and all(isinstance(i, str) and _GATE_ID.fullmatch(i) for i in value):
        return f"lite ladder: {' '.join(value[:_ITEMS_MAX])} not run here (needs Docker)"
    return None


def _pick(data: dict, fields: dict[str, Callable[[Any], Any]]) -> dict:
    """The fields the console knows, each type-checked; anything else is dropped."""
    out = {}
    for key, check in fields.items():
        kept = check(data.get(key)) if key in data else None
        if kept is not None:
            out[key] = kept
    return out


_SUMMARY_FIELDS: dict[str, Callable[[Any], Any]] = {
    **dict.fromkeys(("run_id", "workflow", "status", "mode", "runtime", "where", "ticket", "stopped_at", "reason",
                     "error", "branch", "commit", "review"), _text),
    "credits": _credit, "interventions": _number, "duration_s": _number,
    "credits_by_role": _numbers, "counters": _numbers, "tokens": _numbers, "steps": _texts, "not_run_here": _lite_note,
}  # fmt: skip
_GATES_FIELDS: dict[str, Callable[[Any], Any]] = {
    "ok": _flag, "summary": _text, "duration_ms": _number, "exit_codes": _numbers, "gates": _gate_list,
    "not_run_here": _lite_note,
}  # fmt: skip


class _Count:
    """What a trail (or the watcher, live) says a run spent, and whether that can be believed.

    Per ``turn`` seq it keeps the most credits any line with that seq gave, so a line written ahead with the same seq
    cannot lower the real one; ``floor`` is a total known from before (the live count kept on the record), ``seq`` the
    highest seq seen. ``sound`` turns False for good on any anomaly: a credit amount that is not a finite number of 0
    or more, one turn past ``cap + OVERSHOOT`` (more than the run could spend at all), a seq that is a whole number out
    of range (``_seq``), a line that is not a JSON object or too long to read, a file that is refused, a total that
    does not stay finite. ``cap`` is the run's cap with its extensions.
    """

    def __init__(self, cap: float = math.inf, seq: int = 0, floor: float = 0.0) -> None:
        self.cap = cap
        self.turns: dict[int, float] = {}
        self.seq = seq
        self.floor = floor
        self.sound = True

    def add(self, ev: dict) -> bool:
        """Count one event; True when it raised what a turn seq counts (a new turn, or a higher one)."""
        seq = _seq(ev)
        if seq is not None:
            self.seq = max(self.seq, seq)
        elif isinstance(ev.get("seq"), int) and not isinstance(ev.get("seq"), bool):
            self.sound = False  # written to hide the lines after it from a stream (ids only grow), or worse
        if ev.get("kind") != "turn" or "credits" not in ev:
            return False
        spent = _credit(ev["credits"])
        if spent is None or spent > self.cap + OVERSHOOT:
            self.sound = False
            return False
        if seq is None or spent <= self.turns.get(seq, -1.0):
            return False
        self.turns[seq] = spent
        return True

    def merged(self, other: "_Count") -> "_Count":
        out = _Count(min(self.cap, other.cap), max(self.seq, other.seq), max(self.floor, other.floor))
        out.turns = dict(self.turns)
        for seq, spent in other.turns.items():
            out.turns[seq] = max(out.turns.get(seq, 0.0), spent)
        out.sound = self.sound and other.sound
        return out

    def total(self) -> float:
        """The credits: the sum of the turns (``math.fsum``), at least ``floor``; ``inf`` when the sum overflows."""
        try:
            spent = math.fsum(self.turns.values())
        except OverflowError:
            return math.inf
        return max(spent, self.floor) if math.isfinite(spent) else math.inf

    def believable(self) -> bool:
        return self.sound and _believable(self.total(), self.cap)


def _seq(ev: dict) -> int | None:
    """The event's ``seq`` when it is one a record can carry (a whole number, 0 to 2**53)."""
    seq = ev.get("seq")
    return seq if isinstance(seq, int) and not isinstance(seq, bool) and 0 <= seq < _SEQ_MAX else None


def _believable(total: float, cap: float) -> bool:
    """A run total the console charges as it is: finite, 0 or more, at most one model call past the cap."""
    return math.isfinite(total) and 0 <= total <= cap + OVERSHOOT


def _anomaly_charge(cap: float) -> float:
    """What a run is charged when its credits cannot be believed: the most an honest run can spend."""
    return round(cap + OVERSHOOT, 4)


_said: set[tuple[str, ...]] = set()  # what was said about a run once already (a warning, a traceback)
_said_guard = threading.Lock()


def _first_time(*key: str) -> bool:
    """True the first time ``key`` comes up in this process: a warning about a run is given once, not per request."""
    with _said_guard:
        if key in _said:
            return False
        _said.add(key)
        return True


def _run_file_opener(root: Path, run_id: str, name: str) -> Callable[[Path], BinaryIO]:
    """An opener for ``events.Tail``: the run folder's file, only as a plain file (warned about once per run when it
    is not, however many watchers and requests read it)."""
    parts = (*_RUN_FOLDER, run_id, name)

    def opener(_path: Path) -> BinaryIO:
        try:
            return worktree_ops.open_plain(root, parts)
        except worktree_ops.NotPlain as err:
            if _first_time("not plain", run_id, name):
                log.warning("run %s: not read: %s", run_id, err)
            raise

    return opener


def _trail(root: Path, run_id: str) -> events.Tail:
    """The run's audit trail for the watcher: read as a plain file, at most ``_TICK_BYTES`` a call."""
    return events.Tail(
        root.joinpath(*_RUN_FOLDER, run_id, "events.jsonl"),
        max_bytes=_TICK_BYTES,
        opener=_run_file_opener(root, run_id, "events.jsonl"),
    )


def _read_trail(root: Path, run_id: str, cap: float) -> _Count:
    """Count the whole audit trail (a plain file, at most ``_TRAIL_MAX`` bytes).

    A last line without its newline (a write the kill cut off) is not counted. Not ``sound``: the file is refused or
    larger than ``_TRAIL_MAX``, a line is longer than ``_TICK_BYTES`` (it could hide a turn the harness appended to
    it; the watcher skips such lines too) or is not a JSON object, or a turn is unbelievable (``_Count``).
    """
    count = _Count(cap)
    try:
        fh = worktree_ops.open_plain(root, (*_RUN_FOLDER, run_id, "events.jsonl"))
    except FileNotFoundError:
        return count
    except OSError as err:
        log.warning("run %s: the audit trail is not read: %s", run_id, err)
        count.sound = False
        return count
    with fh:
        buffer, total, skipping = b"", 0, False
        while chunk := fh.read(_TICK_BYTES):
            total += len(chunk)
            if total > _TRAIL_MAX:
                log.warning("run %s: the audit trail is over %d bytes: not counted to the end", run_id, _TRAIL_MAX)
                count.sound = False
                break
            *lines, buffer = (buffer + chunk).split(b"\n")
            if skipping and lines:
                lines, skipping = lines[1:], False  # the end of a line too long to look at
            for raw in lines:
                _count_line(raw, count)
            if len(buffer) > _TICK_BYTES:
                log.warning("run %s: a line of the audit trail is longer than %d bytes", run_id, _TICK_BYTES)
                buffer, skipping, count.sound = b"", True, False
    return count


def _count_line(raw: bytes, count: _Count) -> None:
    text = raw.decode("utf-8", errors="replace").strip()
    if not text:
        return
    try:
        ev = json.loads(text)
    except (ValueError, RecursionError):
        ev = None
    if isinstance(ev, dict):
        count.add(ev)
    else:
        count.sound = False


def _summary(root: Path, run_id: str) -> tuple[dict, bool]:
    """The known fields of the run's summary.json, and whether it is sound.

    Missing: ``({}, True)`` (the harness was stopped before it wrote one). Not a plain file, larger than 1 MB, not a
    JSON object, or a ``credits`` that is not a finite number of 0 or more: not sound.
    """
    try:
        with worktree_ops.open_plain(root, (*_RUN_FOLDER, run_id, "summary.json")) as fh:
            raw = fh.read(_SUMMARY_MAX + 1)
    except FileNotFoundError:
        return {}, True
    except OSError as err:
        log.warning("run %s: summary.json is not read: %s", run_id, err)
        return {}, False
    if len(raw) > _SUMMARY_MAX:
        log.warning("run %s: summary.json is larger than %d bytes: not read", run_id, _SUMMARY_MAX)
        return {}, False
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        data = None
    if not isinstance(data, dict):
        log.warning("run %s: summary.json is not a JSON object", run_id)
        return {}, False
    return _pick(data, _SUMMARY_FIELDS), "credits" not in data or _credit(data["credits"]) is not None


def _gates_summary(log_path: Path) -> dict:
    """The known fields of the JSON ladder a gates run printed: the last line starting a JSON object, else ``{}``."""
    try:
        with open(log_path, "rb") as fh:
            fh.seek(max(0, os.fstat(fh.fileno()).st_size - _LOG_TAIL))
            text = fh.read().decode("utf-8", errors="replace")
    except OSError:
        return {}
    starts = ([0] if text.startswith("{") else []) + [m.start() + 1 for m in re.finditer(r"\n\{", text)]
    decoder = json.JSONDecoder()
    for start in reversed(starts):
        try:
            data, _ = decoder.raw_decode(text, start)
        except (ValueError, RecursionError):
            continue
        if isinstance(data, dict):
            return _pick(data, _GATES_FIELDS)
    return {}


def _read_pending(path: Path) -> dict | None | object:
    """The question in ``pending.json``; ``None`` when there is none, ``_UNREADABLE`` when it cannot be read now.

    Opened, read and closed at once: the harness retries its replace of the file while a reader holds it.
    """
    try:
        with open(path, "rb") as fh:
            raw = fh.read(_PENDING_MAX + 1)
    except FileNotFoundError:
        return None
    except OSError:
        return _UNREADABLE
    try:
        data = json.loads(raw.decode("utf-8"))
    except (UnicodeDecodeError, ValueError, RecursionError):
        return _UNREADABLE
    if not isinstance(data, dict):
        return _UNREADABLE
    ask_id, options = data.get("id"), data.get("options")
    if not isinstance(ask_id, str) or not ask_id or not isinstance(options, list) or not options:
        return _UNREADABLE
    if not all(isinstance(option, str) for option in options):
        return _UNREADABLE
    texts = {key: data[key] if isinstance(data.get(key), str) else "" for key in _PENDING_KEYS}
    return {**texts, "options": list(options)}


def _scrubbed_question(pending: dict, root: Path | None) -> dict:
    """A pending question with its ``question`` and ``details`` without host paths (``events.scrub``): the harness
    writes them from what the run did, paths and all."""
    return {**pending, **{key: events.scrub(pending[key], root) for key in ("question", "details")
                          if isinstance(pending.get(key), str)}}


def _scrubbed_all(value: Any, scrub: Callable[[str], str]) -> Any:
    """``value`` with ``scrub`` applied to every text in it, the keys of a mapping too (a step's name is one)."""
    if isinstance(value, str):
        return scrub(value)
    if isinstance(value, dict):
        return {scrub(key) if isinstance(key, str) else key: _scrubbed_all(item, scrub) for key, item in value.items()}
    if isinstance(value, list):
        return [_scrubbed_all(item, scrub) for item in value]
    return value


def _answered(path: Path, ask_id: str) -> bool:
    """Whether ``answer.json`` holds an answer to the question ``ask_id`` that the harness has not taken yet."""
    try:
        with open(path, "rb") as fh:
            data = json.loads(fh.read(_PENDING_MAX).decode("utf-8"))
    except (OSError, ValueError, RecursionError):  # none, or not one the harness would take
        return False
    return isinstance(data, dict) and data.get("id") == ask_id


def _ticket_key(ticket: str | None) -> str | None:
    """``PROJ-5`` for ``tickets/PROJ-5.md`` (or ``tickets/PROJ-5/ticket.md``), as the harness names its runs."""
    if not ticket:
        return None
    path = PurePosixPath(ticket)
    if path.stem.lower() == "ticket" and path.parent.name:
        return path.parent.name
    name = path.name
    return name[: -len(".agent.md")] if name.endswith(".agent.md") else path.stem  # personal: tickets/TS-101.agent.md


def _valid_ticket(ticket: object) -> bool:
    return isinstance(ticket, str) and len(ticket) <= _TICKET_MAX and bool(_TICKET.fullmatch(ticket))


def _valid_workflow(name: object) -> bool:
    return isinstance(name, str) and bool(_WORKFLOW.fullmatch(name))


def _end_line(rec: RunRecord, seq: int) -> events.Line:
    """The console's last line of an ended run: id ``seq + 1``, one past the highest seq of its audit trail."""
    return events.Line(seq + 1, rec.ended_at or "", "end", f"run {rec.status} ({rec.credits:g} credits)",
                       ok=rec.status == "done")


def _replay(root: Path, run_id: str) -> Iterator[dict]:
    """The run's audit events in the order of the file, read as the watcher reads them (``_trail``), at most
    ``_REPLAY_MAX`` bytes."""
    tail = _trail(root, run_id)
    for _ in range(_REPLAY_MAX // _TICK_BYTES):
        before = tail.offset
        got = tail.read_new()
        if tail.offset == before:  # the end of the file (or of its last whole line)
            return
        yield from got


def _clipped(source: str, data: bytes) -> dict:
    return {"source": source, "text": data[:_DIFF_MAX].decode("utf-8", errors="replace"),
            "truncated": len(data) > _DIFF_MAX}


def _budget_refusal(unit: Unit) -> str:
    if unit.left >= MIN_LEFT:
        return ""
    left = round(unit.left, 2)
    return f"budget left {left if left > 0 else 0:g} of {unit.budget:g} credits"


# --- the runner -------------------------------------------------------------------------------------------------


class _Active:
    """A run this console is starting or watching (memory only)."""

    def __init__(self, rec: RunRecord, cap: float) -> None:
        self.id, self.unit, self.lane = rec.id, rec.unit, rec.lane
        self.proc: subprocess.Popen | None = None
        self.pid, self.started = rec.pid, rec.started_time
        self.status = rec.status
        self.pending_id = (rec.pending or {}).get("id")
        self.count = _Count(cap or math.inf, rec.live_seq, rec.live_credits)  # live, from the record's on a restart
        self.flagged = rec.accounting == "capped"  # the record says the trail cannot be trusted
        self.fake = rec.fake  # the fake model: its credits are no model's, nothing is charged or capped
        self.stop_by: str | None = None
        self.touched = False  # the worktree was moved off ``unit/NN`` (a handover run's branch): it must be put back
        self.finalizing = False
        self.done = threading.Event()


@dataclass
class _Progress:
    """How far a finalize got: the run's process was started; its worktree is known to be back; its credits charged;
    and what ``_settle`` has worked out of the final record so far, which a finalize that fails later still writes."""

    started: bool
    restored: bool
    charged: bool = False
    outcome: tuple[float, str | None] | None = None  # the credits and accounting _settle worked out, once it has
    status: str | None = None  # the run's status, once worked out
    summary: dict | None = None
    result: dict[str, Any] | None = None  # branch, commit, patch and error, once the worktree is wrapped up (or
    # there was nothing to wrap up)


class Runner:
    """Queue, lanes, watchers and the end of every run of one console. Safe to use from many threads.

    ``spawn`` is ``procs.spawn`` (tests wrap it); ``tick_s`` is how often a watcher looks at its run; ``extension`` is
    what a facilitator's continue at a credit cap gives a run (``extension_credits`` of the golden harness.yaml: the
    save rules keep the units on its runtimes).
    """

    def __init__(
        self,
        cfg: ConsoleConfig,
        units: UnitStore,
        store: RunStore,
        broker: Broker,
        spawn: Callable[..., subprocess.Popen] = procs.spawn,
        *,
        extension: float,
        tick_s: float = 0.5,
        check: Callable[[str, str], list[str]] | None = None,
    ) -> None:
        self.cfg, self.units, self.store, self.broker = cfg, units, store, broker
        self.extension = extension
        self._spawn = spawn
        self._tick = tick_s
        self._answering = threading.Lock()  # one answer at a time: a question is answered, and a run extended, once
        self._cond = threading.Condition()
        self._queues: dict[str, deque[str]] = {"agent": deque(), "gates": deque()}
        self._active: dict[str, _Active] = {}
        self._open: dict[str, str] = {}  # unit id -> its queued or going run
        self._keeping: set[str] = set()  # runs whose result is being taken right now
        # unit id -> its ended runs whose charge failed: tried again at the unit's next launch, or by a restart
        self._owed: dict[str, set[str]] = {}
        self._threads: list[threading.Thread] = []
        self._closing = threading.Event()
        self._started = False
        self._rules = check  # the save rules (``App.check``); read from the golden on first use when not given

    # -- life cycle --

    def start(self) -> None:
        """Reconcile the records with what really runs, then start the workers: ``cfg.lanes[lane]`` per lane."""
        with self._cond:
            if self._started:
                raise RuntimeError("the runner is started already")
            self._started = True
        stopped = self._reconcile()
        if stopped:
            log.info("runs stopped while the console was away: %s", ", ".join(stopped))
        for lane in self._queues:
            for n in range(self.cfg.lanes.get(lane, 1)):
                self._thread(self._work, lane, name=f"runs-{lane}-{n + 1}")

    def shutdown(self) -> None:
        """Stop the workers and watchers. Running harness processes go on; the next ``start`` reconciles them."""
        self._closing.set()
        with self._cond:
            self._cond.notify_all()
            threads = list(self._threads)
        for thread in threads:
            thread.join(_JOIN_S)

    def _thread(self, target: Callable[..., None], *args: Any, name: str) -> None:
        thread = threading.Thread(target=target, args=args, name=name, daemon=True)
        with self._cond:
            self._threads = [t for t in self._threads if t.is_alive()]
            self._threads.append(thread)
        thread.start()

    def _forget(self, run_id: str) -> None:
        """The run is no longer queued or going (the runner's lock is held)."""
        self._active.pop(run_id, None)
        for unit_id in [u for u, held in self._open.items() if held == run_id]:
            del self._open[unit_id]
        self._cond.notify_all()

    # -- asking for a run --

    def submit(
        self,
        unit_id: str,
        kind: str,
        *,
        workflow: str | None = None,
        ticket: str | None = None,
        mode: str = "interactive",
        overlay_from: str | None = None,
        fake: bool = False,
    ) -> RunRecord:
        """Queue a run, or refuse it: either way the stored record comes back (``queued``, or ``refused`` + ``error``).

        ``fake``: a workflow run on the harness's scripted fake model (``--fake``), personal mode only. It needs no
        credits left, its cap is the run cap, and it is charged 0 credits whatever its run files count.

        ``ValueError`` for a kind or mode that does not exist and for ``fake`` where it does not apply, ``KeyError``
        for an unknown unit.
        """
        if kind not in KINDS:
            raise ValueError(f"kind must be one of {', '.join(KINDS)}")
        if mode not in MODES:
            raise ValueError(f"mode must be one of {', '.join(MODES)}")
        if fake and not self.cfg.personal:
            raise ValueError("the fake model is for personal mode only")
        if fake and kind != "workflow":
            raise ValueError("only an agent run can use the fake model")
        unit = self.units.get(unit_id)
        flow = kind == "workflow"
        rec = RunRecord(
            id=self._new_id(kind, ticket if flow and _valid_ticket(ticket) else None),
            unit=unit.id,
            kind=kind,
            lane="agent" if flow else "gates",
            workflow=workflow if flow and _valid_workflow(workflow) else None,
            ticket=ticket if flow and _valid_ticket(ticket) else None,
            mode=mode,
            overlay_from=overlay_from if isinstance(overlay_from, str) and _UNIT_NAME.fullmatch(overlay_from) else None,
            queued_at=_now(),
            fake=bool(fake),
        )
        refusal = self._refusal(unit, kind, workflow, ticket, overlay_from)
        reserved = False
        if not refusal:
            with self._cond:
                other = self._open.get(unit.id)
                if other is None:
                    self._open[unit.id], reserved = rec.id, True
            refusal = f"{unit.id} already has run {other}" if other is not None else ""
        try:
            if reserved:  # read again: the unit's last run has ended completely, its credits are counted
                unit = self.units.get(unit.id)
                refusal = self._unit_refusal(unit, kind, rec.fake)
            rec = self._store_new(rec, unit, refusal)
        except BaseException:
            if reserved:
                with self._cond:
                    self._forget(rec.id)
            raise
        if reserved:
            with self._cond:
                self._open.pop(unit.id, None)
                if not refusal:
                    self._open[unit.id] = rec.id
                    self._queues[rec.lane].append(rec.id)
                self._cond.notify_all()
        log.info("run %s for %s: %s%s", rec.id, unit.id, rec.status, f" ({refusal})" if refusal else "")
        self._publish(rec)
        return rec

    def _refusal(self, unit: Unit, kind: str, workflow: object, ticket: object, overlay_from: object) -> str:
        if overlay_from is not None:
            if kind != "workflow":
                return "only a workflow run can use another unit's harness"
            if not isinstance(overlay_from, str) or not _UNIT_NAME.fullmatch(overlay_from):
                return "not a unit"
            if overlay_from == unit.id:
                return f"{unit.id} cannot run with its own harness"
            try:
                self.units.get(overlay_from)
            except KeyError:
                return f"unknown unit: {overlay_from}"
        if kind != "workflow":
            return ""
        if not _valid_workflow(workflow):
            return "unknown workflow"
        if not (unit.worktree / "harness" / "workflows" / f"{workflow}.yaml").is_file():
            return f"unknown workflow: {workflow}"
        if not ticket:
            return "a workflow run needs a ticket"
        if not _valid_ticket(ticket):
            return "not a usable ticket: a file under the unit (letters, digits, '.', '_', '-', '/') or a Jira key"
        return ""

    @staticmethod
    def _unit_refusal(unit: Unit, kind: str, fake: bool = False) -> str:
        if unit.locked_by is not None and unit.locked_by.startswith(NEEDS_RESET):
            return f"{unit.id} needs a reset by the facilitator first"
        return _budget_refusal(unit) if kind == "workflow" and not fake else ""  # the fake model spends nothing

    @staticmethod
    def _new_id(kind: str, ticket: str | None) -> str:
        from harness.state import new_run_id

        return new_run_id(_ticket_key(ticket) if kind == "workflow" else "gates")

    def _store_new(self, rec: RunRecord, unit: Unit, refusal: str) -> RunRecord:
        for attempt in range(5):  # an id is taken only if two runs of one ticket start in the same second
            if attempt:
                rec = dataclasses.replace(rec, id=self._new_id(rec.kind, rec.ticket))
            if refusal:
                named = dataclasses.replace(rec, status="refused", error=refusal, ended_at=rec.queued_at)
            else:
                named = self._prepared(rec, unit)
            try:
                return self.store.create(named)
            except FileExistsError:
                continue
        raise RuntimeError("no free run id")

    def _prepared(self, rec: RunRecord, unit: Unit) -> RunRecord:
        folder = self.cfg.data_dir / "runs" / rec.id
        fields: dict[str, Any] = {"log": str(folder / "console.log")}
        if rec.kind == "workflow":
            fields.update(
                cap=self._new_cap(rec, unit),
                run_dir=str(unit.worktree.joinpath(*_RUN_FOLDER, rec.id)),
                hitl_dir=str(folder / "hitl"),
            )
        rec = dataclasses.replace(rec, **fields)
        return dataclasses.replace(rec, cli=self.cli_equivalent(rec))

    def _new_cap(self, rec: RunRecord, unit: Unit) -> float:
        """What a workflow run may spend: the run cap, at most what the unit has left (a fake run: the run cap, its
        credits are no model's; the harness still stops at its cap)."""
        return round(self.cfg.run_cap if rec.fake else min(self.cfg.run_cap, unit.left), 2)

    def command(self, rec: RunRecord) -> list[str]:
        """What is started for the run, in the unit worktree: on the config's runtime (``facilitator``; in personal
        mode ``copilot-user``, the participant's own login), with ``--fake`` for a fake run."""
        base = self.cfg.harness_argv
        if rec.kind == "workflow":
            argv = base + ["run", "--workflow", str(rec.workflow), "--ticket", str(rec.ticket), "--mode", rec.mode,
                           "--runtime", self.cfg.runtime, "--run-id", rec.id]
            return argv + ["--fake"] if rec.fake else argv
        return base + ["gates", "--profile", "fast", "--json"]

    def cli_equivalent(self, rec: RunRecord) -> str:
        """The command a participant would type for the same run (no run id, no environment)."""
        if rec.kind == "workflow":
            mode = ["--mode", rec.mode] if rec.mode != "interactive" else []
            words = ["run", "--workflow", str(rec.workflow), "--ticket", str(rec.ticket), *mode]
            words += ["--fake"] if rec.fake else []
        else:
            words = ["gates", "--profile", "fast"]
        return " ".join(["python", "-m", "harness", *words])

    # -- the queue --

    def _slot_free(self, lane: str) -> bool:
        going = sum(1 for active in self._active.values() if active.lane == lane)
        return bool(self._queues[lane]) and going < self.cfg.lanes.get(lane, 1)

    def _work(self, lane: str) -> None:
        while True:
            with self._cond:
                while not self._closing.is_set() and not self._slot_free(lane):
                    self._cond.wait()
                if self._closing.is_set():
                    return
                run_id = self._queues[lane].popleft()
                try:
                    rec = self.store.get(run_id)
                    active = self._active[run_id] = _Active(rec, self._cap_of(rec))
                except KeyError:
                    self._forget(run_id)
                    continue
            self._publish_queue(lane)
            try:
                self._launch(active)
            except Exception:
                log.exception("run %s could not be started", run_id)
                if active.proc is not None:  # started, then something after the start failed: no run goes unwatched
                    self._sweep(active.id, active.pid, active.started, active.proc)
                self._finalize(run_id, error="the run could not be started: see the console log",
                               touched=active.proc is not None or active.touched)

    def _launch(self, active: _Active) -> None:
        rec = self.store.get(active.id)
        if not self._lock_unit(rec):
            self._finalize(rec.id, error=f"{rec.unit} is busy: try again", touched=False)
            return
        self._charge_owed(rec.unit)  # an earlier run's charge that failed counts before the budget is looked at
        unit = self.units.get(rec.unit)  # with the lock held: the budget as it is now
        with self._cond:
            stop_by = active.stop_by
        if stop_by is not None:
            self._finalize(rec.id, stop_by=stop_by, touched=self._left_over(rec, unit))
            return
        if rec.kind == "workflow":
            if not rec.fake and (refusal := _budget_refusal(unit)):
                self._finalize(rec.id, status="refused", error=refusal, touched=self._left_over(rec, unit))
                return
            rec = dataclasses.replace(rec, cap=self._new_cap(rec, unit))
            active.count.cap = self._cap_of(rec)
        taken = None
        if rec.overlay_from:
            taken, refusal = self._take_overlay(rec, unit)
            if refusal:
                self._finalize(rec.id, status="refused", error=refusal, touched=self._left_over(rec, unit))
                return
        git = self.units.git_for(unit.id)
        failed = ""
        with worktree_lock(unit.worktree):  # a save under way lands first; a later one finds the unit taken (its guard)
            head = git("rev-parse", "--verify", "-q", f"refs/heads/{unit.branch}").strip()
            if taken is not None:
                failed = self._put_overlay(active, git, unit, rec, taken, head)
            # the unit is ours now, and a handover run's overlay is in place: the files are what the harness will read
            weakened = [] if failed else self._weakened(unit, git)
        if failed:
            self._finalize(rec.id, error=failed, touched=True)  # touched: the worktree is put back on unit/NN
            return
        Path(rec.log).parent.mkdir(parents=True, exist_ok=True)
        if rec.hitl_dir:
            Path(rec.hitl_dir).mkdir(parents=True, exist_ok=True)
        env = procs.run_env(os.environ, self._extra_env(rec))
        proc = self._spawn(self.command(rec), unit.worktree, env, Path(rec.log))
        with self._cond:
            active.proc, active.pid = proc, proc.pid  # first, so a failure from here on still kills it
        started = procs.start_time(proc.pid)
        with self._cond:
            active.started, active.status = started, "running"
            stop_by = active.stop_by
        rec = self.store.update(rec.id, status="running", pid=proc.pid, started_time=started, started_at=_now(),
                                cap=rec.cap, unit_head=head, weakened=weakened)
        log.info("run %s started for %s (pid %s)", rec.id, rec.unit, proc.pid)
        self._publish(rec)
        self._thread(self._watch, active, name=f"watch-{rec.id}")
        if stop_by is not None:
            self._sweep(active.id, active.pid, active.started, active.proc)

    def _check(self) -> Callable[[str, str], list[str]]:
        """The save rules (``validate.check`` bound to the golden config and the allowed models), read on first use."""
        if self._rules is None:
            golden = validate.load_golden(self.cfg.data_dir / "source", self.cfg.start_ref, also=self.cfg.catchup_refs)
            self._rules = functools.partial(validate.check, golden=golden, allowed_models=self.cfg.allowed_models)
        return self._rules

    def _left_over(self, rec: RunRecord, unit: Unit) -> bool:
        """Whether a handover run that ends before its process starts must put the worktree back: an earlier launch of
        it (a console that died in the middle) left ``run/<id>`` made or the worktree off ``unit/NN``. The unit lock is
        held. A run that is not a handover run never touched the worktree."""
        if not rec.overlay_from:
            return False
        git = self.units.git_for(unit.id)
        with worktree_lock(unit.worktree):
            if worktree_ops.head_branch(git) != unit.branch:
                return True
            try:
                git("rev-parse", "--verify", "-q", f"refs/heads/run/{rec.id}")
            except RuntimeError:  # exit 1: there is no such branch
                return False
            return True

    def _release_queued(self, rec: RunRecord) -> None:
        """A run stopped while queued: if a console that died left the unit locked by it (its launch got as far as
        taking the unit), free the unit. A handover run first puts the worktree back on ``unit/NN`` (or holds the unit
        for a reset); any other run never touched the worktree before its process started."""
        try:
            unit = self.units.get(rec.unit)
            if unit.locked_by != rec.id:  # the launch never took the unit
                return
            if not rec.overlay_from:
                self._unlock(rec)
                log.info("%s freed: run %s held it while queued", unit.id, rec.id)
                return
            git = self.units.git_for(unit.id)
            with worktree_lock(unit.worktree):
                wrap = worktree_ops.wrap_up(git, unit.worktree, unit.branch, self.cfg.data_dir / "runs" / rec.id,
                                            index_free=True, base=rec.unit_head)
            if wrap.restored:
                self._unlock(rec)
            else:
                self._hold(rec)
        except Exception:
            log.exception("run %s: its unit could not be freed", rec.id)

    def _take_overlay(self, rec: RunRecord, unit: Unit) -> tuple[handover.Overlay | None, str]:
        """The harness files of ``rec.overlay_from`` for the run of ``unit``, or why the run is refused (nothing is
        changed)."""
        try:
            source = self.units.get(str(rec.overlay_from))
        except KeyError:
            return None, f"unknown unit: {rec.overlay_from}"
        try:
            taken = handover.collect(source, unit, self.cfg.overlay, self.cfg.editable)
        except Exception:  # git, the source folder or a damaged listing: a refusal, so a left-over worktree is put back
            log.exception("run %s: the harness of %s could not be read", rec.id, source.id)
            return None, f"the harness of {source.id} could not be read: see the console log"
        bad = handover.problems(taken, self._check())
        if bad:
            return None, f"{source.id}'s harness does not pass the save rules: {'; '.join(bad)}"[:_ERROR_MAX]
        if not taken.files:
            return None, f"{source.id} has no harness files to take"
        return taken, ""

    def _put_overlay(
        self, active: _Active, git: Callable[..., str], unit: Unit, rec: RunRecord, taken: handover.Overlay, head: str
    ) -> str:
        """Make the overlay commit and check out ``run/<id>`` (the worktree lock is held); ``""`` when the worktree is
        ready, else why not (the caller finalizes the run as one that started, which puts the worktree back)."""
        branch = f"run/{rec.id}"
        message = (
            f"console: harness of {taken.source}\n\nConsole-Unit: {unit.id}\n"
            f"Overlay-Source: {taken.source} at {taken.commit}\n"
        )
        try:
            if worktree_ops.head_branch(git) != unit.branch:
                raise RuntimeError(f"{unit.id} is not on {unit.branch}")
            commit = files.commit_files(unit.worktree, taken.files, message, head)  # from bytes: no file written
            git("update-ref", f"refs/heads/{branch}", commit, "")  # "": the branch must not exist yet
            active.touched = True  # from here on the worktree has to be put back, whatever happens
            git("checkout", "-q", branch, "--")
            self.store.update(rec.id, unit_head=head, overlay_commit=commit, overlay_skipped=taken.skipped,
                              overlay_source_commit=taken.commit)
        except Exception:
            log.exception("run %s: the harness of %s could not be put in place", rec.id, taken.source)
            return f"the harness of {taken.source} could not be put in place: see the console log"
        return ""

    def _weakened(self, unit: Unit, git: Callable[..., str]) -> list[dict]:
        """The guardrails the unit's harness has given up now (``structure.weakening``). Never refuses or fails a run:
        when they cannot be worked out the run goes ahead unflagged, and the console log says so."""
        try:
            return structure.weakening(structure.read(unit.worktree), structure.golden(git, self.cfg.start_ref))
        except Exception:
            log.warning("run for %s: the weakening of its harness could not be worked out", unit.id, exc_info=True)
            return []

    def _lock_unit(self, rec: RunRecord) -> bool:
        for attempt in range(_LOCK_TRIES):
            try:
                self.units.lock(rec.unit, rec.id)
                return True
            except Busy:
                if attempt + 1 < _LOCK_TRIES:
                    time.sleep(_LOCK_WAIT_S)
        return False

    def _extra_env(self, rec: RunRecord) -> dict[str, str]:
        """What the console sets in a run's environment: values it computed itself, never a user's. In personal mode
        what the ladder decided at start adds (``console.ladder.env``): in lite ``TS_LITE=1`` and Docker hidden, so
        nothing builds; in full the console's own ``TS_LITE`` (``0`` on the stage: the harness never goes lite)."""
        extra = {"HARNESS_RUNTIME": self.cfg.runtime, "HARNESS_RUN_ID": rec.id, "PYTHONUTF8": "1"}
        if rec.kind == "workflow":
            extra.update(HARNESS_MAX_CREDITS_RUN=f"{rec.cap:g}", HARNESS_HITL_DIR=rec.hitl_dir)
        if self.cfg.personal:
            extra.update(ladder.env(self.cfg.ladder))
        return extra

    # -- watching --

    def _watch(self, active: _Active) -> None:
        try:
            rec = self.store.get(active.id)
            root = self.units.get(rec.unit).worktree
        except Exception:
            log.exception("run %s cannot be watched: stopping it", active.id)
            self._sweep(active.id, active.pid, active.started, active.proc)  # no run goes on unwatched
            self._finalize(active.id, stop_by="console", error="the run could not be watched: see the console log")
            return
        tail = _trail(root, rec.id) if rec.run_dir else None
        loops = events.LoopDetector(root=root)
        pending = Path(rec.hitl_dir) / "pending.json" if rec.hitl_dir else None
        while True:
            gone, code = self._exited(active)  # before the read, so the last read has every event
            try:
                if tail is not None:
                    self._relay(active, tail, loops, root)
                if pending is not None and not gone:
                    self._follow_pending(active, pending, root)
            except Exception:
                log.exception("run %s: watching failed for a moment", active.id)
            if gone:
                break
            if self._closing.wait(self._tick):
                return  # shutdown: the next start reconciles the run
        self._finalize(active.id, exit_code=code, stop_by=active.stop_by)

    @staticmethod
    def _exited(active: _Active) -> tuple[bool, int | None]:
        """Whether the run's process has ended, and its exit code when known (not for a run watched after a restart)."""
        if active.proc is not None:
            code = active.proc.poll()
            return code is not None, code
        return active.pid is None or not procs.alive(active.pid, active.started), None

    def _relay(self, active: _Active, tail: events.Tail, loops: events.LoopDetector, root: Path) -> None:
        topic = f"run:{active.id}"
        skipped = tail.skipped
        for ev in tail.read_new():
            try:  # one bad event never costs the rest of the chunk
                if active.count.add(ev):
                    self._keep_count(active)
                line = events.readable(ev, root=root)
                if line is not None and _seq(ev) is not None:  # an id out of range would hide the lines after it
                    self.broker.publish(topic, "line", line.to_dict(), id=line.seq)
                flag = loops.feed(ev)
                if flag is not None:
                    self.store.update(active.id, loop_flag=flag)
                    ts = ev.get("ts") if isinstance(ev.get("ts"), str) else ""
                    line = events.Line(active.count.seq, ts, "loop", flag, ok=False)
                    self.broker.publish(topic, "loop", line.to_dict())
            except Exception:
                log.exception("run %s: an event could not be relayed", active.id)
        if tail.skipped != skipped:  # a line too long to read could hide a turn the harness appended to it
            active.count.sound = False
        if not active.flagged and not active.fake and not active.count.believable():
            active.flagged = True
            self.store.update(active.id, accounting="capped")  # on the record: it holds across a restart
            log.warning("run %s: its audit trail cannot be trusted for accounting: it is charged its ceiling",
                        active.id)

    def _keep_count(self, active: _Active) -> None:
        """Put the live count on the record (one write per turn counted), so a restart does not lose it."""
        spent = active.count.total()
        if math.isfinite(spent):
            self.store.update(active.id, live_credits=round(spent, 4), live_seq=active.count.seq)

    def _follow_pending(self, active: _Active, path: Path, root: Path) -> None:
        doc = _read_pending(path)
        if isinstance(doc, dict) and doc["id"] != active.pending_id:
            doc = _scrubbed_question(doc, root)  # on the record and on the topic as a browser sees it
            active.pending_id, active.status = doc["id"], "waiting"
            rec = self.store.update(active.id, status="waiting", pending=doc)
            # the question as the record's view has it: an open page paints its card from this event, so it needs the
            # personal credit cap's words (extend_credits and budget, or fake) too
            shown = self.view(rec)["pending"]
            self.broker.publish(f"run:{active.id}", "approval", shown,
                                admin={**shown, "allowed_options": allowed_options(doc, "admin")})
            self._publish(rec)
        elif doc is None and active.status == "waiting":
            active.status = "running"
            self._publish(self.store.update(active.id, status="running", pending=None))

    # -- the end of a run --

    def _finalize(
        self,
        run_id: str,
        *,
        exit_code: int | None = None,
        stop_by: str | None = None,
        reconciled: bool = False,
        status: str | None = None,
        error: str = "",
        touched: bool = True,
    ) -> None:
        """Settle a run, once (see the module doc). ``touched`` is False for a run whose process never started;
        ``status`` forces the status (a run refused as it was about to start)."""
        with self._cond:
            active = self._active.get(run_id)
            if active is not None:
                if active.finalizing:
                    return
                active.finalizing = True
        progress = _Progress(started=touched, restored=not touched)  # a run never started left nothing to put back
        try:
            self._settle(run_id, active, exit_code, stop_by, reconciled, status, error, progress)
        except Exception:
            log.exception("run %s could not be finalized", run_id)
            self._settle_anyway(run_id, active, progress)
        finally:
            with self._cond:
                self._forget(run_id)
            if active is not None:
                active.done.set()

    def _settle(
        self,
        run_id: str,
        active: _Active | None,
        exit_code: int | None,
        stop_by: str | None,
        reconciled: bool,
        forced: str | None,
        error: str,
        progress: _Progress,
    ) -> None:
        rec = self.store.get(run_id)
        if rec.status in FINAL:
            log.warning("run %s was settled already (%s): left as it is", run_id, rec.status)
            progress.restored = progress.charged = True
            return
        unit = self.units.get(rec.unit)
        pid, started = (active.pid, active.started) if active is not None else (rec.pid, rec.started_time)
        if progress.started:
            self._sweep(run_id, pid, started, active.proc if active is not None else None)
        status, summary, credits, seq, accounting = self._outcome(
            rec, unit.worktree, active, exit_code, stop_by, reconciled, forced
        )
        progress.outcome, progress.status, progress.summary = (credits, accounting), status, summary
        result: dict[str, Any] = {}
        if progress.started:
            git = self.units.git_for(unit.id)  # before the worktree lock: no wait on the UnitStore lock inside it
            with worktree_lock(unit.worktree):
                wrap = worktree_ops.wrap_up(git, unit.worktree, unit.branch, self.cfg.data_dir / "runs" / run_id,
                                            index_free=pid is None or not procs.alive(pid, started),
                                            base=rec.unit_head)
            progress.restored = wrap.restored
            result = {"branch": wrap.branch, "commit": wrap.commit, "patch": wrap.patch}
            error = "; ".join(part for part in (error, *wrap.problems) if part)
        if status == "error" and not error:
            code = f" (exit {exit_code})" if exit_code is not None else ""
            error = f"the run ended without a result{code}: see its log"
        progress.result = {**result, "error": error}
        if not progress.restored:
            self._hold(rec)  # before the final record: a crash in between must not leave a dirty unit free
        # Before the final record too, which says whether it is charged. A charge that fails is owed: the run keeps its
        # status and result, and the charge is made at the unit's next launch (or at a restart).
        self._attempt(run_id, "credits", lambda: self._charge(rec.unit, credits, progress))
        rec = self.store.update(run_id, ended_at=_now(), credits=credits, pending=None, accounting=accounting,
                                charged=progress.charged, **self._known(progress))
        self._owe(rec, progress)
        self._free(rec, progress)
        log.info("run %s ended: %s, %s credits%s%s", run_id, status, f"{credits:g}",
                 " (capped)" if accounting else "", f" ({error})" if error else "")
        end = _end_line(rec, seq)
        self.broker.publish(f"run:{run_id}", "line", end.to_dict(), id=end.seq)
        self._publish(rec)

    def _settle_anyway(self, run_id: str, active: _Active | None, progress: _Progress) -> None:
        """After a failed finalize: the status, summary and result ``_settle`` had worked out (an ``error`` status when
        it had not got as far as the result) unless one was settled, a believable charge (else cap + ``OVERSHOOT``),
        the unit released or held for a reset. Each step on its own: one failing never keeps another from happening.
        A charge that fails here too is owed (``_owe``)."""
        try:
            rec = self.store.get(run_id)
        except Exception:
            log.exception("run %s: its record cannot be read", run_id)
            return
        if not progress.restored:
            self._attempt(run_id, "unit", lambda: self._hold(rec))  # before the record, as in _settle
        if rec.status not in FINAL:
            amount, accounting = self._fallback_amount(rec, active, progress)
            self._attempt(run_id, "credits", lambda: self._charge(rec.unit, amount, progress))
            try:
                rec = self.store.update(run_id, ended_at=_now(), pending=None, credits=amount, accounting=accounting,
                                        charged=progress.charged, **self._known(progress))
            except Exception:
                log.exception("run %s: its record could not be settled", run_id)
        elif not rec.charged and not progress.charged:
            self._attempt(run_id, "credits", lambda: self._charge(rec.unit, rec.credits, progress))
            if progress.charged:
                self._attempt(run_id, "record", lambda: self.store.update(run_id, charged=True))
        if progress.charged:  # owed no more (``_settle`` may have owed it), even if the record cannot say so
            with self._cond:
                self._owed.get(rec.unit, set()).discard(run_id)
        self._owe(rec, progress)
        self._attempt(run_id, "unit", lambda: self._free(rec, progress))
        self._attempt(run_id, "record", lambda: self._publish(rec))

    @staticmethod
    def _known(progress: _Progress) -> dict[str, Any]:
        """The fields of the final record that ``_settle`` worked out: the run's status, summary and result (branch,
        commit, patch, error) once it got as far as the result; before that an ``error`` status (with the summary, if
        it was read)."""
        known: dict[str, Any] = {} if progress.summary is None else {"summary": progress.summary}
        if progress.status is not None and progress.result is not None:
            return {**known, "status": progress.status, **progress.result}
        return {**known, "status": "error", "error": "the run could not be finalized: see the console log"}

    def _owe(self, rec: RunRecord, progress: _Progress) -> None:
        """A run whose final record says it is not charged: its charge is tried again at the unit's next launch
        (``_charge_owed``), and at a restart (``_reconcile``)."""
        if rec.status not in FINAL or rec.charged or progress.charged:
            return
        with self._cond:
            owed = self._owed.setdefault(rec.unit, set())
            new = rec.id not in owed
            owed.add(rec.id)
        if new:
            log.error("run %s: its %s credits are not charged to %s yet: tried again at its next run or a restart",
                      rec.id, f"{rec.credits:g}", rec.unit)

    def _charge_owed(self, unit_id: str) -> None:
        """Charge the unit what its ended runs owe (``_owe``), once each; one that fails again stays owed. Only its
        ``_launch`` calls this, with the unit lock held (a restart charges from the records: ``_reconcile``)."""
        with self._cond:
            owed = sorted(self._owed.pop(unit_id, ()))
        for run_id in owed:
            try:
                rec = self.store.get(run_id)
                amount = _credit(rec.credits) if rec.status in FINAL and not rec.charged else None
                if amount:
                    self.units.add_spent(rec.unit, amount)
            except Exception:
                log.exception("run %s: its credits could not be charged: tried again at the next run", run_id)
                with self._cond:
                    self._owed.setdefault(unit_id, set()).add(run_id)
                continue
            # charged (or nothing to charge): never again in this console, even if the record cannot say so
            if amount:
                log.info("run %s: its %s credits charged to %s", run_id, f"{amount:g}", rec.unit)
                self._attempt(run_id, "record", functools.partial(self.store.update, run_id, charged=True))

    @staticmethod
    def _attempt(run_id: str, what: str, act: Callable[[], Any]) -> None:
        try:
            act()
        except Exception:
            log.exception("run %s: its %s could not be settled", run_id, what)

    def _fallback_amount(self, rec: RunRecord, active: _Active | None, progress: _Progress) -> tuple[float, str | None]:
        """What a failed finalize charges: what ``_settle`` had worked out, if it got that far; else the live count
        when it is believable (finite, at most the ceiling), else the ceiling (also when a workflow run started and
        nothing was counted)."""
        if progress.outcome is not None:
            return progress.outcome
        if rec.fake:  # no model: nothing to charge, whatever was counted
            return 0.0, None
        cap = self._cap_of(rec)
        count = active.count if active is not None else _Count(cap, rec.live_seq, rec.live_credits)
        spent = count.total()
        believable = count.believable() and rec.accounting != "capped" and _believable(spent, cap)
        if rec.kind == "workflow" and progress.started and (not believable or not spent):
            return _anomaly_charge(cap), "capped"
        return (round(spent, 4) if believable else 0.0), rec.accounting

    def _charge(self, unit_id: str, amount: float, progress: _Progress) -> None:
        """Add the run's credits to the unit, once."""
        if not progress.charged:
            if amount:
                self.units.add_spent(unit_id, amount)
            progress.charged = True

    def _free(self, rec: RunRecord, progress: _Progress) -> None:
        if progress.restored:
            self._unlock(rec)

    def _unlock(self, rec: RunRecord) -> None:
        """The worktree is back: free the unit (also from a hold of this run's, a finalize that crashed before)."""
        self.units.unlock(rec.unit, rec.id)
        self.units.unlock(rec.unit, NEEDS_RESET + rec.id)

    def _hold(self, rec: RunRecord) -> None:
        """The worktree could not be put back: the unit stays locked until a facilitator reset."""
        self.units.hold_for_reset(rec.unit, rec.id)
        log.error("%s stays locked (needs a reset): run %s could not put its worktree back", rec.unit, rec.id)

    def _outcome(
        self,
        rec: RunRecord,
        root: Path,
        active: _Active | None,
        exit_code: int | None,
        stop_by: str | None,
        reconciled: bool,
        forced: str | None,
    ) -> tuple[str, dict, float, int, str | None]:
        """Status, summary, credits, the highest audit seq and the accounting (``capped`` or ``None``) of an ended run.

        A workflow run's credits are the most of summary.json's and of the turns, per seq the most that any line of
        the trail or the watcher saw, and at least the live count kept on the record: charged in full when they are
        believable (at most the ceiling: the cap with its extensions, plus ``OVERSHOOT``). Any anomaly charges the
        ceiling and marks the run ``capped``: summary.json or the trail refused or unsound (``_Count``), the trail now
        ending before a seq the watcher saw (lines taken out), the record already flagged by the watcher, or credits
        past the ceiling.
        """
        ended = forced or ("stopped" if stop_by is not None or reconciled else None)
        accounting = None
        if rec.kind == "workflow":
            summary, summary_sound = _summary(root, rec.id)
            said = summary.get("status")
            status = ended or (_FROM_SUMMARY.get(said, "error") if said is not None else "error")
            cap = self._cap_of(rec)
            trail = _read_trail(root, rec.id, cap)
            live = active.count if active is not None else _Count(cap, rec.live_seq, rec.live_credits)
            count = trail.merged(live)
            credits = max(summary.get("credits", 0.0), count.total())
            seq = count.seq
            sound = summary_sound and count.believable() and trail.seq >= live.seq and rec.accounting != "capped"
            if rec.fake:  # the fake model: what its runtime counts is no model's, nothing is charged
                credits = 0.0
            elif not sound or not _believable(credits, cap):
                credits, accounting = _anomaly_charge(cap), "capped"
                log.warning("run %s: its run files cannot be trusted for accounting: charged its ceiling, %s",
                            rec.id, f"{credits:g}")
            credits = round(credits, 4)
        else:
            summary = _gates_summary(Path(rec.log)) if rec.log else {}
            ok = summary.get("ok")
            if ended is not None:
                status = ended
            elif exit_code is not None:
                status = {0: "done", 1: "failed"}.get(exit_code, "error")
            else:
                status = "error" if ok is None else "done" if ok else "failed"
            credits, seq = 0.0, 0
        if exit_code is not None:
            summary["exit_code"] = exit_code
        return status, summary, credits, seq, accounting

    def _sweep(self, run_id: str, pid: int | None, started: float | None, proc: subprocess.Popen | None) -> None:
        """Kill what is left of a run: its process tree (never by a bare pid), then its gate containers."""
        try:
            if pid is not None and started is not None:
                procs.kill_tree(pid, started=started)  # the int pid: an orphaned group is swept too
            elif proc is not None:
                procs.kill_tree(proc)
        except (ValueError, OSError):
            log.warning("run %s: its process could not be killed", run_id, exc_info=True)
        try:
            procs.kill_containers(run_id)
        except (ValueError, OSError):
            log.warning("run %s: its containers could not be killed", run_id, exc_info=True)

    def position(self, rec: RunRecord) -> int | None:
        """Where a queued run is in its lane's queue (1 = next); ``None`` for a run that is not queued."""
        with self._cond:
            queue = self._queues.get(rec.lane, ())
            return list(queue).index(rec.id) + 1 if rec.status == "queued" and rec.id in queue else None

    def queue(self) -> dict[str, Any]:
        """The runs queued or going, as the public board shows them (the same fields as the ``global`` topic and
        no more): ``{"lanes": {lane: {limit, going, queued}}, "runs": [{id, unit, kind, lane, status, position}]}``.

        Going runs come first (a run still being started counts as ``running``), then the queued ones in the order
        of their lane's queue; ``position`` is the place in the queue (1 = next), ``None`` for a run that goes. A run
        that is over but not yet finalized holds its lane's slot (it counts as going) and is not listed.
        """
        with self._cond:
            going = [(active.id, active.lane) for active in self._active.values()]
            queued = [(run_id, lane, place) for lane, queue in self._queues.items()
                      for place, run_id in enumerate(queue, 1)]  # fmt: skip
            lanes = {lane: {"limit": self.cfg.lanes.get(lane, 1),
                            "going": sum(1 for _, going_lane in going if going_lane == lane), "queued": len(queue)}
                     for lane, queue in self._queues.items()}  # fmt: skip
        runs = []
        for run_id, lane, place in [(run_id, lane, None) for run_id, lane in going] + queued:
            try:
                rec = self.store.get(run_id)
            except KeyError:
                continue
            if rec.status in FINAL:
                continue
            status = "running" if place is None and rec.status == "queued" else rec.status
            runs.append({"id": rec.id, "unit": rec.unit, "kind": rec.kind, "lane": lane, "status": status,
                         "position": place})  # fmt: skip
        return {"lanes": lanes, "runs": runs}

    def _cap_of(self, rec: RunRecord) -> float:
        """The credits the run may spend: its cap, and ``extension`` for each facilitator continue at a credit cap."""
        extensions = rec.extensions if isinstance(rec.extensions, int) and rec.extensions > 0 else 0
        return rec.cap + extensions * self.extension

    def view(self, rec: RunRecord, by: str | None = None) -> dict[str, Any]:
        """The record as ``by`` sees it in a browser: ``RunRecord.view``, its error and the text of a pending question
        without host paths (``events.scrub``; also for a record written before questions were scrubbed) and its place
        in the queue."""
        out = {**rec.view(by, self.cfg.personal), "position": self.position(rec)}
        root = self._root(rec) if rec.error or rec.pending is not None else None
        if rec.error:
            out["error"] = events.scrub(rec.error, root)
        if rec.pending is not None:
            out["pending"] = _scrubbed_question(out["pending"], root)
            if self.cfg.personal and _credit_cap_question(rec.pending):  # what the card tells the participant
                extra = {"fake": True} if rec.fake else {"extend_credits": self.extension,
                                                         "budget": self.units.get(rec.unit).budget}
                out["pending"] = {**out["pending"], **extra}  # a fake run costs nothing: no numbers on its card
        return out

    def _root(self, rec: RunRecord) -> Path | None:
        """The run's unit worktree; ``None`` when the unit cannot be read (a text is then scrubbed without it)."""
        try:
            return self.units.get(rec.unit).worktree
        except (KeyError, OSError, ValueError):
            return None

    def _publish(self, rec: RunRecord) -> None:
        view = self.view(rec)
        self.broker.publish("global", "run", rec.public(view["position"]))
        admin = None
        if rec.pending is not None:  # the admin may answer more
            admin = {**view, "pending": {**view["pending"], "allowed_options": allowed_options(rec.pending, "admin")}}
        self.broker.publish(f"run:{rec.id}", "run", view, admin=admin)

    def _publish_queue(self, lane: str) -> None:
        """Every queued run of the lane with its new place."""
        with self._cond:
            waiting = list(self._queues[lane])
        for run_id in waiting:
            with contextlib.suppress(KeyError):
                self._publish(self.store.get(run_id))

    # -- what a browser reads --

    def settled(self, run_id: str) -> bool:
        """Whether the run is over and everything its end publishes is out: its record is final and no finalize of it
        is under way. ``KeyError`` for an unknown run."""
        rec = self.store.get(run_id)  # first: a finalize writes the final record before it lets go of the run
        with self._cond:
            return rec.status in FINAL and run_id not in self._active

    def lines(self, run_id: str, after: int = 0, *, ended: bool = False) -> Iterator[events.Line]:
        """The readable lines of the run's audit trail with a seq above ``after``, each above the one before it.

        The trail is read as the watcher reads it (a plain file, 4 MB a read, at most ``_REPLAY_MAX`` bytes; no event
        with a seq out of range) and its text made with the same root, so a line has the id and text the run's topic
        gave it. ``ended``: the run was
        ``settled`` before this call, so its end line was published before; it comes last, with the id its finalize
        gave it (one past the highest seq of the trail), unless ``after`` is that far already. ``KeyError`` for an
        unknown run.
        """
        rec = self.store.get(run_id)
        highest, seq = after, 0
        if rec.run_dir:
            root = self.units.get(rec.unit).worktree
            for ev in _replay(root, rec.id):
                if (ev_seq := _seq(ev)) is None:  # never relayed either: an id out of range hides what follows
                    continue
                seq = max(seq, ev_seq)
                line = events.readable(ev, root=root)
                if line is not None and line.seq > highest:
                    highest = line.seq
                    yield line
        if ended:
            end = _end_line(rec, seq)
            if end.seq > highest:
                yield end

    def progress(self, rec: RunRecord) -> dict | None:
        """``events.progress`` of the run's audit trail (read as ``lines`` reads it), every text in it without host
        paths (``events.scrub``: a step name, a gate summary).

        A gates or warm run has no trail: its ladder (the ``summary`` text the harness printed, without host paths) is
        the one gate attempt of an otherwise empty progress, once it has ended with one. ``None`` for any other run
        without a trail.
        """
        if not rec.run_dir:
            text = rec.summary.get("summary") if rec.kind != "workflow" else None
            if not isinstance(text, str) or not text:
                return None
            ok = rec.summary.get("ok")
            shown = events.progress([])
            shown["gates"] = [{"step": None, "attempt": 1, "summary": events.scrub(text, self._root(rec)),
                               "ok": ok if isinstance(ok, bool) else None}]  # fmt: skip
            return shown
        root = self.units.get(rec.unit).worktree
        return _scrubbed_all(events.progress(_replay(root, rec.id)), functools.partial(events.scrub, root=root))

    def artifact(self, rec: RunRecord, name: str) -> str | None:
        """``plan.md`` or ``review.md`` of the run folder: a plain file (no link, one name) of at most 200 KB, else
        ``None``; without host paths (``events.scrub``: the harness writes what the model said, paths and all)."""
        if name not in _ARTIFACTS:
            raise ValueError(f"not a run artifact: {name}")
        if not rec.run_dir:
            return None
        root = self.units.get(rec.unit).worktree
        data = worktree_ops.read_plain(root, (*_RUN_FOLDER, rec.id, name), _ARTIFACT_MAX)
        return None if data is None else events.scrub(data.decode("utf-8", errors="replace"), root)

    def diff(self, rec: RunRecord) -> dict | None:
        """What the run changed, as ``{source, text, truncated}`` (at most 500 KB of the text); ``None`` for nothing.

        ``source`` is ``commit`` for a run that left a commit: the diff from where ``unit/NN`` was when it started (git,
        no external diff or textconv), else ``patch``: the patch finalize saved of what it left uncommitted. ``None``
        also when git or the disk cannot give it (logged; with the traceback the first time for a run). A handover run
        is diffed from its overlay commit, so the harness it took is not in the diff; a commit that is the overlay
        commit itself (the agent committed nothing) counts as no commit.
        """
        overlay = rec.overlay_commit
        overlay = overlay if isinstance(overlay, str) and _SHA.fullmatch(overlay) else None
        if isinstance(rec.commit, str) and _SHA.fullmatch(rec.commit) and rec.commit != overlay:
            head = rec.unit_head
            base = overlay or (head if isinstance(head, str) and _SHA.fullmatch(head) else f"{rec.commit}^")
            try:
                text = self.units.git(rec.unit, "diff", "--no-color", "--no-ext-diff", "--no-textconv", base,
                                      rec.commit, "--")
            except RuntimeError:
                log.warning("run %s: its diff could not be made", rec.id, exc_info=_first_time("diff", rec.id))
                return None
            return _clipped("commit", text.encode("utf-8"))
        if rec.patch:
            try:
                with open(rec.patch, "rb") as fh:
                    data = fh.read(_DIFF_MAX + 1)
            except OSError:
                log.warning("run %s: its patch could not be read", rec.id, exc_info=_first_time("patch", rec.id))
                return None
            return _clipped("patch", data)
        return None

    # -- what people do with a run --

    def answer(self, run_id: str, answer: str, by: str) -> None:
        """Answer the question the run waits on, as ``by`` (``unit-NN`` or ``admin``).

        ``ValueError`` when it waits on none, for an answer not offered, or for a question answered already (the
        harness has not taken the answer yet); ``FacilitatorOnly`` (a ``ValueError``) for an answer only the admin may
        give (``allowed_options``): nothing is answered then. A ``continue`` at a credit cap raises the run's ceiling by
        ``extension``: ``extensions`` on the record goes up before the answer is written (the harness spends the
        credits only once it has the answer), and back down if the answer cannot be written.
        """
        with self._answering:
            rec = self.store.get(run_id)
            hitl = Path(rec.hitl_dir) if rec.hitl_dir else None
            doc = _read_pending(hitl / "pending.json") if rec.status == "waiting" and hitl is not None else None
            if hitl is None or not isinstance(doc, dict):
                raise ValueError(f"run {run_id} is not waiting for an answer")
            if answer not in doc["options"]:
                raise ValueError(f"the answer must be one of: {', '.join(doc['options'])}")
            if answer not in allowed_options(doc, by, self.cfg.personal):
                raise FacilitatorOnly("only the facilitator can extend a run")
            if _answered(hitl / "answer.json", doc["id"]):
                raise ValueError(f"run {run_id} has its answer already")
            extends = answer == "continue" and _credit_cap_question(doc)
            if extends and self.cfg.personal and not rec.fake:  # the day's budget must cover what this continue may spend
                refusal = self._personal_refusal(rec)
                if refusal:
                    raise ValueError(refusal)
            if extends:
                rec = self._extend(rec, 1)
            try:
                _write_json(hitl / "answer.json", {"id": doc["id"], "answer": answer})
            except BaseException:
                if extends:
                    self._extend(rec, -1)
                raise
        log.info("run %s: %s answered %s at %s%s", run_id, by, answer, doc["step"],
                 f" (ceiling raised by {self.extension:g} credits)" if extends else "")

    def _personal_refusal(self, rec: RunRecord) -> str:
        """Why a participant's continue at a credit cap is refused: today's budget, less what this run has used (not
        charged before it ends), cannot cover ``extension`` more credits. ``""`` when it can."""
        left = self.units.get(rec.unit).left - max(0.0, rec.live_credits)
        if left >= self.extension:
            return ""
        return (f"budget left {max(0.0, round(left, 2)):g} of {self.units.get(rec.unit).budget:g} credits today: "
                f"not enough for {self.extension:g} more. Stop the run instead")

    def _extend(self, rec: RunRecord, step: int) -> RunRecord:
        """``extensions`` of the run up (or back down) by ``step``, on the record and in the watcher's live count."""
        rec = self.store.update(rec.id, extensions=max(0, rec.extensions + step))
        with self._cond:
            active = self._active.get(rec.id)
            if active is not None:
                active.count.cap = self._cap_of(rec)
        return rec

    def stop(self, run_id: str, by: str) -> None:
        """Stop a queued or going run: kill its process tree and gate containers, then wait until it is finalized.

        ``ValueError`` for a run that is not queued or going; ``KeyError`` for an unknown run.
        """
        with self._cond:
            rec = self.store.get(run_id)
            queue = self._queues.get(rec.lane)
            active = None
            if queue is not None and run_id in queue:
                queue.remove(run_id)
                rec = self.store.update(run_id, status="stopped", ended_at=_now())
                self._forget(run_id)
            else:
                active = self._active.get(run_id)
                if active is None:
                    raise ValueError(f"run {run_id} is not running")
                active.stop_by = by
                pid, started, proc = active.pid, active.started, active.proc
        log.info("run %s: stopped by %s", run_id, by)
        if active is None:
            self._release_queued(rec)
            self._publish(rec)
            self._publish_queue(rec.lane)
            return
        if pid is not None:
            self._sweep(run_id, pid, started, proc)
        if not active.done.wait(_STOP_WAIT_S):
            log.warning("run %s: not finalized %s s after the stop", run_id, f"{_STOP_WAIT_S:g}")

    def keep(self, run_id: str) -> str:
        """Fast-forward ``unit/NN`` to the commit of a ``done`` run; returns the new head.

        ``ValueError`` when the run has nothing to keep (or is being kept), the unit has another run queued or going,
        or ``unit/NN`` moved on since; ``units.Busy`` when something else holds the unit.
        """
        with self._cond:
            rec = self.store.get(run_id)
            keepable = isinstance(rec.commit, str) and bool(_SHA.fullmatch(rec.commit))
            if rec.status != "done" or not rec.branch or not keepable:
                raise ValueError(f"run {run_id} has no result to keep")
            if rec.overlay_from:  # its commit holds the other unit's files, which the diff the unit saw does not
                raise ValueError(
                    f"run {run_id} used the harness of {rec.overlay_from}: its result stays on {rec.branch}, "
                    f"it is not put on {rec.unit}'s branch"
                )
            if run_id in self._keeping:
                raise ValueError(f"run {run_id} is being kept already")
            other = self._open.get(rec.unit)
            if other is not None:
                raise ValueError(f"{rec.unit} has run {other} going: keep this one after it ends")
            self._keeping.add(run_id)
        try:
            unit = self.units.get(rec.unit)
            git = self.units.git_for(unit.id)  # before the worktree lock
            self.units.lock(unit.id, rec.id)
            try:
                with worktree_lock(unit.worktree):
                    if worktree_ops.head_branch(git) != unit.branch:
                        raise ValueError(f"{unit.id} is not on {unit.branch}")
                    try:
                        git("merge", "--ff-only", "-q", rec.commit)
                    except RuntimeError:
                        raise ValueError(
                            f"{unit.branch} has moved on since the run: its commit cannot be taken as it is"
                        ) from None
                    head = git("rev-parse", "HEAD").strip()
            finally:
                self.units.unlock(unit.id, rec.id)
            with self._cond:
                rec = self.store.update(run_id, kept=True)
        finally:
            with self._cond:
                self._keeping.discard(run_id)
        self._publish(rec)
        log.info("run %s kept: %s is at %s", run_id, unit.branch, head)
        return head

    def discard(self, run_id: str) -> None:
        """Mark an ended run's result as not taken. ``ValueError`` for a run that has not ended, or is (being) kept."""
        with self._cond:
            rec = self.store.get(run_id)
            if rec.status not in FINAL or rec.status == "refused":
                raise ValueError(f"run {run_id} has no result to discard")
            if rec.kept or run_id in self._keeping:
                raise ValueError(f"run {run_id} is kept already")
            rec = self.store.update(run_id, kept=False)
        self._publish(rec)

    # -- after a restart --

    def _reconcile(self) -> list[str]:
        """Bring the records in line with what really runs (``start`` does this first); returns the runs it stopped.

        A ``running`` or ``waiting`` run whose process still runs (same pid and start time) is watched again. One
        whose process is gone, or whose start time was never known (a bare pid is never trusted), is finalized as
        ``stopped``: what is left of it is swept, its credits come from the audit trail and the live count on its
        record. A ``queued`` run goes back in its lane's queue. An ended run whose credits are not on its unit yet (a
        finalize that failed before it could charge) is charged now, once; one whose charge fails again is owed
        (``_owed``: charged at the unit's next launch). A unit still locked by a run that has ended is unlocked, and so
        is one locked by a queued run that is not a handover run (its launch took the unit, then the console died
        before the process started: the launch takes it again). A queued handover run keeps the unit: its launch, or
        its stop, puts the worktree back first.
        """
        stopped = []
        for rec in sorted(self.store.list(), key=lambda r: (r.queued_at, r.id)):
            with self._cond:
                if rec.id in self._active or any(rec.id in queue for queue in self._queues.values()):
                    continue
            if rec.status in ACTIVE:
                if rec.pid is not None and rec.started_time is not None and procs.alive(rec.pid, rec.started_time):
                    active = _Active(rec, self._cap_of(rec))
                    with self._cond:
                        self._active[rec.id] = active
                        self._open[rec.unit] = rec.id
                    log.info("run %s still runs (pid %s): watching it again", rec.id, rec.pid)
                    self._thread(self._watch, active, name=f"watch-{rec.id}")
                    continue
                self._finalize(rec.id, reconciled=True)
                stopped.append(rec.id)
            elif rec.status == "queued" and rec.lane in self._queues:
                with self._cond:
                    self._queues[rec.lane].append(rec.id)
                    self._open[rec.unit] = rec.id
                    self._cond.notify_all()
            elif rec.status in FINAL and not rec.charged and (amount := _credit(rec.credits)):
                try:
                    self.units.add_spent(rec.unit, amount)
                except Exception:
                    log.exception("run %s: its credits could not be charged: tried again at the unit's next run",
                                  rec.id)
                    with self._cond:
                        self._owed.setdefault(rec.unit, set()).add(rec.id)
                    continue
                log.info("run %s: its %s credits charged to %s after a restart", rec.id, f"{amount:g}", rec.unit)
                self._attempt(rec.id, "record", functools.partial(self.store.update, rec.id, charged=True))
        for unit in self.units.all():
            holder = unit.locked_by
            with self._cond:
                if holder is None or holder in self._active:
                    continue
            with contextlib.suppress(KeyError):  # a needs-reset lock is not a run id: it stays
                held = self.store.get(holder)
                if held.status in FINAL:
                    self.units.unlock(unit.id, holder)
                    log.info("%s was still locked by run %s, which has ended: unlocked", unit.id, holder)
                elif held.status == "queued" and not held.overlay_from:
                    self.units.unlock(unit.id, holder)
                    log.info("%s was locked by run %s, which is queued again: unlocked", unit.id, holder)
        return stopped
