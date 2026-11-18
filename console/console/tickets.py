"""Tickets from Jira: the project's list, one ticket as markdown, and the skeleton of an agent-ready ticket.

``Tickets(cfg, secrets, client=None)`` is what the endpoints in ``routes_tickets`` work with.

* ``list()``: ``[{key, title, status, type}]`` of the project in ``cfg.jira["project"]`` (JQL
  ``project = <project> ORDER BY key ASC``), kept for ``CACHE_S`` seconds per process; one refresh at a time, so ten
  units opening the view together make one Jira call. A failed call is kept for ``FAIL_CACHE_S`` seconds: the
  requests in that time get its error at once, without asking Jira (none waits 15 s in turn for its own timeout).
* ``markdown(key)``: ``# KEY: title``, ``**Type:** <type>`` and the description as markdown: the text a person reads
  next to the editor.
* ``template(key, worktree=None)``: the same header and the ``## `` headings of ``.github/ISSUE_TEMPLATE/agent-fix.md``
  in ``worktree`` (the built-in list when the file is not there), empty, with ``## Acceptance tests`` always among them.
* ``issue(key)``: the one Jira call behind both, for a caller that needs both texts.

What leaves this module is the key, the summary, the status name, the issue type name and the description. Never labels,
components, priority or custom fields (they would give away the evaluation set and the injection ticket): the requests
name their fields, and the answers are read field by field.

``Tickets`` only reads Jira. Its client is the bridge's ``JiraClient`` (from ``<harness_repo>/tools/bridge``, linked by
``harnesslink.bootstrap``) on a transport that records writes instead of sending them. Credentials come from ``Secrets``
(``JIRA_BASE_URL`` or ``JIRA_SITE``, ``JIRA_EMAIL``, ``JIRA_API_TOKEN``); the client is made on first use. The one
client that sends a write is ``make_writer``'s, for ``console.ship`` alone (a run's report on its ticket, behind the
admin switch). A failure is one of three exceptions whose text is fixed (no exception text, no URL, never a credential):

* ``NotConfigured``: no credentials (or a site that is not https);
* ``Unreachable``: Jira did not answer well; the text is the reason, e.g. ``HTTP 401``, ``timed out``;
* ``NoSuchTicket``: a key that is not of the project, or that Jira does not know.

The text of a ticket is untrusted data: it is shown, never interpreted (the harness wraps it again when a run reads it).
"""

from __future__ import annotations

import logging
import re
import threading
import time
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Callable

from console import files
from console.config import ConsoleConfig
from console.secrets import Secrets

__all__ = [
    "AGENT_SUFFIX", "CACHE_S", "FAIL_CACHE_S", "FileTickets", "Issue", "NoSuchTicket", "NotConfigured", "Tickets", "Unreachable", "make_client",
    "make_writer", "project_key",
]  # fmt: skip

log = logging.getLogger(__name__)

CACHE_S = 60  # how long the list is kept
FAIL_CACHE_S = 30  # how long a failed list call is kept: the requests in that time get its error at once
TIMEOUT_S = 15  # one Jira call; a request thread of the server waits this long at most
PAGE_SIZE = 100
MAX_PAGES = 20  # of PAGE_SIZE issues: far more than a workshop project holds
LIST_FIELDS = "summary,status,issuetype"
ISSUE_FIELDS = "summary,issuetype,description"
TEMPLATE_FILE = ".github/ISSUE_TEMPLATE/agent-fix.md"
TEMPLATE_MAX_BYTES = 64 * 1024
MAX_SECTIONS = 20
MAX_HEADING = 80
ACCEPTANCE = "Acceptance tests"
DEFAULT_SECTIONS = (
    "Description",
    "Steps to reproduce",
    "Expected",
    "Actual",
    "Environment",
    "Acceptance criteria",
    ACCEPTANCE,
    "Out of scope",
)

