"""Result records (results/<experiment>.jsonl), aggregation and the leaderboard file (results.json)."""
from __future__ import annotations

import glob
import json
import math
import os
import statistics
import threading
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Iterable, Optional

from .. import names

SCHEMA = 1
FINAL_STATUSES = {"done"}            # a record with run_status "done" is not re-run on --resume


def now_iso() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


def run_key(rec: dict[str, Any]) -> tuple[str, str, int]:
    return (str(rec.get("variant")), str(rec.get("ticket")), int(rec.get("repeat", 0)))


class ResultsWriter:
    """Appends one JSON line per run; safe to call from worker threads."""

    def __init__(self, path: Path):
        self.path = Path(path)
        self.path.parent.mkdir(parents=True, exist_ok=True)
        self._lock = threading.Lock()

    def append(self, rec: dict[str, Any]) -> None:
        line = json.dumps(rec, sort_keys=True, default=str)
        with self._lock, open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(line + "\n")
            fh.flush()
            os.fsync(fh.fileno())


def load_records(paths: Iterable[Path | str]) -> list[dict[str, Any]]:
    out = []
    for p in paths:
        try:
            fh = open(p, encoding="utf-8", errors="replace")
        except OSError:
            continue
        with fh:
            for line in fh:
                line = line.strip()
                if not line:
                    continue
                try:
                    rec = json.loads(line)
                except json.JSONDecodeError:
                    continue      # a torn last line after a crash
                if isinstance(rec, dict) and rec.get("schema") == SCHEMA:
                    out.append(plain_names(rec))
    return out


def plain_names(rec: dict[str, Any]) -> dict[str, Any]:
    """A record of an older run with today's names: variant raw -> raw, gate build -> build (harness/names.py)."""
    if isinstance(rec.get("variant"), str):
        rec["variant"] = names.variant(rec["variant"])
    for key in ("gate_results", "gate_violations"):
        if isinstance(rec.get(key), dict):
            rec[key] = {names.gate(str(k)): v for k, v in rec[key].items()}
    return rec


def expand(patterns: list[str]) -> list[Path]:
    files: list[Path] = []
    for pat in patterns:
        hits = sorted(glob.glob(pat)) if any(c in pat for c in "*?[") else [pat]
        files.extend(Path(h) for h in hits if Path(h).is_file())
    return files


