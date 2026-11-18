"""budgets.py -- compare the summary of one HIL check with its budgets (hil.yaml).

Every budget key maps to one summary value (measure.summarize) and a
comparison. A value that was not measured fails its budget (a gate never
passes work it did not do); the only exception is exact_match_min_pct when
the check has no reference clip (the real camera): that row is SKIP.
"""
from __future__ import annotations

from typing import Any, Optional

# budget key -> (summary key, operator, what it means on the projector)
RULES: dict[str, tuple[str, str, str]] = {
    "fps_min": ("fps_clean", ">=", "frame rate before the scripted event (fps)"),
    "fps_after_min": ("fps_after", ">=", "frame rate after the scripted event (fps)"),
    "nack_ratio_max_pct": ("nack_ratio_clean_pct", "<=", "NACK entries per received packet, clean link (%)"),
    "crc_fail_max": ("crc_fail_clean", "<=", "CRC failures on the clean link"),
    "freezes_max": ("freezes", "<=", "freezes (gaps > freeze threshold without a decoded frame)"),
    "recovery_ms_max": ("recovery_ms", "<=", "picture back after the link cut (ms)"),
    "tear_frames_max": ("tear_frames", "<=", "torn frames (new row-pair discontinuities)"),
    "stats_gap_ms_max": ("stats_gap_ms", "<=", "longest gap between daemon STATS (ms)"),
    "reconnects_max": ("reconnects", "<=", "viewer reconnects (re-HELLO)"),
    "exact_match_min_pct": ("exact_match_pct", ">=", "decoded frames bit-exact with the clip (%)"),
    "residual_loss_max_pct": ("residual_loss_pct", "<=", "packets never repaired (%)"),
    "decode_errors_max": ("decode_errors", "<=", "frames the decoder rejected"),
    "nack_too_old_max": ("nack_too_old", "<=", "NACKs the daemon answered TOO_OLD"),
    "keys_answered_min": ("keys_answered", ">=", "viewer 'k' presses answered with a keyframe"),
    "keys_ignored_max": ("keys_ignored", "<=", "viewer 'k' presses without a keyframe within 1.25 s"),
}

# what a failing row looks like on the projector (report "Symptoms")
SYMPTOMS: dict[str, str] = {
    "fps_clean": "the stream runs below the frame-rate budget",
    "fps_after": "the stream does not get back to full rate after the event",
    "nack_ratio_clean_pct": "a clean link needs repairs: NACK traffic on every frame",
    "crc_fail_clean": "datagrams fail the CRC check on a clean link",
    "freezes": "the picture freezes more often than the scripted event explains",
    "recovery_ms": "the picture does not come back within the budget after the link cut",
    "tear_frames": "torn frames: a horizontal line where two pictures meet",
    "stats_gap_ms": "the daemon stops answering (no STATS) while it waits for its source",
    "reconnects": "the viewer loses the daemon and says HELLO again",
    "exact_match_pct": "the decoded picture differs from the source clip",
    "residual_loss_pct": "lost packets are never repaired",
    "decode_errors": "the decoder rejects frames",
    "nack_too_old": "the daemon refuses repairs it should still hold",
    "keys_answered": "the viewer's keyframe requests are not answered",
    "keys_ignored": "viewer keys are ignored: a 'k' brings no keyframe",
    "daemon_alive": "the daemon died during the check",
    "errors": "the check did not run to the end",
}


def _cmp(value: float, op: str, limit: float) -> bool:
    return value >= limit if op == ">=" else value <= limit


def evaluate(summary: dict, budgets: dict, has_ref: bool = True,
             extra: Optional[dict] = None, skip: Optional[dict] = None) -> list[dict[str, Any]]:
    """Rows {key, metric, label, value, op, limit, status} in budget order.

    skip: {metric: reason} for budgets that cannot apply to this run (SKIP, with
    the reason in the row); exact_match without a reference clip is one of them.
    """
    skip = dict(skip or {})
    if not has_ref:
        skip.setdefault("exact_match_pct", "no reference clip")
    rows: list[dict[str, Any]] = []
    for bkey, limit in budgets.items():
        if bkey not in RULES:
            raise ValueError("unknown budget '%s' (known: %s)" % (bkey, ", ".join(sorted(RULES))))
        metric, op, label = RULES[bkey]
        value = summary.get(metric)
        if metric in skip:
            status = "SKIP"
            label = "%s (skipped: %s)" % (label, skip[metric])
        elif value is None:
            status = "FAIL"
        else:
            status = "PASS" if _cmp(float(value), op, float(limit)) else "FAIL"
        rows.append({"key": bkey, "metric": metric, "label": label, "value": value, "op": op,
                     "limit": limit, "status": status})
    for metric, (ok, value, label) in (extra or {}).items():
        rows.append({"key": metric, "metric": metric, "label": label, "value": value, "op": "",
                     "limit": "", "status": "PASS" if ok else "FAIL"})
    return rows


def verdict(rows: list[dict]) -> str:
    return "FAIL" if any(r["status"] == "FAIL" for r in rows) else "PASS"


def symptoms(rows: list[dict]) -> list[str]:
    return [SYMPTOMS.get(r["metric"], r["label"]) for r in rows if r["status"] == "FAIL"]
