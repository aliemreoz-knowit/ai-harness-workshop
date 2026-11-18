"""The usage guide endpoints (both ``unit`` routes): every unit's guide, and a unit's own to start.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. The files go through ``console.handover`` (``files.read`` and ``files.write``).

* ``GET /api/guides`` (any login): ``{guides: [{unit, name, state, text}]}``, every unit's usage guide
  (``docs/usage-guide.md``; ``state`` ``ok``, ``missing``, ``too long`` or ``unreadable``; ``text`` only when
  ``ok``): the guides page lists them, read-only.
* ``POST /api/guides/start``: saves the template as the unit's own guide, one commit like any save (409 when it has a
  guide already or a run holds the unit). A guide is changed in the Code view, as any file; no route writes another
  unit's.
"""

from __future__ import annotations

import logging
import re

from console import files, handover
from console.server import ROUTES, Handler, HttpError, Route

log = logging.getLogger(__name__)


def api_guides(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, {"guides": handover.guides(self.app.units.all(), self.app.cfg.editable)})


def api_guides_start(self: Handler, _match: re.Match[str]) -> None:
    unit = self.target_unit()
    if self.app.units.get(unit.id).locked_by is not None:  # a run owns the worktree meanwhile
        raise HttpError(409, "unit is running")
    try:
        handover.start_guide(unit, self.app.cfg.editable, self.app.check, guard=lambda: self.app.units.holder(unit.id))
    except files.Busy:
        raise HttpError(409, "unit is running") from None
    except files.Conflict:
        raise HttpError(409, "you have a guide already") from None
    except (files.Forbidden, files.Invalid, RuntimeError, OSError):  # git or the disk failed, or the rules refuse
        log.error("starting the guide failed for %s", unit.id, exc_info=True)
        raise HttpError(500, "could not start the guide") from None
    self.send_json(200, {"path": handover.GUIDE})


for _handler in (api_guides, api_guides_start):
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/api/guides"), "api_guides", "unit"),
        Route("POST", re.compile(r"/api/guides/start"), "api_guides_start", "unit"),
    ]
)
