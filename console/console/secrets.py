"""Reader for the facilitator's ``.env`` file. Values live in process memory only.

Rules for callers: never print, log, return in an API response, or pass a value to a
subprocess. Use :meth:`Secrets.names` for anything a human or a log will see.
:meth:`Secrets.hidden_values` are the texts the console hides from every browser (``events.set_secret_values``).

Accepted lines: ``KEY=VALUE``, ``KEY="v"``, ``KEY='v'``, ``export KEY=v``, blank lines
and ``#`` comments (also after an unquoted value, when preceded by whitespace).
Malformed lines are skipped; they are never echoed, because they may hold a secret.
"""

from __future__ import annotations

import base64
import re
from pathlib import Path

from console import procs

__all__ = ["MIN_HIDDEN", "Secrets"]

MIN_HIDDEN = 8  # a shorter value is not hidden: it would turn up in ordinary text
# Names the deny rules match (they stay out of a run's environment) whose values are addresses the pages show: the
# pull request links name the repo, Atlassian tool arguments in the run log name the cloud id. Not credentials, so not
# hidden.
_ADDRESSES = frozenset({"BRIDGE_GITHUB_REPO", "JIRA_BASE_URL", "JIRA_CLOUD_ID", "JIRA_SITE"})

_NAME = re.compile(r"[A-Za-z_][A-Za-z0-9_]*")
_EXPORT = re.compile(r"export\s+")
_INLINE_COMMENT = re.compile(r"\s+#.*$")
_ESCAPE = re.compile(r'\\(["\\])')  # inside "...": \" and \\ only; other backslashes stay


class Secrets:
    """Name -> value map with a ``repr`` that shows names only."""

    __slots__ = ("_values",)

    def __init__(self, values: dict[str, str] | None = None) -> None:
        self._values: dict[str, str] = dict(values or {})

    @classmethod
    def from_file(cls, path: Path) -> "Secrets":
        """Read ``path``. A missing file gives an empty ``Secrets`` (the doctor reports it)."""
        try:
            text = Path(path).read_text(encoding="utf-8-sig")
        except FileNotFoundError:
            return cls()
        values: dict[str, str] = {}
        for line in text.splitlines():
            parsed = _parse_line(line)
            if parsed is not None:
                values[parsed[0]] = parsed[1]
        return cls(values)

    def get(self, name: str) -> str | None:
        return self._values.get(name)

    def names(self) -> list[str]:
        """Names in file order (a repeated name keeps its first position)."""
        return list(self._values)

    def hidden_values(self) -> list[str]:
        """The texts that must never reach a browser: the value of every name the deny rules of ``procs`` match
        (``JIRA_*``, ``GH_*``, ``*_TOKEN``, ...; not the addresses in ``_ADDRESSES``) that is at least ``MIN_HIDDEN``
        characters long, the base64 of each, and the base64 of ``JIRA_EMAIL:JIRA_API_TOKEN`` (Jira's Basic auth).
        A base64 form is there with and without its ``=`` padding, in the standard and the URL-safe alphabet. Not to
        be logged or shown: these are the secrets themselves."""
        plain = [
            value for name, value in self._values.items()
            if procs.denied(name) and name.upper() not in _ADDRESSES and len(value) >= MIN_HIDDEN
        ]  # fmt: skip
        email, token = self._values.get("JIRA_EMAIL"), self._values.get("JIRA_API_TOKEN")
        basic = [f"{email}:{token}"] if email and token else []
        forms = list(plain)
        for value in (*plain, *basic):
            for encode in (base64.b64encode, base64.urlsafe_b64encode):
                padded = encode(value.encode("utf-8")).decode("ascii")
                forms += [padded, padded.rstrip("=")]
        return list(dict.fromkeys(forms))

    def __repr__(self) -> str:
        return f"Secrets(names={self.names()!r})"


def _parse_line(line: str) -> tuple[str, str] | None:
    line = line.strip()
    if not line or line.startswith("#"):
        return None
    export = _EXPORT.match(line)
    if export:
        line = line[export.end():]
    name, eq, raw = line.partition("=")
    name = name.strip()
    if not eq or not _NAME.fullmatch(name):
        return None
    return name, _parse_value(raw.strip())


def _parse_value(raw: str) -> str:
    if raw[:1] in ("'", '"'):
        quote = raw[0]
        i = 1
        while i < len(raw):
            if quote == '"' and raw[i] == "\\" and raw[i + 1 : i + 2] in ('"', "\\"):
                i += 2
                continue
            if raw[i] == quote:
                inner = raw[1:i]
                return _ESCAPE.sub(r"\1", inner) if quote == '"' else inner
            i += 1
        return raw  # no closing quote: keep the text as it is
    return _INLINE_COMMENT.sub("", raw).strip()