AGENT_SUFFIX = ".agent.md"  # personal mode: the unit's agent-ready ticket is tickets/<KEY>.agent.md (<KEY>.md is the source)
FILE_MAX_BYTES = 256 * 1024  # a ticket file of the repo is read up to this size
_FILE_KEY = re.compile(r"TS-[0-9]{3}")  # personal mode: the keys of the workshop's ticket files
_FILE_TITLE = re.compile(r"#[ \t]+(?P<key>[A-Z][A-Z0-9]*-[0-9]+)[ \t]*:[ \t]*(?P<title>\S.*)")
_FILE_TYPE = re.compile(r"\*\*Type:\*\*[ \t]*(?P<type>\S.{0,39}?)[ \t]*")  # the ticket file's own "**Type:** Bug" line
_PROJECT = re.compile(r"[A-Z][A-Z0-9]+")
_KEY = re.compile(r"[A-Z][A-Z0-9]+-[0-9]+")
_HEADING = re.compile(r"## +(?P<title>\S.*?)\s*")
_Rows = list[dict[str, str]]
_Titles = list[str]


class NotConfigured(RuntimeError):
    """Jira cannot be asked: a credential is missing or the site is not an https address."""


class Unreachable(RuntimeError):
    """Jira did not answer well; ``str(err)`` is a fixed reason without secrets."""


class NoSuchTicket(LookupError):
    """The key is not a key of the project, or Jira has no such issue."""


@dataclass(frozen=True)
class Issue:
    key: str
    title: str  # one line
    type: str
    description: str  # markdown, "" when Jira has none

    def markdown(self) -> str:
        body = self.description.strip() or "(no description)"
        return f"# {self.key}: {self.title}\n\n**Type:** {self.type}\n\n{body}\n"


def make_client(site: str, email: str, token: str, transport: Any = None) -> Any:
    """A bridge ``JiraClient`` with the two reads the console needs; ``find(jql, fields)`` and ``issue(key, fields)``.

    The bridge's own ``search`` always asks for labels as well, so these ask for exactly the fields given. Without a
    ``transport`` the network is used through a dry-run wrapper: a write that got in here would be recorded, not sent.
    ``ValueError`` when ``site`` is not https.
    """
    from jira_client import JiraClient
    from transport import DryRunTransport, UrllibTransport

    class ConsoleJira(JiraClient):
        def find(self, jql: str, fields: str) -> list[dict[str, Any]]:
            found: list[dict[str, Any]] = []
            token_ = None
            for _ in range(MAX_PAGES):
                answer = self._call(
                    "GET", "/rest/api/3/search/jql", jql=jql, fields=fields, maxResults=PAGE_SIZE, nextPageToken=token_
                )
                if not isinstance(answer, dict):
                    raise ValueError("unexpected answer")
                found += answer.get("issues") or []
                token_ = answer.get("nextPageToken")
                if not token_ or answer.get("isLast", True):
                    break
            return found

        def issue(self, key: str, fields: str) -> dict[str, Any]:
            return self._call("GET", f"/rest/api/3/issue/{key}", fields=fields)

    if transport is None:
        transport = DryRunTransport(UrllibTransport(TIMEOUT_S), log=lambda _line: None)
    return ConsoleJira(site, email, token, transport)


def _credentials(secrets: Secrets) -> tuple[str, str, str]:
    """``(https site, email, token)`` from ``Secrets``; ``NotConfigured`` when one is missing."""
    site = (secrets.get("JIRA_BASE_URL") or secrets.get("JIRA_SITE") or "").strip()
    email = secrets.get("JIRA_EMAIL") or ""
    token = secrets.get("JIRA_API_TOKEN") or ""
    if not (site and email and token):
        raise NotConfigured("Jira credentials are missing")
    return (site if "://" in site else f"https://{site}"), email, token


