"""HTTP transports for the bridge clients (GitHub, Jira).

Every external call of the bridge goes through one ``Transport``:

- ``UrllibTransport``  the real network (standard library only): https (or a
                       local server) only, and it never follows a redirect,
- ``DryRunTransport``  wraps another transport: only reads (GET/HEAD) pass
                       through; every other method (POST/PUT/PATCH/DELETE, and
                       whatever else) is NOT sent -- it is recorded in
                       ``.writes``, logged, and answered with a synthetic
                       response,
- ``ReplayTransport``  answers from a recorded fixture file (tests, ``--fixtures``):
                       no network at all; every call is kept in ``.calls``.

A fixture file is JSON::

    {"interactions": [
        {"method": "GET", "url": "/repos/o/r/issues?labels=agent%3Afix&state=open",
         "status": 200, "body": [...], "headers": {...}, "once": false},
        ...]}

``url`` is matched against the request's path and query (scheme and host are
ignored, query parameters in any order). ``url`` ending in ``*`` matches a
prefix. An interaction with ``"once": true`` is consumed by its first match;
the first unconsumed match wins, so a fixture can script a sequence.
"""

from __future__ import annotations

import json
import re
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

READ_METHODS = ("GET", "HEAD")      # a write is any other method: DryRunTransport and ReplayTransport agree on that


@dataclass
class Response:
    status: int
    body: Any = None
    headers: dict[str, str] = field(default_factory=dict)


class HttpError(RuntimeError):
    def __init__(self, method: str, url: str, status: int, body: Any = None) -> None:
        detail = body.get("message") if isinstance(body, dict) else (str(body)[:200] if body else "")
        super().__init__(f"{method} {url} -> HTTP {status}" + (f": {detail}" if detail else ""))
        self.method, self.url, self.status, self.body = method, url, status, body


class Transport:
    """request() returns a Response for 2xx, raises HttpError otherwise."""

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Any = None) -> Response:  # pragma: no cover - interface
        raise NotImplementedError


def path_and_query(url: str) -> str:
    p = urllib.parse.urlsplit(url)
    return p.path + (f"?{p.query}" if p.query else "")


def _canon(url: str) -> tuple[str, tuple[tuple[str, str], ...]]:
    p = urllib.parse.urlsplit(url)
    return p.path.rstrip("/") or "/", tuple(sorted(urllib.parse.parse_qsl(p.query, keep_blank_values=True)))


class _RefuseRedirects(urllib.request.HTTPRedirectHandler):
    """Never follow a 3xx. A request carries credentials (Authorization), and urllib would send
    them on to the Location, which may be another host. The APIs the bridge talks to need no redirect.

    The refusal is the first thing that happens: the Location is not read or parsed, so a missing,
    malformed or odd one (a ``file:`` URL, a bad IPv6 literal) is refused in the same way."""

    def http_error_302(self, req: Any, fp: Any, code: int, msg: str, headers: Any) -> Any:
        fp.close()
        raise HttpError(req.get_method(), req.full_url, code, "redirect refused")

    http_error_301 = http_error_303 = http_error_307 = http_error_308 = http_error_302


# http is allowed for a server on this machine only (the tests'): exactly localhost or 127.0.0.1,
# with an optional port and nothing else in the authority (no user name, no backslash). ASCII only:
# case folding must not make a look-alike letter (U+017F for s) match.
_LOCAL_AUTHORITY = re.compile(r"(?:localhost|127\.0\.0\.1)(?::[0-9]{1,5})?", re.IGNORECASE | re.ASCII)
_URL_UNSAFE = re.compile(r"[\x00-\x20\x7f]")        # whitespace and control characters: parsers disagree on them


def _is_https_or_local(url: str) -> bool:
    if _URL_UNSAFE.search(url):
        return False
    try:
        parts = urllib.parse.urlsplit(url)
        return ((parts.scheme == "https" and bool(parts.hostname))
                or (parts.scheme == "http" and _LOCAL_AUTHORITY.fullmatch(parts.netloc) is not None))
    except ValueError:                       # e.g. a bracketed host that is no IPv6 address
        return False


