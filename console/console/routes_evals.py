"""The eval endpoints: a unit's variant card, the batch the facilitator launches and stops, and its progress.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. Everything goes through ``app.evals`` (``console.evals``). Nothing here names a host path:
a failure is logged and answered in plain words.

* ``GET /api/eval/card`` (``unit``): ``{unit, card, defaults, options}``. ``card`` is the stored card or ``null``;
  ``defaults`` are the values of the reference variant cheap a form starts from; ``options`` the models, the cap of one run
  and the largest retry budget a card may have.
* ``POST /api/eval/cards`` (``unit``): the body is the card itself, e.g. ``{"retry_budget": 2}``. Answers
  ``{unit, card}``, or 422 ``{errors: [...]}`` with one sentence per problem. The admin names the unit in ``?unit=``.
* ``GET /api/eval/cards`` (``admin``): ``{cards: {unit-NN: card}, plan}`` of every unit that has submitted one. ``plan``
  is ``Evals.plan``: ``{variants, units, skipped, repeats, cap, overshoot}`` of the batch a launch would start now (the
  page multiplies it with the tickets for the cost estimate), or ``null`` when it cannot be worked out: the cards are
  listed all the same.
* ``POST /api/admin/eval`` (``admin``): ``{tickets: ["TS-101", ...], parallel: 2, experiment?: "name"}`` starts the batch and
  answers ``{experiment, total, units, skipped}``. 400 for a request that is wrong (with the reason), 409
  ``an eval batch is running``, 500 when the process could not be started.
* ``POST /api/admin/eval/stop`` (``admin``): kills the batch's process tree and answers the status afterwards
  (``stopped`` true; the runs finished so far stay in the results). 409 ``no eval batch is running``, 500 when the
  process could not be stopped.
* ``GET /api/eval/status`` (``public``): ``{running, experiment, done, total, credits, exit, stopped}`` of the batch
  launched last.

The public leaderboard data, ``GET /api/eval/results.json``, is Task 17's (``routes_board``): it serves the file the
batch writes, ``<data>/evals/results/results.json``, scrubbed. Nothing here reads or serves it.
"""

from __future__ import annotations

import logging
import re

from console.evals import BatchRunning, CardInvalid, EvalError, NotRunning, StartFailed, StopFailed
from console.server import ROUTES, Handler, HttpError, Route

log = logging.getLogger(__name__)

_LAUNCH_FIELDS = frozenset({"tickets", "parallel", "experiment"})


def api_eval_card(self: Handler, _match: re.Match[str]) -> None:
    unit, evals = self.target_unit(), self.app.evals
    self.send_json(
        200, {"unit": unit.id, "card": evals.card(unit.id), "defaults": evals.defaults(), "options": evals.options()}
    )


def api_eval_card_save(self: Handler, _match: re.Match[str]) -> None:
    unit = self.target_unit()
    card = self.json_body()
    try:
        saved = self.app.evals.save_card(unit.id, card)
    except CardInvalid as err:
        raise HttpError(422, errors=err.errors) from None
    except EvalError as err:
        raise HttpError(400, str(err)) from None
    except OSError:  # the disk failed: the details (host paths) stay in the log
        log.error("the card of %s could not be saved", unit.id, exc_info=True)
        raise HttpError(500, "save failed") from None
    self.send_json(200, {"unit": unit.id, "card": saved})


def api_eval_cards(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, {"cards": self.app.evals.cards(), "plan": self.app.evals.plan()})


def api_admin_eval(self: Handler, _match: re.Match[str]) -> None:
    body = self.json_body()
    unknown = sorted(str(key)[:40] for key in body if key not in _LAUNCH_FIELDS)
    if unknown:
        raise HttpError(400, f"unknown field: {', '.join(unknown)}")
    try:
        answer = self.app.evals.launch(body.get("tickets"), body.get("parallel"), body.get("experiment"))
    except EvalError as err:
        raise HttpError(400, str(err)) from None
    except BatchRunning:
        raise HttpError(409, "an eval batch is running") from None
    except StartFailed:
        raise HttpError(500, "the eval batch could not be started") from None
    self.send_json(200, answer)


def api_admin_eval_stop(self: Handler, _match: re.Match[str]) -> None:
    try:
        status = self.app.evals.stop()
    except NotRunning:
        raise HttpError(409, "no eval batch is running") from None
    except StopFailed:
        raise HttpError(500, "the eval batch could not be stopped") from None
    self.send_json(200, status)


def api_eval_status(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, self.app.evals.status())


for _handler in (api_eval_card, api_eval_card_save, api_eval_cards, api_admin_eval, api_admin_eval_stop, api_eval_status):
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/eval/card"), "api_eval_card", "unit"),
        Route("POST", re.compile(r"/api/eval/cards"), "api_eval_card_save", "unit"),
        Route("GET", re.compile(r"/api/eval/cards"), "api_eval_cards", "admin"),
        Route("POST", re.compile(r"/api/admin/eval"), "api_admin_eval", "admin"),
        Route("POST", re.compile(r"/api/admin/eval/stop"), "api_admin_eval_stop", "admin"),
        Route("GET", re.compile(r"/api/eval/status"), "api_eval_status", "public"),
    ]
)
