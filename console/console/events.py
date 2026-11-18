"""Events: a run's audit trail (``.harness/runs/<id>/events.jsonl``) as readable lines, progress, credits and a loop warning.

The harness appends one JSON object per line. Every object has ``seq``, ``ts`` and ``kind``; harness events
(``step.start``, ``gates``, ``turn``, ...) carry their fields at the top level, the agent runtime's events
(``sdk.tool.execution_start``, ``sdk.assistant.message``, ...) carry theirs in ``data``.

* ``Tail`` reads what a file gained since the last call: whole lines only, so a line the writer has not finished
  is kept back until it is.
* ``readable`` turns one event into a ``Line`` for the log view (or ``None``: most events are noise). A ``Line``
  has a ``kind`` (``say``, ``tool``, ``result``, ``step``, ``gate``, ``deny``, ``retry``, ``review``, ``approval``,
  ``escalation``, ``cost``, ``end``, ``error``; the runner adds ``loop``), the ``seq`` of its event (a stream
  de-duplicates by it), and ``ok`` (``True``/``False`` where the line is a pass or a fail, else ``None``).
* ``progress`` and ``credits`` fold a whole trail into the step strip, the gate matrix, the credit numbers and
  whether the run is going, stopped by an error or an interrupt, or resumed.
* ``LoopDetector`` notices an agent repeating one tool call with the same outcome.

What the text of a line does not show (``summarize``, ``readable``, the loop warning and ``scrub`` use the same rules):

* the folder the run works in, given as ``root`` (in its long and, on Windows, its 8.3 short spelling, either
  separator, any case; ``src/a.c`` instead of ``<root>/src/a.c``), and the ``<wt>`` marker of a scrubbed trail;
* a home folder: ``C:\\Users\\<name>``, ``/home/<name>``, ``/Users/<name>``, ``/c/Users/<name>`` and this machine's own
  home folder become ``~``;
* the name of the user who runs the console (``getpass.getuser()``) as a path part, which becomes ``<user>``;
* a known secret value (``set_secret_values``: the app makes its ``Secrets.hidden_values`` known when it is built),
  which becomes ``<secret>``. ``hide_secrets`` and ``hide_secrets_in`` do only this; the server puts every JSON
  answer and every event stream frame through them. The values are never logged and never in a ``repr``.

A path anywhere else on the disk (``D:\\data\\x``) is shown as it is.

Nothing here raises on a malformed or unknown event: it is skipped.
"""

from __future__ import annotations

import dataclasses
import functools
import getpass
import json
import math
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Any, BinaryIO, Callable, Iterable

__all__ = [
    "SECRET", "Line", "LoopDetector", "Tail", "credits", "hide_secrets", "hide_secrets_in", "progress", "readable",
    "scrub", "secrets_in_json", "set_secret_values", "summarize",
]  # fmt: skip

SECRET = "<secret>"  # what a known secret value becomes

SAY_MAX = 600  # what the agent said
SHORT_MAX = 300  # a tool call, a result, a reason
_ERROR_MAX = 600
_STRIP_DEPTH = 10  # how deep arguments are searched for the work folder (they are a few levels at most)

_START = "sdk.tool.execution_start"
_COMPLETE = "sdk.tool.execution_complete"


@dataclass(frozen=True)
class Line:
    """One line of the run log. ``to_dict`` gives the plain JSON values the stream sends."""

    seq: int
    ts: str
    kind: str
    text: str
    step: str | None = None
    role: str | None = None
    ok: bool | None = None

    def to_dict(self) -> dict[str, Any]:
        return dataclasses.asdict(self)


# --- small helpers --------------------------------------------------------------------------------------------


def _clip(text: str, limit: int) -> str:
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _str(value: Any) -> str | None:
    """``value`` when it is a non-empty string."""
    return value if isinstance(value, str) and value else None