def make_writer(secrets: Secrets, transport: Any = None) -> Any:
    """The one Jira client whose writes are sent: ``console.ship`` reports a run on its ticket with it (the bridge's
    ``upsert_comment``), behind the admin switch. Nothing else uses it. ``NotConfigured`` like ``Tickets``."""
    from transport import UrllibTransport

    site, email, token = _credentials(secrets)
    try:
        return make_client(site, email, token, UrllibTransport(TIMEOUT_S) if transport is None else transport)
    except ValueError:  # the site is not https; the text of the error names it
        raise NotConfigured("the Jira site is not an https address") from None


def project_key(cfg: ConsoleConfig, key: object) -> bool:
    """Whether ``key`` is a well-formed issue key of the project in ``cfg.jira`` (``PROJ-5``); in personal mode one of
    the repo's ticket files (``TS-101``)."""
    if cfg.personal:
        return isinstance(key, str) and _FILE_KEY.fullmatch(key) is not None
    project = (cfg.jira or {}).get("project")
    return (
        isinstance(key, str)
        and isinstance(project, str)
        and _PROJECT.fullmatch(project) is not None
        and _KEY.fullmatch(key) is not None
        and key.rpartition("-")[0] == project
    )


def _name(value: Any) -> str:
    """The ``name`` of a Jira object such as a status or an issue type."""
    name = value.get("name") if isinstance(value, dict) else None
    return name.strip() if isinstance(name, str) else ""


def _one_line(text: Any) -> str:
    return " ".join(text.split()) if isinstance(text, str) else ""


def _reason(err: BaseException) -> str:
    """Why a Jira call failed, in words that carry nothing of the exception's text (it may hold a URL or a token)."""
    status = getattr(err, "status", None)  # the bridge's HttpError
    if isinstance(status, int) and not isinstance(status, bool):
        return f"HTTP {status}"
    if isinstance(err, TimeoutError):
        return "timed out"
    if isinstance(err, OSError):  # no route, refused, DNS, TLS
        return "connection failed"
    if isinstance(err, ValueError):  # a reply that was not what Jira sends
        return "unexpected answer"
    return "request failed"


