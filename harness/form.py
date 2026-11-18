"""style and comments & docs: the form of the change, checked on the diff (pure Python, ~ms).

Both look only at what the change adds, so old code never fails them; new code has to
meet the house style. C files under src/, tools/, tests/ and fuzz/ only.

style (the house style of the existing code):
- no tab characters, no trailing whitespace on added lines,
- added lines at most ``MAX_COLS`` columns,
- a changed C file ends with a newline.

comments & docs:
- no TODO / FIXME / XXX in new comments: finish the work, or name what is left in the
  report (ticket ids may not appear in the tree, misra),
- a new function of ``LONG_FUNCTION`` lines or more has a comment directly above it
  that says what it does (``test_*`` functions are named for what they check: exempt),
- a new C file's header comment says what the file is for (one sentence besides the
  SPDX line and the ``tinystream v2 -- <path>`` line).
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

from . import gitops
from .config import GateDef
from .gates import FeedbackItem, GateContext, GateResult, excerpt

ROOTS = ("src/", "tools/", "tests/", "fuzz/")
MAX_COLS = 120
LONG_FUNCTION = 15        # body lines; shorter helpers explain themselves

_FUNC = re.compile(r"^(?!(?:if|for|while|switch|return|else|do|case|typedef|struct|enum|union)\b)"
                   r"[A-Za-z_][\w \t\*]*?\b([A-Za-z_]\w*)\s*\(")
_TODO = re.compile(r"\b(TODO|FIXME|XXX)\b")
_WORD = re.compile(r"[A-Za-z]{2,}")


@dataclass
class Change:
    """What the gates need from the diff: added lines per file, removed text, new files."""
    added: dict[str, list[tuple[int, str]]]
    removed: dict[str, list[str]] = field(default_factory=dict)
    new_files: set[str] = field(default_factory=set)


def in_scope(path: str) -> bool:
    return path.startswith(ROOTS) and path.endswith((".c", ".h"))


def removed_lines(diff_text: str) -> dict[str, list[str]]:
    out: dict[str, list[str]] = {}
    current: str | None = None
    for raw in diff_text.splitlines():
        if raw.startswith("--- "):
            src = raw[4:].strip()
            current = None if src == "/dev/null" else src[2:] if src.startswith("a/") else src
            continue
        if raw.startswith(("+++ ", "diff --git", "@@")):
            continue
        if current is not None and raw.startswith("-"):
            out.setdefault(current, []).append(raw[1:])
    return out


def from_diff(diff_text: str, new_files: set[str]) -> Change:
    return Change(gitops.added_lines(diff_text), removed_lines(diff_text), set(new_files))


# -- the rules (pure: a Change and a way to read a file) -------------------------------
Reader = Callable[[str], "str | None"]   # path -> the file's text (None: unreadable)


def style_findings(change: Change, read: Reader, gate: str = "style") -> list[FeedbackItem]:
    items: list[FeedbackItem] = []
    for path, lines in change.added.items():
        if not in_scope(path):
            continue
        for lineno, text in lines:
            text = text.rstrip("\r")
            if "\t" in text:
                items.append(FeedbackItem(gate, path, lineno, "tab", "tab character: indent with 4 spaces"))
            elif text != text.rstrip():
                items.append(FeedbackItem(gate, path, lineno, "trailing-space", "trailing whitespace"))
            if len(text) > MAX_COLS:
                items.append(FeedbackItem(gate, path, lineno, "line-length",
                                          f"line is {len(text)} columns, the limit is {MAX_COLS}"))
        text = read(path) if lines else None
        if text and not text.endswith("\n"):
            items.append(FeedbackItem(gate, path, len(text.splitlines()), "final-newline",
                                      "file does not end with a newline"))
    return items


def docs_findings(change: Change, read: Reader, gate: str = "comments") -> list[FeedbackItem]:
    items: list[FeedbackItem] = []
    for path, lines in change.added.items():
        if not in_scope(path):
            continue
        for lineno, text in lines:
            comment = _comment_part(text)
            m = _TODO.search(comment) if comment else None
            if m:
                items.append(FeedbackItem(gate, path, lineno, "todo",
                                          f"{m.group(1)} left in a comment: finish it, or name what is left "
                                          f"in your report"))
        file_lines = (read(path) or "").splitlines()
        if not file_lines:
            continue
        added_at = {n for n, _ in lines}
        removed = "\n".join(change.removed.get(path, []))
        for name, start, length in _functions(file_lines):
            if start not in added_at or length < LONG_FUNCTION or name == "main" or name.startswith("test_"):
                continue                      # a test's name is its description
            if re.search(r"\b" + re.escape(name) + r"\s*\(", removed):
                continue                      # an existing function whose signature changed
            if not _comment_above(file_lines, start):
                items.append(FeedbackItem(gate, path, start, "function-comment",
                                          f"new function {name}() ({length} lines) needs a comment above it "
                                          f"saying what it does"))
        if path in change.new_files and not _describes_itself(file_lines):
            items.append(FeedbackItem(gate, path, 1, "file-description",
                                      "new file: its header comment must say what the file is for"))
    return items


def _comment_part(text: str) -> str:
    s = re.sub(r'"(\\.|[^"\\])*"', '""', text)
    for mark in ("//", "/*"):
        i = s.find(mark)
        if i >= 0:
            return s[i:]
    return s if s.lstrip().startswith("*") else ""


def _functions(lines: list[str]) -> list[tuple[str, int, int]]:
    """(name, 1-based signature line, body lines) of each function definition at column 0."""
    out: list[tuple[str, int, int]] = []
    i = 0
    while i < len(lines):
        m = _FUNC.match(lines[i])
        if not m or lines[i].rstrip().endswith(";"):
            i += 1
            continue
        j = i
        while j < len(lines) and j - i < 8 and "{" not in lines[j] and ";" not in lines[j]:
            j += 1
        if j >= len(lines) or "{" not in lines[j] or ";" in lines[j].split("{")[0]:
            i += 1
            continue
        if "}" in lines[j].split("{", 1)[1]:          # a one-line function: no body lines
            out.append((m.group(1), i + 1, 0))
            i = j + 1
            continue
        k = j + 1
        while k < len(lines) and not lines[k].startswith("}"):
            k += 1
        out.append((m.group(1), i + 1, k - j - 1))
        i = k + 1
    return out


def _comment_above(lines: list[str], start: int) -> bool:
    if start < 2:
        return False
    above = lines[start - 2].strip()          # a blank line in between: not attached
    return above.endswith("*/") or above.startswith("//")


def _describes_itself(lines: list[str]) -> bool:
    head = lines[:15]
    for text in head:
        s = text.strip().lstrip("/*").rstrip("*/").strip()
        if not s or "SPDX-License-Identifier" in s or s.startswith("tinystream v2 --"):
            continue
        if text.lstrip().startswith(("/*", "*", "//")) and len(_WORD.findall(s)) >= 3:
            return True
        if not text.lstrip().startswith(("/*", "*", "//")):
            break                             # code started: the header is over
    return False


# -- the gates ------------------------------------------------------------------------
def _gather(ctx: GateContext) -> tuple[Change, Reader]:
    changes = [c for c in gitops.changes(ctx.repo) if c.path not in ctx.baseline and in_scope(c.path)]
    # a renamed file: with its source in the pathspec git shows the rename, not the whole file as added
    paths = [p for c in changes for p in (c.path, c.orig) if p]
    diff_text = gitops.diff(ctx.repo, paths) if changes else ""
    change = from_diff(diff_text, {c.path for c in changes if c.untracked or c.status[0] == "A"})
    return change, _reader(ctx.repo)


def _reader(repo: Path) -> Reader:
    def read(path: str) -> str | None:
        try:
            return (repo / path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            return None
    return read


def _result(gate: GateDef, items: list[FeedbackItem], repo: Path, what: str, n_files: int) -> GateResult:
    for it in items:
        if it.line and not it.excerpt:
            it.excerpt = excerpt(repo, it.file, it.line, 0)
    if items:
        return GateResult(gate.id, gate.name, "FAIL", reason=f"{len(items)} {what} finding(s)", items=items)
    return GateResult(gate.id, gate.name, "PASS", reason=f"{n_files} changed C file(s) checked")


def style(ctx: GateContext, gate: GateDef) -> GateResult:
    try:
        change, read = _gather(ctx)
    except gitops.GitError as exc:
        return GateResult(gate.id, gate.name, "ERROR", reason=f"git failed: {exc}")
    return _result(gate, style_findings(change, read, gate.id), ctx.repo, "style", len(change.added))


def docs(ctx: GateContext, gate: GateDef) -> GateResult:
    try:
        change, read = _gather(ctx)
    except gitops.GitError as exc:
        return GateResult(gate.id, gate.name, "ERROR", reason=f"git failed: {exc}")
    return _result(gate, docs_findings(change, read, gate.id), ctx.repo, "comment/doc", len(change.added))
