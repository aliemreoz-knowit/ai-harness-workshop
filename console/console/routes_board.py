"""The board endpoints, all public (no login): what the projector shows.

Importing this module adds its routes to ``console.server.ROUTES`` and its handlers to ``Handler``; ``server.py``
imports it once, at its end. Everything here is public to the LAN, so only public data goes out: the fields of
``GET /api/units``, ticket keys, titles and statuses, what the receiver and the eval batch wrote for the board.

* ``GET /board`` and ``GET /leaderboard`` serve the harness's own pages, ``<harness_repo>/tools/board/mission.html``
  and ``leaderboard.html``, read only: exactly those two files, no other file of that folder, no listing, read as
  plain files of at most ``MAX_BYTES`` (``worktree_ops.read_plain``: no link, one name). The pages take their data
  sources from the query (``data=``, ``src=``, ``metrics=``, ``frame=``, ``units=``, ``tickets=``, ``feed2=``), and
  mission.html puts metrics values into HTML. So the request is normalised: only ``theme``, ``rate``, ``demo=1``
  (board) and ``theme``, ``refresh`` (leaderboard) with a plain value are kept, and ``data=/api/board/`` /
  ``src=/api/eval/results.json`` is forced; a request that asks for anything else is sent on (302) to that URL. Both
  answer with a Content-Security-Policy that lets the page run its own inline script and style and talk to this
  server only (``connect-src 'self'``; no other origin, no ``data:`` URL), and that only this site may frame; and with
  a second one, ``sandbox allow-scripts``: the page runs with no origin of its own (as in the board view's sandboxed
  frames), so it can read nothing of the console that a stranger cannot (no cookie, no ``/api/me``). Personal mode
  serves ``/leaderboard`` too (from the checkout, its harness repo), for the Evals page; not ``/board``.
* The data the pages read (the four ``/api/board/`` routes and ``/api/eval/results.json``, a 404 too) says
  ``Access-Control-Allow-Origin: *``: the sandboxed pages ask with ``Origin: null``. It is public data, read without a
  cookie; nothing else of the console says so.
* ``GET /api/board/units.json``: ``{levels, units}`` in the shape ``harness/eval/board.py`` writes; a unit is its
  public fields (id, name, level, spent, budget, locked) and the empty ``ticket`` and ``variant`` the page shows
  beside its level.
* ``GET /api/board/tickets.json``: ``{states, tickets}`` in that shape too, from ``app.tickets`` (Jira, task 16): a
  ticket is ``{id, title, state, unit, note}`` made from its key, title and status only (``Tickets.list`` keeps labels
  out already, and nothing else is read here). A Jira status becomes the board column it names; one the board does not
  know goes to ``todo`` with the status as ``note``. Without ``app.tickets``, or when Jira is not configured
  (``tickets.NotConfigured``: no credentials), the list is empty and nothing is logged. When Jira does not answer it is
  the last good list, else empty: the board never fails because of Jira, and a Jira that failed is not asked again for
  ``RETRY_S`` seconds (the page polls every few seconds; each call may wait for a timeout).
* ``GET /api/board/metrics.json`` and ``frame.pgm``: what the receiver (``tsrecv --metrics --frame``) writes to
  ``<data>/board/``, read as plain files (``worktree_ops.read_plain``) of at most ``MAX_BYTES``. Metrics are a JSON
  object reduced to what the page can show safely, or ``{}``: numbers and booleans, objects of those (``daemon``,
  ``commands``), ``state`` and ``psnr_src`` as one word, ``tiles`` as rows of 0 and 1; every other value is dropped (the
  page puts metrics values into HTML). A frame is a file that starts with ``P5``, else 404.
* ``GET /api/eval/results.json``: ``<data>/evals/results/results.json`` (the leaderboard file of the eval batch), read
  the same way, at most ``RESULTS_MAX_BYTES``; a missing, unusable or too large file is the empty board. What goes out
  is an allowlist: the top-level keys of ``results.build_board``, the keys of a run row (``SLIM_KEYS``) and of a
  variant row (``aggregate``), without ``overlay_from`` (a path on the facilitator's disk), ``overlay_commit`` and
  ``applied_knobs`` (the unit's card); the text of a run's ``error`` is reduced to the word ``error`` (the page only
  counts them). One key is the console's own: ``stale: true`` on a variant row ``U<NN>`` whose unit changed its harness
  files after a batch that ran it. The batch recorded the ``structure.config_hash`` of each unit's files when it was
  launched (``<data>/evals/<experiment>.meta.json``, ``console.evals``, read as a plain file of at most
  ``META_MAX_BYTES``); the row is stale when that differs from the hash now, for any experiment that has a run row of the
  variant (else any experiment of the file). A batch without a record, a unit that is gone and the reference ``cheap``
  are never stale. The hash of a unit is reused for ``HASH_TTL_S`` seconds: every browser polls this route. The mark is
  never taken from the file.
* Personal mode (the participant's laptop) has no batch of its own: there ``GET /api/eval/results.json`` is the
  leaderboard of the experiments run from this checkout (``python -m harness experiment start ...``, which writes
  ``<repo>/results/<experiment>.jsonl``), read from two places: the checkout itself and the console's copy of it
  (``.console-data/units/unit-01/repo``), where the day's commands may run. Every plain ``results/*.jsonl`` of both
  (at most ``MAX_RESULT_FILES`` a place, ``RESULTS_MAX_BYTES`` a file, ``PERSONAL_MAX_BYTES`` together) is read; a run
  that is in both places, or was run again (``--resume``), counts once, with its newest record (``finished_at``). The
  board is what ``harness.eval.results.build_board`` makes of them (as ``python -m harness experiment leaderboard``
  does), through the same allowlist, never marked stale; ``?experiment=<name>`` makes it of that experiment's runs only.
  Two keys are personal mode's own: ``experiment`` (the one asked for, ``""`` for all) and ``experiment_runs``, one
  ``{experiment, runs, done, last, where}`` per experiment of either place, newest first (``last``: its newest
  ``finished_at``; ``where``: ``checkout`` or ``console copy``, the place of that run; no path). The answer is made again
  only when a result file changed. It says ``Access-Control-Allow-Origin: *`` only to the sandboxed leaderboard page
  (``Origin: null``): another site's page in the participant's browser cannot read it. A ``results.json`` there is not
  read: the runner rewrites it after every run from its own folder only, so the ``.jsonl`` files are the record.

Nothing here fails because a file is missing or damaged: the answer is the empty shape.

Every open board asks the data routes (``/api/board/...``, ``/api/eval/results.json``) about once a second, so their
usual answers (2xx, 304, 404) are logged at debug level; the request log keeps what people do (``Handler.log_request``
is wrapped here). The two pages stay at info: a board asks for them once, and their 404 is a setup fault.
"""

