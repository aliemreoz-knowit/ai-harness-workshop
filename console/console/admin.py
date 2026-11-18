"""What the facilitator's admin page needs besides the stores it drives: the checks of its requests, the doctor's checks
in a thread of their own, warming the build caches and the unit cards. No HTTP here (``routes_admin`` has it).

* ``check_unit_change`` is the one place that says what ``{budget_add, level, name}`` may hold; a request with one bad
  field changes nothing.
* ``DoctorJob`` runs ``doctor.run_checks`` (it can take a minute: the harness check starts the Copilot CLI) in a daemon
  thread, one run at a time; the page asks for the state, so a closed page or a second click costs nothing.
* ``warm_caches`` queues one ``warm`` run per unit that is free (the runner puts it on the gates lane).
* ``unit_cards`` / ``write_unit_cards`` make ``<data>/unit-cards.txt``: the codes and where to open the console, for
  printing. The file is private to its owner where the OS supports it. The admin code is never in it.

``doctor`` imports the server, and the server imports the modules of the routes, which import this one: so this module
imports ``doctor`` inside the functions that need it.
"""

from __future__ import annotations

import logging
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import TYPE_CHECKING, Any, Mapping, Sequence

from console.atomic import atomic_write
from console.config import ConsoleConfig
from console.runs import ACTIVE
from console.units import NEEDS_RESET

if TYPE_CHECKING:
    from console.runs import Runner

__all__ = [
    "CARDS_FILE", "MAX_BUDGET_ADD", "MAX_LEVEL", "MAX_NAME", "DoctorJob", "check_unit_change", "unit_cards",
    "warm_caches", "write_unit_cards",
]  # fmt: skip

log = logging.getLogger(__name__)

MAX_BUDGET_ADD = 1000  # credits one top-up may add
MAX_LEVEL = 5  # the levels of the workshop run 0 to 5
MAX_NAME = 40
UNIT_FIELDS = ("budget_add", "level", "name")
CARDS_FILE = "unit-cards.txt"
CRASHED = "the checks could not run: see the console log"

_OPEN = ACTIVE | {"queued"}  # a run that is queued or going


# --- the checks of a unit change -------------------------------------------------------------------------------


def check_unit_change(body: Mapping[str, Any]) -> dict[str, Any]:
    """The change a request asks for, checked: ``budget_add`` (a number above 0 and at most ``MAX_BUDGET_ADD``),
    ``level`` (a whole number from 0 to ``MAX_LEVEL``), ``name`` (1 to ``MAX_NAME`` printable characters, stripped).

    ``ValueError`` with a sentence for the person: a field that is not one of those, a value that is wrong (``null``
    included: it is not "not given"; ``NaN`` and infinity fail the range, and so does an integer too large for a
    float), or nothing to change.
    """
    unknown = sorted(str(key)[:40] for key in body if key not in UNIT_FIELDS)
    if unknown:
        raise ValueError(f"unknown field: {', '.join(unknown)}")
    change: dict[str, Any] = {}
    if "budget_add" in body:
        credits = body["budget_add"]
        if isinstance(credits, bool) or not isinstance(credits, (int, float)) or not 0 < credits <= MAX_BUDGET_ADD:
            raise ValueError(f"budget_add must be a number above 0 and at most {MAX_BUDGET_ADD}")
        change["budget_add"] = float(credits)
    if "level" in body:
        level = body["level"]
        if isinstance(level, bool) or not isinstance(level, int) or not 0 <= level <= MAX_LEVEL:
            raise ValueError(f"level must be a whole number from 0 to {MAX_LEVEL}")
        change["level"] = level
    if "name" in body:
        name = body["name"]
        if not isinstance(name, str):
            raise ValueError("name must be text")
        name = name.strip()
        if not name or len(name) > MAX_NAME or not name.isprintable():
            raise ValueError(f"name must be 1 to {MAX_NAME} printable characters")
        change["name"] = name
    if not change:
        raise ValueError("nothing to change: give budget_add, level or name")
    return change


# --- the doctor ------------------------------------------------------------------------------------------------


def _utc_iso() -> str:
    return datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


