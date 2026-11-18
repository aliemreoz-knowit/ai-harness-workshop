"""Handover (block 5): another unit's harness structure for a run, and the units' usage guides.

A handover run (``Runner.submit(..., overlay_from="unit-04")``) runs a ticket with the harness structure of another
unit. This module takes that structure from git and checks it; ``Runner`` puts it in a commit
(``files.commit_files``) on a branch ``run/<id>`` made from the running unit's branch, and runs there.

``collect`` reads the overlay from the tip of the other unit's branch (``refs/heads/unit/NN``), resolved to one full
commit id first (``Overlay.commit``, kept on the run record), never from its worktree: every console save commits to
that branch and a kept run lands on it, so the tip is the harness of that unit, whatever its worktree holds meanwhile
(a run of its own on ``run/<id>`` with another unit's harness, an agent's uncommitted edits). Nothing in its worktree
is read, so no lock of it is taken, and no link, junction or folder swap there can matter. The tree and the blobs come
from the hardened git of ``files`` (explicit git dir, empty hooks folder, no fsmonitor, allowlisted environment):
``ls-tree -r -z -l`` once, then ``cat-file --batch`` for the files taken.

What is taken: blobs (mode 100644 or 100755) whose path matches an ``overlay`` glob (spec sections 4 and 6) **and** an
``editable`` glob (a file a unit could not have saved, such as a script under ``.github/skills/`` or Python under
``harness/``, never travels, whoever committed it), spelled as ``files.usable_path`` allows, at most ``files.MAX_BYTES``
each, UTF-8 text without NUL, at most ``MAX_FILES`` files and ``MAX_TOTAL`` bytes. Left out and listed in
``Overlay.skipped``: links (mode 120000) and gitlinks (160000) at an overlay path or where an overlay path would be
below them, and every other entry that fails one of those rules, among them a path that differs only in case from
another taken path or from a path of the running unit's own tree (a checkout on a case-insensitive disk would merge
them). Only ``MAX_ENTRIES`` entries of the tree are looked at; the rest is counted in the list. Files the running unit
has and the other has not stay as they are: an overlay adds and replaces, it never removes.

``problems`` runs the save rules (``validate.check``) on each file as the editor would show it. The files were
validated when the other unit saved them, but an agent's change that was kept was not, so a file that fails still
refuses the run.

``guides`` reads every unit's ``docs/usage-guide.md`` (an editable file, written in the Code view) for the guides
page, read-only, through ``files.read``. The Code view cannot make a file, and the export has no guide, so
``start_guide`` saves the template (``usage-guide-template.md``, the block 5 template of the facilitator's materials)
as a unit's guide, once, through the same save as any file.
"""

from __future__ import annotations

import codecs
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable, Iterable, NamedTuple, Sequence

from console import files
from console.units import Unit

__all__ = [
    "GUIDE", "GUIDE_MAX", "MAX_ENTRIES", "MAX_FILES", "MAX_SKIPPED", "MAX_TOTAL", "Overlay", "collect", "guides",
    "problems", "start_guide",
]  # fmt: skip

GUIDE = "docs/usage-guide.md"  # a unit's usage guide (editable: spec section 6)
GUIDE_MAX = 64 * 1024  # a guide longer than this is not shown on the guides page
TEMPLATE = Path(__file__).with_name("usage-guide-template.md")
MAX_FILES = 200  # files an overlay takes
MAX_TOTAL = 2 * 1024 * 1024  # bytes an overlay takes in all
MAX_ENTRIES = 20000  # entries of the other unit's tree that are looked at
MAX_SKIPPED = 100  # paths listed as left out (the rest is counted)
_PATH_SHOWN = 200
_MAX_SHOWN_ERROR = 200
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_FILE_MODES = frozenset({"100644", "100755"})
_LINK_MODES = frozenset({"120000", "160000"})  # a symbolic link, a gitlink (a submodule)


@dataclass(frozen=True)
class Overlay:
    """What ``collect`` took: ``files`` (path with ``/`` -> the blob's bytes), the ``skipped`` paths and the
    ``commit`` (full id) of the other unit's branch tip they come from."""

    source: str  # the unit it comes from
    files: dict[str, bytes] = field(default_factory=dict)
    skipped: list[str] = field(default_factory=list)
    commit: str = ""