from __future__ import annotations

import hashlib
import json
import logging
import math
import os
import re
import threading
import time
import weakref
from pathlib import Path
from typing import Any, Collection
from urllib.parse import parse_qsl, urlencode

from console import structure
from console.evals import EXPERIMENT, META_SUFFIX, unit_of
from console.server import _MIME, ROUTES, App, Handler, HttpError, Route, _printable, _unit_public
from console.tickets import NotConfigured
from console.units import PERSONAL_UNIT
from console.worktree_ops import read_plain

__all__ = [
    "EMPTY_RESULTS", "HASH_TTL_S", "LEVELS", "MAX_BYTES", "META_MAX_BYTES", "RESULTS_MAX_BYTES", "RETRY_S", "STATES",
    "ticket_row",
]  # fmt: skip

log = logging.getLogger(__name__)

MAX_BYTES = 1024 * 1024  # metrics.json and frame.pgm (a 320x240 frame is 77 KB)
RESULTS_MAX_BYTES = 8 * 1024 * 1024
META_MAX_BYTES = 64 * 1024  # <experiment>.meta.json: a few hash lines
HASH_TTL_S = 5.0  # how long the hash of a unit's harness files is reused (the results are polled by every browser)
RETRY_S = 30.0  # how long a Jira that failed is left alone
MAX_TEXT = 200  # a ticket title or status, in characters
MAX_RESULT_FILES = 200  # personal mode: the results/*.jsonl files read in one place
PERSONAL_MAX_BYTES = 4 * RESULTS_MAX_BYTES  # personal mode: the result files of both places together

