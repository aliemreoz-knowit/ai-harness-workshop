"""Data files of the mission board (tools/board/mission.html): tickets.json and units.json.

    python -m harness board init   [--dir .harness/board] [--units 10] [--force]
    python -m harness board ticket <ID> <state> [--unit U3] [--note text]
    python -m harness board unit   <U3> [--level 2] [--ticket ID] [--variant team] [--name "Unit 3"]
    python -m harness board show

`init` takes the ticket ids and their Jira titles (the first heading of
tickets/<ID>/ticket.md) from the hidden-tests repository. The files are
written atomically (the board polls them) to .harness/board/ (default), which
is git-ignored and outside src/ include/ tests/ tools/ fuzz/, so ticket ids on
the board never trip the misra ticket-id scan of a checkout.
"""
from __future__ import annotations

import argparse
import json
import os
import re
import sys
from pathlib import Path
from typing import Any, Optional

from . import tickets as ticketmod
from .results import now_iso

STATES = ["todo", "in_progress", "gates", "review", "hil_ready", "on_pi", "done", "blocked"]
LEVELS = {
    0: "raw agent",
    1: "L1 context + prompts",
    2: "L2 gates + iteration",
    3: "L3 roles + guardrails + compliance",
    4: "L4 autonomy + eval",
    5: "L5 team workflow + handover",
}
REPO = Path(__file__).resolve().parents[2]
DEFAULT_DIR = REPO / ".harness" / "board"


def _load(path: Path, default: dict[str, Any]) -> dict[str, Any]:
    if not path.is_file():
        return default
    with open(path, encoding="utf-8") as fh:
        return json.load(fh)


def _save(path: Path, doc: dict[str, Any]) -> None:
    doc["updated_at"] = now_iso()
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".tmp")
    with open(tmp, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(doc, fh, indent=1)
        fh.write("\n")
    os.replace(tmp, path)


def jira_title(hidden: Optional[Path], tid: str) -> str:
    """'# ID: title' of ticket.md without the id; '' when there is none."""
    if hidden is None:
        return ""
    md = hidden / "tickets" / tid / "ticket.md"
    if not md.is_file():
        return ""
    for line in md.read_text(encoding="utf-8", errors="replace").splitlines():
        if line.startswith("# "):
            title = re.sub(rf"^{re.escape(tid)}\s*[:\-]\s*", "", line[2:].strip())
            return "" if title.startswith("<") else title
    return ""


def init(board_dir: Path, hidden: Optional[Path], units: int, ids: Optional[list[str]], force: bool) -> None:
    tpath, upath = board_dir / "tickets.json", board_dir / "units.json"
    if not force and (tpath.exists() or upath.exists()):
        raise SystemExit(f"{board_dir}: tickets.json / units.json exist (use --force to overwrite)")
    if ids is None:
        root = hidden / "tickets" if hidden else None
        ids = sorted(d.name for d in root.iterdir() if d.is_dir() and ticketmod.TICKET_RE.match(d.name)) \
            if root and root.is_dir() else []
    tickets = []
    for tid in ids:
        t = ticketmod.load_ticket(hidden, tid)
        tickets.append({"id": tid, "title": jira_title(hidden, tid), "state": "todo", "unit": "",
                        "eval": t.eval, "note": ""})
    _save(tpath, {"states": STATES, "tickets": tickets})
    _save(upath, {"levels": {str(k): v for k, v in LEVELS.items()},
                  "units": [{"id": f"U{i}", "name": f"Unit {i}", "level": 0, "ticket": "", "variant": ""}
                            for i in range(1, units + 1)]})


def set_ticket(board_dir: Path, tid: str, state: str, unit: Optional[str], note: Optional[str]) -> dict:
    if state not in STATES:
        raise SystemExit(f"state must be one of {', '.join(STATES)}")
    path = board_dir / "tickets.json"
    doc = _load(path, {"states": STATES, "tickets": []})
    entry = next((t for t in doc["tickets"] if t["id"] == tid), None)
    if entry is None:
        entry = {"id": tid, "title": "", "state": "todo", "unit": "", "note": ""}
        doc["tickets"].append(entry)
    entry["state"] = state
    entry["since"] = now_iso()
    if unit is not None:
        entry["unit"] = unit
    if note is not None:
        entry["note"] = note
    _save(path, doc)
    return entry


def set_unit(board_dir: Path, uid: str, level: Optional[int], ticket: Optional[str],
             variant: Optional[str], name: Optional[str]) -> dict:
    if level is not None and level not in LEVELS:
        raise SystemExit("level must be 0..5")
    path = board_dir / "units.json"
    doc = _load(path, {"levels": {str(k): v for k, v in LEVELS.items()}, "units": []})
    entry = next((u for u in doc["units"] if u["id"] == uid), None)
    if entry is None:
        entry = {"id": uid, "name": name or uid, "level": 0, "ticket": "", "variant": ""}
        doc["units"].append(entry)
    for key, val in (("level", level), ("ticket", ticket), ("variant", variant), ("name", name)):
        if val is not None:
            entry[key] = val
    _save(path, doc)
    return entry


def main(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness board", description=__doc__.split("\n\n")[0])
    p.add_argument("--dir", type=Path, default=DEFAULT_DIR, help="board data directory (default .harness/board)")
    sub = p.add_subparsers(dest="cmd", required=True)
    pi = sub.add_parser("init", help="create tickets.json and units.json")
    pi.add_argument("--hidden-tests", type=Path, default=None)
    pi.add_argument("--units", type=int, default=10)
    pi.add_argument("--tickets", default=None, help="comma list (default: every ticket in hidden-tests)")
    pi.add_argument("--force", action="store_true")
    pt = sub.add_parser("ticket", help="move a ticket to a state")
    pt.add_argument("id")
    pt.add_argument("state", choices=STATES)
    pt.add_argument("--unit", default=None)
    pt.add_argument("--note", default=None)
    pu = sub.add_parser("unit", help="update a unit (harness level, ticket, variant)")
    pu.add_argument("id")
    pu.add_argument("--level", type=int, default=None)
    pu.add_argument("--ticket", default=None)
    pu.add_argument("--variant", default=None)
    pu.add_argument("--name", default=None)
    sub.add_parser("show", help="print the board files")
    args = p.parse_args(argv)
    board_dir: Path = args.dir
    if args.cmd == "init":
        from .cli import find_hidden_tests
        hidden = args.hidden_tests or find_hidden_tests(REPO)
        ids = [t.strip() for t in args.tickets.split(",") if t.strip()] if args.tickets else None
        init(board_dir, hidden, args.units, ids, args.force)
        print(f"wrote {board_dir / 'tickets.json'} and {board_dir / 'units.json'}")
    elif args.cmd == "ticket":
        print(json.dumps(set_ticket(board_dir, args.id, args.state, args.unit, args.note)))
    elif args.cmd == "unit":
        print(json.dumps(set_unit(board_dir, args.id, args.level, args.ticket, args.variant, args.name)))
    else:
        for name in ("tickets.json", "units.json"):
            f = board_dir / name
            print(f"--- {f}\n{f.read_text(encoding='utf-8') if f.is_file() else '(missing)'}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
