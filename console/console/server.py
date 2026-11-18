"""The HTTP server: routes, login, CSRF, the unit file endpoints, static files and the live event stream.

Standard library only: ``ThreadingHTTPServer`` with one daemon thread per connection (at most 256 connections at a
time; the rest are closed unanswered). Every request goes through ``Handler._handle``, in this order:

0. Host: the ``Host`` header must name this server (``localhost``, ``127.0.0.1``, ``[::1]``, an address of this machine
   that ``lan_urls`` found at startup, the address the server was bound to, this machine's own name and
   ``<name>.local``, or a name in ``allowed_hosts`` of the config) with the port it is served on;
   else 421 on every method. A browser that was lured to a DNS name of an attacker that points at this machine sends
   that name as ``Host``.
1. The path picks the route (``ROUTES``; an ``App`` works on its own copy, ``App.routes``): no match is 404, a match
   for another method is 405 with ``Allow``. Patterns are anchored regexes on the path without its query.
2. CSRF: any method but GET needs an ``Origin`` header that is ``http://`` plus the request's own ``Host``; else 403.
   A GET never changes anything.
3. Access: ``public`` needs nothing; ``unit`` needs a session (401); ``admin`` needs the admin session (403 for a
   unit). On a ``unit`` route a unit session may only name itself in ``?unit=`` (403); the admin names the unit it
   works on there (``Handler.target_unit``).
4. The handler method named by the route runs. A ``HttpError`` it raises is the JSON answer ``{"error": ...}``;
   anything else is logged with its traceback and answered with a plain 500. Errors never carry git output or host
   paths: those stay in the log.

The session is the cookie ``hc_session`` (``auth.Sessions``; the key is in ``<data>/secret.key``). Bodies are JSON
objects, at most 1 MB, with ``Content-Type: application/json``, all of it within ``BODY_DEADLINE_S``; a body that the
answer comes without (an early error) is read and dropped first, so the browser gets the answer and the connection
stays usable (``Handler._skip_body``).
The request log has one line per request: method, path without query, status, who is logged in; never a body, a
cookie or a header.

What anything published on the ``global`` topic carries is public to the LAN, like ``GET /api/units``.

To add an endpoint: a ``Route`` in ``ROUTES`` (or in ``App.routes`` of one app) naming a ``Handler`` method that
takes the regex match, reads ``self.query`` / ``self.json_body()`` / ``self.target_unit()`` and answers with
``self.send_json`` or raises ``HttpError``. ``make_server`` refuses a route whose method does not exist.

A group of endpoints can live in a module of its own (``routes_structure``, ``routes_runs``, ``routes_tickets``,
``routes_board``, ``routes_evals``, ``routes_admin``, ``routes_guides``, ``routes_ship``, ``routes_workshop``):
importing it adds its ``Route`` entries to ``ROUTES`` and its handler functions to ``Handler``. They are imported at the
end of this file, once ``ROUTES``, ``Route``, ``HttpError`` and ``Handler`` exist.

An event stream (``Handler.event_stream``) is subscribed to its topic before its headers go out, holds one of the
client's ``MAX_STREAMS_PER_CLIENT`` slots (429 beyond) and ends the connection when it ends.

Known secret values (``App.secrets``, ``Secrets.hidden_values``) never reach a browser: every JSON answer
(``Handler.send_json``) and every event stream frame (``_SecretsHidden``) shows ``<secret>`` in their place. A unit
file's own text is the exception (``GET /api/files/read``, and the file in a save's answer): the editor saves it back.

Only this site may frame a console page: every HTML answer carries ``Content-Security-Policy: frame-ancestors 'self'``
(unless it has a policy of its own, as the board pages do) and ``X-Frame-Options: SAMEORIGIN``; no fuller policy
(Monaco).

Personal mode (``cfg.personal``: one participant's console on their own laptop) changes three things. A client that is
not this computer (not a loopback address) gets 403 on everything, before the Host check, and so does a request that
came through a proxy or a tunnel (``_PROXY_HEADERS``: it arrives from loopback, but for someone else); the server
binds loopback only and its ``Host`` names are the loopback ones. There is no login: every request is the session of
``units.PERSONAL_UNIT`` whatever its cookie says, and ``/api/me`` adds ``"personal": true``; the Host, Origin and
``?unit=`` checks are the same. The routes of the room's pages are left out (``_shared_only``: Admin, Board, Ship, so
they answer 404; not the leaderboard, which personal mode makes of the checkout's own experiments: ``routes_board``);
the handover run, a run with another unit's harness, is refused with 404 by ``routes_runs``. Only
personal mode serves ``GET /api/setup`` (what ``harness doctor --depth`` says of the laptop: ``console.setupcheck``,
answered at once, ``checking`` while the doctor runs; 403 with ``Sec-Fetch-Site: cross-site``, as it can start the
doctor) and ``POST /api/setup`` ("Check again"); the room's console answers 404 there (``_PERSONAL_ONLY``), as it
does on the day's commands of the Today page (``routes_workshop``: ``/api/workshop/...``, run in the console's copy).
``/api/me`` names the unit's folder in personal mode (``folder``: the worktree under ``.console-data``), for the CLI
drawer.
"""

from __future__ import annotations

import contextlib
import dataclasses
import functools
import ipaddress
import json
import logging
import os
import re
import socket
import socketserver
import sys
import threading
import time
from dataclasses import dataclass
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Iterator, Sequence
from urllib.parse import parse_qs, unquote

from console import events, files, runs, sse, validate
from console.admin import DoctorJob
from console.atomic import atomic_write
from console.auth import RateLimit, Sessions
from console.config import ConsoleConfig
from console.evals import Evals
from console.runs import Runner, RunStore
from console.secrets import Secrets
from console.setupcheck import SetupCheck
from console.ship import Ship
from console.sse import Broker, StreamLimit, Subscriber
from console.tickets import FileTickets, Tickets
from console.units import PERSONAL_UNIT, Unit, UnitStore
from console.workshop import Jobs