class _Entry(NamedTuple):
    mode: str
    kind: str
    blob: str
    size: int  # -1 for an entry that is not a blob
    path: str


# --- taking the files ---------------------------------------------------------------------------------------


def _matches(rel: str, patterns: Sequence[str]) -> bool:
    return files.is_editable(rel, patterns)[0]


def _tip(unit: Unit) -> str:
    """The full id of the commit ``unit``'s branch is on; ``RuntimeError`` when git cannot say."""
    out = files.git_bytes(unit.worktree, "rev-parse", "--verify", f"refs/heads/{unit.branch}^{{commit}}")
    sha = out.decode("ascii", errors="replace").strip()
    if not _SHA.fullmatch(sha):
        raise RuntimeError(f"{unit.branch} is not on a commit")
    return sha


def _entries(worktree: Path, commit: str) -> tuple[list[_Entry], int]:
    """The entries of the tree of ``commit`` (at most ``MAX_ENTRIES``) and how many more there are."""
    raw = files.git_bytes(worktree, "ls-tree", "-r", "-z", "-l", "--full-tree", commit)
    items = [item for item in raw.split(b"\0") if item]
    out = []
    for item in items[:MAX_ENTRIES]:
        meta, _, name = item.partition(b"\t")
        mode, kind, blob, size = meta.decode("ascii").split()
        path = name.decode("utf-8", errors="replace")
        out.append(_Entry(mode, kind, blob, int(size) if size.isdigit() else -1, path))
    return out, max(0, len(items) - MAX_ENTRIES)


def _names(unit: Unit) -> list[str]:
    """Every file path in the tree of ``unit``'s branch tip."""
    raw = files.git_bytes(unit.worktree, "ls-tree", "-r", "-z", "--name-only", "--full-tree", _tip(unit))
    return [name.decode("utf-8", errors="replace") for name in raw.split(b"\0") if name]


def _prefixes(rel: str) -> list[str]:
    parts = rel.split("/")
    return ["/".join(parts[:n]) for n in range(1, len(parts) + 1)]


def _colliding(candidates: Sequence[str], own: Iterable[str]) -> set[str]:
    """The ``candidates`` that, on a disk that does not tell upper case from lower case, would be the same file or
    folder as another candidate or as a path in ``own`` that is spelled differently."""
    spelled: dict[str, set[str]] = {}
    for rel in candidates:
        for prefix in _prefixes(rel):
            spelled.setdefault(prefix.casefold(), set()).add(prefix)
    for rel in own:
        for prefix in _prefixes(rel):
            found = spelled.get(prefix.casefold())
            if found is not None:
                found.add(prefix)
    return {rel for rel in candidates if any(len(spelled[prefix.casefold()]) > 1 for prefix in _prefixes(rel))}


def _text(data: bytes) -> str | None:
    """The file as the editor shows it (one byte order mark and CR LF taken out); ``None`` when it is not UTF-8 text."""
    body = data[len(codecs.BOM_UTF8) :] if data.startswith(codecs.BOM_UTF8) else data
    try:
        text = body.decode("utf-8")
    except UnicodeDecodeError:
        return None
    return None if "\0" in text else text.replace("\r\n", "\n")


def _blobs(worktree: Path, ids: Sequence[str]) -> list[bytes | None]:
    """The content of each blob (``None`` for one git does not have), through one ``cat-file --batch``."""
    if not ids:
        return []
    raw = files.git_bytes(worktree, "cat-file", "--batch", stdin="".join(f"{blob}\n" for blob in ids))
    out: list[bytes | None] = []
    pos = 0
    for _ in ids:
        end = raw.index(b"\n", pos)
        head = raw[pos:end].split()
        if len(head) == 3 and head[1] == b"blob" and head[2].isdigit():
            size = int(head[2])
            data = raw[end + 1 : end + 1 + size]
            out.append(data if len(data) == size else None)
            pos = end + 1 + size + 1
        else:  # "<id> missing"
            out.append(None)
            pos = end + 1
    return out


