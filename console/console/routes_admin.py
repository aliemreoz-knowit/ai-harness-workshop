"""The admin endpoints, all ``admin`` routes (a unit's session gets 403): what the facilitator does to the units, the runs,
the build caches, the doctor and the eval results.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. The work is in ``app.units``, ``app.runner``, ``app.evals`` and ``console.admin``; nothing
here runs git or opens a file. A failure is logged and answered in plain words, never with a host path or git's output.
Every change is logged with who made it (the session's subject, ``admin``) and never with a code or a secret.

* ``GET /api/admin/units``: ``{units, refs, max_level}``. A unit is ``{id, name, branch, level, budget, spent, left,
  locked_by, lock}``; ``lock`` is ``free``, ``run`` (``locked_by`` is the run id) or ``needs-reset`` (a run could not put
  the worktree back; ``locked_by`` is ``needs-reset:<run id>``). ``refs`` are the start ref and the catch-up refs a
  reset may use.
* ``POST /api/admin/units/<id> {budget_add?, level?, name?}``: ``{unit}``. 400 with the reason for a field that is wrong
  (``admin.check_unit_change``) and then nothing at all changes.
* ``POST /api/admin/units/<id>/reset {ref}``: puts the unit's branch on ``ref`` and keeps its old work on backup
  branches: ``{unit, backup, backups}``. ``backup`` holds the unit's saved work (the old tip of ``unit/NN``);
  ``backups`` lists it and, when HEAD was on another commit (an agent's branch), that one too (``-head``). 409 while a
  run holds the unit (a unit that waits for a reset is what this is for), 400 for a ref that is not one of ``refs`` or
  not in the repo, 500 when git or the closing check fails (the unit is then held for a reset).
* ``POST /api/admin/units/<id>/restore``: puts a unit that waits for a reset back on its own branch and frees it, every
  save kept (``UnitStore.restore`` with the held run's ``unit_head``: the branch does not move, no backup; a branch that
  is gone is made again there): ``{unit, warning}``. ``warning`` (else null) says when the branch has a commit the run
  made. 409 for a unit that does not wait for a reset (free, or a run holds it); 500 when git or the closing check
  fails (the hold stays).
* ``POST /api/admin/units/<id>/code``: a new code, shown once: ``{unit, code, sessions_ended}``. In this order: the key
  file is replaced (``server.rotate_session_key``), the code's hash is changed, and last the key in memory, so every
  session ends and none can be made with the old code in between. The answer logs the admin in again, and so does the
  500 when the hash could not be changed (the key is new then, the code is not). 500 and nothing changed when the key
  file cannot be replaced.
* ``POST /api/admin/runs/<id>/stop``: stops a queued or going run (``Runner.stop(by="admin")``): the run as a browser sees
  it. 409 when it is neither, 404 for a run that is not there.
* ``POST /api/admin/warm``: one ``warm`` run (gates lane) for each unit that is free: ``{runs: [{unit, run}], skipped:
  [{unit, reason}]}``.
* ``GET /api/admin/doctor``: ``{state, started_at, finished_at, error, checks}`` of the doctor's last run; it never
  starts one. ``POST /api/admin/doctor`` starts a run in a thread of its own (one at a time) and answers the state at
  once: the checks can take a minute. ``checks`` are rows ``{name, status (OK, WARN or FAIL), detail, fix}``.
* ``POST /api/admin/eval/archive``: moves the eval results to ``<data>/evals/archive/<UTC timestamp>/`` so that a trial
  batch does not mix into the day's leaderboard: ``{archived, files}``. 409 while a batch runs, and when there is nothing
  to archive.
"""

from __future__ import annotations

import logging
import re
import threading

from console import admin, runs
from console.auth import Sessions
from console.evals import BatchRunning, NothingToArchive
from console.server import ROUTES, Handler, HttpError, Route, rotate_session_key
from console.units import NEEDS_RESET, Busy, Unit

log = logging.getLogger(__name__)

_UNIT = r"/api/admin/units/(?P<id>unit-[0-9]{2})"
_RUN = rf"/api/admin/runs/(?P<id>{runs.RUN_ID.pattern})"
_code_lock = threading.Lock()  # a new code changes the session key, the stored hash and the answer's cookie as one


def _lock_kind(unit: Unit) -> str:
    if unit.locked_by is None:
        return "free"
    return "needs-reset" if unit.locked_by.startswith(NEEDS_RESET) else "run"