def _num(value: Any) -> float | None:
    """``value`` as a float when it is a finite number (a ``bool`` is not one, nor an int too large for a float)."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        number = float(value)
    except OverflowError:
        return None
    return number if math.isfinite(number) else None


def _int(value: Any) -> int | None:
    return value if isinstance(value, int) and not isinstance(value, bool) else None


def _dict(value: Any) -> dict[str, Any]:
    return value if isinstance(value, dict) else {}


def _dump(value: Any, *, canonical: bool = False) -> str:
    """``value`` as JSON text; ``canonical`` makes equal values give equal text. Never raises."""
    try:
        if canonical:
            return json.dumps(value, sort_keys=True, separators=(",", ":"), ensure_ascii=False, default=str)
        return json.dumps(value, ensure_ascii=False, default=str)
    except (TypeError, ValueError, RecursionError):
        return "?"


# --- keeping the work folder, home folders and the user's name out of the text --------------------------------

_SEP = r"[\\/]"  # either path separator
_NAME = r"[^\\/\s\x22\x27`]+"  # one path part of a home folder: no separator, space or quote (an 8.3 name has "~")
_HOME_DIRS = (
    r"\b[A-Za-z]:" + _SEP + "+Users" + _SEP + "+" + _NAME,  # C:\Users\name, C:/Users/name, doubled ones in JSON text
    r"(?<![\w.~-])/(?:home|Users|[A-Za-z]/Users)/+" + _NAME,  # /home/name, /Users/name, /c/Users/name
)


def _path_regex(path: str) -> str | None:
    """A regex for ``path`` with either separator in any case; it does not match inside a longer path."""
    parts = [part for part in re.split(r"[\\/]+", path) if part]
    if not parts:
        return None
    lead = path[0] in "\\/"  # an absolute POSIX path: what comes before must not be part of a name
    guard = r"(?<![\w.-])" if lead else r"(?<![\w.\\/-])"
    return guard + (_SEP + "+" if lead else "") + (_SEP + "+").join(re.escape(part) for part in parts)


def _root_forms(root: str) -> list[str]:
    """The folder as given, and on Windows (when it exists) its 8.3 short and long spellings too."""
    forms = [root]
    if sys.platform == "win32" and os.path.exists(root):
        import ctypes

        kernel32 = ctypes.windll.kernel32  # type: ignore[attr-defined]
        for spell in (kernel32.GetShortPathNameW, kernel32.GetLongPathNameW):
            try:
                size = spell(root, None, 0)
                buffer = ctypes.create_unicode_buffer(size)
                if size and spell(root, buffer, size):
                    forms.append(buffer.value)
            except (OSError, ValueError):
                continue
    return list(dict.fromkeys(forms))


@functools.lru_cache(maxsize=32)
def _root_pattern(root: str | None) -> re.Pattern[str]:
    """Matches the work folder (any spelling of it) or ``<wt>``, as a whole path part."""
    names = [re.escape("<wt>")]
    if root:
        names += [alt for form in _root_forms(root) if (alt := _path_regex(form))]
    return re.compile("(?:" + "|".join(names) + ")(?:(?P<sep>" + _SEP + "+)|(?![\\w.-]))", re.IGNORECASE)


@functools.lru_cache(maxsize=8)
def _home_patterns(user: str | None, home: str | None) -> tuple[re.Pattern[str], re.Pattern[str] | None]:
    """What makes a home folder (to become ``~``) and a path part that is the user's name (to become ``<user>``)."""
    alts = list(_HOME_DIRS)
    if home and not re.fullmatch(r"(?:[A-Za-z]:)?[\\/]*", home):  # this machine's own home, even if it holds a space
        alts.insert(0, (_path_regex(home) or "(?!)") + r"(?![\w.-])")
    named = re.compile(r"(?<=[\\/])" + re.escape(user) + r"(?![\w.-])", re.IGNORECASE) if user else None
    return re.compile("|".join(alts), re.IGNORECASE), named


def _user_name() -> str | None:
    try:
        return getpass.getuser() or None
    except (OSError, KeyError, ImportError):  # no name in the environment and none in the password file
        return None


def _home_dir() -> str | None:
    home = os.path.expanduser("~")
    return None if home == "~" else home


def _root_key(root: str | os.PathLike[str] | None) -> str | None:
    return os.fspath(root) if root else None


