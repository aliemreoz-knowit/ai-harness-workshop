"""Bridge state (what was handled) and the HIL queue file shared with hil.py.

State ``.harness/bridge/state.json`` (atomic replace)::

    {"items": {"gh#12": {"status": "done", "run_id": ..., "pr": 31, ...}},
     "pr_sections": {"31": {"agent": "...markdown...", "hil": "..."}}}

An item is handled once. When it stops matching its trigger (label removed,
status changed) and later matches again, it is re-armed: remove and re-add the
label to run it again. An item left ``running`` longer than harness.timeout_s
(the bridge died mid-run) is re-armed as well.

HIL queue ``.harness/hil/queue.jsonl`` -- append-only JSON lines; the current
state of an entry is the fold of all lines with its ``id`` (later fields win)::

    {"op": "enqueue", "id": "pr31-1a2b3c4", "pr": 31, "sha": "<40 hex>", "branch": "agent/gh-12-ab12",
     "repo": "owner/name", "url": "...", "title": "...", "status": "queued", "enqueued_at": "...",
     "check_run_id": 123, "comment_tag": "pr"}
    {"op": "update", "id": "pr31-1a2b3c4", "status": "running" | "passed" | "failed" | "skipped", ...}

hil.py takes the oldest ``queued`` entry, appends ``update`` lines, and edits
the PR's bridge comment (marker ``<!-- tinystream-bridge:pr -->``, section "hil").
"""

from __future__ import annotations

import datetime as _dt
import json
import os
import tempfile
from pathlib import Path
from typing import Any, Optional


def now() -> str:
    return _dt.datetime.now(_dt.timezone.utc).isoformat(timespec="seconds")


class State:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)
        try:
            self.data: dict[str, Any] = json.loads(self.path.read_text(encoding="utf-8"))
        except (OSError, ValueError):
            self.data = {}
        self.data.setdefault("items", {})
        self.data.setdefault("pr_sections", {})

    def save(self) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self.path.parent, prefix=".state-", suffix=".json")
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            json.dump(self.data, fh, indent=2, sort_keys=True)
            fh.write("\n")
        os.replace(tmp, self.path)

    @property
    def items(self) -> dict[str, dict[str, Any]]:
        return self.data["items"]

    def get(self, item_id: str) -> Optional[dict[str, Any]]:
        return self.items.get(item_id)

    def handled(self, item_id: str) -> bool:
        return item_id in self.items

    def update(self, item_id: str, **fields: Any) -> dict[str, Any]:
        it = self.items.setdefault(item_id, {"first_seen": now()})
        it.update(fields, updated=now())
        self.save()
        return it

    def stale(self, kind: str, stale_s: Optional[float]) -> list[str]:
        """Items of ``kind`` left ``running`` for more than ``stale_s`` seconds: the bridge died
        mid-run or the run outlived harness.timeout_s; they will never finish on their own."""
        if not stale_s:
            return []
        cutoff = _dt.datetime.now(_dt.timezone.utc) - _dt.timedelta(seconds=float(stale_s))
        out = []
        for i, it in self.items.items():
            if it.get("kind") != kind or it.get("status") != "running":
                continue
            try:
                since = _dt.datetime.fromisoformat(str(it.get("updated") or it.get("first_seen")))
            except ValueError:
                since = None
            if since is None or since <= cutoff:
                out.append(i)
        return out

    def rearm(self, kind: str, matching: set[str], stale_s: Optional[float] = None) -> list[str]:
        """Forget finished items of ``kind`` that no longer match their trigger, and items left
        ``running`` longer than ``stale_s`` (matching or not: a run that never ended is not handled)."""
        stale = set(self.stale(kind, stale_s))
        gone = [i for i, it in self.items.items()
                if it.get("kind") == kind
                and (i in stale or (i not in matching and it.get("status") != "running"))]
        for i in gone:
            del self.items[i]
        if gone:
            self.save()
        return gone

    def pr_section(self, pr: int, name: str, markdown: str) -> dict[str, str]:
        sec = self.data["pr_sections"].setdefault(str(pr), {})
        sec[name] = markdown
        self.save()
        return dict(sec)


class HilQueue:
    def __init__(self, path: Path) -> None:
        self.path = Path(path)

    def _append(self, rec: dict[str, Any]) -> None:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        with open(self.path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True) + "\n")

    def entries(self) -> list[dict[str, Any]]:
        folded: dict[str, dict[str, Any]] = {}
        try:
            lines = self.path.read_text(encoding="utf-8").splitlines()
        except OSError:
            return []
        for line in lines:
            try:
                rec = json.loads(line)
            except ValueError:
                continue
            if not isinstance(rec, dict) or "id" not in rec:
                continue
            cur = folded.setdefault(rec["id"], {})
            cur.update({k: v for k, v in rec.items() if k != "op"})
        return list(folded.values())

    def find(self, pr: int, sha: str) -> Optional[dict[str, Any]]:
        return next((e for e in self.entries() if e.get("pr") == pr and e.get("sha") == sha), None)

    def pending(self) -> list[dict[str, Any]]:
        return [e for e in self.entries() if e.get("status") == "queued"]

    def position(self, entry_id: str) -> int:
        ids = [e["id"] for e in self.pending()]
        return ids.index(entry_id) + 1 if entry_id in ids else 0

    def enqueue(self, **fields: Any) -> dict[str, Any]:
        rec = {"op": "enqueue", "status": "queued", "enqueued_at": now(), **fields}
        rec.setdefault("id", f"pr{fields['pr']}-{str(fields['sha'])[:7]}")
        self._append(rec)
        return {k: v for k, v in rec.items() if k != "op"}

    def update(self, entry_id: str, **fields: Any) -> None:
        self._append({"op": "update", "id": entry_id, "updated_at": now(), **fields})