__all__ = [
    "App", "Handler", "HttpError", "ROUTES", "Route", "allowed_hosts", "lan_urls", "make_server", "rotate_session_key",
]  # fmt: skip

log = logging.getLogger(__name__)

WEB_DIR = Path(__file__).resolve().parents[1] / "web"
COOKIE = "hc_session"
MAX_BODY = 1024 * 1024
BODY_DEADLINE_S = 10.0  # how long a request body may take to arrive, all of it (read or dropped)
_KEY_BYTES = 32
_LOGIN_ATTEMPTS, _LOGIN_WINDOW_S = 20, 60  # failed logins per address: a room behind one NAT makes typos (25^6 codes)
MAX_CONNECTIONS = 256
MAX_STREAMS_PER_CLIENT = 8
_LOOPBACK_HOSTS = ("localhost", "127.0.0.1", "[::1]")
# Headers a reverse proxy or a tunnel (ngrok, a port forward) adds: personal mode refuses a request that has one.
_PROXY_HEADERS = ("Forwarded", "X-Forwarded-For", "X-Forwarded-Host", "X-Forwarded-Proto", "X-Real-IP", "Via")
_MAX_QUERY_FIELDS = 20
_UNIT_ID = re.compile(r"unit-[0-9]{2}")
_BAD_STATIC_NAME = re.compile(r'[\\:*?"<>|\x00-\x1f]')
_MIME = {
    ".html": "text/html; charset=utf-8",
    ".js": "text/javascript; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".svg": "image/svg+xml",
    ".json": "application/json; charset=utf-8",
    ".woff2": "font/woff2",
    ".ttf": "font/ttf",
    ".png": "image/png",
    ".ico": "image/x-icon",
}
_PLACEHOLDER = (
    b'<!doctype html><html lang="en"><meta charset="utf-8"><title>Harness Console</title>'
    b"<body><h1>Harness Console</h1><p>The console is running, but its web page is not installed yet.</p></body></html>"
)
_UNSET: Any = object()
_FRAME_ANCESTORS = "frame-ancestors 'self'"


class HttpError(Exception):
    """An answer to send instead of the normal one: ``{"error": error, **body}`` with ``status``."""

    def __init__(self, status: int, error: str | None = None, *, headers: Sequence[tuple[str, str]] = (), **body: Any):
        super().__init__(error or str(status))
        self.status = status
        self.body = ({"error": error} if error else {}) | body
        self.headers = list(headers)


# --- routes ----------------------------------------------------------------------------------------


@dataclass(frozen=True)
class Route:
    """``handler`` is the name of a ``Handler`` method, called with the regex match of ``pattern`` on the path.

    ``access``: ``public``, ``unit`` (a unit's session, or the admin naming a unit) or ``admin``.
    """

    method: str
    pattern: re.Pattern[str]
    handler: str
    access: str

    def __post_init__(self) -> None:
        if self.access not in ("public", "unit", "admin"):
            raise ValueError(f"route {self.method} {self.pattern.pattern}: unknown access {self.access!r}")


def _route(method: str, pattern: str, handler: str, access: str) -> Route:
    return Route(method, re.compile(pattern), handler, access)


# The routes of the Board and Ship pages (by handler), which personal mode leaves out with every ``admin`` route. Not
# ``api_board_results`` and ``leaderboard_page``: the Evals page shows the leaderboard of the experiments run from the
# checkout (``/api/eval/results.json``) and links the full page (``/leaderboard``). The batch's launch and stop are
# ``admin`` routes: personal mode has none.
_ROOM_PAGES = re.compile(r"board_page|api_board_(?!results$)\w+|api_ship_\w+")


def _shared_only(route: Route) -> bool:
    """A route of the room's pages (Admin, Board, Ship): personal mode does not serve it, so it answers 404 there."""
    return route.access == "admin" or _ROOM_PAGES.fullmatch(route.handler) is not None


# the participant's own laptop (its setup, the day's commands run in its checkout): 404 in the room
_PERSONAL_ONLY = frozenset({
    "api_setup", "api_setup_again",
    "api_workshop_commands", "api_workshop_run", "api_workshop_recorded", "api_workshop_jobs", "api_workshop_job",
    "api_workshop_job_stop",
})  # fmt: skip


def _served(route: Route, personal: bool) -> bool:
    """Whether a console of this mode serves ``route``."""
    return not (_shared_only(route) if personal else route.handler in _PERSONAL_ONLY)


ROUTES: list[Route] = [
    _route("POST", r"/api/login", "api_login", "public"),
    _route("POST", r"/api/logout", "api_logout", "public"),
    _route("GET", r"/api/me", "api_me", "public"),
    _route("GET", r"/api/setup", "api_setup", "unit"),
    _route("POST", r"/api/setup", "api_setup_again", "unit"),
    _route("GET", r"/api/units", "api_units", "public"),
    _route("GET", r"/api/stream", "api_stream", "public"),
    _route("GET", r"/api/files/tree", "api_files_tree", "unit"),
    _route("GET", r"/api/files/read", "api_files_read", "unit"),
    _route("POST", r"/api/files/write", "api_files_write", "unit"),
    _route("GET", r"/api/files/diff", "api_files_diff", "unit"),
    _route("GET", r"/", "static_index", "public"),
    _route("GET", r"/web/(?P<path>.+)", "static_file", "public"),
]


# --- the app ---------------------------------------------------------------------------------------