class _Known:
    """Known secret values and how to find them; its ``repr`` says how many there are, never which."""

    __slots__ = ("_count", "_pattern", "_in_json")

    def __init__(self, values: Iterable[str]) -> None:
        found = sorted({value for value in values if isinstance(value, str) and value}, key=len, reverse=True)
        self._count = len(found)  # longest first: a value inside a longer one does not cut the longer one short
        self._pattern = re.compile("|".join(map(re.escape, found))) if found else None
        # In JSON text a value can stand escaped (a quote, a backslash, a character beyond ASCII): both spellings.
        spelled = {spelling for value in found for spelling in (value, json.dumps(value)[1:-1])}
        self._in_json = re.compile("|".join(map(re.escape, spelled))) if found else None

    def hide(self, text: str) -> str:
        return text if self._pattern is None else self._pattern.sub(SECRET, text)

    def in_json(self, text: str) -> bool:
        return self._in_json is not None and self._in_json.search(text) is not None

    def __bool__(self) -> bool:
        return self._count > 0

    def __repr__(self) -> str:
        return f"<{self._count} known secret values>"


_known = _Known(())


def set_secret_values(values: Iterable[str]) -> None:
    """Make ``values`` the known secret values (in place of the ones before; empty: none). From now on ``scrub``,
    ``summarize``, ``readable``, the loop warning, ``hide_secrets`` and ``hide_secrets_in`` put ``<secret>`` where one
    stands."""
    global _known
    _known = _Known(values)


def hide_secrets(text: str) -> str:
    """``text`` with ``<secret>`` for every known secret value in it (nothing else changed)."""
    return _known.hide(text)