class Tickets:
    """The Jira tickets of the workshop project. See the module docstring."""

    def __init__(
        self,
        cfg: ConsoleConfig,
        secrets: Secrets,
        client: Any = None,
        *,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self._cfg = cfg
        self._secrets = secrets
        self._client = client
        self._clock = clock
        self._client_lock = threading.Lock()
        self._list_lock = threading.Lock()
        self._cached: tuple[float, _Rows] | None = None  # (valid until, rows)
        self._failed: tuple[float, str] | None = None  # (kept until, the reason): the list's last call failed

    # -- Jira --

    @property
    def project(self) -> str:
        project = (self._cfg.jira or {}).get("project")
        if not isinstance(project, str) or not _PROJECT.fullmatch(project):
            raise NotConfigured("no Jira project")
        return project

    def _jira(self) -> Any:
        with self._client_lock:
            if self._client is None:
                site, email, token = _credentials(self._secrets)
                try:
                    self._client = make_client(site, email, token)
                except ValueError:  # the site is not https; the text of the error names it
                    raise NotConfigured("the Jira site is not an https address") from None
            return self._client

    def _ask(self, call: Callable[[Any], Any]) -> Any:
        """``call(client)``; whatever goes wrong becomes ``Unreachable`` with a fixed reason."""
        client = self._jira()
        try:
            return call(client)
        except Exception as err:
            reason = _reason(err)
            level = logging.INFO if reason == "HTTP 404" else logging.WARNING  # an unknown key is not a problem
            # never str(err): it may hold a URL or a secret
            log.log(level, "jira request failed: %s (%s)", type(err).__name__, reason)
            raise Unreachable(reason) from None

    # -- the list --

    def list(self) -> _Rows:
        """``[{key, title, status, type}]`` in key order; at most ``CACHE_S`` seconds old.

        When Jira fails (``Unreachable``), the failure is kept for ``FAIL_CACHE_S`` seconds: a request in that time gets
        the same fixed error at once, without asking Jira, instead of each waiting its turn for a call that times out.
        """
        with self._list_lock:
            now = self._clock()
            if self._failed is not None and now < self._failed[0]:
                raise Unreachable(self._failed[1])
            if self._cached is None or now >= self._cached[0]:
                try:
                    rows = self._fetch_list()
                except Unreachable as err:
                    self._failed = (self._clock() + FAIL_CACHE_S, str(err))
                    raise
                self._cached, self._failed = (now + CACHE_S, rows), None
            return [dict(row) for row in self._cached[1]]

    def _fetch_list(self) -> _Rows:
        project = self.project
        found = self._ask(lambda client: client.find(f"project = {project} ORDER BY key ASC", LIST_FIELDS))
        if not isinstance(found, list):
            raise Unreachable("unexpected answer")
        rows: _Rows = []
        for raw in found:
            if not isinstance(raw, dict):
                continue
            key, fields = raw.get("key"), raw.get("fields")
            fields = fields if isinstance(fields, dict) else {}
            if not isinstance(key, str) or not self._is_ours(key):  # a key becomes a file name: only well-formed ones
                continue
            rows.append(
                {
                    "key": key,
                    "title": _one_line(fields.get("summary")) or "(no title)",
                    "status": _name(fields.get("status")),
                    "type": _name(fields.get("issuetype")) or "Task",
                }
            )
        return rows

    # -- one ticket --

    def _is_ours(self, key: str) -> bool:
        return _KEY.fullmatch(key) is not None and key.rpartition("-")[0] == self.project

    def issue(self, key: str) -> Issue:
        """The ticket from Jira; ``NoSuchTicket`` for a key not of the project (Jira is not asked) or unknown to it."""
        if not self._is_ours(key):
            raise NoSuchTicket(key)
        try:
            raw = self._ask(lambda client: client.issue(key, ISSUE_FIELDS))
        except Unreachable as err:
            if str(err) == "HTTP 404":
                raise NoSuchTicket(key) from None
            raise
        fields = raw.get("fields") if isinstance(raw, dict) else None
        if not isinstance(fields, dict):
            raise Unreachable("unexpected answer")
        return Issue(
            key=key,
            title=_one_line(fields.get("summary")) or "(no title)",
            type=_name(fields.get("issuetype")) or "Task",
            description=_description(fields.get("description")),
        )

    def markdown(self, key: str) -> str:
        return self.issue(key).markdown()

    def template(self, key: str, worktree: Path | None = None) -> str:
        return self.template_of(self.issue(key), worktree)

    def template_of(self, issue: Issue, worktree: Path | None = None) -> str:
        """The skeleton of an agent-ready ticket for ``issue``: header, ``**Type:**``, an empty section per heading."""
        sections = [f"## {title}" for title in self._sections(worktree)]
        return "\n\n".join([f"# {issue.key}: {issue.title}", f"**Type:** {issue.type}", *sections]) + "\n"

    def _sections(self, worktree: Path | None) -> _Titles:
        titles = self._template_titles(worktree) or list(DEFAULT_SECTIONS)
        titles = titles[:MAX_SECTIONS]
        if ACCEPTANCE.casefold() not in {t.casefold() for t in titles}:
            titles.append(ACCEPTANCE)
        return titles

    def _template_titles(self, worktree: Path | None) -> _Titles:
        """The ``## `` headings of the worktree's issue template, each once, in order; ``[]`` if it cannot be read."""
        if worktree is None:
            return []
        try:
            view = files.read(worktree, TEMPLATE_FILE, self._cfg.editable, TEMPLATE_MAX_BYTES)
        except (OSError, RuntimeError, files.Forbidden, files.Invalid):  # not there, or not a plain small file
            return []
        titles: _Titles = []
        seen: set[str] = set()
        for line in (view.content or "").splitlines():
            match = _HEADING.fullmatch(line)
            title = match["title"][:MAX_HEADING] if match else ""
            if title and title.casefold() not in seen:
                seen.add(title.casefold())
                titles.append(title)
        return titles


@dataclass(frozen=True)
class FileIssue(Issue):
    """An issue read from a ticket file: ``markdown()`` is the file's text as it is (never rebuilt, never cleaned)."""

    text: str = ""

    def markdown(self) -> str:
        return self.text


class FileTickets(Tickets):
    """The tickets of personal mode: ``tickets/TS-nnn.md`` of the unit's worktree (``cfg.source_repo`` when none is
    given). Same methods as ``Tickets``; ``list``, ``issue``, ``markdown`` and ``template`` take the ``worktree``.

    Key = file stem, title = the first ``# TS-nnn: <title>`` line, status ``Open``, type ``Task``. ``TEMPLATE.md`` and
    the unit's own ``<KEY>.agent.md`` are not tickets. A file is read as it is: a ticket is untrusted text, shown and
    never interpreted (the harness sanitizer flags what must be flagged).
    """

    def __init__(self, cfg: ConsoleConfig, secrets: Secrets | None = None) -> None:
        super().__init__(cfg, secrets or Secrets())

    def _folder(self, worktree: Path | None) -> Path:
        return Path(worktree if worktree is not None else self._cfg.source_repo) / "tickets"

    def _read(self, key: str, worktree: Path | None) -> str:
        if _FILE_KEY.fullmatch(key) is None:
            raise NoSuchTicket(key)
        path = self._folder(worktree) / f"{key}.md"
        try:
            if path.is_symlink() or not path.is_file() or path.stat().st_size > FILE_MAX_BYTES:
                raise NoSuchTicket(key)
            return path.read_bytes().decode("utf-8-sig", errors="replace")
        except OSError:
            raise NoSuchTicket(key) from None

    @staticmethod
    def _title(text: str) -> str:
        first = next((line for line in text.splitlines() if line.strip()), "")
        match = _FILE_TITLE.fullmatch(first.rstrip())
        return _one_line(match["title"]) if match else "(no title)"

    @staticmethod
    def _type(text: str) -> str:
        """The type the file names in its head ("**Type:** Bug"), or "Task" when it names none."""
        for line in text.splitlines()[:12]:
            match = _FILE_TYPE.fullmatch(line.strip())
            if match:
                return _one_line(match["type"]) or "Task"
        return "Task"

    def list(self, worktree: Path | None = None) -> _Rows:  # type: ignore[override]
        try:
            names = sorted(entry.name for entry in self._folder(worktree).iterdir())
        except OSError:
            return []
        rows: _Rows = []
        for name in names:
            key = name[:-3] if name.endswith(".md") else ""
            if _FILE_KEY.fullmatch(key) is None:
                continue
            try:
                text = self._read(key, worktree)
            except NoSuchTicket:
                continue
            rows.append({"key": key, "title": self._title(text), "status": "Open", "type": self._type(text)})
        return rows

    def issue(self, key: str, worktree: Path | None = None) -> Issue:  # type: ignore[override]
        text = self._read(key, worktree)
        return FileIssue(key=key, title=self._title(text), type=self._type(text), description=text, text=text)

    def markdown(self, key: str, worktree: Path | None = None) -> str:  # type: ignore[override]
        return self.issue(key, worktree).markdown()

    def template(self, key: str, worktree: Path | None = None) -> str:
        return self.template_of(self.issue(key, worktree), worktree)


def _description(value: Any) -> str:
    """The description as markdown: Jira's document format through the bridge's converter, or the text as it is."""
    from jira_client import adf_to_markdown

    try:
        return adf_to_markdown(value) if isinstance(value, (dict, str)) or value is None else ""
    except (AttributeError, TypeError, ValueError, RecursionError):  # a document that is not shaped like ADF
        return ""
