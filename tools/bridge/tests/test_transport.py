"""The real transport against local servers, and the dry-run transport's method allowlist.

The only network here is 127.0.0.1: two throw-away http.server instances on ephemeral ports.
"""

from __future__ import annotations

import threading
import urllib.request
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from typing import Callable, Iterator

import pytest

from transport import DryRunTransport, HttpError, ReplayTransport, UrllibTransport


class Local:
    """A local server whose handler answers with ``reply(handler) -> (status, headers, body)`` and
    keeps what it received in ``.seen`` as (method, path, headers)."""

    def __init__(self, reply: Callable[[BaseHTTPRequestHandler], tuple[int, dict[str, str], bytes]]) -> None:
        self.seen: list[tuple[str, str, dict[str, str]]] = []
        outer = self

        class Handler(BaseHTTPRequestHandler):
            def _answer(self) -> None:
                length = int(self.headers.get("Content-Length") or 0)
                if length:
                    self.rfile.read(length)
                outer.seen.append((self.command, self.path, dict(self.headers.items())))
                status, headers, body = reply(self)
                self.send_response(status)
                for k, v in headers.items():
                    self.send_header(k, v)
                self.send_header("Content-Length", str(len(body)))
                self.end_headers()
                if self.command != "HEAD":
                    self.wfile.write(body)

            do_GET = do_POST = do_PUT = do_PATCH = do_DELETE = do_HEAD = _answer

            def log_message(self, *args: object) -> None:      # keep the test output pristine
                pass

        self.server = ThreadingHTTPServer(("127.0.0.1", 0), Handler)
        self.port = self.server.server_address[1]
        self.thread = threading.Thread(target=self.server.serve_forever, kwargs={"poll_interval": 0.01}, daemon=True)
        self.thread.start()

    def url(self, path: str = "/", host: str = "127.0.0.1") -> str:
        return f"http://{host}:{self.port}{path}"

    def close(self) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


@pytest.fixture
def servers(monkeypatch: pytest.MonkeyPatch) -> Iterator[Callable[..., Local]]:
    # the servers are on this machine: no proxy of the laptop (environment or system settings) in between
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.delenv(name, raising=False)
    monkeypatch.setenv("NO_PROXY", "127.0.0.1,localhost")
    monkeypatch.setenv("no_proxy", "127.0.0.1,localhost")
    monkeypatch.setattr(urllib.request, "getproxies", dict)     # read when a transport builds its opener
    started: list[Local] = []

    def start(reply: Callable[[BaseHTTPRequestHandler], tuple[int, dict[str, str], bytes]]) -> Local:
        s = Local(reply)
        started.append(s)
        return s

    yield start
    for s in started:
        s.close()


@pytest.fixture
def dead_proxy(monkeypatch: pytest.MonkeyPatch) -> None:
    """A proxy in the environment that nothing listens on; used before ``servers`` it must not matter."""
    for name in ("HTTP_PROXY", "HTTPS_PROXY", "http_proxy", "https_proxy"):
        monkeypatch.setenv(name, "http://127.0.0.1:9")
    monkeypatch.delenv("NO_PROXY", raising=False)
    monkeypatch.delenv("no_proxy", raising=False)


def ok_json(_h: BaseHTTPRequestHandler) -> tuple[int, dict[str, str], bytes]:
    return 200, {"Content-Type": "application/json", "X-Seen": "yes"}, b'{"ok": true}'


# -- UrllibTransport: a request never follows a redirect --------------------------------------
@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
def test_a_redirect_is_refused_and_the_target_never_receives_the_request(servers, code: int) -> None:
    target = servers(ok_json)
    origin = servers(lambda h: (code, {"Location": target.url("/elsewhere")}, b""))
    with pytest.raises(HttpError) as exc:
        UrllibTransport(timeout_s=5).request("GET", origin.url("/start"),
                                             headers={"Authorization": "Bearer t0k"})
    assert exc.value.status == code and exc.value.method == "GET"
    assert exc.value.url == origin.url("/start")
    assert "redirect refused" in str(exc.value)
    assert [s[:2] for s in origin.seen] == [("GET", "/start")]       # the first request was made, with its header
    assert origin.seen[0][2]["Authorization"] == "Bearer t0k"
    assert target.seen == []                                          # nothing, with or without Authorization


def test_a_redirected_write_is_refused_too(servers) -> None:
    target = servers(ok_json)
    origin = servers(lambda h: (307, {"Location": target.url("/w")}, b""))
    with pytest.raises(HttpError) as exc:
        UrllibTransport(timeout_s=5).request("POST", origin.url("/w"), body={"a": 1},
                                             headers={"Authorization": "Bearer t0k"})
    assert exc.value.status == 307 and "redirect refused" in str(exc.value)
    assert target.seen == []


