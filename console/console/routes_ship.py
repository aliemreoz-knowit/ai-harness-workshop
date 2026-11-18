"""The ship endpoints: a unit's run to GitHub (push, draft PR, ladder check), its PR status, its Jira report, and the
admin switch "GitHub writes on".

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. The work is ``app.ship`` (``console.ship.Ship``); nothing here runs git or calls GitHub.

* ``POST /api/ship/<run>/push`` answers ``{remote_branch, cli}``; ``.../pr`` ``{number, url, draft, branch, cli}``
  (``cli``: the plain command for what the console did); ``.../check`` ``{name, conclusion, fallback}``;
  ``.../jira {key}`` ``{key, tag}``. These are ``unit`` routes: the run's own unit, or the admin for any unit's run
  (else 403 ``not your run``); 404 for an unknown run.
* ``GET /api/ship/status`` (``unit``; the admin names the unit in ``?unit=``): ``{github_writes, pulls, problem}``.
  ``pulls`` is ``Ship.status`` (at most a minute old) or ``null`` when GitHub could not be asked, and ``problem`` then
  says why.
* ``GET /api/admin/ship`` and ``POST /api/admin/ship {github_writes: true|false}`` (``admin``): the switch,
  ``{github_writes, day}``.

Errors are ``Ship``'s fixed texts with their status: 409 ``GitHub writes are off — ask the facilitator`` and for a run
that cannot be shipped, 400 for a bad Jira key, 503 when GitHub or Jira is not configured, 502 when GitHub, Jira or the
push failed. Never an exception's text, a URL or a token.
"""

from __future__ import annotations

import re

from console import runs
from console.server import ROUTES, Handler, HttpError, Route
from console.ship import ShipError

_RUN = rf"/api/ship/(?P<id>{runs.RUN_ID.pattern})"


def _as_unit(self: Handler, run_id: str) -> str:
    """Whom the request ships as: a unit itself, or for the admin the run's own unit (404 for an unknown run)."""
    if self.subject != "admin":
        return self.subject
    try:
        return self.app.runner.store.get(run_id).unit
    except KeyError:
        raise HttpError(404, "no such run") from None


def _ship(self: Handler, match: re.Match[str], act) -> None:
    run_id = match["id"]
    try:
        answer = act(self.app.ship, _as_unit(self, run_id), run_id)
    except KeyError:
        raise HttpError(404, "no such run") from None
    except ShipError as err:
        raise HttpError(err.status, str(err)) from None
    self.send_json(200, answer)


def api_ship_push(self: Handler, match: re.Match[str]) -> None:
    _ship(self, match, lambda ship, unit, run_id: ship.push(unit, run_id))


def api_ship_pr(self: Handler, match: re.Match[str]) -> None:
    _ship(self, match, lambda ship, unit, run_id: ship.open_pr(unit, run_id))


def api_ship_check(self: Handler, match: re.Match[str]) -> None:
    _ship(self, match, lambda ship, unit, run_id: ship.post_ladder_check(unit, run_id))


def api_ship_jira(self: Handler, match: re.Match[str]) -> None:
    key = self.json_body().get("key")
    _ship(self, match, lambda ship, unit, run_id: ship.report_to_jira(unit, run_id, key))


def api_ship_status(self: Handler, _match: re.Match[str]) -> None:
    unit, ship = self.target_unit(), self.app.ship
    try:
        pulls, problem = ship.status(unit.id), None
    except ShipError as err:
        pulls, problem = None, str(err)
    self.send_json(200, {"github_writes": ship.writes_on(), "pulls": pulls, "problem": problem})


def api_admin_ship(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, self.app.ship.switch())


def api_admin_ship_set(self: Handler, _match: re.Match[str]) -> None:
    on = self.json_body().get("github_writes")
    if not isinstance(on, bool):
        raise HttpError(400, "github_writes must be true or false")
    self.send_json(200, self.app.ship.set_writes(on))


_HANDLERS = (api_ship_push, api_ship_pr, api_ship_check, api_ship_jira, api_ship_status, api_admin_ship,
             api_admin_ship_set)  # fmt: skip

for _handler in _HANDLERS:
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        *(Route("POST", re.compile(rf"{_RUN}/{action}"), f"api_ship_{action}", "unit")
          for action in ("push", "pr", "check", "jira")),
        Route("GET", re.compile(r"/api/ship/status"), "api_ship_status", "unit"),
        Route("GET", re.compile(r"/api/admin/ship"), "api_admin_ship", "admin"),
        Route("POST", re.compile(r"/api/admin/ship"), "api_admin_ship_set", "admin"),
    ]
)
