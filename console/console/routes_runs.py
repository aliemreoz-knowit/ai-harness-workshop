"""The run endpoints (all ``unit`` routes): ask for a run, list them, a run's detail and stream, and what people do with
a run.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. Everything about a run goes through ``app.runner`` (``console.runs``): nothing here runs
git or the harness or opens a file of a run.

* ``POST /api/runs {kind, workflow, ticket, mode, overlay_from, fake}`` asks for a run for the unit (the admin names it in
  ``?unit=``): the stored record, ``queued`` or ``refused`` with its ``error``, both 200. A kind or mode that does not
  exist, or a field that is not text (``null`` is "not given"), is 400. ``overlay_from: "unit-04"`` is a handover run
  (a workflow run with that unit's harness structure; see ``console.runs``): refused when that unit does not exist, is
  the unit itself, or its harness files fail the save rules. In personal mode (one unit) it is 404. ``fake: true``
  (``true``, ``false`` or ``null``, else 400) is a workflow run on the harness's scripted fake model (``--fake``: no
  model, charged 0 credits, needs none left): personal mode only, 400 in the room and for a gates run.
* ``GET /api/runs``: the unit's own runs, newest first; the admin gets every unit's (one unit's with ``?unit=``).
* ``GET /api/runs/queue`` (no login, like the ``global`` topic): ``{lanes, runs}`` of ``Runner.queue``: per lane its
  ``limit``, how many ``going`` and ``queued``, and the runs queued or going with ``id``, ``unit``, ``kind``,
  ``lane``, ``status`` and ``position`` only (no ticket, error, credits or command). For the top bar and the board.
* ``GET /api/runs/<id>``: ``{record, progress, plan, review, diff}``. ``progress`` is ``events.progress`` of the audit
  trail (for a gates run: its ladder as the one gate attempt, once it has ended; else ``null``); ``plan`` / ``review``
  the text of ``plan.md`` / ``review.md`` without host paths
  (``null`` unless it is a plain file of at most 200 KB); ``diff`` is ``{source, text, truncated}`` (``commit``: the
  run's commit against where ``unit/NN`` was when it started (a handover run: against its overlay commit, so the
  other unit's harness is not in it); ``patch``: what it left uncommitted; at most 500 KB) or ``null``.
* ``GET /api/runs/<id>/events``: the run as server-sent events, below.
* ``POST /api/runs/<id>/answer {answer}``, ``.../stop``, ``.../keep``, ``.../discard``: the record afterwards. Only the
  run's own unit or the admin (else 403 ``not your run``); 409 with the runner's reason when the run is in no state
  for it; 403 ``only the facilitator can extend a run`` for a unit's ``continue`` at a credit cap (the run's or a
  role's; a failed gate ladder is not one).

Any unit may read any run (block 5: watching another unit's run): its record and its stream. A record goes out as
``Runner.view`` for whoever asks (no pid, no host paths, an error without them; ``has_patch``; a pending question's
``allowed_options``) with its ``position`` in its lane's queue. Run ids in a path are what ``runs.RUN_ID`` allows
(else 404).

The stream sends first the record as it is (event ``run``), then the run's lines (event ``line``, id = the audit
seq), live ``loop``, ``approval`` and ``run`` events (with the answers this viewer may give: the admin's stream gets
the admin's), and last an ``end`` event with the record once the run is over (the page closes its EventSource on
it). The id the browser had last is its ``Last-Event-ID`` header (an EventSource sends it when it comes back), else
``?last=``; anything but a whole number counts as 0. The stream subscribes to ``run:<id>`` first, then replays the
trail's lines after that id (``Runner.lines``), then sends what the topic got, dropping any line whose id is not
above the last one sent: every line once, in order, none missing (Review Focus 5). A stream that falls too far
behind is dropped by the broker and ends without ``end``: the browser comes back with its ``Last-Event-ID``.
"""

from __future__ import annotations

import re

from console import runs, sse
from console.runs import RunRecord
from console.server import ROUTES, Handler, HttpError, Route
from console.units import Busy

_RUN = rf"/api/runs/(?P<id>{runs.RUN_ID.pattern})"
_ID_DIGITS = 100  # the longest Last-Event-ID read as a number (a seq is any whole number a trail line carries)


def _record(self: Handler, match: re.Match[str]) -> RunRecord:
    try:
        return self.app.runner.store.get(match["id"])
    except KeyError:
        raise HttpError(404, "no such run") from None


def _own_record(self: Handler, match: re.Match[str]) -> RunRecord:
    """The run, for a request that changes it: the run's own unit or the admin only."""
    rec = _record(self, match)
    if self.subject != "admin" and rec.unit != self.subject:
        raise HttpError(403, "not your run")
    return rec


def _view(self: Handler, rec: RunRecord) -> dict:
    return self.app.runner.view(rec, self.subject)


def _text(body: dict, name: str) -> str | None:
    value = body.get(name)
    if value is not None and not isinstance(value, str):
        raise HttpError(400, f"{name} must be text")
    return value


def _last_event_id(self: Handler) -> int:
    """The id the browser had last (``Last-Event-ID``, else ``?last=``); 0 for none or for not a whole number."""
    value = (self.headers.get("Last-Event-ID") or "").strip() or self.query.get("last", "").strip()
    return int(value) if value.isascii() and value.isdigit() and len(value) <= _ID_DIGITS else 0