@pytest.mark.parametrize("code", [301, 302, 303, 307, 308])
@pytest.mark.parametrize("location", [
    None,                                        # no Location at all
    "",
    "file:///etc/hostname",                      # a scheme urllib would refuse to follow
    "ftp://example.invalid/x",
    "/relative/path",
    "http://[::1/x",                             # malformed: urllib's own parse of it raises ValueError
    "http://[127.0.0.1]/",
    "http://exa mple.invalid/x",
])
def test_every_redirect_is_refused_the_same_way_whatever_its_location(servers, code: int,
                                                                      location: str | None) -> None:
    headers = {} if location is None else {"Location": location}
    origin = servers(lambda h: (code, headers, b""))
    with pytest.raises(HttpError) as exc:
        UrllibTransport(timeout_s=5).request("GET", origin.url("/"), headers={"Authorization": "Bearer t0k"})
    assert (exc.value.status, exc.value.body) == (code, "redirect refused")
    assert str(exc.value).endswith(f"-> HTTP {code}: redirect refused")
    assert len(origin.seen) == 1


def test_the_local_servers_are_reached_with_a_proxy_in_the_environment(dead_proxy, servers) -> None:
    s = servers(ok_json)
    assert UrllibTransport(timeout_s=5).request("GET", s.url("/")).status == 200


def test_a_plain_request_still_works_over_a_local_server(servers) -> None:
    s = servers(ok_json)
    r = UrllibTransport(timeout_s=5).request("POST", s.url("/x?y=1"), body={"a": 1},
                                             headers={"Authorization": "Bearer t0k"})
    assert (r.status, r.body) == (200, {"ok": True}) and r.headers["X-Seen"] == "yes"
    assert s.seen[0][:2] == ("POST", "/x?y=1")
    assert s.seen[0][2]["Content-Type"] == "application/json" and s.seen[0][2]["Authorization"] == "Bearer t0k"


def test_an_error_status_is_an_http_error_with_the_decoded_body(servers) -> None:
    s = servers(lambda h: (422, {"Content-Type": "application/json"}, b'{"message": "nope"}'))
    with pytest.raises(HttpError, match="422: nope") as exc:
        UrllibTransport(timeout_s=5).request("PATCH", s.url("/w"), body={})
    assert exc.value.status == 422 and exc.value.body == {"message": "nope"}


# -- UrllibTransport: https, or a local server -------------------------------------------------
@pytest.mark.parametrize("url", [
    "http://example.invalid/x",                  # plain http to anywhere else
    "http://localhost.example.invalid/x",        # a name that only starts with localhost
    "http://127.0.0.1.example.invalid/x",
    "http://127.0.0.1@example.invalid/x",        # localhost as the user name of another host
    "http://localhost:80@example.invalid/x",
    "http://example.invalid\\@localhost/x",
    "http://example.invalid#@localhost/x",
    "http://localho\u017ft/x",                 # U+017F folds to s under a Unicode ignore-case match
    "http://127.0.0.2/x",                        # loopback, but not the one name that is allowed
    "http://[::1]/x",
    "http://localhost\t.example.invalid/x",
    "http://localhost/x y",
    "https://[::1/x",                            # urlsplit itself raises ValueError for these
    "http://[127.0.0.1]/",
    "http://[/x",
    "https://[x]/",
    "ftp://localhost/x",
    "file:///etc/hostname",
    "data:text/plain,x",
    "/relative/path",
    "",
])
def test_a_url_that_is_not_https_or_local_is_refused_before_anything_is_sent(url: str) -> None:
    with pytest.raises(HttpError) as exc:
        UrllibTransport(timeout_s=5).request("GET", url, headers={"Authorization": "Bearer t0k"})
    assert exc.value.status == 0 and "refused" in str(exc.value)     # HttpError, not a URLError from a connect


@pytest.mark.parametrize("url", [
    "https://api.example.invalid/x?y=1",
    "HTTPS://API.EXAMPLE.INVALID:8443/x",
    "http://localhost/x",
    "http://LOCALHOST:8080/x",
    "http://127.0.0.1:1234/x?y=1",
])
def test_https_and_local_urls_are_not_refused_by_the_url_check(url: str, monkeypatch: pytest.MonkeyPatch) -> None:
    class Reached(Exception):
        pass

    def open_(self, req, *a, **kw):                 # the opener is reached: the url check let the url through
        raise Reached(req.full_url)

    monkeypatch.setattr("urllib.request.OpenerDirector.open", open_)
    with pytest.raises(Reached) as exc:
        UrllibTransport(timeout_s=5).request("GET", url)
    assert exc.value.args == (url,)


