"""Prompt templates (``prompts/*.md``) rendered with ``string.Template``.

``$name`` / ``${name}`` placeholders; missing values render empty, literal
braces in code samples need no escaping. A line that starts with ``?{flag}`` is
kept (without the marker) only when ``flag`` is set for the session or step:
``?{run_gates}`` = the role has the run_gates tool, ``?{gates}`` = the step is
gated. A gate-less variant (raw) thus never reads about a tool it does not have. Role prompts (``prompts/roles``) and
``system.md`` are rendered once per session and never change during a run:
a stable prefix is what makes the prompt cache hit.
"""

from __future__ import annotations

import re
import string
from pathlib import Path
from typing import Any, Iterable


class Prompts:
    """``variant`` (e.g. "v2", experiment team-prompt-v2) overlays ``prompts/<variant>/``: a file there
    replaces the base template of the same name, every other template stays the base one."""

    def __init__(self, directory: Path, variant: str | None = None) -> None:
        self.dir = Path(directory)
        self.variant = variant if variant and variant != "v1" else None
        if self.variant and not (self.dir / self.variant).is_dir():
            raise FileNotFoundError(f"prompt template set {self.dir / self.variant} not found")

    def _read(self, rel: str) -> str:
        candidates = ([self.dir / self.variant / rel] if self.variant else []) + [self.dir / rel]
        for path in candidates:
            try:
                return path.read_text(encoding="utf-8")
            except OSError:
                continue
        raise FileNotFoundError(f"prompt template {candidates[-1]} not found") from None

    def source(self, name: str) -> str:
        """The raw template text (overlay first), "" when there is none."""
        try:
            return self._read(f"{name}.md")
        except FileNotFoundError:
            return ""

    def render(self, name: str, flags: Iterable[str] = (), **values: Any) -> str:
        return render_text(select_lines(self._read(f"{name}.md"), flags), **values)

    def role(self, role: str, flags: Iterable[str] = ()) -> str:
        return select_lines(self._read(f"roles/{role}.md"), flags).strip()

    def system(self, **values: Any) -> str:
        return render_text(self._read("system.md"), **values).strip()


_FLAG_LINE = re.compile(r"^\?\{([a-z_]+)\}\s?")


def select_lines(text: str, flags: Iterable[str]) -> str:
    """Keep a ``?{flag}`` line (marker removed) only when ``flag`` is in ``flags``; other lines stay."""
    on = set(flags)
    out = []
    for line in text.split("\n"):
        m = _FLAG_LINE.match(line)
        if m is None:
            out.append(line)
        elif m.group(1) in on:
            out.append(line[m.end():])
    return "\n".join(out)


def render_text(text: str, **values: Any) -> str:
    # one pass only: substituted values (ticket text!) are never parsed as templates
    mapping = _Empty({k: "" if v is None else str(v) for k, v in values.items()})
    out = string.Template(text).safe_substitute(mapping)
    lines = out.split("\n")
    # collapse runs of blank lines left by empty placeholders
    collapsed: list[str] = []
    for ln in lines:
        if not ln.strip() and collapsed and not collapsed[-1].strip():
            continue
        collapsed.append(ln.rstrip())
    return "\n".join(collapsed).strip() + "\n"


class _Empty(dict):
    def __missing__(self, key: str) -> str:
        return ""