def _session_key(data_dir: Path) -> bytes:
    """The key sessions are signed with: 32 random bytes in ``<data>/secret.key``, made on the first start."""
    path = data_dir / "secret.key"
    try:
        key = path.read_bytes()
    except FileNotFoundError:
        data_dir.mkdir(parents=True, exist_ok=True)
        try:
            fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL | getattr(os, "O_BINARY", 0), 0o600)
        except FileExistsError:  # another start made it between our two looks
            key = path.read_bytes()
        else:
            key = os.urandom(_KEY_BYTES)
            with os.fdopen(fd, "wb") as f:
                f.write(key)
    if len(key) < _KEY_BYTES:
        raise RuntimeError(f"{path} is damaged (it should hold {_KEY_BYTES} bytes): delete it; everyone logs in again")
    return key


def rotate_session_key(data_dir: Path) -> bytes:
    """Replace ``<data>/secret.key`` with a new random key (one atomic step) and return it.

    Sessions are stateless: a token is valid as long as it verifies against the key. A new key ends every session at
    once, which is what a new unit code needs (the old code's cookie would stay valid otherwise). A console that is
    running must take the returned key into its ``Sessions`` (the admin endpoint does); the next start reads the file.
    The file is private to its owner where the OS supports it (``atomic_write``).
    """
    key = os.urandom(_KEY_BYTES)
    atomic_write(data_dir / "secret.key", key)
    return key


class App:
    """What the handlers share. Later tasks extend ``routes``.

    ``golden`` is the harness config at ``start_ref`` of ``<data>/source``, read once; ``check`` is the save rule
    function bound to it and to the allowed models. ``web_dir`` is the folder ``/web/*`` is served from. ``runner`` runs
    the units' harness runs (records in ``<data>/runs``); it is built here unless given, and not started: whoever
    serves the app starts it (``Runner.start``) and shuts it down. What a facilitator's continue at a credit cap gives a
    run comes from the golden harness.yaml. ``evals`` holds the variant cards and the eval batch (files under
    ``<data>/evals``); it starts nothing until the admin launches a batch. ``doctor`` runs the doctor's checks for the
    admin page, in a thread of its own, when asked. ``workshop`` (personal mode) runs the day's commands of
    ``<checkout>/workshop/commands.json`` for the Today page (``console.workshop``); whoever serves the app stops what
    still runs at the end (``Jobs.shutdown``). ``ship`` pushes the units' runs to GitHub and reports them on Jira,
    behind the admin switch "GitHub writes on" (``<data>/ship.json``). In personal mode ``routes`` leaves out the
    routes of the room's pages (``_shared_only``).
    """

    def __init__(
        self,
        cfg: ConsoleConfig,
        *,
        units: UnitStore | None = None,
        broker: Broker | None = None,
        secrets: Secrets | None = None,
        runner: Runner | None = None,
        evals: Evals | None = None,
        ship: Ship | None = None,
    ) -> None:
        self.cfg = cfg
        self.units = UnitStore(cfg) if units is None else units
        self.units.all()  # fails with "run units init" before anything else when there are no units yet
        self.broker = Broker() if broker is None else broker
        self.secrets = Secrets.from_file(cfg.env_file) if secrets is None else secrets
        events.set_secret_values(self.secrets.hidden_values())  # hidden from every browser from now on
        self.golden = validate.load_golden(cfg.data_dir / "source", cfg.start_ref, also=cfg.catchup_refs)
        self.check = functools.partial(validate.check, golden=self.golden, allowed_models=cfg.allowed_models)
        self.sessions = Sessions(_session_key(cfg.data_dir))
        self.login_limit = RateLimit(_LOGIN_ATTEMPTS, _LOGIN_WINDOW_S)
        self.stream_limit = StreamLimit(MAX_STREAMS_PER_CLIENT)
        self.routes: list[Route] = [r for r in ROUTES if _served(r, cfg.personal)]
        self.web_dir = WEB_DIR
        if runner is None:
            runner = Runner(cfg, self.units, RunStore(cfg.data_dir / "runs"), self.broker,
                            extension=runs.extension_credits(self.golden.harness, cfg.runtime if cfg.personal else None),
                            check=self.check)
        self.runner = runner
        self.evals = Evals(cfg, self.units) if evals is None else evals
        self.tickets = FileTickets(cfg, self.secrets) if cfg.personal else Tickets(cfg, self.secrets)
        self.doctor = DoctorJob(cfg)
        self.ship = Ship(cfg, self.units, self.runner, self.secrets) if ship is None else ship
        self.setup = SetupCheck(cfg) if cfg.personal else None  # what the harness doctor says of this laptop
        # the day's commands: named by the checkout's workshop/commands.json, run in the console's copy (the unit's worktree)
        self.workshop = (Jobs(cfg.source_repo, run_in=lambda: self.units.get(PERSONAL_UNIT).worktree)
                         if cfg.personal else None)


def _unit_public(unit: Unit) -> dict:
    return {
        "id": unit.id,
        "name": unit.name,
        "level": unit.level,
        "spent": unit.spent,
        "budget": unit.budget,
        "locked": unit.locked_by is not None,
    }


def _unit_own(unit: Unit) -> dict:
    return {**_unit_public(unit), "left": unit.left}


def _printable(text: str, limit: int = 200) -> str:
    return re.sub(r"[^\x20-\x7e]", "?", text)[:limit]


class _SecretsHidden:
    """The output of an event stream: each frame written to it goes out with the known secret values hidden.

    A frame (``sse``) is a few ``field: value`` lines; a ``data`` line is JSON. A frame in which no known value can
    stand (``events.secrets_in_json``, nearly every one) goes out as it is; in another, the JSON of each ``data`` line
    is decoded, has its strings hidden (``events.hide_secrets_in``) and is encoded again as ``sse`` does it, so the
    JSON stays whole whatever a value sits next to. A line that is not JSON has the values hidden as text.
    """

    def __init__(self, out: Any) -> None:
        self._out = out

    def write(self, frame: bytes) -> Any:
        text = frame.decode("utf-8", errors="surrogateescape")
        if events.secrets_in_json(text):
            frame = "\n".join(_hide_in_line(line) for line in text.split("\n")).encode("utf-8", "surrogateescape")
        return self._out.write(frame)

    def flush(self) -> None:
        self._out.flush()


