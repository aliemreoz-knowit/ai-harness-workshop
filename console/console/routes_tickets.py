"""The ticket endpoints: ``GET /api/tickets`` and ``GET /api/tickets/<KEY>`` (both ``unit`` routes).

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. The work is ``app.tickets`` (``console.tickets.Tickets``); saving a ticket text is not
here: the browser writes ``tickets/<KEY>.md`` through ``POST /api/files/write`` like any file of the unit.

* ``GET /api/tickets`` answers ``{tickets: [{key, title, status, type}]}`` (the same for every unit; at most a minute
  old).
* ``GET /api/tickets/<KEY>`` answers ``{markdown, template, saved}``: the Jira text, the skeleton of an agent-ready
  ticket built from the unit's own issue template, and whether the unit has ``tickets/<KEY>.md`` already. It names the
  unit like the file endpoints do (the admin adds ``?unit=``).

Personal mode (``cfg.personal``, no Jira): ``app.tickets`` is ``FileTickets``, the tickets are the unit worktree's
``tickets/TS-nnn.md``. ``tickets/<KEY>.md`` is then the source text, so the agent-ready version is saved as
``tickets/<KEY>.agent.md``: the list answers ``suffix`` (``.agent.md``) and the ticket ``path`` (the file to write and
to run). Shared mode answers neither.

Errors: 404 ``no such ticket`` (a key that is not of the project, or unknown to Jira); 503 ``Jira is not configured``;
503 ``Jira unreachable: <reason>`` with a fixed reason (``HTTP 401``, ``timed out``, ...). Neither the exception text
nor a credential is in an answer or in the log.
"""

from __future__ import annotations

import contextlib
import logging
import re
from collections.abc import Iterator, Sequence
from pathlib import Path

from console import files
from console.server import ROUTES, Handler, HttpError, Route
from console.tickets import AGENT_SUFFIX, NoSuchTicket, NotConfigured, Unreachable

log = logging.getLogger(__name__)


@contextlib.contextmanager
def _jira_errors() -> Iterator[None]:
    """The tickets' exceptions as the answers of the endpoints."""
    try:
        yield
    except NotConfigured:
        raise HttpError(503, "Jira is not configured") from None
    except Unreachable as err:
        raise HttpError(503, f"Jira unreachable: {err}") from None
    except NoSuchTicket:
        raise HttpError(404, "no such ticket") from None


def api_tickets(self: Handler, _match: re.Match[str]) -> None:
    if self.app.cfg.personal:  # the repo's ticket files, as the unit's worktree has them
        rows = self.app.tickets.list(self.target_unit().worktree)
        self.send_json(200, {"tickets": rows, "suffix": AGENT_SUFFIX})
        return
    with _jira_errors():
        rows = self.app.tickets.list()
    self.send_json(200, {"tickets": rows})


def api_ticket(self: Handler, match: re.Match[str]) -> None:
    app, key = self.app, match["key"]
    unit = self.target_unit()
    personal = app.cfg.personal
    with _jira_errors():
        issue = app.tickets.issue(key, unit.worktree) if personal else app.tickets.issue(key)
        template = app.tickets.template_of(issue, unit.worktree)
    path = f"tickets/{key}{AGENT_SUFFIX if personal else '.md'}"  # personal: tickets/<KEY>.md is the source text
    try:
        saved = _is_saved(unit.worktree, path, app.cfg.editable)
    except (files.Forbidden, OSError, RuntimeError):  # the disk or git failed: the details stay in the log
        log.error("ticket %s of %s could not be looked up", key, unit.id, exc_info=True)
        raise HttpError(500, "ticket lookup failed") from None
    answer = {"markdown": issue.markdown(), "template": template, "saved": saved}
    self.send_json(200, {**answer, "path": path} if personal else answer)


def _is_saved(worktree: Path, path: str, editable: Sequence[str]) -> bool:
    """Whether the worktree has ``path`` (a file too large to read counts: it is there)."""
    try:
        files.read(worktree, path, editable, files.MAX_BYTES)
    except FileNotFoundError:
        return False
    except files.Invalid:
        return True
    return True


for _handler in (api_tickets, api_ticket):
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/tickets"), "api_tickets", "unit"),
        Route("GET", re.compile(r"/api/tickets/(?P<key>[^/]+)"), "api_ticket", "unit"),
    ]
)