def collect(source: Unit, running: Unit, overlay: Sequence[str], editable: Sequence[str]) -> Overlay:
    """The overlay files of unit ``source`` for a run of unit ``running``; see the module doc for what is taken from
    where, and what is not. ``RuntimeError`` when git cannot give a tree."""
    tip = _tip(source)
    entries, more = _entries(source.worktree, tip)
    left: list[str] = []
    found: list[_Entry] = []
    for entry in entries:
        rel = entry.path
        named = _matches(rel, overlay)
        if entry.mode in _LINK_MODES or entry.kind == "commit":
            if named or _matches(f"{rel}/_", overlay):  # at an overlay path, or where one would be below it
                left.append(rel)
        elif named:
            takeable = (
                entry.mode in _FILE_MODES
                and entry.kind == "blob"
                and 0 <= entry.size <= files.MAX_BYTES
                and "�" not in rel
                and files.usable_path(rel)
                and _matches(rel, editable)
            )
            if takeable:
                found.append(entry)
            else:
                left.append(rel)
    if found:
        clash = _colliding([entry.path for entry in found], _names(running))
        left.extend(entry.path for entry in found if entry.path in clash)
        found = [entry for entry in found if entry.path not in clash]

    chosen: list[_Entry] = []
    total = 0
    for entry in sorted(found, key=lambda e: e.path):
        if len(chosen) >= MAX_FILES or total + entry.size > MAX_TOTAL:
            left.append(entry.path)
            continue
        chosen.append(entry)
        total += entry.size
    taken: dict[str, bytes] = {}
    for entry, data in zip(chosen, _blobs(source.worktree, [e.blob for e in chosen])):
        if data is None or _text(data) is None:
            left.append(entry.path)
        else:
            taken[entry.path] = data

    left.sort()
    shown = [path if len(path) <= _PATH_SHOWN else f"{path[:_PATH_SHOWN]}..." for path in left[:MAX_SKIPPED]]
    if len(left) > MAX_SKIPPED:
        shown.append(f"... and {len(left) - MAX_SKIPPED} more")
    if more:
        shown.append(f"... and {more} more entries of the tree were not looked at")
    return Overlay(source.id, taken, shown, tip)


def problems(overlay: Overlay, check: Callable[[str, str], list[str]]) -> list[str]:
    """The files of ``overlay`` that fail ``check`` (the save rules): one sentence each, ``path: first reason``.

    ``check(path, text)`` is ``validate.check`` bound to the golden config; it gets each file as the editor shows it.
    """
    out = []
    for rel in sorted(overlay.files):
        text = _text(overlay.files[rel])
        reasons = check(rel, text) if text is not None else [f"{rel}: not UTF-8 text"]
        if not reasons:
            continue
        reason = reasons[0]
        for prefix in (f"{rel}: ", f"{rel.rsplit('/', 1)[-1]}: "):  # the rules name the file; so does this sentence
            reason = reason.removeprefix(prefix)
        more = f" (and {len(reasons) - 1} more)" if len(reasons) > 1 else ""
        out.append(f"{rel}: {reason[:_MAX_SHOWN_ERROR]}{more}")
    return out


# --- the usage guides ---------------------------------------------------------------------------------------


def guides(units: Sequence[Unit], editable: Sequence[str]) -> list[dict]:
    """Every unit's usage guide as ``{unit, name, state, text}``, for the guides page.

    ``state`` is ``ok`` (``text`` is the guide), ``missing`` (the unit has none yet), ``too long`` (over
    ``GUIDE_MAX`` bytes) or ``unreadable`` (a link, not text, or the disk said no); ``text`` is ``None`` unless ``ok``.
    """
    out = []
    for unit in units:
        state, text = "ok", None
        try:
            view = files.read(unit.worktree, GUIDE, editable, GUIDE_MAX)
            text = view.content
            if text is None:
                state = "unreadable"
        except FileNotFoundError:
            state = "missing"
        except files.Invalid:
            state = "too long"
        except (files.Forbidden, OSError, RuntimeError):
            state = "unreadable"
        out.append({"unit": unit.id, "name": unit.name, "state": state, "text": text})
    return out


def start_guide(
    unit: Unit,
    editable: Sequence[str],
    check: Callable[[str, str], list[str]],
    guard: Callable[[], str | None] | None = None,
) -> files.FileView:
    """Save the template as ``unit``'s guide: a new file, one commit (``files.write``, which says what it raises:
    ``files.Conflict`` when the unit has a guide already, ``files.Busy`` when ``guard`` says a run holds the unit)."""
    text = TEMPLATE.read_text(encoding="utf-8")
    return files.write(unit.worktree, GUIDE, text, "", editable, unit.id, check, guard)
