"""compliance: the diff policy, checked on the working tree (pure Python, ~ms).

- every changed path is inside the union of the workflow roles' write scopes,
- no protected path changed (include/, src/pool/, .github/, scripts/, ...),
- added lines do not call forbidden functions (per path glob),
- new C sources carry the license header (``tinystream v2 -- <path>``) and the
  SPDX identifier (``/* SPDX-License-Identifier: MIT */``, policy ``license_spdx``),
- a PROTOCOL.md change comes with a ``PROTOCOL_VERSION`` bump.

The Jira key in the commit subject is enforced by the commit step itself.
"""

from __future__ import annotations

import re

from . import gitops
from .config import GateDef
from .gates import FeedbackItem, GateContext, GateResult, excerpt
from .policy import any_match, protected_hit

_COMMENT_LINE = re.compile(r"^\s*(//|/\*|\*)")


def check(ctx: GateContext, gate: GateDef) -> GateResult:
    repo = ctx.repo
    policy = ctx.policy
    items: list[FeedbackItem] = []
    try:
        changes = [c for c in gitops.changes(repo) if c.path not in ctx.baseline]
        diff_text = gitops.diff(repo, [c.path for c in changes]) if changes else ""
    except gitops.GitError as exc:
        return GateResult(gate.id, gate.name, "ERROR", reason=f"git failed: {exc}")
    for c in changes:
        hit = protected_hit(policy, c.path)
        if hit:
            items.append(FeedbackItem(gate.id, c.path, 0, "protected-path",
                                      f"{c.path} is protected ({hit}); revert this change"))
        elif not any_match(ctx.allowed_writes, c.path):
            items.append(FeedbackItem(gate.id, c.path, 0, "allowed-paths",
                                      f"{c.path} is outside the allowed paths ({', '.join(ctx.allowed_writes)})"))
    if ctx.expect_changes and not changes:
        items.append(FeedbackItem(gate.id, "", 0, "no-change", "the working tree has no change yet"))

    added = gitops.added_lines(diff_text)
    forbidden: dict[str, list[str]] = {str(k): [str(x) for x in v]
                                       for k, v in (policy.get("forbidden_calls") or {}).items()}
    for path, lines in added.items():
        if not path.endswith((".c", ".h")):
            continue
        names: set[str] = set()
        for glob, calls in forbidden.items():
            if any_match([glob], path):
                names.update(calls)
        if names:
            pat = re.compile(r"\b(" + "|".join(sorted(map(re.escape, names))) + r")\s*\(")
            for lineno, text in lines:
                if _COMMENT_LINE.match(text):
                    continue
                m = pat.search(_strip_strings(text))
                if m:
                    items.append(FeedbackItem(gate.id, path, lineno, f"forbidden-call:{m.group(1)}",
                                              f"{m.group(1)}() is not allowed in {path}",
                                              excerpt(repo, path, lineno, 0)))
    header = str(policy.get("license_header", "") or "")
    spdx = str(policy.get("license_spdx", "") or "")
    spdx_re = re.compile(r"SPDX-License-Identifier:\s*" + re.escape(spdx) + r"(?![A-Za-z0-9.+-])") if spdx else None
    for c in changes:
        if c.untracked and c.path.endswith((".c", ".h")) and (header or spdx_re):
            try:
                head = "\n".join((repo / c.path).read_text(encoding="utf-8", errors="replace").splitlines()[:6])
            except OSError:
                head = ""
            if header:
                want = header.format(path=c.path)
                if not head.lstrip().startswith("/*") or want not in head:
                    items.append(FeedbackItem(gate.id, c.path, 1, "license-header",
                                              f"new file must start with a block comment containing '{want}'"))
            if spdx_re and not spdx_re.search(head):
                items.append(FeedbackItem(gate.id, c.path, 1, "license-spdx",
                                          f"new file needs '/* SPDX-License-Identifier: {spdx} */' as its first line"))
    if any(c.path == "PROTOCOL.md" for c in changes):
        proto_added = [t for _, t in added.get("PROTOCOL.md", [])]
        if not any(t.startswith("PROTOCOL_VERSION:") for t in proto_added):
            items.append(FeedbackItem(gate.id, "PROTOCOL.md", 3, "protocol-version",
                                      "PROTOCOL.md changed without a PROTOCOL_VERSION bump (and the matching "
                                      "TS_PROTOCOL_VERSION in include/ts_config.h)"))
    if items:
        return GateResult(gate.id, gate.name, "FAIL", reason=f"{len(items)} compliance finding(s)", items=items)
    return GateResult(gate.id, gate.name, "PASS", reason=f"{len(changes)} changed path(s) compliant")


def _strip_strings(text: str) -> str:
    text = re.sub(r'"(\\.|[^"\\])*"', '""', text)
    return re.sub(r"//.*$", "", text)