# What harness/eval/board.py writes (STATES, LEVELS); the console does not import the harness for two constants.
STATES = ("todo", "in_progress", "gates", "review", "hil_ready", "on_pi", "done", "blocked")
LEVELS = {
    "0": "raw agent",
    "1": "L1 context + prompts",
    "2": "L2 gates + iteration",
    "3": "L3 roles + guardrails + compliance",
    "4": "L4 autonomy + eval",
    "5": "L5 team workflow + handover",
}
EMPTY_RESULTS = {"schema": 1, "generated_at": "", "experiments": [], "tickets": [], "variants": [], "runs": []}

_THEME = re.compile(r"dark|light")
_NUMBER = re.compile(r"[0-9]{1,6}")
_ONE = re.compile(r"1")
# the page of a route: (file in <harness_repo>/tools/board, the parameter that names its data, the console's data, the
# other parameters that stay and what their value may look like)
_PAGES = {
    "/board": ("mission.html", "data", "/api/board/", {"theme": _THEME, "rate": _NUMBER, "demo": _ONE}),
    "/leaderboard": ("leaderboard.html", "src", "/api/eval/results.json", {"theme": _THEME, "refresh": _NUMBER}),
}
# The pages run their own inline script and style and fetch from this server; nothing else may load or be fetched.
_PAGE_CSP = (
    "default-src 'none'; script-src 'unsafe-inline'; style-src 'unsafe-inline'; connect-src 'self'; "
    "img-src 'self' data:; frame-ancestors 'self'"
)
_SANDBOX_CSP = "sandbox allow-scripts"  # no origin of its own: the page reads only what anyone may read
_ANY_ORIGIN = ("Access-Control-Allow-Origin", "*")  # the board data, for the sandboxed pages (Origin: null)
# the data routes every open board polls (path without query): their usual answers are logged at debug level
_POLLED = re.compile(r"/api/board/(?:units|tickets|metrics)\.json|/api/board/frame\.pgm|/api/eval/results\.json")
_STATE_OF = {  # Jira status words (lower case, one space between words) -> board column
    "to do": "todo", "todo": "todo", "open": "todo", "backlog": "todo",
    "in progress": "in_progress", "gates": "gates", "in review": "review", "review": "review",
    "hil ready": "hil_ready", "on the pi": "on_pi", "on pi": "on_pi",
    "done": "done", "closed": "done", "resolved": "done", "blocked": "blocked",
}  # fmt: skip


# --- the pages ---------------------------------------------------------------------------------------


def _page(self: Handler, route: str) -> None:
    name, param, console_data, kept = _PAGES[route]
    wanted = [(key, self.query[key]) for key, shape in kept.items() if shape.fullmatch(self.query.get(key, ""))]
    wanted.append((param, console_data))
    asked = parse_qsl(self.path.partition("#")[0].partition("?")[2], keep_blank_values=True)
    if sorted(asked) != sorted(wanted):  # anything else (another source, a repeated name, an odd value) is dropped
        self.send_response(302)
        self.send_header("Location", f"{route}?{urlencode(wanted, safe='/')}")
        self.send_header("Content-Length", "0")
        self.send_header("Cache-Control", "no-store")
        self.end_headers()
        return
    page = read_plain(self.app.cfg.harness_repo, ("tools", "board", name), MAX_BYTES)
    if page is None:
        raise HttpError(404, "not found")
    etag = f'"{hashlib.sha256(page).hexdigest()[:16]}"'
    fresh = self.headers.get("If-None-Match") == etag  # the browser has this page already
    self.send_response(304 if fresh else 200)
    if not fresh:
        self.send_header("Content-Type", _MIME[".html"])
        self.send_header("Content-Length", str(len(page)))
    self.send_header("Cache-Control", "no-cache")  # as the static files: always ask, a 304 saves sending it
    self.send_header("ETag", etag)
    self.send_header("Content-Security-Policy", _PAGE_CSP)
    self.send_header("Content-Security-Policy", _SANDBOX_CSP)
    self.end_headers()
    if not fresh:
        self.wfile.write(page)


