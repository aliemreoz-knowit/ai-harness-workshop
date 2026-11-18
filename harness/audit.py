"""Audit trail per run: events.jsonl, prompts, gate results, approvals, credits.

Layout of ``.harness/runs/<id>/``::

    state.json          resumable run state (state.py)
    events.jsonl        one JSON object per line: harness events + every SDK session event
    prompts/NN-<step>-<role>.md   every prompt sent to a model, verbatim
    gates/<step>-<n>.json         every ladder result with structured feedback
    logs/<gate>-<step>-<n>.log    copies of failing gate logs
    ticket.json, plan.md, review.md, review.json, summary.json

Credits come from the SDK usage events: ``assistant.usage`` (per model call,
``copilot_usage.total_nano_aiu``) and ``session.usage_checkpoint`` (per
session, cumulative ``total_nano_aiu``). 1 AI credit = 1e9 nano AIU.
"""

from __future__ import annotations

import dataclasses
import datetime as _dt
import enum
import json
import shutil
import threading
import uuid
from pathlib import Path
from typing import Any

NANO_PER_CREDIT = 1e9


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="milliseconds")


def to_jsonable(obj: Any, depth: int = 0) -> Any:
    """Best-effort conversion of SDK objects (dataclasses, enums, datetimes) to JSON."""
    if depth > 12:
        return repr(obj)
    if obj is None or isinstance(obj, (bool, int, float, str)):
        return obj
    if isinstance(obj, enum.Enum):
        return obj.value
    if isinstance(obj, (_dt.datetime, _dt.date)):
        return obj.isoformat()
    if isinstance(obj, _dt.timedelta):
        return obj.total_seconds()
    if isinstance(obj, uuid.UUID):
        return str(obj)
    if isinstance(obj, Path):
        return str(obj)
    if isinstance(obj, dict):
        return {str(k): to_jsonable(v, depth + 1) for k, v in obj.items()}
    if isinstance(obj, (list, tuple, set, frozenset)):
        return [to_jsonable(v, depth + 1) for v in obj]
    to_dict = getattr(obj, "to_dict", None)
    if callable(to_dict):
        try:
            return to_jsonable(to_dict(), depth + 1)
        except Exception:  # noqa: BLE001 - fall back to attribute walk
            pass
    if dataclasses.is_dataclass(obj):
        return {f.name: to_jsonable(getattr(obj, f.name), depth + 1) for f in dataclasses.fields(obj)}
    if hasattr(obj, "__dict__"):
        return {k: to_jsonable(v, depth + 1) for k, v in vars(obj).items() if not k.startswith("__")}
    return repr(obj)


def event_type(event: Any) -> str:
    t = getattr(event, "type", None)
    if t is None and isinstance(event, dict):
        t = event.get("type")
    return str(getattr(t, "value", t) or "")


def _attr(obj: Any, name: str, default: Any = None) -> Any:
    if isinstance(obj, dict):
        return obj.get(name, default)
    return getattr(obj, name, default)


class Usage:
    """Credits and tokens per session/role from SDK usage events."""

    def __init__(self, data: dict[str, Any] | None = None) -> None:
        self.sessions: dict[str, dict[str, Any]] = (data or {}).get("sessions", {})

    def _session(self, session_id: str, role: str, step: str) -> dict[str, Any]:
        s = self.sessions.setdefault(session_id, {"role": role, "step": step, "calls": 0, "call_nano": 0.0,
                                                  "checkpoint_nano": 0.0, "input_tokens": 0, "output_tokens": 0,
                                                  "cache_read_tokens": 0, "cache_write_tokens": 0, "models": {}})
        return s

    def observe(self, session_id: str, role: str, step: str, event: Any) -> None:
        et = event_type(event)
        data = _attr(event, "data")
        if data is None:
            return
        if et == "assistant.usage":  # sub-agent calls (agent_id set) bill the same session
            s = self._session(session_id, role, step)
            s["calls"] += 1
            for k in ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens"):
                s[k] += int(_attr(data, k) or 0)
            cu = _attr(data, "copilot_usage")
            nano = float(_attr(cu, "total_nano_aiu") or 0) if cu is not None else 0.0
            s["call_nano"] += nano
            model = str(_attr(data, "model") or "?")
            s["models"][model] = s["models"].get(model, 0) + 1
        elif et == "session.usage_checkpoint":
            s = self._session(session_id, role, step)
            s["checkpoint_nano"] = max(s["checkpoint_nano"], float(_attr(data, "total_nano_aiu") or 0))

    @staticmethod
    def session_credits(s: dict[str, Any]) -> float:
        # the checkpoint is cumulative and authoritative; per-call sums cover sessions without one
        return max(float(s.get("checkpoint_nano", 0)), float(s.get("call_nano", 0))) / NANO_PER_CREDIT

    @property
    def credits(self) -> float:
        return sum(self.session_credits(s) for s in self.sessions.values())

    def by_role(self) -> dict[str, float]:
        out: dict[str, float] = {}
        for s in self.sessions.values():
            out[s["role"]] = out.get(s["role"], 0.0) + self.session_credits(s)
        return out

    def tokens(self) -> dict[str, int]:
        keys = ("input_tokens", "output_tokens", "cache_read_tokens", "cache_write_tokens")
        return {k: sum(int(s.get(k, 0)) for s in self.sessions.values()) for k in keys}

    def to_dict(self) -> dict[str, Any]:
        return {"credits": round(self.credits, 4), "by_role": {k: round(v, 4) for k, v in self.by_role().items()},
                "tokens": self.tokens(), "sessions": self.sessions}