def _hide_in_line(line: str) -> str:
    field, colon, value = line.partition(":")
    if field == "data" and colon:
        try:
            data = json.loads(value)
        except ValueError:
            pass
        else:
            return "data: " + json.dumps(events.hide_secrets_in(data), separators=(",", ":"), allow_nan=False)
    return events.hide_secrets(line)


# --- the request handler -----------------------------------------------------------------------------


class Handler(BaseHTTPRequestHandler):
    protocol_version = "HTTP/1.1"  # keep-alive: a page loads many files; every reply but a stream has a length
    timeout = 60  # seconds a connection may sit silent (or stuck on a write) before it is closed

    # State of the request being served. A connection serves many requests, so ``handle_one_request`` puts these
    # back first; the values here are for code that runs before a request is parsed.
    _subject: str | None = _UNSET
    _own_unit: Unit | None = None
    _body_read = False  # the request body has been read
    _started = False  # the status line is out: too late to answer with an error
    _said_close = False  # this response has a Connection header already
    _is_page = False  # this response is HTML
    _has_csp = False  # this response has a Content-Security-Policy header already
    query: dict[str, str]

    def handle_one_request(self) -> None:
        self._subject, self._own_unit = _UNSET, None
        self._body_read = self._started = self._said_close = False
        self.query = {}
        self.headers = None  # type: ignore[assignment]  # the last request's headers say nothing about this one's body
        super().handle_one_request()

    def handle_expect_100(self) -> bool:
        # curl and PowerShell ask "Expect: 100-continue" before a body. The base class answers through our
        # end_headers, which needs a response in progress; this is the bare interim answer.
        self.send_response_only(100)
        BaseHTTPRequestHandler.end_headers(self)
        return True

    def do_GET(self) -> None:
        self._handle()

    # No do_HEAD: the base class answers 501 without a body. A HEAD answer from here would carry one.
    do_POST = do_PUT = do_PATCH = do_DELETE = do_OPTIONS = do_GET

    @property
    def app(self) -> App:
        return self.server.app  # type: ignore[attr-defined]

    # -- one request --

    def _handle(self) -> None:
        try:
            self._serve()
        except HttpError as err:
            self._fail(err.status, err.body, err.headers)
        except (ConnectionError, TimeoutError):  # the browser went away (closed the tab, lost the network)
            self.close_connection = True
        except Exception:
            log.exception("unhandled error on %s %s", self.command, _printable(self.path.partition("?")[0]))
            self._fail(500, {"error": "internal error"})

    def _fail(self, status: int, body: dict, headers: Sequence[tuple[str, str]] = ()) -> None:
        if self._started:  # an answer is under way and cannot be changed: all that is left is to hang up
            self.close_connection = True
            return
        try:
            self.send_json(status, body, headers)
        except OSError:
            self.close_connection = True

    def _serve(self) -> None:
        if self.app.cfg.personal and (
            not _is_loopback_address(self.client_address[0]) or any(h in self.headers for h in _PROXY_HEADERS)
        ):  # it has no login: nobody else may use it, directly or through a proxy on this computer
            raise HttpError(403, "this console only answers this computer")
        if not self._host_is_ours():
            raise HttpError(421, "unknown host")
        path, _, query = self.path.partition("#")[0].partition("?")
        if not path.startswith("/"):
            raise HttpError(400, "bad request")
        try:
            fields = parse_qs(query, keep_blank_values=True, max_num_fields=_MAX_QUERY_FIELDS)
        except ValueError:
            raise HttpError(400, "bad query") from None
        self.query = {name: values[0] for name, values in fields.items()}
        subject = self.subject  # looked up first, so that even a 404 is logged with who asked

        matches = [(r, m) for r in self.app.routes if (m := r.pattern.fullmatch(path))]
        if not matches:
            raise HttpError(404, "not found")
        found = [(r, m) for r, m in matches if r.method == self.command]
        if not found:
            allowed = ", ".join(sorted({r.method for r, _ in matches}))
            raise HttpError(405, "method not allowed", headers=[("Allow", allowed)])
        route, match = found[0]

        if self.command != "GET" and not self._same_origin():
            raise HttpError(403, "bad origin")
        if route.access != "public":
            if subject is None:
                raise HttpError(401, "login required")
            if route.access == "admin" and subject != "admin":
                raise HttpError(403, "admin only")
            if route.access == "unit" and subject != "admin" and self.query.get("unit", subject) != subject:
                raise HttpError(403, "not your unit")
        getattr(self, route.handler)(match)

    def _host_is_ours(self) -> bool:
        parsed = _split_host(self.headers.get("Host", ""))
        return parsed is not None and parsed[1] == self.server.server_address[1] and parsed[0] in self.server.hosts

    def _same_origin(self) -> bool:
        origin, host = self.headers.get("Origin"), self.headers.get("Host")
        return bool(origin and host) and origin.lower() == f"http://{host}".lower()

    # -- who is asking --

    @property
    def subject(self) -> str | None:
        """``unit-NN``, ``admin`` or ``None``: the verified session of this request (a unit that is gone counts as none).

        In personal mode always ``PERSONAL_UNIT``, with no cookie needed and whatever cookie comes (only this computer
        gets this far)."""
        if self._subject is _UNSET:
            subject = None
            if self.app.cfg.personal:
                subject = PERSONAL_UNIT
            elif (token := self._cookie(COOKIE)) is not None:
                subject = self.app.sessions.verify(token)
            if subject is not None and subject != "admin":
                try:
                    self._own_unit = self.app.units.get(subject)
                except KeyError:
                    subject = None
            self._subject = subject
        return self._subject

    def _cookie(self, name: str) -> str | None:
        for header in self.headers.get_all("Cookie", []):
            for part in header.split(";"):
                key, eq, value = part.strip().partition("=")
                if eq and key == name:
                    return value
        return None

    def target_unit(self) -> Unit:
        """The unit a ``unit`` route works on: the session's own, or for the admin the one in ``?unit=``."""
        if self.subject is None:
            raise HttpError(401, "login required")
        if self.subject != "admin":
            assert self._own_unit is not None
            return self._own_unit
        name = self.query.get("unit")
        if name is None:
            raise HttpError(400, "unit required")
        if not _UNIT_ID.fullmatch(name):
            raise HttpError(404, "no such unit")
        try:
            return self.app.units.get(name)
        except KeyError:
            raise HttpError(404, "no such unit") from None

    # -- reading the body --

    def json_body(self) -> dict:
        """The request body as a JSON object: 400 if empty, not an object, or not all there within ``BODY_DEADLINE_S``
        (``_read_body``), 413 over 1 MB, 415 if not JSON."""
        if "Transfer-Encoding" in self.headers:
            raise HttpError(411, "length required")
        text = self.headers.get("Content-Length", "0").strip()
        if not (text.isascii() and text.isdigit()):
            raise HttpError(400, "bad content length")
        length = int(text)
        if length > MAX_BODY:
            raise HttpError(413, "body too large")
        if length == 0:
            raise HttpError(400, "JSON body required")
        if self.headers.get_content_type() != "application/json":
            raise HttpError(415, "send JSON")
        raw = self._read_body(length)
        if raw is None:
            raise HttpError(400, "incomplete body")
        try:
            body = json.loads(raw.decode("utf-8"))
        except (ValueError, RecursionError):  # bad JSON, bad UTF-8, nested too deep
            raise HttpError(400, "invalid JSON") from None
        if not isinstance(body, dict):
            raise HttpError(400, "body must be a JSON object")
        return body

    # -- answering --

    def send_json(
        self, status: int, body: object, headers: Sequence[tuple[str, str]] = (), *, as_is: bool = False
    ) -> None:
        """Answer with ``body`` as JSON. Every string in it (keys too) has the known secret values hidden
        (``events.hide_secrets_in``), unless ``as_is``: only for a file's own text, which the editor saves back."""
        if not as_is:
            body = events.hide_secrets_in(body)
        data = json.dumps(body, separators=(",", ":")).encode("utf-8")
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-store")
        for name, value in headers:
            self.send_header(name, value)
        self.end_headers()
        self.wfile.write(data)

    def send_response(self, code: int, message: str | None = None) -> None:
        self._started = True
        self._said_close = self._is_page = self._has_csp = False
        super().send_response(code, message)

    def send_header(self, keyword: str, value: str) -> None:
        name = keyword.lower()
        self._said_close = self._said_close or name == "connection"
        self._is_page = self._is_page or (name == "content-type" and value.lower().startswith("text/html"))
        self._has_csp = self._has_csp or name == "content-security-policy"
        super().send_header(keyword, value)

    def end_headers(self) -> None:
        self.send_header("X-Content-Type-Options", "nosniff")
        if self._is_page:  # only this site may frame a console page (clickjacking); a page with a policy names it
            if not self._has_csp:
                self.send_header("Content-Security-Policy", _FRAME_ANCESTORS)  # no more than that: Monaco needs eval
            self.send_header("X-Frame-Options", "SAMEORIGIN")  # for browsers that do not read frame-ancestors
        headers = getattr(self, "headers", None)  # none yet when the request line itself was bad
        if headers is not None and not getattr(self, "_body_read", False):
            self._skip_body(headers)
        if self.close_connection and not self._said_close:
            self.send_header("Connection", "close")  # tell the browser, so it does not reuse the connection
        super().end_headers()

    def _skip_body(self, headers: Any) -> None:
        """Read and drop the body of a request answered without it (an early error), before the answer goes out.

        Body bytes nobody read would be taken for the next request; and a connection closed with them unread is reset
        (the client may lose the answer: WinError 10053). So a body of at most ``MAX_BODY`` with a ``Content-Length``
        is read (``_read_body``: all of it within ``BODY_DEADLINE_S``) and the connection stays; a chunked body, a
        larger or malformed length, a body that stops short or is not all there by the deadline end the connection
        after the answer.
        """
        self._body_read = True
        text = headers.get("Content-Length", "0").strip() or "0"
        if "Transfer-Encoding" in headers or not (text.isascii() and text.isdigit()) or int(text) > MAX_BODY:
            self.close_connection = True
            return
        self._read_body(int(text), keep=False)

    def _read_body(self, length: int, *, keep: bool = True) -> bytes | None:
        """The ``length`` bytes of the request body (``b""`` when not ``keep``), or ``None`` when they did not all come.

        All of it must be there within ``BODY_DEADLINE_S`` of the first read: before each read the socket waits only for
        the time left, so a client that sends a byte now and then cannot hold the connection longer. A body that
        stops short, comes too slowly or fails to read ends the connection after the answer.
        """
        self._body_read = True
        deadline = time.monotonic() + BODY_DEADLINE_S
        parts, left = [], length
        try:
            while left > 0 and (wait := deadline - time.monotonic()) > 0:
                self.connection.settimeout(wait)
                chunk = self.rfile.read1(min(left, 65536))  # one receive at most: the deadline is checked in between
                if not chunk:
                    break
                left -= len(chunk)
                if keep:
                    parts.append(chunk)
        except OSError:  # the time ran out, the connection was reset
            pass
        finally:
            self.connection.settimeout(self.timeout)
        if left:
            self.close_connection = True
            return None
        return b"".join(parts)

    def log_request(self, code: Any = "-", size: Any = "-") -> None:
        """The one log line of a request (called by ``send_response``)."""
        subject = getattr(self, "_subject", None)
        log.info(
            "%s %s %s %s",
            getattr(self, "command", None) or "-",
            _printable(getattr(self, "path", "").partition("?")[0]),
            getattr(code, "value", code),
            subject if isinstance(subject, str) else "-",
        )

    def log_message(self, format: str, *args: Any) -> None:  # protocol-level problems: for debugging only
        log.debug(format, *args)

    # -- login --

    def _me(self, subject: str | None) -> dict:
        unit = None
        if subject is not None and subject != "admin":
            unit = _unit_own(self.app.units.get(subject))
        if self.app.cfg.personal:  # the page leaves out the login and the room's pages
            # the folder the console saves in (a worktree in .console-data, not the checkout): the CLI drawer's cd
            folder = str(self.app.units.get(subject).worktree) if unit is not None else ""
            return {"subject": subject, "unit": unit, "personal": True, "folder": folder}
        return {"subject": subject, "unit": unit}

    def session_cookie(self, subject: str) -> tuple[str, str]:
        """The ``Set-Cookie`` header that logs ``subject`` in, with a token signed by the key of the app now."""
        token = self.app.sessions.issue(subject)
        return "Set-Cookie", f"{COOKIE}={token}; HttpOnly; SameSite=Strict; Path=/"

    def api_login(self, _match: re.Match[str]) -> None:
        client = self.client_address[0]
        if not self.app.login_limit.allow(client):
            raise HttpError(429, "too many attempts, wait a minute")
        subject = self.app.units.login(self.json_body().get("code"))
        if subject is None:
            raise HttpError(401, "wrong code")
        self.app.login_limit.refund(client)  # only failures count: a room behind one address (a NAT) logs in
        self.send_json(200, self._me(subject), [self.session_cookie(subject)])

    def api_logout(self, _match: re.Match[str]) -> None:
        cookie = f"{COOKIE}=; HttpOnly; SameSite=Strict; Path=/; Max-Age=0"
        self.send_json(200, {"ok": True}, [("Set-Cookie", cookie)])

    def api_me(self, _match: re.Match[str]) -> None:
        self.send_json(200, self._me(self.subject))

    # -- the participant's setup (personal mode) --

    def api_setup(self, _match: re.Match[str]) -> None:
        """What ``harness doctor --depth`` says of this laptop (``SetupCheck``): answered at once, ``checking`` while
        the doctor runs in the background (the first time, and when the result is older than 10 minutes).

        This GET can start the doctor, so a request that the browser marks as sent by another site
        (``Sec-Fetch-Site: cross-site``: an image or a no-cors fetch on another page) gets 403 and starts nothing."""
        assert self.app.setup is not None  # the route is personal mode's only
        if self.headers.get("Sec-Fetch-Site", "").strip().lower() == "cross-site":
            raise HttpError(403, "another site cannot ask for the setup check")
        self.send_json(200, self.app.setup.state())

    def api_setup_again(self, _match: re.Match[str]) -> None:
        """The page's "Check again": the doctor runs now (unless it runs already); the answer is as ``GET /api/setup``'s."""
        assert self.app.setup is not None
        self.send_json(200, self.app.setup.again())

    def api_units(self, _match: re.Match[str]) -> None:
        self.send_json(200, {"units": [_unit_public(u) for u in self.app.units.all()]})

    # -- the live stream --

    def api_stream(self, _match: re.Match[str]) -> None:
        with self.event_stream("global") as sub:
            sse.stream(self.wfile, sub, self.app.broker.heartbeat_s)

    @contextlib.contextmanager
    def event_stream(self, topic: str) -> Iterator[Subscriber]:
        """Answer with an event stream of ``topic``: the subscriber, once the headers are out.

        429 when the client holds ``MAX_STREAMS_PER_CLIENT`` streams already. The subscription comes before the
        headers: whoever sees them is subscribed, and a stream that replays a file first misses nothing published
        meanwhile. When the block ends the subscription ends, the client's slot is freed and so is the connection.
        Inside the block ``self.wfile`` hides the known secret values in every frame written to it (``_SecretsHidden``).
        """
        broker, limit, client = self.app.broker, self.app.stream_limit, self.client_address[0]
        if not limit.acquire(client):
            raise HttpError(429, "too many streams")
        sub = broker.subscribe(topic)
        out = self.wfile
        try:
            self.close_connection = True
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Accel-Buffering", "no")
            self.end_headers()  # adds "Connection: close": the stream ends the connection
            self.wfile = _SecretsHidden(out)  # every frame written from here on, however it is written
            yield sub
        finally:
            self.wfile = out
            broker.unsubscribe(topic, sub)
            limit.release(client)

    # -- the unit's files --

    def api_files_tree(self, _match: re.Match[str]) -> None:
        unit, rel = self.target_unit(), self.query.get("path", "")
        try:
            entries = files.tree(unit.worktree, rel, self.app.cfg.editable)
        except files.Forbidden as err:
            raise HttpError(403, str(err)) from None
        except FileNotFoundError:
            raise HttpError(404, "not found") from None
        self.send_json(200, {"path": rel, "entries": entries})

    def api_files_read(self, _match: re.Match[str]) -> None:
        unit, rel = self.target_unit(), self.query.get("path", "")
        try:
            view = files.read(unit.worktree, rel, self.app.cfg.editable)
        except files.Forbidden as err:
            raise HttpError(403, str(err)) from None
        except FileNotFoundError:
            raise HttpError(404, "not found") from None
        # As it is, secrets and all: the editor saves this text back, so a hidden value would be overwritten in the
        # file. A secret is in a unit's file only if an agent wrote it there; the diff, which is hidden, shows it.
        self.send_json(200, dataclasses.asdict(view), as_is=True)

    def api_files_write(self, _match: re.Match[str]) -> None:
        unit = self.target_unit()
        body = self.json_body()
        path, content, base_sha = body.get("path"), body.get("content"), body.get("base_sha")
        if not all(isinstance(value, str) for value in (path, content, base_sha)):
            raise HttpError(400, "path, content and base_sha must be text")
        if self.app.units.get(unit.id).locked_by is not None:  # a run owns the worktree meanwhile
            raise HttpError(409, "unit is running")
        try:
            view = files.write(
                unit.worktree, path, content, base_sha, self.app.cfg.editable, unit.id, self.app.check,
                guard=lambda: self.app.units.holder(unit.id),  # asked again under the worktree lock: a run may start
            )
        except files.Busy:
            raise HttpError(409, "unit is running") from None
        except files.Forbidden as err:
            raise HttpError(403, str(err)) from None
        except files.Invalid as err:
            raise HttpError(422, errors=err.errors) from None
        except files.Conflict as err:  # the file now, as a read gives it: "use theirs" saves it back
            self.send_json(409, {"error": "changed", "current": dataclasses.asdict(err.current)}, as_is=True)
            return
        except (RuntimeError, OSError):  # git or the disk failed: the details (host paths, git's stderr) stay in the log
            log.error("save failed for %s", unit.id, exc_info=True)
            raise HttpError(500, "save failed") from None
        self.send_json(200, dataclasses.asdict(view), as_is=True)  # the file as saved, as a read gives it

    def api_files_diff(self, _match: re.Match[str]) -> None:
        unit, base = self.target_unit(), self.app.cfg.start_ref
        try:
            text = files.diff(unit.worktree, base)
            unsaved = files.unsaved_diff(unit.worktree)
        except (files.Invalid, RuntimeError):
            log.error("diff failed for %s", unit.id, exc_info=True)
            raise HttpError(500, "diff failed") from None
        self.send_json(200, {"base": base, "diff": text, "unsaved": unsaved})

    # -- the web page --

    def static_index(self, _match: re.Match[str]) -> None:
        found = self._static_file("index.html")
        if found is not None:
            self._send_file(*found)
            return
        self.send_response(200)
        self.send_header("Content-Type", _MIME[".html"])
        self.send_header("Content-Length", str(len(_PLACEHOLDER)))
        self.send_header("Cache-Control", "no-cache")
        self.end_headers()
        self.wfile.write(_PLACEHOLDER)

    def static_file(self, match: re.Match[str]) -> None:
        found = self._static_file(unquote(match["path"]))
        if found is None:
            raise HttpError(404, "not found")
        self._send_file(*found)

    def _static_file(self, rel: str) -> tuple[Path, str] | None:
        """``(file, MIME type)`` for ``rel`` below the web folder, or ``None`` if it is anything else.

        Refused: an empty part, a part that starts with ``.`` (so no ``..`` and no dotfile), characters Windows
        reads as something else (``\\`` ``:`` ...), a type that is not served, and a file whose real path (links
        followed) is not inside the web folder.
        """
        parts = rel.split("/")
        if any(not part or part.startswith(".") or _BAD_STATIC_NAME.search(part) for part in parts):
            return None
        mime = _MIME.get(os.path.splitext(parts[-1])[1].lower())
        if mime is None:
            return None
        try:
            root = self.app.web_dir.resolve()
            target = root.joinpath(*parts).resolve()
            if not target.is_relative_to(root) or not target.is_file():
                return None
        except OSError:
            return None
        return target, mime

    def _send_file(self, path: Path, mime: str) -> None:
        try:
            with path.open("rb") as f:
                info = os.fstat(f.fileno())
                data = f.read()
        except OSError:
            raise HttpError(404, "not found") from None
        etag = f'"{info.st_mtime_ns:x}-{len(data):x}"'
        if self.headers.get("If-None-Match") == etag:
            self.send_response(304)
            self.send_header("ETag", etag)
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            return
        self.send_response(200)
        self.send_header("Content-Type", mime)
        self.send_header("Content-Length", str(len(data)))
        self.send_header("Cache-Control", "no-cache")  # always ask, but a 304 saves sending Monaco again
        self.send_header("ETag", etag)
        self.end_headers()
        self.wfile.write(data)