def board_page(self: Handler, _match: re.Match[str]) -> None:
    _page(self, "/board")


def leaderboard_page(self: Handler, _match: re.Match[str]) -> None:
    _page(self, "/leaderboard")


# --- units and tickets -------------------------------------------------------------------------------


def api_board_units(self: Handler, _match: re.Match[str]) -> None:
    units = [{**_unit_public(unit), "ticket": "", "variant": ""} for unit in self.app.units.all()]
    self.send_json(200, {"levels": LEVELS, "units": units}, [_ANY_ORIGIN])


def _words(text: str) -> str:
    return " ".join(re.sub(r"[^a-z0-9]+", " ", text.lower()).split())


def ticket_row(item: Any) -> dict | None:
    """The board's row for one ticket of ``Tickets.list()``: ``None`` when it has no key. Reads key, title, status."""
    if not isinstance(item, dict) or not isinstance(item.get("key"), str) or not item["key"]:
        return None
    title, status = item.get("title"), item.get("status")
    title = " ".join(title.split())[:MAX_TEXT] if isinstance(title, str) else ""
    status = " ".join(status.split())[:MAX_TEXT] if isinstance(status, str) else ""
    state = _STATE_OF.get(_words(status))
    return {"id": item["key"], "title": title, "state": state or "todo", "unit": "", "note": "" if state else status}


class _TicketBoard:
    """The rows of tickets.json, with what a failing Jira must not cost: one call at a time, none for ``RETRY_S`` after a
    failure, and the last good rows meanwhile."""

    def __init__(self) -> None:
        self._busy = threading.Lock()
        self._rows: list[dict] = []
        self._retry_at = 0.0

    def rows(self, tickets: Any) -> list[dict]:
        if time.monotonic() < self._retry_at or not self._busy.acquire(blocking=False):
            return self._rows
        try:
            listed = tickets.list()
            self._rows = [row for row in map(ticket_row, listed) if row is not None]
        except NotConfigured:  # no Jira here (the usual case without credentials): no tickets, nothing to report
            self._rows = []
        except Exception as err:  # noqa: BLE001 - whatever Jira's client raises, the board stays up
            self._retry_at = time.monotonic() + RETRY_S
            log.warning("board: tickets are not available (%s)", type(err).__name__)  # the type only: no text, no URL
        finally:
            self._busy.release()
        return self._rows


_boards: weakref.WeakKeyDictionary[App, _TicketBoard] = weakref.WeakKeyDictionary()
_boards_guard = threading.Lock()


def _tickets_of(app: App) -> list[dict]:
    if app.tickets is None:
        return []
    with _boards_guard:
        board = _boards.setdefault(app, _TicketBoard())
    return board.rows(app.tickets)


def api_board_tickets(self: Handler, _match: re.Match[str]) -> None:
    self.send_json(200, {"states": list(STATES), "tickets": _tickets_of(self.app)}, [_ANY_ORIGIN])


# --- what the receiver and the eval batch wrote -------------------------------------------------------

_NAME = re.compile(r"[A-Za-z0-9_]{1,40}")
_WORD = re.compile(r"[A-Za-z0-9_]{1,32}")
_TILE_ROW = re.compile(r"[01]{1,256}")
_METRIC_WORDS = ("state", "psnr_src")  # the two metrics that are text (tsrecv: "streaming", "truth", ...)
MAX_TILE_ROWS = 64
MAX_DEPTH = 2  # objects inside objects: daemon, commands

