"""Ticket metadata from the hidden-tests repository (tickets/<ID>/meta.yaml).

Only the keys the runner needs are read: ticket_branch, eval, allowed_paths,
modules. Facilitator-only text (root cause, fix) is never copied anywhere.
"""
from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

from .. import names
from .yamlio import load_yaml

TICKET_RE = re.compile(r"^[A-Z][A-Z0-9]*-[0-9]+$")


@dataclass
class Ticket:
    id: str
    ticket_branch: str
    eval: bool = False
    allowed_paths: list[str] = field(default_factory=list)
    modules: list[str] = field(default_factory=list)
    has_meta: bool = False


def default_ticket_branch(ticket_id: str) -> str:
    return f"ticket/{ticket_id}"


def load_ticket(hidden_dir: Optional[Path], ticket_id: str) -> Ticket:
    if not TICKET_RE.match(ticket_id):
        raise ValueError(f"bad ticket id {ticket_id!r}")
    meta_path = Path(hidden_dir) / "tickets" / ticket_id / "meta.yaml" if hidden_dir else None
    if meta_path is None or not meta_path.is_file():
        return Ticket(id=ticket_id, ticket_branch=default_ticket_branch(ticket_id))
    meta = load_yaml(meta_path) or {}
    return Ticket(
        id=ticket_id,
        ticket_branch=names.branch(str(meta.get("ticket_branch") or meta.get("ticket_branch")  # ticket_branch: older files
                                       or default_ticket_branch(ticket_id))),
        eval=bool(meta.get("eval", False)),
        allowed_paths=[str(p) for p in (meta.get("allowed_paths") or [])],
        modules=[str(p) for p in (meta.get("modules") or [])],
        has_meta=True,
    )


def eval_set(hidden_dir: Path) -> list[str]:
    """Ticket ids with eval: true, sorted."""
    out = []
    root = Path(hidden_dir) / "tickets"
    if not root.is_dir():
        return out
    for d in sorted(root.iterdir()):
        if d.is_dir() and TICKET_RE.match(d.name) and load_ticket(hidden_dir, d.name).eval:
            out.append(d.name)
    return out


def parse_ticket_list(spec: str, hidden_dir: Optional[Path]) -> list[str]:
    """'KEY-1,KEY-2' or 'eval' (every eval ticket in the hidden-tests repo) or a mix."""
    out: list[str] = []
    for part in (p.strip() for p in spec.split(",")):
        if not part:
            continue
        if part.lower() == "eval":
            if hidden_dir is None:
                raise ValueError("--tickets eval needs --hidden-tests")
            ids = eval_set(hidden_dir)
            if not ids:
                raise ValueError(f"no eval tickets under {hidden_dir}/tickets")
            out.extend(ids)
        elif TICKET_RE.match(part):
            out.append(part)
        else:
            raise ValueError(f"bad ticket id {part!r}")
    seen: set[str] = set()
    return [t for t in out if not (t in seen or seen.add(t))]


def _glob_match(path: str, pattern: str) -> bool:
    """fnmatch with '**' spanning directories and 'dir/**' matching everything below dir."""
    if pattern.endswith("/**") and path.startswith(pattern[:-3] + "/"):
        return True
    regex = ""
    i = 0
    while i < len(pattern):
        if pattern.startswith("**/", i):
            regex += "(?:.*/)?"
            i += 3
        elif pattern.startswith("**", i):
            regex += ".*"
            i += 2
        else:
            c = pattern[i]
            regex += "[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c)
            i += 1
    return re.fullmatch(regex, path) is not None


def path_violations(files: list[str], allowed: list[str]) -> list[str]:
    """Changed files outside the ticket's allowed paths (empty when no allow-list is known)."""
    if not allowed:
        return []
    return [f for f in files if not any(_glob_match(f, p) for p in allowed)]