def api_runs_submit(self: Handler, _match: re.Match[str]) -> None:
    unit, body = self.target_unit(), self.json_body()
    kind, mode = _text(body, "kind"), _text(body, "mode")
    if kind is None:
        raise HttpError(400, "kind required")
    fields = {name: _text(body, name) for name in ("workflow", "ticket", "overlay_from")}
    if self.app.cfg.personal and fields["overlay_from"] is not None:  # a handover run: personal mode has one unit
        raise HttpError(404, "no handover runs in personal mode")
    fake = body.get("fake")
    if fake is not None and not isinstance(fake, bool):
        raise HttpError(400, "fake must be true or false")
    try:
        rec = self.app.runner.submit(unit.id, kind, mode="interactive" if mode is None else mode, fake=bool(fake),
                                     **fields)
    except ValueError as err:  # a kind or mode that does not exist, the fake model where it does not apply
        raise HttpError(400, str(err)) from None
    self.send_json(200, _view(self, rec))


def api_runs_list(self: Handler, _match: re.Match[str]) -> None:
    unit = None if self.subject == "admin" and "unit" not in self.query else self.target_unit().id
    self.send_json(200, {"runs": [_view(self, rec) for rec in self.app.runner.store.list(unit)]})


def api_runs_queue(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, self.app.runner.queue())


def api_run(self: Handler, match: re.Match[str]) -> None:
    runner, rec = self.app.runner, _record(self, match)
    self.send_json(200, {
        "record": _view(self, rec),
        "progress": runner.progress(rec),
        "plan": runner.artifact(rec, "plan.md"),
        "review": runner.artifact(rec, "review.md"),
        "diff": runner.diff(rec),
    })  # fmt: skip


def api_run_events(self: Handler, match: re.Match[str]) -> None:
    runner, rec = self.app.runner, _record(self, match)
    after = _last_event_id(self)
    with self.event_stream(f"run:{rec.id}") as sub:  # subscribed before the trail is read: nothing falls in between
        ended = runner.settled(rec.id)  # then its end line went out before the subscription: the replay gives it
        sse.write_sse(self.wfile, "run", _view(self, runner.store.get(rec.id)))  # the record as it is now
        for line in runner.lines(rec.id, after, ended=ended):
            sse.write_sse(self.wfile, "line", line.to_dict(), id=line.seq)
            after = line.seq
        sse.stream(self.wfile, sub, self.app.broker.heartbeat_s, after=after, done=lambda: runner.settled(rec.id),
                   admin=self.subject == "admin")
        if not sub.dropped:
            sse.write_sse(self.wfile, "end", _view(self, runner.store.get(rec.id)))


def api_run_answer(self: Handler, match: re.Match[str]) -> None:
    rec = _own_record(self, match)
    answer = self.json_body().get("answer")
    if not isinstance(answer, str):
        raise HttpError(400, "answer must be text")
    try:
        self.app.runner.answer(rec.id, answer, by=self.subject)
    except runs.FacilitatorOnly as err:
        raise HttpError(403, str(err)) from None
    except ValueError as err:  # it waits on nothing, or not on this answer
        raise HttpError(409, str(err)) from None
    self.send_json(200, _view(self, self.app.runner.store.get(rec.id)))


def api_run_stop(self: Handler, match: re.Match[str]) -> None:
    rec = _own_record(self, match)
    try:
        self.app.runner.stop(rec.id, by=self.subject)
    except ValueError as err:  # not queued or going
        raise HttpError(409, str(err)) from None
    self.send_json(200, _view(self, self.app.runner.store.get(rec.id)))


def api_run_keep(self: Handler, match: re.Match[str]) -> None:
    rec = _own_record(self, match)
    try:
        self.app.runner.keep(rec.id)
    except (ValueError, Busy) as err:  # nothing to keep, another run going, unit/NN moved on, the unit is held
        raise HttpError(409, str(err)) from None
    self.send_json(200, _view(self, self.app.runner.store.get(rec.id)))


def api_run_discard(self: Handler, match: re.Match[str]) -> None:
    rec = _own_record(self, match)
    try:
        self.app.runner.discard(rec.id)
    except ValueError as err:  # not ended, or kept
        raise HttpError(409, str(err)) from None
    self.send_json(200, _view(self, self.app.runner.store.get(rec.id)))


_ACTIONS = ("answer", "stop", "keep", "discard")
_HANDLERS = (api_runs_submit, api_runs_list, api_runs_queue, api_run, api_run_events, api_run_answer, api_run_stop,
             api_run_keep, api_run_discard)  # fmt: skip

for _handler in _HANDLERS:
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("POST", re.compile(r"/api/runs"), "api_runs_submit", "unit"),
        Route("GET", re.compile(r"/api/runs"), "api_runs_list", "unit"),
        Route("GET", re.compile(r"/api/runs/queue"), "api_runs_queue", "public"),  # before _RUN: "queue" is an id too
        Route("GET", re.compile(_RUN), "api_run", "unit"),
        Route("GET", re.compile(_RUN + r"/events"), "api_run_events", "unit"),
        *(Route("POST", re.compile(rf"{_RUN}/{action}"), f"api_run_{action}", "unit") for action in _ACTIONS),
    ]
)