# harness/eval/results.py: what build_board writes; a run row is SLIM_KEYS and a variant row is aggregate()'s, without
# overlay_from (a path on the facilitator's disk), overlay_commit and applied_knobs (the unit's card). Nothing else goes out.
_RUN_KEYS = (
    "experiment", "variant", "ticket", "repeat", "success", "hidden_pass", "workflow_committed", "commits",
    "run_status", "status", "ai_credits", "interventions", "approvals", "escalations", "retries", "duration_s",
    "gate_violations", "gate_violation_score", "reviewer_score", "error", "finished_at", "branch",
)  # fmt: skip
_VARIANT_KEYS = (
    "variant", "label", "tests", "runs", "passes", "pass_rate", "pass_ci", "pass_rate_sd", "commits", "commit_rate",
    "credits_mean", "credits_total", "credits_per_pass", "interventions_mean", "approvals_mean", "escalations_mean",
    "retries_mean", "duration_mean_s", "gate_violations_mean", "gate_score_mean", "gate_catches_total",
    "reviewer_score_mean", "path_violations_total", "diff_lines_mean", "tokens_in_total", "tokens_out_total",
    "tokens_cached_total", "errors", "per_ticket", "stale",
)  # fmt: skip


def _no_constant(name: str) -> float:
    raise ValueError(f"{name} is not JSON")


def _finite(text: str) -> float:
    value = float(text)
    if not math.isfinite(value):  # 1e999: a browser would read it, send_json would not write it back
        raise ValueError("number out of range")
    return value


def _json_object(root: Path, parts: tuple[str, ...], limit: int) -> dict | None:
    """The JSON object in the plain file ``root/parts``; ``None`` for anything else (a missing file, a link, too large,
    not JSON, NaN, a list)."""
    data = read_plain(root, parts, limit)
    if data is None:
        return None
    try:
        value = json.loads(data, parse_constant=_no_constant, parse_float=_finite)
    except (ValueError, RecursionError):  # bad JSON, bad UTF-8, nested too deep
        return None
    return value if isinstance(value, dict) else None


def _public_metrics(metrics: dict, depth: int = 0) -> dict:
    """What of a metrics object the page may show: it puts these values into HTML, so only numbers, booleans, objects
    of those, two one-word texts and the 0/1 rows of ``tiles`` are kept."""
    out: dict[str, Any] = {}
    for key, value in metrics.items():
        if not _NAME.fullmatch(key):
            continue
        if key == "tiles":  # the page walks these as rows of text
            if isinstance(value, list):
                out[key] = [row for row in value[:MAX_TILE_ROWS] if isinstance(row, str) and _TILE_ROW.fullmatch(row)]
        elif key in _METRIC_WORDS:
            if isinstance(value, str) and _WORD.fullmatch(value):
                out[key] = value
        elif isinstance(value, (bool, int, float)):
            out[key] = value
        elif isinstance(value, dict) and depth < MAX_DEPTH:
            out[key] = _public_metrics(value, depth + 1)
    return out


def api_board_metrics(self: Handler, _match: re.Match[str]) -> None:
    metrics = _json_object(self.app.cfg.data_dir, ("board", "metrics.json"), MAX_BYTES)
    self.send_json(200, {} if metrics is None else _public_metrics(metrics), [_ANY_ORIGIN])


def api_board_frame(self: Handler, _match: re.Match[str]) -> None:
    frame = read_plain(self.app.cfg.data_dir, ("board", "frame.pgm"), MAX_BYTES)
    if frame is None or not frame.startswith(b"P5") or not frame[2:3].isspace():  # a binary PGM
        raise HttpError(404, "not found", headers=[_ANY_ORIGIN])  # the page sees "no frame", not a blocked request
    self.send_response(200)
    self.send_header("Content-Type", "image/x-portable-graymap")
    self.send_header("Content-Length", str(len(frame)))
    self.send_header("Cache-Control", "no-store")
    self.send_header(*_ANY_ORIGIN)
    self.end_headers()
    self.wfile.write(frame)


def _rows(rows: Any, keys: tuple[str, ...]) -> list[dict]:
    """The dict rows of ``rows`` with only ``keys``; the text of an ``error`` becomes the word ``error`` (the page counts
    the failures, and what they say can name paths)."""
    out = []
    for row in rows if isinstance(rows, list) else ():
        if isinstance(row, dict):
            picked = {key: row[key] for key in keys if key in row}
            if picked.get("error"):
                picked["error"] = "error"
            out.append(picked)
    return out