def hide_secrets_in(value: Any) -> Any:
    """A JSON value (dicts, lists, strings, numbers ...) with ``hide_secrets`` applied to every string in it, the keys
    of a dict too; a tuple becomes a list, as in JSON. ``value`` itself is not changed."""
    if not _known:
        return value
    if isinstance(value, str):
        return _known.hide(value)
    if isinstance(value, dict):
        return {(_known.hide(k) if isinstance(k, str) else k): hide_secrets_in(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [hide_secrets_in(item) for item in value]
    return value


def secrets_in_json(text: str) -> bool:
    """Whether a known secret value may stand in the JSON text ``text`` (as it is, or escaped as JSON does it)."""
    return _known.in_json(text)


def _scrubber(root: str | os.PathLike[str] | None) -> Callable[[str], str]:
    """A function that takes known secret values, the work folder, home folders and the user's name out of a text
    (see the module doc)."""
    work = _root_pattern(_root_key(root))
    homes, named = _home_patterns(_user_name(), _home_dir())

    def scrub(text: str) -> str:
        text = _known.hide(text)  # first: taking paths out could split a value
        text = work.sub(lambda m: "" if m.group("sep") else ".", text)  # <root>/src/a.c -> src/a.c, <root> -> .
        text = homes.sub("~", text)
        return named.sub("<user>", text) if named else text

    return scrub


def _strip(value: Any, scrub: Callable[[str], str], depth: int = 0) -> Any:
    """``value`` with ``scrub`` applied to every string in it."""
    if isinstance(value, str):
        return scrub(value)
    if depth >= _STRIP_DEPTH:
        return "…" if isinstance(value, (dict, list, tuple)) else value
    if isinstance(value, dict):
        return {key: _strip(item, scrub, depth + 1) for key, item in value.items()}
    if isinstance(value, (list, tuple)):
        return [_strip(item, scrub, depth + 1) for item in value]
    return value


def scrub(text: str, root: str | os.PathLike[str] | None = None) -> str:
    """``text`` without known secret values, the work folder ``root``, ``<wt>``, home folders and the user's name (see
    the module doc); nothing is cut. For a text a run wrote that goes to a browser as it is (``plan.md``,
    ``review.md``, an error)."""
    return _scrubber(root)(text)


def summarize(args: Any, root: str | os.PathLike[str] | None = None) -> str:
    """A tool call's arguments in one short piece of text (at most 300 characters).

    The ``command``, else the ``path``, ``pattern`` or ``url``, else the arguments as JSON. ``root`` is the folder the
    run works in; it, ``<wt>``, home folders and the user's name are left out of the result (see the module doc).
    """
    args = _strip(args, _scrubber(root))
    if isinstance(args, dict):
        for key in ("command", "path", "pattern", "url"):
            if _str(args.get(key)):
                return _clip(args[key], SHORT_MAX)
    if args is None:
        return ""
    return _clip(args if isinstance(args, str) else _dump(args), SHORT_MAX)


# --- one event as one line ------------------------------------------------------------------------------------

_Made = tuple[str, str, "bool | None"]  # line kind, text, ok


def _say(ev: dict) -> _Made | None:
    content = _dict(ev.get("data")).get("content")
    text = content.strip() if isinstance(content, str) else ""
    return ("say", _clip(text, SAY_MAX), None) if text else None


def _tool(ev: dict) -> _Made | None:
    data = _dict(ev.get("data"))
    name = _str(data.get("toolName"))
    if name is None:
        return None
    return ("tool", f"{name} {summarize(data.get('arguments'))}".rstrip(), None)


def _error_text(error: Any) -> str:
    if isinstance(error, dict):
        error = error.get("message")
    return error if isinstance(error, str) and error else "failed"


def _result(ev: dict) -> _Made | None:
    data = _dict(ev.get("data"))
    if data.get("success") is True:
        return ("result", "ok", True)
    if data.get("success") is False or data.get("error") is not None:
        return ("result", _clip(_error_text(data.get("error")), SHORT_MAX), False)
    return None


def _step_start(ev: dict) -> _Made | None:
    step = _str(ev.get("step"))
    if step is None:
        return None
    role = _str(ev.get("role"))
    return ("step", f"{step} started" + (f" ({role})" if role else ""), None)


def _step_end(ev: dict) -> _Made | None:
    step = _str(ev.get("step"))
    if step is None:
        return None
    status = _str(ev.get("status")) or _str(ev.get("outcome")) or "ended"
    if ev.get("outcome") == "jump":
        target = _str(ev.get("target"))
        text, ok = f"{step} sent back" + (f" to {target}" if target else ""), False
    else:
        text, ok = f"{step} {status}", status == "done"
    reason = _str(ev.get("reason"))
    return ("step", text + (f" ({_clip(reason, SHORT_MAX)})" if reason else ""), ok)


def _gates(ev: dict) -> _Made | None:
    summary = _str(ev.get("summary"))
    if summary is None:
        return None
    ok = ev.get("ok")
    return ("gate", _clip(summary, SHORT_MAX), ok if isinstance(ok, bool) else None)


def _baseline(ev: dict) -> _Made | None:
    """The gates run once on the untouched code before any agent works: what fails there is the defect, on record."""
    failures = _dict(ev.get("failures"))
    if failures:
        failed = ", ".join(f"{gid} FAIL" for gid in failures)
        return ("gate", _clip(f"before the agent: {failed}. That is the defect, on record before anyone changes the code", SHORT_MAX), None)
    reason = _str(ev.get("reason"))
    summary = _str(ev.get("summary"))
    return ("gate", _clip("before the agent: " + (reason or summary or "the gates ran on the untouched code"), SHORT_MAX), None)


def _deny(ev: dict) -> _Made | None:
    reason = _str(ev.get("reason"))
    return ("deny", _clip(reason, SHORT_MAX), False) if reason else None


def _retry(ev: dict) -> _Made | None:
    step, attempt, reason = _str(ev.get("step")), _int(ev.get("attempt")), _str(ev.get("reason"))
    text = "retry" + (f" {step}" if step else "") + (f", attempt {attempt}" if attempt is not None else "")
    return ("retry", text + (f": {_clip(reason, SHORT_MAX)}" if reason else ""), None)


def _review(ev: dict) -> _Made | None:
    verdict, score, findings = _str(ev.get("verdict")), _num(ev.get("score")), _int(ev.get("findings"))
    pieces = [verdict] if verdict else []
    if score is not None:
        pieces.append(f"score {score:g}")
    if findings is not None:
        pieces.append(f"{findings} finding" + ("" if findings == 1 else "s"))
    ok = True if verdict == "approve" else False if verdict == "reject" else None
    return ("review", "review" + (": " + ", ".join(pieces) if pieces else ""), ok)


def _approval(ev: dict) -> _Made | None:
    approved, by = ev.get("approved"), _str(ev.get("by"))
    text = _clip(_str(ev.get("question")) or "approval", SHORT_MAX)
    if isinstance(approved, bool):
        text += " approved" if approved else " rejected"
    return ("approval", text + (f" by {by}" if by else ""), approved if isinstance(approved, bool) else None)


def _escalation(ev: dict) -> _Made | None:
    reason, choice = _str(ev.get("reason")), _str(ev.get("choice"))
    text = "escalation" + (f": {_clip(reason, SHORT_MAX)}" if reason else "")
    return ("escalation", text + (f" (answer: {choice})" if choice else ""), None)


def _turn(ev: dict) -> _Made | None:
    model, spent, seconds = _str(ev.get("model")), _num(ev.get("credits")), _num(ev.get("seconds"))
    text = _str(ev.get("role")) or "agent"
    if model:
        text += f" ({model})"
    pieces = ([f"{spent:g} credits"] if spent is not None else []) + ([f"{seconds:g} s"] if seconds is not None else [])
    return ("cost", text + (": " + ", ".join(pieces) if pieces else ""), None)


def _end(ev: dict) -> _Made | None:
    status, reason = _str(ev.get("status")), _str(ev.get("reason"))
    text = f"run {status or 'ended'}" + (f" ({_clip(reason, SHORT_MAX)})" if reason else "")
    return ("end", text, status == "done" if status else None)


def _error(ev: dict) -> _Made | None:
    error = _str(ev.get("error"))
    return ("error", _clip(error, _ERROR_MAX), False) if error else None


def _resume(ev: dict) -> _Made | None:
    return ("step", "run resumed", None)


def _interrupted(ev: dict) -> _Made | None:
    step = _str(ev.get("step"))
    return ("step", "run interrupted" + (f" at {step}" if step else ""), False)


_READERS: dict[str, Callable[[dict], "_Made | None"]] = {
    "sdk.assistant.message": _say,
    _START: _tool,
    _COMPLETE: _result,
    "step.start": _step_start,
    "step.end": _step_end,
    "gates": _gates,
    "baseline": _baseline,
    "policy.deny": _deny,
    "retry": _retry,
    "review": _review,
    "approval": _approval,
    "escalation": _escalation,
    "turn": _turn,
    "run.end": _end,
    "run.error": _error,
    "run.resume": _resume,
    "run.interrupted": _interrupted,
}


def readable(ev: Any, root: str | os.PathLike[str] | None = None) -> Line | None:
    """The log line for one event, or ``None`` when the event is not worth a line (or is malformed).

    ``root`` is the folder the run works in; it, home folders and the user's name are left out of the text.
    """
    if not isinstance(ev, dict):
        return None
    kind, seq = ev.get("kind"), ev.get("seq")
    reader = _READERS.get(kind) if isinstance(kind, str) else None
    if reader is None or _int(seq) is None:
        return None
    ev = _strip(ev, _scrubber(root))  # before any text is cut, so a cut cannot leave half a path
    made = reader(ev)
    if made is None:
        return None
    line_kind, text, ok = made
    ts = ev.get("ts")
    return Line(seq, ts if isinstance(ts, str) else "", line_kind, text, _str(ev.get("step")), _str(ev.get("role")), ok)


# --- reading a growing file -----------------------------------------------------------------------------------


class Tail:
    """The events a JSON-lines file gained since the last ``read_new``.

    ``offset`` is the byte position after the last whole line read, so a line the writer is still writing is read
    by the call after it is finished. A line that is not a JSON object is skipped; bytes that are not UTF-8 become
    U+FFFD. A file that does not exist (yet), or cannot be opened just now, has no events. A file that has become
    shorter than ``offset`` was truncated or replaced: it is read again from the start (a stream de-duplicates by
    ``seq``). A replacement at least as long as ``offset`` cannot be told from growth.

    ``max_bytes`` (default: no limit) caps what one call reads; the rest comes with the next calls, and a single line
    longer than the cap is skipped (``skipped`` counts those lines). ``opener`` (default: ``open(path, "rb")``) opens
    the file; a ``FileNotFoundError`` or ``PermissionError`` from it means no events this time.
    """

    def __init__(
        self,
        path: Path,
        offset: int = 0,
        *,
        max_bytes: int | None = None,
        opener: Callable[[Path], BinaryIO] | None = None,
    ) -> None:
        self.path = Path(path)
        self.offset = offset
        self.max_bytes = max_bytes
        self._opener = opener or (lambda p: open(p, "rb"))
        self._skipping = False  # inside a line longer than max_bytes: drop it up to its end
        self.skipped = 0  # lines dropped for being longer than max_bytes

    def read_new(self) -> list[dict[str, Any]]:
        try:
            with self._opener(self.path) as fh:
                if os.fstat(fh.fileno()).st_size < self.offset:
                    self.offset, self._skipping = 0, False
                fh.seek(self.offset)
                chunk = fh.read() if self.max_bytes is None else fh.read(self.max_bytes)
        except (FileNotFoundError, PermissionError):
            return []
        end = chunk.rfind(b"\n") + 1  # up to and including the last newline
        if not end:
            if self.max_bytes is not None and len(chunk) >= self.max_bytes:  # one line longer than the cap
                self.offset += len(chunk)
                self.skipped += 0 if self._skipping else 1
                self._skipping = True
            return []
        start = chunk.find(b"\n") + 1 if self._skipping else 0
        self._skipping = False
        self.offset += end
        events: list[dict[str, Any]] = []
        for raw in chunk[start:end].split(b"\n"):
            text = raw.decode("utf-8", errors="replace").strip()
            if not text:
                continue
            try:
                event = json.loads(text)
            except (ValueError, RecursionError):
                continue
            if isinstance(event, dict):
                events.append(event)
        return events


# --- progress and credits -------------------------------------------------------------------------------------


def _audit_credits(trail: list[dict]) -> float | None:
    """The credits the harness's own audit reader finds (it reads the eval schema), else ``None``.

    Needs the harness on ``sys.path`` (``harnesslink.bootstrap``); without it there is nothing to ask.
    """
    try:
        from harness.eval.audit import parse_events
    except ImportError:
        return None
    return parse_events(trail).ai_credits


def credits(events: Iterable[Any]) -> float:
    """AI credits a trail spent: what ``harness.eval.audit.parse_events`` finds, else the sum of the ``turn`` events.

    The reader needs events spelled ``type`` with ``nanoAiu`` usage, which the run's own ``events.jsonl`` is not
    (it says ``kind``), so for that file the sum of ``turn.credits`` is the answer; the two agree to within a
    rounding of 0.0001 per turn.
    """
    trail = [ev for ev in events if isinstance(ev, dict)]
    metered = _audit_credits(trail)
    if metered is not None:
        return round(metered, 4)
    spent = (_num(ev.get("credits")) for ev in trail if ev.get("kind") == "turn")
    return round(sum(c for c in spent if c is not None), 4)


def progress(events: Iterable[Any]) -> dict[str, Any]:
    """Where a run is: step statuses, the step in progress, every gate run, credits, approvals and retries.

    A step is ``running`` from its ``step.start`` and takes the ``status`` of its ``step.end``. A step sent back
    (``outcome: jump``) makes every step from the target to itself ``pending`` again, as the harness does in its
    own ``summary.json``. ``state`` is ``running``, ``stopped`` from a ``run.error`` or ``run.interrupted`` on, and
    ``resumed`` from a ``run.resume`` on (a later stop makes it ``stopped`` again).
    """
    trail = [ev for ev in events if isinstance(ev, dict)]
    steps: dict[str, str] = {}
    state = "running"
    current: str | None = None
    gates: list[dict[str, Any]] = []
    by_role: dict[str, float] = {}
    approvals = retries = 0
    ended = baselined = finished = False
    only_ticket = True  # every step so far is the ticket step: the baseline runs right after it
    for ev in trail:
        kind, step = ev.get("kind"), _str(ev.get("step"))
        if kind == "step.start" and step:
            steps[step] = "running"
            current = step
            only_ticket = only_ticket and (step == "ticket" or ev.get("step_kind") == "ticket")
        elif kind == "step.end" and step:
            steps.setdefault(step, "running")
            order = list(steps)
            target = _str(ev.get("target"))
            if ev.get("outcome") == "jump":
                last = order.index(step)
                first = min(order.index(target), last) if target in steps else last
                for name in order[first : last + 1]:
                    steps[name] = "pending"
            else:
                steps[step] = _str(ev.get("status")) or _str(ev.get("outcome")) or "ended"
            if current == step:
                current = None
            ended = True
        elif kind == "gates" and _str(ev.get("summary")):
            ok = ev.get("ok")
            gates.append({"step": step, "attempt": _int(ev.get("attempt")), "trigger": _str(ev.get("trigger")), "summary": ev["summary"],
                          "ok": ok if isinstance(ok, bool) else None})
        elif kind == "turn":
            spent = _num(ev.get("credits"))
            if spent is not None:
                role = _str(ev.get("role")) or "unknown"
                by_role[role] = by_role.get(role, 0.0) + spent
        elif kind == "approval":
            approvals += 1
        elif kind == "retry":
            retries += 1
        elif kind == "baseline":
            baselined = True
        elif kind == "run.end":
            finished = True
        elif kind in ("run.error", "run.interrupted"):
            state = "stopped"
        elif kind == "run.resume":
            state = "resumed"
    if current is None and ended and only_ticket and not baselined and not finished and state != "stopped":
        # between the ticket and the first agent step the harness runs the gates once on the untouched code (the
        # baseline): no step is running, and it can take minutes on cold builds
        current = "baseline: the gates on the code before the agent starts"
    return {"steps": steps, "state": state, "current": current, "gates": gates,
            "credits_by_role": {role: round(spent, 4) for role, spent in by_role.items()},
            "credits": credits(trail), "approvals": approvals, "retries": retries}


# --- a run that goes round in circles -------------------------------------------------------------------------


def _outcome_text(data: dict[str, Any]) -> str:
    """The text a finished tool call gave back: its result, or its error."""
    value = data.get("result") if data.get("success") is True else data.get("error")
    if isinstance(value, dict):
        for key in ("content", "message", "detailedContent"):
            if isinstance(value.get(key), str):
                return value[key]
    return value if isinstance(value, str) else "" if value is None else _dump(value, canonical=True)


class LoopDetector:
    """Flags an agent that makes the same tool call, with the same outcome, ``repeats`` times in a row.

    Feed it every event of the trail in order. A call is its tool name, its arguments, whether it succeeded and the
    first 300 characters of what it gave back; ``sdk.tool.execution_start`` and ``sdk.tool.execution_complete`` are
    matched by ``toolCallId``, and the call counts when it completes. Any other completed call starts the count over,
    as does one whose start was never seen. ``feed`` returns the warning once, when the count reaches ``repeats``
    (``loop: <tool> <summary> ×3``), and ``None`` otherwise. ``root`` is the folder the run works in (see ``summarize``).
    """

    def __init__(self, repeats: int = 3, root: str | os.PathLike[str] | None = None) -> None:
        if repeats < 2:
            raise ValueError("repeats must be at least 2")
        self.repeats = repeats
        self._root = root
        self._started: dict[str, tuple[str, Any]] = {}
        self._last: tuple[str, str, bool, str] | None = None
        self._count = 0

    def feed(self, ev: Any) -> str | None:
        if not isinstance(ev, dict) or ev.get("kind") not in (_START, _COMPLETE):
            return None
        data = _dict(ev.get("data"))
        call = _str(data.get("toolCallId"))
        if ev["kind"] == _START:
            name = _str(data.get("toolName"))
            if call and name:
                self._started[call] = (name, data.get("arguments"))
            return None
        started = self._started.pop(call, None) if call else None
        if started is None:
            self._last, self._count = None, 0
            return None
        name, args = started
        signature = (name, _dump(args, canonical=True), data.get("success") is True, _outcome_text(data)[:SHORT_MAX])
        if signature == self._last:
            self._count += 1
        else:
            self._last, self._count = signature, 1
        if self._count == self.repeats:
            return f"loop: {name} {summarize(args, self._root)} ×{self.repeats}"
        return None