class Audit:
    def __init__(self, run_dir: Path) -> None:
        self.dir = Path(run_dir)
        self.dir.mkdir(parents=True, exist_ok=True)
        (self.dir / "prompts").mkdir(exist_ok=True)
        (self.dir / "gates").mkdir(exist_ok=True)
        (self.dir / "logs").mkdir(exist_ok=True)
        self.events_path = self.dir / "events.jsonl"
        self._lock = threading.Lock()
        self._seq = sum(1 for _ in self.events_path.open(encoding="utf-8")) if self.events_path.exists() else 0

    def event(self, kind: str, /, **data: Any) -> None:
        if "kind" in data:  # the record's own "kind" wins; keep the caller's value under another key
            data["detail_kind"] = data.pop("kind")
        with self._lock:
            self._seq += 1
            rec = {"seq": self._seq, "ts": _now(), "kind": kind, **to_jsonable(data)}
            with self.events_path.open("a", encoding="utf-8", newline="\n") as fh:
                fh.write(json.dumps(rec, ensure_ascii=False) + "\n")

    def sdk_event(self, step: str, role: str, session_id: str, event: Any) -> None:
        self.event("sdk." + (event_type(event) or "unknown"), step=step, role=role, session=session_id,
                   agent_id=_attr(event, "agent_id"), data=to_jsonable(_attr(event, "data")))

    def prompt(self, step: str, role: str, text: str, kind: str = "user") -> Path:
        n = len(list((self.dir / "prompts").glob("*.md"))) + 1
        path = self.dir / "prompts" / f"{n:02d}-{step}-{role}-{kind}.md"
        path.write_text(text, encoding="utf-8", newline="\n")
        self.event("prompt", step=step, role=role, prompt_kind=kind, file=path.relative_to(self.dir).as_posix(),
                   chars=len(text))
        return path

    def gates(self, step: str, attempt: int, ladder: Any, trigger: str) -> Path:
        path = self.dir / "gates" / f"{step}-{attempt:02d}-{trigger}.json"
        data = ladder.to_dict()
        path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8", newline="\n")
        extra: dict[str, Any] = {}
        b = getattr(ladder, "baseline", None) or {}
        if b:   # the baseline rule this ladder was judged by (harness/baseline.py)
            extra["baseline"] = {"rule": b.get("rule"), "new": b.get("new") or [], "fixed": b.get("fixed") or [],
                                 "pre_existing": b.get("pre_existing", 0), "progress": b.get("progress")}
        note = ladder.lite_note() if hasattr(ladder, "lite_note") else ""
        if note:  # the Docker-free ladder: these gates need the container and did not run
            extra["not_run_here"] = note
        self.event("gates", step=step, attempt=attempt, trigger=trigger, ok=ladder.ok, summary=ladder.summary(),
                   file=path.relative_to(self.dir).as_posix(), duration_ms=ladder.duration_ms, **extra)
        return path

    def copy_log(self, src: Path, name: str) -> None:
        try:
            shutil.copyfile(src, self.dir / "logs" / name)
        except OSError:
            pass

    def write(self, name: str, text: str) -> Path:
        path = self.dir / name
        path.write_text(text, encoding="utf-8", newline="\n")
        return path

    def write_json(self, name: str, obj: Any) -> Path:
        return self.write(name, json.dumps(to_jsonable(obj), indent=2, ensure_ascii=False))

    def read_events(self) -> list[dict[str, Any]]:
        if not self.events_path.exists():
            return []
        out = []
        for line in self.events_path.read_text(encoding="utf-8").splitlines():
            try:
                out.append(json.loads(line))
            except ValueError:
                continue
        return out
