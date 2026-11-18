"""Metrics from a run's audit trail (events.jsonl, schema in harness/api.py)."""
from __future__ import annotations

import json
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Iterable, Iterator, Optional

from .. import names

NANO = 1e9


@dataclass
class AuditMetrics:
    events: int = 0
    bad_lines: int = 0
    model_calls: int = 0
    tokens_in: int = 0
    tokens_out: int = 0
    tokens_cached: int = 0
    ai_credits: Optional[float] = None       # None: the trail has no cost information
    approvals: int = 0
    escalations: int = 0
    retries: int = 0
    reviewer_score: Optional[float] = None   # last review event
    reviews: int = 0
    gate_results: dict[str, str] = field(default_factory=dict)   # final attempt per gate
    gate_violations: Optional[int] = None    # None: no gate events
    gate_failures_total: int = 0             # every FAIL over all attempts ("gate catches")
    models: dict[str, int] = field(default_factory=dict)         # model -> calls
    end_status: Optional[str] = None

    @property
    def interventions(self) -> int:
        return self.approvals + self.escalations


def iter_events(path: Path) -> Iterator[Any]:
    """Yield parsed lines; a line that is not JSON yields None (counted by the caller)."""
    with open(path, encoding="utf-8", errors="replace") as fh:
        for line in fh:
            line = line.strip()
            if not line:
                continue
            try:
                yield json.loads(line)
            except json.JSONDecodeError:
                yield None


def _int(v: Any) -> int:
    try:
        return int(v or 0)
    except (TypeError, ValueError):
        return 0


def _num(v: Any) -> Optional[float]:
    try:
        return None if v is None else float(v)
    except (TypeError, ValueError):
        return None


def _kind(t: str) -> str:
    """Normalise an event type: 'harness.approval' / 'approval' / 'Harness.Approval' -> 'approval'."""
    t = t.strip().lower()
    for pre in ("harness.", "session.", "assistant."):
        if t.startswith(pre) and t not in ("assistant.usage",):
            return t[len(pre):]
    return t


def parse_events(events: Iterable[Any]) -> AuditMetrics:
    m = AuditMetrics()
    usage_nano = 0.0
    usage_has_cost = False
    checkpoints: dict[str, dict[str, float]] = {}
    gate_attempt: dict[str, int] = {}
    for ev in events:
        if not isinstance(ev, dict) or not isinstance(ev.get("type"), str):
            m.bad_lines += 1
            continue
        m.events += 1
        data = ev.get("data") if isinstance(ev.get("data"), dict) else {}
        kind = _kind(ev["type"])
        if kind == "assistant.usage":
            m.model_calls += 1
            m.tokens_in += _int(data.get("inputTokens"))
            m.tokens_out += _int(data.get("outputTokens"))
            m.tokens_cached += _int(data.get("cacheReadTokens"))
            model = str(data.get("model") or "unknown")
            m.models[model] = m.models.get(model, 0) + 1
            nano = _num(data.get("nanoAiu"))
            if nano is not None:
                usage_nano += nano
                usage_has_cost = True
        elif kind == "usage_checkpoint":
            sid = str(data.get("sessionId") or data.get("session") or "_")
            cp = checkpoints.setdefault(sid, {})
            for key in ("totalNanoAiu", "totalInputTokens", "totalOutputTokens", "totalCacheReadTokens"):
                v = _num(data.get(key))
                if v is not None:
                    cp[key] = max(cp.get(key, 0.0), v)   # cumulative: the largest is the latest
        elif kind == "approval":
            m.approvals += 1
        elif kind == "escalation":
            m.escalations += 1
        elif kind == "retry":
            m.retries += 1
        elif kind == "review":
            m.reviews += 1
            s = _num(data.get("score"))
            if s is not None:
                m.reviewer_score = s
        elif kind == "gate":
            gate = names.gate(str(data.get("gate") or "?"))
            status = str(data.get("status") or "").upper()
            attempt = _int(data.get("attempt"))
            if status == "FAIL":
                m.gate_failures_total += 1
            if attempt >= gate_attempt.get(gate, -1):
                gate_attempt[gate] = attempt
                m.gate_results[gate] = status
        elif kind == "run_end":
            st = data.get("status")
            m.end_status = str(st) if st is not None else None
    if checkpoints:
        nano = sum(cp.get("totalNanoAiu", 0.0) for cp in checkpoints.values())
        if any("totalNanoAiu" in cp for cp in checkpoints.values()):
            m.ai_credits = nano / NANO
        # checkpoint token totals win over summed usage events when present
        for key, attr in (("totalInputTokens", "tokens_in"), ("totalOutputTokens", "tokens_out"),
                          ("totalCacheReadTokens", "tokens_cached")):
            if any(key in cp for cp in checkpoints.values()):
                setattr(m, attr, int(sum(cp.get(key, 0.0) for cp in checkpoints.values())))
    if m.ai_credits is None and usage_has_cost:
        m.ai_credits = usage_nano / NANO
    if m.gate_results:
        m.gate_violations = sum(1 for s in m.gate_results.values() if s == "FAIL")
    return m


def parse_audit(path: Optional[Path]) -> Optional[AuditMetrics]:
    """None when there is no audit trail at the path."""
    if path is None or not Path(path).is_file():
        return None
    return parse_events(iter_events(Path(path)))
