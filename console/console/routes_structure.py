"""The structure endpoints: ``GET /api/structure`` and ``POST /api/validate`` (both ``unit`` routes).

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. The handlers work the way every ``unit`` route does: ``self.target_unit()`` is the
session's unit (the admin names one in ``?unit=``), an ``HttpError`` is the answer, and nothing that names a host
path or carries git's words leaves the process (they go to the log).

* ``GET /api/structure`` answers ``{structure, changed, hash, weakened}``: the harness of the unit as ``structure.read``
  has it, the overlay files that differ from ``start_ref``, the hash of the overlay and the guardrails the unit gave up
  (``structure.weakening``: flags ``{rule, detail, file}``), see ``console.structure``. When git cannot list the
  changes, ``changed`` is ``null`` (logged) and the rest is answered; so is ``weakened`` when the start harness cannot
  be read.
* ``POST /api/validate`` answers ``{errors, harness: {ok, exit, output}}``. ``errors`` are what the save rules say about
  every overlay file of the unit as it is now. ``harness`` is ``python -m harness workflows`` run in the unit's worktree
  (the same command a participant would type, with the allowlisted environment of a run, at most
  ``HARNESS_TIMEOUT_S`` seconds): whether it exited 0, its exit status (``null`` when it did not finish or did not
  start) and the last 40 lines of what it printed, with the worktree and home folders left out. Refused with 409
  while a run holds the unit, and while another validation of the unit is in progress.
"""

from __future__ import annotations

import contextlib
import logging
import os
import re
import subprocess
import tempfile
import threading
from pathlib import Path

from console import events, files, procs, structure
from console.config import ConsoleConfig
from console.server import ROUTES, App, Handler, HttpError, Route

log = logging.getLogger(__name__)

HARNESS_TIMEOUT_S = 30
KILL_WAIT_S = 10  # how long to wait for a harness that was killed to be gone
OUTPUT_LINES = 40

_validating: set[tuple[str, str]] = set()  # (data dir, unit id) of the validations in progress
_validating_guard = threading.Lock()


def _harness_check(cfg: ConsoleConfig, worktree: Path) -> dict:
    """Run ``<harness_cmd> workflows`` in ``worktree``: ``{ok, exit, output}``."""
    cmd = [*cfg.harness_argv, "workflows"]
    env = procs.run_env(os.environ, {"PYTHONUTF8": "1"})
    fd, name = tempfile.mkstemp(prefix="console-validate-", suffix=".log")
    os.close(fd)
    log_file, exit_code, notice = Path(name), None, ""
    try:
        try:
            proc = procs.spawn(cmd, worktree, env, log_file)
        except OSError:
            return {"ok": False, "exit": None, "output": "the harness could not be started"}
        try:
            exit_code = proc.wait(timeout=HARNESS_TIMEOUT_S)
        except subprocess.TimeoutExpired:
            notice = f"the harness did not answer within {HARNESS_TIMEOUT_S:g} s"
        finally:
            if proc.poll() is None:  # timed out, or the wait was interrupted: no harness is left running
                procs.kill_tree(proc)
                try:
                    proc.wait(timeout=KILL_WAIT_S)
                except subprocess.TimeoutExpired:
                    log.warning("validate: harness pid %s is still running after it was killed; not waiting", proc.pid)
        lines = log_file.read_text(encoding="utf-8", errors="replace").splitlines()
    finally:
        with contextlib.suppress(OSError):  # Windows: not while a process that would not die still holds it
            log_file.unlink(missing_ok=True)
    shown = [events.summarize(line, root=worktree) for line in lines[-OUTPUT_LINES:]]
    if notice:
        shown = [*shown[-(OUTPUT_LINES - 1) :], notice]
    return {"ok": exit_code == 0, "exit": exit_code, "output": "\n".join(shown)}


def weakened(app: App, unit_id: str, current: dict) -> list[dict] | None:
    """The flags of ``current`` (the unit's structure) against the start harness: ``structure.weakening``. ``None``
    when they cannot be worked out (git failed; logged): that is not the same as no flags."""
    try:
        start = structure.golden(app.units.git_for(unit_id), app.cfg.start_ref)
        return structure.weakening(current, start)
    except Exception:  # git failed, or a shape the rules did not expect: the flags are advice, the page still opens
        log.error("the weakening of %s could not be worked out", unit_id, exc_info=True)
        return None


def api_structure(self: Handler, _match: re.Match[str]) -> None:
    unit, app = self.target_unit(), self.app
    try:
        answer = {"structure": structure.read(unit.worktree), "changed": None}
        answer["hash"] = structure.config_hash(unit.worktree, app.cfg.overlay)
    except (RuntimeError, OSError, files.Forbidden):  # the disk failed: the details stay in the log
        log.error("structure failed for %s", unit.id, exc_info=True)
        raise HttpError(500, "structure failed") from None
    try:
        git = app.units.git_for(unit.id)
        answer["changed"] = structure.changed(unit.worktree, app.cfg.start_ref, app.cfg.overlay, git)
    except (RuntimeError, ValueError, OSError, files.Forbidden):  # git failed: the rest is still worth showing
        log.error("changes of %s could not be listed", unit.id, exc_info=True)
    answer["weakened"] = weakened(app, unit.id, answer["structure"])
    self.send_json(200, answer)


def api_validate(self: Handler, _match: re.Match[str]) -> None:
    unit, app = self.target_unit(), self.app
    if app.units.get(unit.id).locked_by is not None:  # a run owns the worktree meanwhile
        raise HttpError(409, "unit is running")
    key = (str(app.cfg.data_dir), unit.id)
    with _validating_guard:  # one at a time per unit: each one starts a process for up to 30 s
        if key in _validating:
            raise HttpError(409, "validation running")
        _validating.add(key)
    try:
        errors = structure.overlay_errors(unit.worktree, app.cfg.overlay, app.check)
        harness = _harness_check(app.cfg, unit.worktree)
    except (RuntimeError, OSError, files.Forbidden):
        log.error("validate failed for %s", unit.id, exc_info=True)
        raise HttpError(500, "validate failed") from None
    finally:
        with _validating_guard:
            _validating.discard(key)
    self.send_json(200, {"errors": errors, "harness": harness})


for _handler in (api_structure, api_validate):
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/structure"), "api_structure", "unit"),
        Route("POST", re.compile(r"/api/validate"), "api_validate", "unit"),
    ]
)
