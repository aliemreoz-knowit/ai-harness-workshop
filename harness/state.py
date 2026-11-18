"""Resumable run state: ``.harness/runs/<id>/state.json``.

The orchestrator saves after every state change (atomic replace), so a run
that dies -- Ctrl-C, a crashed laptop, an expired token -- continues with
``python -m harness run --resume <id>`` at the first step that is not done.
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import secrets
import time
import re
from pathlib import Path
from typing import Any

STATE_FILE = "state.json"
REPLACE_TRIES = 20          # state.json replace on Windows: retried while a reader holds the file
REPLACE_PAUSE_S = 0.05
STATUSES = ("running", "done", "failed", "escalated", "aborted")


def _now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


def new_run_id(key: str | None, now: _dt.datetime | None = None) -> str:
    now = now or _dt.datetime.now()
    slug = re.sub(r"[^a-z0-9]+", "-", (key or "run").lower()).strip("-") or "run"
    return f"{now:%Y%m%d-%H%M%S}-{slug}-{secrets.token_hex(2)}"


class RunState:
    def __init__(self, path: Path, data: dict[str, Any]) -> None:
        self.path = path
        self.data = data

    # -- construction -----------------------------------------------------------
    @classmethod
    def create(cls, runs_dir: Path, run_id: str, workflow: str, steps: list[str], **fields: Any) -> RunState:
        run_dir = runs_dir / run_id
        run_dir.mkdir(parents=True, exist_ok=True)
        try:
            # exclusive create: of two creators of one id (a caller-chosen --run-id) exactly one gets here
            (run_dir / STATE_FILE).open("x").close()
        except FileExistsError:
            raise FileExistsError(f"run {run_id} already exists") from None
        data = {
            "run_id": run_id, "workflow": workflow, "status": "running", "cursor": 0,
            "created": _now(), "updated": _now(), "resumed": 0,
            "steps": {s: {"status": "pending", "attempts": 0, "rounds": 0} for s in steps},
            "order": list(steps),
            "counters": {"approvals_asked": 0, "approvals_auto": 0, "approvals_counted": 0,
                         "escalations": 0, "retries": 0, "review_rejects": 0, "denials": 0},
            "usage": {}, "sessions": {}, "feedback": {}, "artifacts": {},
            **fields,
        }
        st = cls(run_dir / STATE_FILE, data)
        st.save()
        return st

    @classmethod
    def load(cls, runs_dir: Path, run_id: str) -> RunState:
        path = runs_dir / run_id / STATE_FILE
        if not path.is_file():
            known = ", ".join(sorted(p.name for p in runs_dir.glob("*") if (p / STATE_FILE).exists())[-5:])
            raise FileNotFoundError(f"no run {run_id} in {runs_dir} (latest: {known or 'none'})")
        return cls(path, json.loads(path.read_text(encoding="utf-8")))

    @staticmethod
    def list_runs(runs_dir: Path) -> list[dict[str, Any]]:
        out = []
        for p in sorted(runs_dir.glob("*/" + STATE_FILE)):
            try:
                d = json.loads(p.read_text(encoding="utf-8"))
            except (OSError, ValueError):
                continue
            out.append({k: d.get(k) for k in ("run_id", "workflow", "status", "mode", "created", "updated")} |
                       {"ticket": (d.get("ticket") or {}).get("key"),
                        "credits": (d.get("usage") or {}).get("credits")})
        return out

    # -- persistence ------------------------------------------------------------
    def save(self) -> None:
        self.data["updated"] = _now()
        tmp = self.path.with_suffix(".tmp")
        tmp.write_text(json.dumps(self.data, indent=2, ensure_ascii=False, default=str), encoding="utf-8",
                       newline="\n")
        for attempt in range(REPLACE_TRIES):
            try:
                os.replace(tmp, self.path)
                return
            except PermissionError:      # Windows: a reader (the console, the board) has state.json open
                if attempt + 1 == REPLACE_TRIES:
                    raise
                time.sleep(REPLACE_PAUSE_S)

    # -- accessors --------------------------------------------------------------
    @property
    def run_id(self) -> str:
        return str(self.data["run_id"])

    @property
    def dir(self) -> Path:
        return self.path.parent

    @property
    def cursor(self) -> int:
        return int(self.data.get("cursor", 0))

    @cursor.setter
    def cursor(self, value: int) -> None:
        self.data["cursor"] = value

    @property
    def status(self) -> str:
        return str(self.data.get("status"))

    @status.setter
    def status(self, value: str) -> None:
        assert value in STATUSES, value
        self.data["status"] = value

    def step(self, step_id: str) -> dict[str, Any]:
        return self.data["steps"].setdefault(step_id, {"status": "pending", "attempts": 0, "rounds": 0})

    def count(self, name: str, n: int = 1) -> None:
        c = self.data.setdefault("counters", {})
        c[name] = int(c.get(name, 0)) + n

    @property
    def counters(self) -> dict[str, int]:
        return self.data.setdefault("counters", {})

    def last_done(self) -> str | None:
        done = [s for s in self.data.get("order", []) if self.step(s)["status"] == "done"]
        return done[-1] if done else None