# -- DryRunTransport: only GET and HEAD are forwarded ------------------------------------------
def dry() -> tuple[DryRunTransport, ReplayTransport, list[str]]:
    inner = ReplayTransport([{"method": m, "url": "/r", "body": [m]} for m in ("GET", "HEAD", "OPTIONS", "TRACE",
                                                                              "CONNECT", "BREW", "POST")])
    logs: list[str] = []
    return DryRunTransport(inner, logs.append), inner, logs


@pytest.mark.parametrize("method", ["GET", "HEAD", "get", "Head"])
def test_get_and_head_are_forwarded_to_the_inner_transport(method: str) -> None:
    t, inner, logs = dry()
    r = t.request(method, "https://x/r")
    assert r.body == [method.upper()] and t.writes == [] and logs == []
    assert [c["method"] for c in inner.calls] == [method.upper()]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW",
                                    "PROPFIND", "options", " GET", "GET ", "G\u0415T", ""])
def test_every_other_method_is_recorded_as_a_write_and_never_sent(method: str) -> None:
    t, inner, logs = dry()
    r = t.request(method, "https://x/r", body={"a": 1})
    assert inner.calls == []                                  # nothing reached the inner transport
    assert r.body["dry_run"] is True
    assert t.writes == [{"method": method.upper(), "url": "https://x/r", "body": {"a": 1}}]
    assert logs[0].startswith(f"[dry-run] {method.upper()} /r")


def test_the_synthetic_answers_for_the_writes_are_unchanged() -> None:
    t, _, _ = dry()
    assert t.request("POST", "https://x/w").status == 201
    assert [t.request(m, "https://x/w").status for m in ("PUT", "PATCH", "DELETE")] == [200, 200, 200]
    assert t.request("OPTIONS", "https://x/w").body["id"] == 900005


def test_a_mixed_run_forwards_only_the_reads() -> None:
    t, inner, _ = dry()
    for m in ("GET", "OPTIONS", "HEAD", "TRACE", "POST", "CONNECT"):
        t.request(m, "https://x/r")
    assert [c["method"] for c in inner.calls] == ["GET", "HEAD"]
    assert [w["method"] for w in t.writes] == ["OPTIONS", "TRACE", "POST", "CONNECT"]


# -- ReplayTransport: a write is any method that is not a read, as in DryRunTransport ----------
ALL_METHODS = ["GET", "HEAD", "POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW"]


@pytest.mark.parametrize("method", ["POST", "PUT", "PATCH", "DELETE", "OPTIONS", "TRACE", "CONNECT", "BREW",
                                    "options", ""])
def test_replay_counts_every_method_but_get_and_head_as_a_write(method: str) -> None:
    t = ReplayTransport([], strict_writes=False)
    assert t.request(method, "https://x/w", body={"a": 1}).status == 201     # unrecorded write: answered, not a 599
    assert t.writes() == [{"method": method.upper(), "url": "/w", "body": {"a": 1}}]
    strict = ReplayTransport([])
    with pytest.raises(HttpError) as exc:
        strict.request(method, "https://x/w")
    assert exc.value.status == 599 and len(strict.writes()) == 1


@pytest.mark.parametrize("method", ["GET", "HEAD"])
def test_replay_does_not_count_a_read_as_a_write(method: str) -> None:
    t = ReplayTransport([{"method": method, "url": "/r", "body": 1}], strict_writes=False)
    assert t.request(method, "https://x/r").body == 1
    assert t.writes() == []
    with pytest.raises(HttpError) as exc:                                    # an unrecorded read is an error
        t.request(method, "https://x/other")
    assert exc.value.status == 599 and t.writes() == []


def test_replay_and_dry_run_agree_on_what_a_write_is() -> None:
    reads = [{"method": m, "url": "/r", "body": 1} for m in ("GET", "HEAD")]
    replay = ReplayTransport(reads, strict_writes=False)
    dry_run = DryRunTransport(ReplayTransport(reads), lambda line: None)
    for m in ALL_METHODS:
        replay.request(m, "https://x/r")
        dry_run.request(m, "https://x/r")
    assert [c["method"] for c in replay.writes()] == [w["method"] for w in dry_run.writes]
    assert [c["method"] for c in replay.writes()] == [m for m in ALL_METHODS if m not in ("GET", "HEAD")]