def latest(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """The last record per (experiment, variant, ticket, repeat): a re-run replaces the earlier one."""
    best: dict[tuple, dict[str, Any]] = {}
    for rec in records:
        best[(rec.get("experiment"),) + run_key(rec)] = rec
    return list(best.values())


def wilson(passes: int, n: int, z: float = 1.96) -> tuple[float, float]:
    if n == 0:
        return 0.0, 0.0
    p = passes / n
    den = 1 + z * z / n
    centre = (p + z * z / (2 * n)) / den
    half = z * math.sqrt(p * (1 - p) / n + z * z / (4 * n * n)) / den
    return max(0.0, centre - half), min(1.0, centre + half)


def _mean(xs: list[float]) -> Optional[float]:
    xs = [x for x in xs if x is not None]
    return round(statistics.fmean(xs), 4) if xs else None


def aggregate(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """One row per variant, ordered by pass rate then credits."""
    by_var: dict[str, list[dict[str, Any]]] = {}
    for rec in records:
        if rec.get("run_status") != "done":
            continue
        by_var.setdefault(str(rec["variant"]), []).append(rec)
    rows = []
    for var, recs in by_var.items():
        n = len(recs)
        passes = sum(1 for r in recs if r.get("success"))
        commits = sum(1 for r in recs if committed(r))
        lo, hi = wilson(passes, n)
        per_ticket: dict[str, dict[str, int]] = {}
        per_repeat: dict[int, list[bool]] = {}
        for r in recs:
            t = per_ticket.setdefault(str(r["ticket"]), {"n": 0, "pass": 0})
            t["n"] += 1
            t["pass"] += 1 if r.get("success") else 0
            per_repeat.setdefault(int(r.get("repeat", 0)), []).append(bool(r.get("success")))
        rep_rates = [sum(v) / len(v) for v in per_repeat.values() if v]
        credits = [r.get("ai_credits") for r in recs]
        rows.append({
            "variant": var,
            "label": recs[-1].get("variant_label", var),
            "tests": recs[-1].get("variant_tests", ""),
            "overlay_from": recs[-1].get("overlay_from"),       # passed through, one value per variant
            "overlay_commit": recs[-1].get("overlay_commit"),
            "applied_knobs": recs[-1].get("applied_knobs"),     # the same: null without an overlay
            "runs": n,
            "passes": passes,
            "pass_rate": round(passes / n, 4) if n else 0.0,
            "pass_ci": [round(lo, 4), round(hi, 4)],
            "pass_rate_sd": round(statistics.pstdev(rep_rates), 4) if len(rep_rates) > 1 else None,
            "commits": commits,
            "commit_rate": round(commits / n, 4) if n else 0.0,
            "credits_mean": _mean(credits),
            "credits_total": round(sum(c for c in credits if c is not None), 4),
            "credits_per_pass": round(sum(c for c in credits if c is not None) / passes, 4) if passes else None,
            "interventions_mean": _mean([r.get("interventions") for r in recs]),
            "approvals_mean": _mean([r.get("approvals") for r in recs]),
            "escalations_mean": _mean([r.get("escalations") for r in recs]),
            "retries_mean": _mean([r.get("retries") for r in recs]),
            "duration_mean_s": _mean([r.get("duration_s") for r in recs]),
            "gate_violations_mean": _mean([r.get("gate_violations") for r in recs]),
            "gate_score_mean": _mean([r.get("gate_violation_score") for r in recs]),
            "gate_catches_total": sum(int(r.get("gate_failures_total") or 0) for r in recs),
            "reviewer_score_mean": _mean([r.get("reviewer_score") for r in recs]),
            "path_violations_total": sum(len(r.get("path_violations") or []) for r in recs),
            "diff_lines_mean": _mean([(r.get("diff") or {}).get("lines") for r in recs]),
            "tokens_in_total": sum(int((r.get("tokens") or {}).get("in") or 0) for r in recs),
            "tokens_out_total": sum(int((r.get("tokens") or {}).get("out") or 0) for r in recs),
            "tokens_cached_total": sum(int((r.get("tokens") or {}).get("cached") or 0) for r in recs),
            "errors": sum(1 for r in recs if r.get("error")),
            "per_ticket": dict(sorted(per_ticket.items())),
        })
    rows.sort(key=lambda r: (-r["pass_rate"], r["credits_mean"] if r["credits_mean"] is not None else 1e18,
                             _variant_order(r["variant"])))
    return rows


def committed(rec: dict[str, Any]) -> bool:
    """The workflow ended with a commit (records before the field existed: commits > 0)."""
    if rec.get("workflow_committed") is not None:
        return bool(rec["workflow_committed"])
    return int(rec.get("commits") or 0) > 0


_ORDER = {v: i for i, v in enumerate(names.VARIANTS.values())}


def _variant_order(name: str) -> tuple[int, int, str]:
    """Any other name first, alphabetically (the units' own harnesses in the room), then the known variants in the
    table's order (raw, gates ... cheap)."""
    return (1, _ORDER[name], name) if name in _ORDER else (0, 0, name)


SLIM_KEYS = ("experiment", "variant", "ticket", "repeat", "success", "hidden_pass", "workflow_committed", "commits",
             "run_status", "status", "ai_credits",
             "interventions", "approvals", "escalations", "retries", "duration_s", "gate_violations",
             "gate_violation_score", "reviewer_score", "error", "finished_at", "branch",
             "overlay_from", "overlay_commit", "applied_knobs")


def build_board(records: list[dict[str, Any]]) -> dict[str, Any]:
    recs = latest(records)
    return {
        "schema": SCHEMA,
        "generated_at": now_iso(),
        "experiments": sorted({str(r.get("experiment")) for r in recs}),
        "tickets": sorted({str(r.get("ticket")) for r in recs}),
        "variants": aggregate(recs),
        "runs": [{k: r.get(k) for k in SLIM_KEYS} for r in
                 sorted(recs, key=lambda r: (_variant_order(str(r.get("variant"))), str(r.get("ticket")),
                                             int(r.get("repeat", 0))))],
    }


def write_board(records: list[dict[str, Any]], out: Path) -> dict[str, Any]:
    board = build_board(records)
    out = Path(out)
    out.parent.mkdir(parents=True, exist_ok=True)
    tmp = out.with_name(out.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(board, fh, indent=1, sort_keys=True)
        fh.write("\n")
    os.replace(tmp, out)      # the board polls the file: never serve a half-written one
    return board


def format_table(rows: list[dict[str, Any]]) -> str:
    w = max([len("variant")] + [len(str(r["variant"])) for r in rows])     # the longest name sets the first column
    head = f"{'variant':<{w}} {'runs':>4} {'pass':>9} {'95% CI':>11} {'commit':>9} {'credits':>8} {'interv':>6} " \
           f"{'retries':>7} {'dur s':>7} {'gates':>5} {'review':>6}  label"
    lines = [head, "-" * len(head)]

    def f(v: Any, nd: int = 1) -> str:
        return "-" if v is None else f"{v:.{nd}f}"
    for r in rows:
        ci = f"{r['pass_ci'][0] * 100:.0f}-{r['pass_ci'][1] * 100:.0f}%"
        lines.append(f"{r['variant']:<{w}} {r['runs']:>4} {r['passes']:>3}/{r['runs']:<2} {r['pass_rate'] * 100:>3.0f}%"
                     f" {ci:>9} {r['commits']:>3}/{r['runs']:<2} {r['commit_rate'] * 100:>3.0f}%"
                     f" {f(r['credits_mean'], 2):>8} {f(r['interventions_mean']):>6}"
                     f" {f(r['retries_mean']):>7} {f(r['duration_mean_s'], 0):>7} {f(r['gate_violations_mean']):>5}"
                     f" {f(r['reviewer_score_mean']):>6}  {r['label']}")
    return "\n".join(lines)
