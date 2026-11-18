"""The day's commands on the Today page (personal mode only; all ``unit`` routes): ``console.workshop``.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. Every handler is in ``server._PERSONAL_ONLY``: the room's console answers 404 on them, and
personal mode has no admin.

* ``GET /api/workshop/commands``: ``<checkout>/workshop/commands.json`` validated (``workshop.read_commands``), or
  ``{"sections": {}}`` when there is none.
* ``POST /api/workshop/commands/<id>/run``: runs that command (the text from the file on disk now, never from the
  request), in the console's copy (``workshop.Jobs.folder``): the job (``{id, command_id, cmd, state, exit_code,
  started_at, ended_at}``). 404 for an id the file does not have, 403 for an interactive command (it runs in a
  terminal), 409 when this command runs already or ``workshop.MAX_RUNNING`` commands run, 503 when there is no bash or
  no copy.
* ``GET /api/workshop/commands/<id>/recorded``: the command's output as it was recorded before the day
  (``workshop.read_recorded``: the file its ``recorded`` names under ``<checkout>/workshop/recorded/``, read now):
  ``{id, recorded, text, truncated, bytes, max_bytes}``, ``text`` the first ``workshop.MAX_RECORDED`` bytes
  (``truncated`` when the file is longer: ``bytes`` is its size). 404 for an id the file does not have, a command without ``recorded``, or a file that is not
  there or not under ``workshop/recorded/``.
* ``GET /api/workshop/jobs``: ``{jobs: [...]}``, the jobs of this console's life: the running ones first, then the most
  recent that ended (``workshop.LIST_JOBS`` at most).
* ``GET /api/workshop/jobs/<job_id>``: the job with ``output`` (the last ``workshop.MAX_OUTPUT`` bytes, as text).
* ``POST /api/workshop/jobs/<job_id>/stop``: stops it and everything it started; the job afterwards (with output). 409
  when it is not running.
"""

from __future__ import annotations

import re

from console import workshop
from console.server import ROUTES, Handler, HttpError, Route

_COMMAND = rf"/api/workshop/commands/(?P<id>{workshop.COMMAND_ID.pattern})"
_JOB = rf"/api/workshop/jobs/(?P<id>{workshop.JOB_ID.pattern})"


def _jobs(self: Handler) -> workshop.Jobs:
    assert self.app.workshop is not None  # the routes are personal mode's only
    return self.app.workshop


def _job(self: Handler, match: re.Match[str]) -> workshop.Job:
    job = _jobs(self).get(match["id"])
    if job is None:
        raise HttpError(404, "no such job")
    return job


def api_workshop_commands(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, workshop.read_commands(_jobs(self).checkout))


def api_workshop_run(self: Handler, match: re.Match[str]) -> None:
    jobs = _jobs(self)
    command = workshop.find_command(jobs.checkout, match["id"])
    if command is None:
        raise HttpError(404, "no such command")
    if command["interactive"]:
        raise HttpError(403, "this command needs a terminal: copy it and run it there")
    try:
        job = jobs.start(command)
    except (workshop.AlreadyRunning, workshop.TooMany) as err:
        raise HttpError(409, str(err)) from None
    except (workshop.NoBash, workshop.NoCopy) as err:
        raise HttpError(503, str(err)) from None
    self.send_json(200, job.summary())


def api_workshop_recorded(self: Handler, match: re.Match[str]) -> None:
    jobs = _jobs(self)
    command = workshop.find_command(jobs.checkout, match["id"])
    if command is None:
        raise HttpError(404, "no such command")
    if not command["recorded"]:
        raise HttpError(404, "this command has no recorded result")
    recorded = workshop.read_recorded(jobs.checkout, command)
    if recorded is None:
        raise HttpError(404, f"the recorded result is not there: workshop/{command['recorded']}")
    self.send_json(200, recorded)


def api_workshop_jobs(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, {"jobs": _jobs(self).list()})


def api_workshop_job(self: Handler, match: re.Match[str]) -> None:
    self.send_json(200, _jobs(self).view(_job(self, match)))


def api_workshop_job_stop(self: Handler, match: re.Match[str]) -> None:
    jobs, job = _jobs(self), _job(self, match)
    try:
        jobs.stop(job.id)
    except ValueError as err:  # it has ended
        raise HttpError(409, str(err)) from None
    self.send_json(200, jobs.view(job))


HANDLERS = (api_workshop_commands, api_workshop_run, api_workshop_recorded, api_workshop_jobs, api_workshop_job,
            api_workshop_job_stop)  # fmt: skip

for _handler in HANDLERS:
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/workshop/commands"), "api_workshop_commands", "unit"),
        Route("POST", re.compile(_COMMAND + r"/run"), "api_workshop_run", "unit"),
        Route("GET", re.compile(_COMMAND + r"/recorded"), "api_workshop_recorded", "unit"),
        Route("GET", re.compile(r"/api/workshop/jobs"), "api_workshop_jobs", "unit"),
        Route("GET", re.compile(_JOB), "api_workshop_job", "unit"),
        Route("POST", re.compile(_JOB + r"/stop"), "api_workshop_job_stop", "unit"),
    ]
)