def _unit_view(unit: Unit) -> dict:
    return {
        "id": unit.id,
        "name": unit.name,
        "branch": unit.branch,
        "level": unit.level,
        "budget": unit.budget,
        "spent": unit.spent,
        "left": unit.left,
        "locked_by": unit.locked_by,
        "lock": _lock_kind(unit),
    }


def _unit(self: Handler, match: re.Match[str]) -> Unit:
    try:
        return self.app.units.get(match["id"])
    except KeyError:
        raise HttpError(404, "no such unit") from None


def api_admin_units(self: Handler, _match: re.Match[str]) -> None:
    cfg = self.app.cfg
    self.send_json(200, {
        "units": [_unit_view(unit) for unit in self.app.units.all()],
        "refs": list(dict.fromkeys((cfg.start_ref, *cfg.catchup_refs))),
        "max_level": admin.MAX_LEVEL,
    })  # fmt: skip


def api_admin_unit_change(self: Handler, match: re.Match[str]) -> None:
    unit = _unit(self, match)
    try:
        change = admin.check_unit_change(self.json_body())
    except ValueError as err:
        raise HttpError(400, str(err)) from None
    said = []
    if "budget_add" in change:
        said.append(f"budget +{change['budget_add']:g}")
    said += [f"{key} {change[key]}" for key in ("level", "name") if key in change]
    try:
        unit = self.app.units.update(unit.id, **change)  # one write: every field or none
    except OSError:  # the disk failed: the details (host paths) stay in the log
        log.error("%s could not change %s", self.subject, unit.id, exc_info=True)
        raise HttpError(500, "the unit could not be changed: see the console log") from None
    log.info("%s changed %s: %s (budget now %g)", self.subject, unit.id, ", ".join(said), unit.budget)
    self.send_json(200, {"unit": _unit_view(unit)})


def api_admin_unit_reset(self: Handler, match: re.Match[str]) -> None:
    unit, body = _unit(self, match), self.json_body()
    unknown = sorted(str(key)[:40] for key in body if key != "ref")
    if unknown:
        raise HttpError(400, f"unknown field: {', '.join(unknown)}")
    ref = body.get("ref")
    if not isinstance(ref, str):
        raise HttpError(400, "ref must be text")
    try:
        backups = self.app.units.reset(unit.id, ref)
    except Busy as err:  # a run holds it: no reset under a run
        raise HttpError(409, str(err)) from None
    except ValueError as err:  # not a ref a reset may use, or not in this repo
        raise HttpError(400, str(err)) from None
    except (RuntimeError, OSError):  # git or the disk failed: its output (host paths) stays in the log
        log.error("%s could not reset %s to %s", self.subject, unit.id, ref, exc_info=True)
        raise HttpError(500, "the reset failed: see the console log") from None
    log.info("%s reset %s to %s (old work kept as %s)", self.subject, unit.id, ref, ", ".join(backups) or "nothing")
    self.send_json(200, {"unit": _unit_view(self.app.units.get(unit.id)), "backup": backups[0] if backups else None,
                         "backups": backups})


def _held_run_start(self: Handler, unit: Unit) -> str | None:
    """The commit ``unit/NN`` had when the run that holds the unit for a reset started (its ``unit_head``), if known."""
    held = unit.locked_by or ""
    if not held.startswith(NEEDS_RESET):
        return None
    try:
        return self.app.runner.store.get(held[len(NEEDS_RESET):]).unit_head
    except (KeyError, OSError, ValueError, TypeError):  # no such record (or not one that can be read)
        return None


def api_admin_unit_restore(self: Handler, match: re.Match[str]) -> None:
    unit = _unit(self, match)
    try:
        moved = self.app.units.restore(unit.id, base=_held_run_start(self, unit))
    except (Busy, ValueError) as err:  # a run holds it, or it does not wait for a reset
        raise HttpError(409, str(err)) from None
    except (RuntimeError, OSError):  # git, the check or the disk failed: the output (host paths) stays in the log
        log.error("%s could not restore %s to %s", self.subject, unit.id, unit.branch, exc_info=True)
        raise HttpError(500, "the restore failed: see the console log") from None
    log.info("%s restored %s to its own branch %s (%s cleared)", self.subject, unit.id, unit.branch, unit.locked_by)
    warning = "the unit's branch has a commit the run made; Reset if you do not want it" if moved else None
    self.send_json(200, {"unit": _unit_view(self.app.units.get(unit.id)), "warning": warning})