def _public_results(results: dict, stale: Collection[str] = ()) -> dict:
    """The allowlisted part of the leaderboard file; ``stale`` are the variants to mark (``stale: true``)."""
    out: dict[str, Any] = {"schema": results.get("schema") if isinstance(results.get("schema"), int) else 1}
    out["generated_at"] = results["generated_at"] if isinstance(results.get("generated_at"), str) else ""
    for name in ("experiments", "tickets"):
        values = results.get(name)
        out[name] = [value for value in values if isinstance(value, str)] if isinstance(values, list) else []
    out["variants"] = _rows(results.get("variants"), _VARIANT_KEYS)
    for row in out["variants"]:
        row.pop("stale", None)  # the mark is the console's own: what the file says about it is not believed
        variant = row.get("variant")
        if isinstance(variant, str) and variant in stale:  # a damaged row (a list is not hashable) is only not marked
            row["stale"] = True
    out["runs"] = _rows(results.get("runs"), _RUN_KEYS)
    return out


class _Hashes:
    """The config hash of the harness files of each unit as of now, reused for ``HASH_TTL_S`` seconds: hashing the
    files of a unit is disk work, and this is a public route that every browser polls."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._seen: dict[str, tuple[float, str | None]] = {}

    def of(self, app: App, unit_id: str) -> str | None:
        """The hash, or ``None`` when the unit is not there or its files cannot be read."""
        now = time.monotonic()
        with self._guard:
            seen = self._seen.get(unit_id)
        if seen is not None and now - seen[0] < HASH_TTL_S:
            return seen[1]
        try:
            digest: str | None = structure.config_hash(app.units.get(unit_id).worktree, app.cfg.overlay)
        except (KeyError, OSError):
            digest = None
        with self._guard:
            self._seen[unit_id] = (now, digest)
        return digest


_hashes: weakref.WeakKeyDictionary[App, _Hashes] = weakref.WeakKeyDictionary()


def _recorded(app: App, experiment: str, cache: dict[str, dict]) -> dict:
    """The ``hashes`` of ``<experiment>.meta.json`` (``{}`` when there is no usable record)."""
    if experiment not in cache:
        meta = _json_object(app.cfg.data_dir, ("evals", experiment + META_SUFFIX), META_MAX_BYTES)
        hashes = meta.get("hashes") if meta is not None else None
        cache[experiment] = hashes if isinstance(hashes, dict) else {}
    return cache[experiment]


def _stale_variants(app: App, results: dict) -> set[str]:
    """The ``U<NN>`` variants of ``results`` whose unit changed its harness files after a batch that ran it (see the
    module doc): the hash recorded at a launch differs from the hash now. Nothing is read for a variant with no record."""
    ran: dict[str, set[str]] = {}  # variant -> the experiments that have a run row of it
    for row in results.get("runs") if isinstance(results.get("runs"), list) else ():
        if isinstance(row, dict) and isinstance(row.get("variant"), str) and isinstance(row.get("experiment"), str):
            ran.setdefault(row["variant"], set()).add(row["experiment"])
    listed = results.get("experiments") if isinstance(results.get("experiments"), list) else []
    with _boards_guard:
        hashes = _hashes.setdefault(app, _Hashes())
    stale: set[str] = set()
    cache: dict[str, dict] = {}
    for row in results.get("variants") if isinstance(results.get("variants"), list) else ():
        variant = row.get("variant") if isinstance(row, dict) else None
        unit_id = unit_of(variant)
        if unit_id is None:
            continue
        for experiment in sorted(ran.get(variant) or {name for name in listed if isinstance(name, str)}):
            recorded = _recorded(app, experiment, cache).get(variant) if EXPERIMENT.fullmatch(experiment) else None
            if isinstance(recorded, str) and recorded:
                now = hashes.of(app, unit_id)
                if now is not None and now != recorded:
                    stale.add(variant)
                    break
    return stale


# --- personal mode: the experiments run from this checkout ----------------------------------------------


def _places(app: App) -> list[tuple[str, Path]]:
    """Where personal mode looks for results: ``(name, folder that holds results/)``, the checkout first."""
    places = [("checkout", app.cfg.source_repo)]
    try:
        places.append(("console copy", app.units.get(PERSONAL_UNIT).worktree))
    except KeyError:  # not built yet
        pass
    return places


def _result_files(root: Path) -> list[tuple[str, int, int]]:
    """``(name, mtime_ns, size)`` of the plain ``results/*.jsonl`` files under ``root`` (at most ``MAX_RESULT_FILES``)."""
    folder = root / "results"
    files = []
    try:
        if folder.is_symlink() or not folder.is_dir():
            return []
        with os.scandir(folder) as found:
            for entry in found:
                if entry.name.endswith(".jsonl") and entry.is_file(follow_symlinks=False):
                    stat = entry.stat(follow_symlinks=False)
                    files.append((entry.name, stat.st_mtime_ns, stat.st_size))
    except OSError:
        return []
    return sorted(files)[:MAX_RESULT_FILES]


def _records(data: bytes) -> list[dict]:
    """The run records of one ``.jsonl`` file as ``harness.eval.results.load_records`` takes them (schema 1), with the
    keys ``build_board`` needs in a usable shape; a torn or odd line is left out."""
    out = []
    for line in data.decode("utf-8", errors="replace").splitlines():
        if not line.strip():
            continue
        try:
            rec = json.loads(line, parse_constant=_no_constant, parse_float=_finite)
        except (ValueError, RecursionError):
            continue
        if not isinstance(rec, dict) or rec.get("schema") != 1:
            continue
        if not all(isinstance(rec.get(key), str) for key in ("experiment", "variant", "ticket")):
            continue
        repeat = rec.get("repeat", 0)
        if not isinstance(repeat, int) or isinstance(repeat, bool):
            continue
        out.append(rec)
    return out


def _finished(rec: dict) -> str:
    return rec["finished_at"] if isinstance(rec.get("finished_at"), str) else ""


def _experiment_runs(runs: list[tuple[str, dict]]) -> list[dict]:
    """One line per experiment, newest first: ``{experiment, runs, done, last, where}``; ``where`` is the place of its
    newest run (``checkout`` or ``console copy``). ``runs`` are ``(place, record)``, oldest first, one per run."""
    seen: dict[str, dict] = {}
    for place, rec in runs:
        row = seen.setdefault(rec["experiment"], {"experiment": rec["experiment"], "runs": 0, "done": 0, "last": "", "where": place})
        row["runs"] += 1
        row["done"] += 1 if rec.get("run_status") == "done" else 0
        if _finished(rec) >= row["last"]:
            row["last"], row["where"] = _finished(rec), place
    return sorted(seen.values(), key=lambda row: (row["last"], row["experiment"]), reverse=True)


def _personal_board(places: list[tuple[str, Path, list[tuple[str, int, int]]]], experiment: str) -> dict:
    """The answer of ``GET /api/eval/results.json`` in personal mode (see the module doc)."""
    tagged: list[tuple[str, dict]] = []
    budget = PERSONAL_MAX_BYTES
    for name, root, files in places:
        for file_name, _mtime, size in files:
            if size > min(budget, RESULTS_MAX_BYTES):
                log.warning("results/%s not read: too large, or the result files are too large together", file_name)
                continue
            data = read_plain(root, ("results", file_name), RESULTS_MAX_BYTES)
            if data is None:
                continue
            budget -= len(data)
            tagged.extend((name, rec) for rec in _records(data))
    # oldest first, so that the newest record of a run wins: a run found in both places, or run again by --resume
    tagged.sort(key=lambda item: _finished(item[1]))
    latest: dict[tuple, tuple[str, dict]] = {}
    for place, rec in tagged:
        latest[(rec["experiment"], rec["variant"], rec["ticket"], rec.get("repeat", 0))] = (place, rec)
    runs = sorted(latest.values(), key=lambda item: _finished(item[1]))
    chosen = [rec for _place, rec in runs if not experiment or rec["experiment"] == experiment]
    board: dict[str, Any] = dict(EMPTY_RESULTS)
    if chosen:
        try:
            from harness.eval.results import build_board  # the harness is linked in by console.config

            board = build_board(chosen)
        except Exception:  # noqa: BLE001 - records the harness cannot add up: the empty board, the reason in the log
            log.warning("the leaderboard of this checkout could not be built", exc_info=True)
    out = _public_results(board)
    out["generated_at"] = max((_finished(rec) for rec in chosen), default="")  # stable while no run finishes
    out["experiment"] = experiment
    out["experiment_runs"] = _experiment_runs(runs)
    return out


class _PersonalBoard:
    """The answers of personal mode, made again only when a result file changed: every open Evals page polls."""

    def __init__(self) -> None:
        self._guard = threading.Lock()
        self._seen: dict[str, tuple[tuple, dict]] = {}  # experiment ("" for all) -> (the files then, the answer)

    def answer(self, app: App, experiment: str) -> dict:
        places = [(name, root, _result_files(root)) for name, root in _places(app)]
        files = tuple((name, tuple(found)) for name, _root, found in places)
        with self._guard:
            seen = self._seen.get(experiment)
        if seen is not None and seen[0] == files:
            return seen[1]
        answer = _personal_board(places, experiment)
        with self._guard:
            if len(self._seen) >= 16:
                self._seen.clear()
            self._seen[experiment] = (files, answer)
        return answer


_personal_boards: weakref.WeakKeyDictionary[App, _PersonalBoard] = weakref.WeakKeyDictionary()


def _personal_results(self: Handler) -> None:
    experiment = self.query.get("experiment", "")
    if len(experiment) > MAX_TEXT:
        raise HttpError(400, "bad experiment")
    with _boards_guard:
        boards = _personal_boards.setdefault(self.app, _PersonalBoard())
    # The full leaderboard page runs sandboxed (Origin: null) and must be let read this; a page of another site is not:
    # this is the participant's laptop, not the room's public board.
    headers = [_ANY_ORIGIN] if self.headers.get("Origin") == "null" else []
    self.send_json(200, boards.answer(self.app, experiment), headers)


def api_board_results(self: Handler, _match: re.Match[str]) -> None:
    if self.app.cfg.personal:  # the experiments run from this checkout, not the room's batch
        _personal_results(self)
        return
    results = _json_object(self.app.cfg.data_dir, ("evals", "results", "results.json"), RESULTS_MAX_BYTES)
    if results is None:
        self.send_json(200, EMPTY_RESULTS, [_ANY_ORIGIN])
    else:
        self.send_json(200, _public_results(results, _stale_variants(self.app, results)), [_ANY_ORIGIN])


# --- the request log ------------------------------------------------------------------------------------

_log_request = Handler.log_request


def log_request(self: Handler, code: Any = "-", size: Any = "-") -> None:
    """``Handler.log_request``, but a usual answer of a polled board route is a debug line (see the module doc)."""
    status = getattr(code, "value", code)
    path = getattr(self, "path", "").partition("#")[0].partition("?")[0]
    usual = isinstance(status, int) and (200 <= status < 300 or status in (304, 404))
    if getattr(self, "command", None) == "GET" and usual and _POLLED.fullmatch(path):
        subject = getattr(self, "_subject", None)
        log.debug("GET %s %s %s", _printable(path), status, subject if isinstance(subject, str) else "-")
    else:
        _log_request(self, code, size)


_HANDLERS = (board_page, leaderboard_page, api_board_units, api_board_tickets, api_board_metrics, api_board_frame,
             api_board_results, log_request)  # fmt: skip

for _handler in _HANDLERS:
    setattr(Handler, _handler.__name__, _handler)
ROUTES.extend(
    [
        Route("GET", re.compile(r"/board"), "board_page", "public"),
        Route("GET", re.compile(r"/leaderboard"), "leaderboard_page", "public"),
        Route("GET", re.compile(r"/api/board/units\.json"), "api_board_units", "public"),
        Route("GET", re.compile(r"/api/board/tickets\.json"), "api_board_tickets", "public"),
        Route("GET", re.compile(r"/api/board/metrics\.json"), "api_board_metrics", "public"),
        Route("GET", re.compile(r"/api/board/frame\.pgm"), "api_board_frame", "public"),
        Route("GET", re.compile(r"/api/eval/results\.json"), "api_board_results", "public"),
    ]
)