def _refuse_unless_https_or_local(method: str, url: str) -> None:
    if not _is_https_or_local(url):
        raise HttpError(method, url, 0, "refused: not an https URL (http is for localhost only)")


class UrllibTransport(Transport):
    def __init__(self, timeout_s: float = 30.0) -> None:
        self.timeout_s = timeout_s
        self._opener = urllib.request.build_opener(_RefuseRedirects())

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Any = None) -> Response:
        _refuse_unless_https_or_local(method, url)
        data = None
        hdrs = dict(headers or {})
        if body is not None:
            data = json.dumps(body).encode("utf-8")
            hdrs.setdefault("Content-Type", "application/json")
        req = urllib.request.Request(url, data=data, headers=hdrs, method=method)
        try:
            with self._opener.open(req, timeout=self.timeout_s) as resp:  # noqa: S310 - https or local, no redirect
                return Response(resp.status, _decode(resp.read()), dict(resp.headers.items()))
        except urllib.error.HTTPError as exc:
            raise HttpError(method, url, exc.code, _decode(exc.read())) from None


def _decode(raw: bytes) -> Any:
    if not raw:
        return None
    text = raw.decode("utf-8", "replace")
    try:
        return json.loads(text)
    except ValueError:
        return text


class DryRunTransport(Transport):
    """GET and HEAD go to ``inner``; every other method is recorded as a write and never sent."""

    def __init__(self, inner: Transport, log: Callable[[str], None] = print) -> None:
        self.inner = inner
        self.log = log
        self.writes: list[dict[str, Any]] = []

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Any = None) -> Response:
        method = method.upper()
        if method in READ_METHODS:
            return self.inner.request(method, url, headers=headers, body=body)
        n = len(self.writes) + 1
        self.writes.append({"method": method, "url": url, "body": body})
        brief = json.dumps(body, ensure_ascii=False)[:160] if body is not None else ""
        self.log(f"[dry-run] {method} {path_and_query(url)} {brief}".rstrip())
        fake_id = 900000 + n
        return Response(200 if method in ("PATCH", "PUT", "DELETE") else 201, {
            "id": fake_id, "number": fake_id, "dry_run": True,
            "html_url": f"dry-run://{method.lower()}{path_and_query(url)}",
            "self": f"dry-run://{method.lower()}{path_and_query(url)}",
        })


class ReplayTransport(Transport):
    def __init__(self, interactions: list[dict[str, Any]], strict_writes: bool = True) -> None:
        self.interactions = [dict(i) for i in interactions]
        self.strict_writes = strict_writes
        self.calls: list[dict[str, Any]] = []

    @classmethod
    def from_file(cls, path: Path | str, **kw: Any) -> ReplayTransport:
        data = json.loads(Path(path).read_text(encoding="utf-8"))
        return cls(data.get("interactions", []) if isinstance(data, dict) else data, **kw)

    def _match(self, method: str, url: str) -> Optional[dict[str, Any]]:
        want_path, want_q = _canon(url)
        for it in self.interactions:
            if it.get("_used") or str(it.get("method", "GET")).upper() != method:
                continue
            pattern = str(it["url"])
            if pattern.endswith("*"):
                if not path_and_query(url).startswith(pattern[:-1]):
                    continue
            elif _canon(pattern) != (want_path, want_q):
                continue
            if it.get("once"):
                it["_used"] = True
            return it
        return None

    def request(self, method: str, url: str, *, headers: Optional[dict[str, str]] = None,
                body: Any = None) -> Response:
        method = method.upper()
        self.calls.append({"method": method, "url": path_and_query(url), "body": body})
        it = self._match(method, url)
        if it is None:
            if method not in READ_METHODS and not self.strict_writes:
                return Response(201, {"id": len(self.calls), "replayed": False})
            raise HttpError(method, url, 599, {"message": "no recorded interaction"})
        status = int(it.get("status", 200))
        if status >= 400:
            raise HttpError(method, url, status, it.get("body"))
        return Response(status, json.loads(json.dumps(it.get("body"))), dict(it.get("headers") or {}))

    def writes(self) -> list[dict[str, Any]]:
        return [c for c in self.calls if c["method"] not in READ_METHODS]
