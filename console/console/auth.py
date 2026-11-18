"""Authentication building blocks: unit codes, signed session tokens, a login rate limiter.

* A unit code is 6 characters from ``ALPHABET`` (no look-alikes such as 0/O or 1/I). Only
  ``hash_code(code, salt)`` is ever stored, and codes are matched case-insensitively.
* A session token is ``subject.issued.sig``: ``subject`` is ``unit-NN`` or ``admin``,
  ``issued`` whole Unix seconds, ``sig`` the unpadded base64url HMAC-SHA256 of
  ``subject.issued``. ``Sessions.verify`` never raises for a bad token; it returns ``None``.
* ``RateLimit`` is a thread-safe sliding window per key (the server is threaded).
"""

from __future__ import annotations

import base64
import hashlib
import hmac
import re
import secrets
import threading
import time
from collections import deque

__all__ = ["ALPHABET", "RateLimit", "Sessions", "hash_code", "new_code"]

ALPHABET = "ACDEFGHJKMNPQRTUVWXY34679"
_CODE_LENGTH = 6

# ASCII only: "\d" would also match other scripts' digits.
_SUBJECT = re.compile(r"admin|unit-[0-9]{2}")
_ISSUED = re.compile(r"[0-9]{1,15}")  # a Unix time has 10 digits; the cap keeps int() small

_CLOCK_SKEW_S = 60  # how far in the future a token's issue time may lie


def new_code() -> str:
    """A fresh random unit code."""
    return "".join(secrets.choice(ALPHABET) for _ in range(_CODE_LENGTH))


def hash_code(code: str, salt: str) -> str:
    """Hex SHA-256 of ``salt + code`` with the code upper-cased, so entry is case-insensitive."""
    return hashlib.sha256((salt + code.upper()).encode("utf-8")).hexdigest()


class Sessions:
    """Issues and verifies stateless session tokens signed with ``key``."""

    def __init__(self, key: bytes, ttl_s: int = 57600) -> None:
        self._key = bytes(key)
        self.ttl_s = ttl_s

    def issue(self, subject: str, now: float | None = None) -> str:
        """A token for ``subject`` (``unit-NN`` or ``admin``) issued at ``now`` (default: the clock)."""
        if not _SUBJECT.fullmatch(subject):
            raise ValueError(f"session subject must be 'admin' or 'unit-NN', got {subject!r}")
        signed = f"{subject}.{_whole_seconds(now)}"
        return f"{signed}.{self._sign(signed)}"

    def verify(self, token: str, now: float | None = None) -> str | None:
        """The token's subject, or ``None`` if it is malformed, forged, expired or from the future."""
        if not isinstance(token, str):
            return None
        parts = token.split(".")
        if len(parts) != 3:
            return None
        subject, issued_text, sig = parts
        if not (_SUBJECT.fullmatch(subject) and _ISSUED.fullmatch(issued_text) and sig.isascii()):
            return None
        if not hmac.compare_digest(self._sign(f"{subject}.{issued_text}"), sig):
            return None
        issued, now_s = int(issued_text), _whole_seconds(now)
        if issued - now_s > _CLOCK_SKEW_S or now_s - issued > self.ttl_s:
            return None
        return subject

    def _sign(self, message: str) -> str:
        digest = hmac.new(self._key, message.encode("utf-8"), hashlib.sha256).digest()
        return base64.urlsafe_b64encode(digest).rstrip(b"=").decode("ascii")


class RateLimit:
    """At most ``limit`` allowed calls per ``window_s`` seconds for each key (sliding window).

    Only allowed calls are recorded, so a client that keeps retrying while refused is let
    back in ``window_s`` after its last allowed call, not after its last attempt. ``refund``
    takes an allowed call back once it turned out fine (a login that succeeded), so only the
    failures count: twenty people behind one address can all log in. ``now`` is any steadily
    increasing clock in seconds; the default is ``time.monotonic()``.
    """

    def __init__(self, limit: int = 5, window_s: float = 60) -> None:
        self.limit = limit
        self.window_s = window_s
        self._hits: dict[str, deque[float]] = {}
        self._lock = threading.Lock()

    def allow(self, key: str, now: float | None = None) -> bool:
        """Record an attempt for ``key`` and say whether it is within the limit."""
        t = time.monotonic() if now is None else now
        with self._lock:
            hits = self._hits.setdefault(key, deque())
            while hits and t - hits[0] >= self.window_s:
                hits.popleft()
            if len(hits) >= self.limit:
                return False
            hits.append(t)
            return True

    def refund(self, key: str) -> None:
        """Take back one allowed call of ``key`` (the latest); nothing when there is none in the window.

        The call is recorded when it is allowed, before its outcome is known, so attempts made at the same time
        cannot get past the limit together."""
        with self._lock:
            hits = self._hits.get(key)
            if hits:
                hits.pop()


def _whole_seconds(now: float | None) -> int:
    return int(time.time() if now is None else now)