class DoctorJob:
    """The doctor's checks of one console, run in a thread of their own, one run at a time.

    ``start`` begins a run unless one is going, and answers ``state`` at once. ``state`` is
    ``{state, started_at, finished_at, error, checks}``: ``state`` is ``idle`` (never run), ``running`` or ``done``;
    ``checks`` the rows ``{name, status, detail, fix}`` of the last run that finished (while a new run goes they are
    still the old ones; ``None`` before the first, and after a run that crashed: ``error`` says that, in plain words, and
    the details are in the log).
    """

    def __init__(self, cfg: ConsoleConfig) -> None:
        self._cfg = cfg
        self._lock = threading.Lock()
        self._running = False
        self._started: str | None = None
        self._finished: str | None = None
        self._error: str | None = None
        self._checks: list[dict[str, str]] | None = None

    def start(self) -> dict[str, Any]:
        with self._lock:
            if not self._running:
                self._running, self._started, self._finished, self._error = True, _utc_iso(), None, None
                threading.Thread(target=self._work, name="admin-doctor", daemon=True).start()
            return self._state()

    def state(self) -> dict[str, Any]:
        with self._lock:
            return self._state()

    def _state(self) -> dict[str, Any]:
        state = "running" if self._running else "done" if self._finished else "idle"
        return {
            "state": state, "started_at": self._started, "finished_at": self._finished, "error": self._error,
            "checks": None if self._checks is None else [dict(row) for row in self._checks],
        }  # fmt: skip

    def _work(self) -> None:
        from console import doctor

        rows: list[dict[str, str]] | None = None
        error: str | None = None
        try:
            rows = [
                {"name": c.name, "status": c.status, "detail": c.detail, "fix": c.fix}
                for c in doctor.run_checks(self._cfg)
            ]
        except Exception:  # one crash must not leave the page waiting for ever
            log.exception("the doctor's checks crashed")
            error = CRASHED
        with self._lock:
            self._running, self._finished, self._error, self._checks = False, _utc_iso(), error, rows


# --- warming the caches ----------------------------------------------------------------------------------------


def warm_caches(runner: Runner) -> dict[str, list[dict[str, str]]]:
    """Queue one ``warm`` run for every unit that is free: ``{runs: [{unit, run}], skipped: [{unit, reason}]}``.

    A unit held by a run, or waiting for a reset, or with a run queued or going already is left out, with the reason.
    """
    open_runs: dict[str, str] = {}
    for rec in runner.store.list():  # newest first: the newest open run of a unit is the one named
        if rec.status in _OPEN:
            open_runs.setdefault(rec.unit, rec.id)
    queued: list[dict[str, str]] = []
    skipped: list[dict[str, str]] = []
    for unit in runner.units.all():
        if unit.locked_by is not None:
            if unit.locked_by.startswith(NEEDS_RESET):
                reason = "needs a reset first"
            else:
                reason = f"busy: run {unit.locked_by} holds it"
        elif unit.id in open_runs:
            reason = f"already has run {open_runs[unit.id]}"
        else:
            rec = runner.submit(unit.id, "warm")
            if rec.status != "refused":
                queued.append({"unit": unit.id, "run": rec.id})
                continue
            reason = rec.error or "refused"
        skipped.append({"unit": unit.id, "reason": reason})
    return {"runs": queued, "skipped": skipped}


# --- the unit cards --------------------------------------------------------------------------------------------


def unit_cards(codes: Mapping[str, str], urls: Sequence[str], heading: str = "Unit cards") -> str:
    """Plain ASCII text for printing: one card per unit (``codes`` maps ``unit-NN`` to its code) with ``urls``.

    ``urls`` are the addresses of this laptop on the room network (``doctor.room_urls``); with none, the card says to
    run the doctor.
    """
    rule = "-" * 60
    lines = [
        f"Harness Console - {heading}",
        f"Made {datetime.now().strftime('%Y-%m-%d %H:%M')}. Print this file, hand out one card per team, then delete it:",
        "the codes are not stored anywhere else. A lost code is replaced with",
        "    python -m console units codes --regenerate unit-NN",
        'or with "New code" on the Admin page of the running console.',
        "",
    ]
    for unit, code in codes.items():
        lines += [rule, unit, f"  Code:  {code}"]
        if urls:
            lines += [f"  Open:  {urls[0]}", *(f"         {url}" for url in urls[1:])]
        else:
            lines.append('  Open:  the address of this laptop on the room network (run "python -m console doctor")')
        lines += ["  Type the code on the login page; capital letters do not matter."]
    lines.append(rule)
    return "\n".join(lines) + "\n"


def write_unit_cards(
    cfg: ConsoleConfig, codes: Mapping[str, str], heading: str = "Unit cards", filename: str = CARDS_FILE
) -> Path:
    """Write ``<data>/<filename>`` (replacing the one there) and return its path; ``OSError`` when the disk refuses.

    Written through ``atomic_write``: never seen half written, and readable by its owner only where the OS supports it.
    """
    from console import doctor

    path = cfg.data_dir / filename
    atomic_write(path, unit_cards(codes, doctor.room_urls(cfg), heading))
    return path
