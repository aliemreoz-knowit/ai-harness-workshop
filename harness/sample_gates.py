"""Extra Python gate (sample of a block-2 exercise): diff-size, the size of the change.

A small change is easier to review and less likely to hide an unrelated edit, so
the ladder fails a change whose production diff (everything outside ``tests/``
and the test manifest) exceeds a line budget. The budget comes from the gate's
``env`` in gates.yaml (``MAX_DIFF_LINES``, default 150); the structured feedback
names the biggest files first, so the implementer knows where to cut.
"""

from __future__ import annotations

from . import gitops
from .config import GateDef
from .gates import FeedbackItem, GateContext, GateResult

DEFAULT_MAX_LINES = 150
EXEMPT_PREFIXES = ("tests/",)
EXEMPT_FILES = ("scripts/test_manifest.txt",)


def _counted(path: str) -> bool:
    return not (path.startswith(EXEMPT_PREFIXES) or path in EXEMPT_FILES)


def changed_lines(diff_text: str) -> dict[str, int]:
    """Per file, added + removed lines of a unified diff (headers not counted)."""
    out: dict[str, int] = {}
    current: str | None = None
    for line in diff_text.splitlines():
        if line.startswith("diff --git "):
            current = line.split(" b/", 1)[-1] if " b/" in line else None
            if current is not None:
                out.setdefault(current, 0)
            continue
        if current is None or line.startswith(("+++", "---")):
            continue
        if line.startswith(("+", "-")):
            out[current] += 1
    return out


def diff_size(ctx: GateContext, gate: GateDef) -> GateResult:
    try:
        limit = int(gate.env.get("MAX_DIFF_LINES", DEFAULT_MAX_LINES))
    except ValueError:
        return GateResult(gate.id, gate.name, "ERROR", reason="MAX_DIFF_LINES is not an integer")
    try:
        paths = [c.path for c in gitops.changes(ctx.repo) if c.path not in ctx.baseline]
        diff_text = gitops.diff(ctx.repo, paths) if paths else ""
    except gitops.GitError as exc:
        return GateResult(gate.id, gate.name, "ERROR", reason=f"git failed: {exc}")
    sizes = {p: n for p, n in changed_lines(diff_text).items() if _counted(p)}
    total = sum(sizes.values())
    if total <= limit:
        return GateResult(gate.id, gate.name, "PASS", reason=f"{total} of {limit} changed lines")
    items = [FeedbackItem(gate.id, path, 0, "diff-size",
                          f"{path}: {n} changed lines; the whole change is {total}, the budget {limit}. "
                          "Keep the fix minimal and leave unrelated edits out.")
             for path, n in sorted(sizes.items(), key=lambda kv: (-kv[1], kv[0]))]
    return GateResult(gate.id, gate.name, "FAIL", reason=f"{total} changed lines > {limit}", items=items)