def api_admin_unit_code(self: Handler, match: re.Match[str]) -> None:
    unit = _unit(self, match)
    app = self.app
    with _code_lock:
        try:
            # The key file first: if it cannot be replaced, nothing has changed.
            key = rotate_session_key(app.cfg.data_dir)
        except OSError:
            log.error("%s could not make a new code for %s: the session key was not replaced", self.subject, unit.id,
                      exc_info=True)
            raise HttpError(500, "the code could not be changed: see the console log") from None
        code = None
        try:
            code = app.units.regenerate_code(unit.id)  # then the hash: from here the old code logs nobody in
        except Exception:  # any failure: the admin keeps a session of the new key (below), the code stays unchanged
            log.error("%s could not make a new code for %s", self.subject, unit.id, exc_info=True)
        finally:
            # Last, the key in memory: the sessions of the old key end here, when the old code can no longer make one.
            # They end whether or not the code changed, because the key file is new already (a restart would end them).
            app.sessions = Sessions(key, app.sessions.ttl_s)
        cookie = self.session_cookie("admin")  # this admin is logged in again, with a token of the new key
    if code is None:
        raise HttpError(500, "the code could not be changed: see the console log", headers=[cookie])
    log.info("%s made a new code for %s; every session ended", self.subject, unit.id)
    self.send_json(200, {"unit": unit.id, "code": code, "sessions_ended": True}, [cookie])


def api_admin_run_stop(self: Handler, match: re.Match[str]) -> None:
    runner = self.app.runner
    try:
        rec = runner.store.get(match["id"])
        runner.stop(rec.id, by="admin")
        rec = runner.store.get(rec.id)
    except KeyError:
        raise HttpError(404, "no such run") from None
    except ValueError as err:  # neither queued nor going
        raise HttpError(409, str(err)) from None
    self.send_json(200, runner.view(rec, self.subject))


def api_admin_warm(self: Handler, _match: re.Match[str]) -> None:
    answer = admin.warm_caches(self.app.runner)
    log.info("%s warmed the caches: %d runs queued, %d units left out", self.subject, len(answer["runs"]),
             len(answer["skipped"]))
    self.send_json(200, answer)


def api_admin_doctor(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, self.app.doctor.state())


def api_admin_doctor_start(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, self.app.doctor.start())


def api_admin_eval_archive(self: Handler, _match: re.Match[str]) -> None:
    try:
        answer = self.app.evals.archive_results()
    except BatchRunning:
        raise HttpError(409, "an eval batch is running") from None
    except NothingToArchive:
        raise HttpError(409, "there are no results to archive") from None
    except OSError:  # the disk refused: the details (host paths) stay in the log
        log.error("%s could not archive the eval results", self.subject, exc_info=True)
        raise HttpError(500, "the results could not be archived: see the console log") from None
    log.info("%s archived the eval results as %s", self.subject, answer["archived"])
    self.send_json(200, answer)


_HANDLERS = (
    api_admin_units, api_admin_unit_change, api_admin_unit_reset, api_admin_unit_restore, api_admin_unit_code,
    api_admin_run_stop, api_admin_warm, api_admin_doctor, api_admin_doctor_start, api_admin_eval_archive,
)  # fmt: skip

for _handler in _HANDLERS:
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/admin/units"), "api_admin_units", "admin"),
        Route("POST", re.compile(_UNIT), "api_admin_unit_change", "admin"),
        Route("POST", re.compile(_UNIT + r"/reset"), "api_admin_unit_reset", "admin"),
        Route("POST", re.compile(_UNIT + r"/restore"), "api_admin_unit_restore", "admin"),
        Route("POST", re.compile(_UNIT + r"/code"), "api_admin_unit_code", "admin"),
        Route("POST", re.compile(_RUN + r"/stop"), "api_admin_run_stop", "admin"),
        Route("POST", re.compile(r"/api/admin/warm"), "api_admin_warm", "admin"),
        Route("GET", re.compile(r"/api/admin/doctor"), "api_admin_doctor", "admin"),
        Route("POST", re.compile(r"/api/admin/doctor"), "api_admin_doctor_start", "admin"),
        Route("POST", re.compile(r"/api/admin/eval/archive"), "api_admin_eval_archive", "admin"),
    ]
)