# --- the server ------------------------------------------------------------------------------------


class _Server(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64  # twenty browsers load the page at once; the default backlog is 5
    allow_reuse_address = os.name != "nt"  # on Windows this would let a second console share the port
    app: App
    hosts: frozenset[str] = frozenset()  # the Host header names this server answers to (none: refuse all)

    def __init__(self, address: Any, handler: Any, max_connections: int = MAX_CONNECTIONS) -> None:
        self._slots = threading.BoundedSemaphore(max_connections)
        super().__init__(address, handler)

    def process_request(self, request: Any, client_address: Any) -> None:
        """Serve the connection in a new thread, or hang up on it at once when ``max_connections`` are being served."""
        if not self._slots.acquire(blocking=False):
            self.shutdown_request(request)
            return
        try:
            super().process_request(request, client_address)
        except BaseException:  # no thread could be started
            self._slots.release()
            raise

    def process_request_thread(self, request: Any, client_address: Any) -> None:
        try:
            super().process_request_thread(request, client_address)
        finally:
            self._slots.release()

    def server_bind(self) -> None:
        # HTTPServer.server_bind also looks the host name up (reverse DNS), which can stall for seconds on a LAN
        # without DNS; nothing here uses the name.
        socketserver.TCPServer.server_bind(self)
        self.server_name, self.server_port = self.server_address[:2]

    def handle_error(self, request: Any, client_address: Any) -> None:
        if isinstance(sys.exc_info()[1], (ConnectionError, TimeoutError)):  # a browser that went away
            return
        log.error("unhandled error serving %s", client_address[0], exc_info=True)


def make_server(
    app: App, host: str | None = None, port: int | None = None, *, max_connections: int = MAX_CONNECTIONS
) -> ThreadingHTTPServer:
    """A server for ``app`` on ``host:port`` (default: the config's; port 0 picks a free one). Not started.

    At most ``max_connections`` connections are served at once; ``Host`` headers are checked against
    ``allowed_hosts(host, app.cfg)`` and the address the socket got bound to. A personal app is served on a loopback
    address only (``ValueError`` for another).
    """
    for route in app.routes:
        if not callable(getattr(Handler, route.handler, None)):
            raise ValueError(f"route {route.method} {route.pattern.pattern}: Handler has no method {route.handler!r}")
    bind = app.cfg.host if host is None else host
    if app.cfg.personal and not _is_loopback_address(bind):
        raise ValueError(f"personal mode listens on 127.0.0.1 only, not on {bind or 'every address'}")
    server = _Server((bind, app.cfg.port if port is None else port), Handler, max_connections)
    server.app = app
    server.hosts = allowed_hosts(bind, app.cfg) | _bound_names(server.server_address)
    return server


def _is_loopback_address(address: str) -> bool:
    """Whether ``address`` (an IP address as text; an IPv6 one may carry a ``%zone``) is a loopback address of this
    machine: ``127.0.0.0/8``, ``::1``, or ``::ffff:127.x.y.z``. A name, ``0.0.0.0`` or anything else is not."""
    try:
        ip = ipaddress.ip_address(str(address).partition("%")[0])
    except ValueError:
        return False
    mapped = getattr(ip, "ipv4_mapped", None)
    return (mapped or ip).is_loopback


def _split_host(value: str) -> tuple[str, int] | None:
    """``(lower-case host, port)`` of a ``Host`` header (no port means 80), or ``None`` if it is not well formed."""
    value = value.strip().lower()
    if value.startswith("["):  # an IPv6 address: [::1] or [::1]:8765
        end = value.find("]")
        host, rest = (value[: end + 1], value[end + 1 :]) if end > 0 else ("", "x")
    else:
        host, colon, tail = value.partition(":")
        rest = colon + tail
    if not host or host == "[]":
        return None
    if not rest:
        return host, 80
    if rest.startswith(":") and rest[1:].isascii() and rest[1:].isdigit():
        return host, int(rest[1:])
    return None


def _lan_hosts(host: str) -> list[str]:
    """The names a browser can use for a server bound to ``host``: that one, or for a wildcard bind localhost and
    this machine's non-loopback IPv4 addresses (the route to the outside first)."""
    if host not in ("", "0.0.0.0"):
        return [f"[{host}]" if ":" in host else host]
    addresses: list[str] = []
    try:  # the address of the route to the outside; a UDP "connect" sends nothing
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as probe:
            probe.connect(("192.0.2.1", 9))
            addresses.append(probe.getsockname()[0])
    except OSError:
        pass
    try:
        addresses += [info[4][0] for info in socket.getaddrinfo(socket.gethostname(), None, socket.AF_INET)]
    except OSError:
        pass
    return ["localhost", *(a for a in dict.fromkeys(addresses) if not a.startswith("127.") and a != "0.0.0.0")]


def lan_urls(host: str, port: int) -> list[str]:
    """Where a browser can reach a server bound to ``host``: for a wildcard bind, localhost and this machine's addresses."""
    return [f"http://{name}:{port}" for name in _lan_hosts(host)]


def _machine_names() -> list[str]:
    """What this machine calls itself on the LAN: its host name, the short form, and ``<short>.local`` (mDNS)."""
    try:
        name = socket.gethostname().strip().lower()
    except OSError:
        return []
    if not name:
        return []
    short = name.split(".")[0]
    return [name, short, f"{short}.local"]


def _bound_names(address: Any) -> frozenset[str]:
    """The ``Host`` name for the address a socket got bound to (``server_address``): none for a wildcard."""
    host = str(address[0]).lower()
    if host in ("", "0.0.0.0", "::"):  # "0.0.0.0" typed into a browser reaches the local machine: no name to trust
        return frozenset()
    return frozenset({f"[{host}]" if ":" in host else host})


def allowed_hosts(host: str, cfg: ConsoleConfig) -> frozenset[str]:
    """The ``Host`` header names (lower case, no port) a server bound to ``host`` answers to.

    Loopback, whatever ``lan_urls`` names for ``host`` (found now, at startup), this machine's own names
    (``<hostname>``, ``<hostname>.local``), and ``cfg.allowed_hosts``. ``make_server`` adds the address it got bound to.
    In personal mode the loopback names only: a name of the network (``<hostname>.local``) could be made to point here.
    """
    if cfg.personal:
        return frozenset(_LOOPBACK_HOSTS)
    names = (*_LOOPBACK_HOSTS, *_lan_hosts(host), *_machine_names(), *cfg.allowed_hosts)
    return frozenset(name.strip().lower() for name in names)


# --- endpoint groups of their own modules ----------------------------------------------------------------
# Each adds its routes to ROUTES and its handlers to Handler when imported (so this stays the last statement).

from console import (  # noqa: E402,F401
    routes_admin, routes_board, routes_evals, routes_guides, routes_runs, routes_ship, routes_structure,
    routes_tickets, routes_workshop,
)
