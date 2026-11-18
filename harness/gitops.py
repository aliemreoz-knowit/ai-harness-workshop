"""Git plumbing for the harness: what changed, the diff, branch + commit.

The harness never pushes, never changes ``user.name`` / ``user.email`` and
never passes ``-c user.*``: commits use the identity configured in the repo.
"""

from __future__ import annotations

import re
import subprocess
from dataclasses import dataclass
from pathlib import Path


class GitError(RuntimeError):
    pass


def git(repo: Path, *args: str, check: bool = True, timeout: float = 60) -> str:
    try:
        proc = subprocess.run(["git", "-C", str(repo), *args], capture_output=True, text=True,
                              encoding="utf-8", errors="replace", timeout=timeout)
    except (OSError, subprocess.TimeoutExpired) as exc:
        raise GitError(f"git {' '.join(args)}: {exc}") from None
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)} failed ({proc.returncode}): {proc.stderr.strip()}")
    return proc.stdout


def is_repo(repo: Path) -> bool:
    try:
        return git(repo, "rev-parse", "--is-inside-work-tree").strip() == "true"
    except GitError:
        return False


def head(repo: Path) -> str:
    return git(repo, "rev-parse", "HEAD").strip()


def current_branch(repo: Path) -> str:
    return git(repo, "rev-parse", "--abbrev-ref", "HEAD").strip()


@dataclass
class Change:
    path: str
    status: str  # porcelain XY code, "??" for untracked
    orig: str = ""  # rename/copy: the source path

    @property
    def untracked(self) -> bool:
        return self.status == "??"

    @property
    def deleted(self) -> bool:
        return "D" in self.status


def changes(repo: Path) -> list[Change]:
    """Working-tree changes against HEAD, untracked files included, ignored files excluded."""
    out = git(repo, "status", "--porcelain=v1", "-z", "--untracked-files=all")
    items: list[Change] = []
    parts = out.split("\0")
    i = 0
    while i < len(parts):
        entry = parts[i]
        i += 1
        if len(entry) < 4:
            continue
        status, path = entry[:2], entry[3:]
        orig = ""
        if status[0] in "RC":  # rename/copy: the next field is the source path
            orig = parts[i].replace("\\", "/") if i < len(parts) else ""
            i += 1
        items.append(Change(path=path.replace("\\", "/"), status=status, orig=orig))
    return items


def changed_paths(repo: Path) -> list[str]:
    return [c.path for c in changes(repo)]


def diff(repo: Path, paths: list[str] | None = None, max_chars: int | None = None) -> str:
    """Unified diff of tracked changes plus untracked files shown as additions."""
    tracked = git(repo, "diff", "HEAD", "--no-color", "--", *(paths or []))
    extra = []
    for c in changes(repo):
        if not c.untracked or (paths and c.path not in paths):
            continue
        try:
            text = (repo / c.path).read_text(encoding="utf-8", errors="replace")
        except OSError:
            continue
        lines = text.splitlines()
        extra.append(f"diff --git a/{c.path} b/{c.path}\nnew file\n--- /dev/null\n+++ b/{c.path}\n"
                     f"@@ -0,0 +1,{len(lines)} @@\n" + "".join(f"+{ln}\n" for ln in lines))
    text = tracked + "".join(extra)
    if max_chars is not None and len(text) > max_chars:
        text = text[:max_chars] + f"\n... [diff truncated at {max_chars} chars]\n"
    return text


_HUNK = re.compile(r"^@@ -\d+(?:,\d+)? \+(\d+)(?:,\d+)? @@")


def added_lines(diff_text: str) -> dict[str, list[tuple[int, str]]]:
    """Per file, the added lines of a unified diff as (new line number, text)."""
    out: dict[str, list[tuple[int, str]]] = {}
    current: str | None = None
    lineno = 0
    for raw in diff_text.splitlines():
        if raw.startswith("+++ "):
            target = raw[4:].strip()
            current = None if target == "/dev/null" else target[2:] if target.startswith("b/") else target
            if current is not None:
                out.setdefault(current, [])
            continue
        if raw.startswith("--- ") or raw.startswith("diff --git"):
            continue
        m = _HUNK.match(raw)
        if m:
            lineno = int(m.group(1))
            continue
        if current is None:
            continue
        if raw.startswith("+"):
            out[current].append((lineno, raw[1:]))
            lineno += 1
        elif raw.startswith("-"):
            continue
        elif raw.startswith("\\"):
            continue
        else:
            lineno += 1
    return out


def branch_exists(repo: Path, branch: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{branch}", check=False).strip() != ""


def commit_on_branch(repo: Path, branch: str, message: str, paths: list[str]) -> tuple[str, str]:
    """Create ``branch`` at HEAD (suffix -2, -3 ... when taken), stage ``paths``, commit.

    Returns (branch, commit sha). Only the listed paths are staged.
    """
    if not paths:
        raise GitError("nothing to commit")
    name = branch
    n = 2
    while branch_exists(repo, name):
        name = f"{branch}-{n}"
        n += 1
    git(repo, "checkout", "-b", name)
    git(repo, "add", "--all", "--", *paths)
    return name, _commit(repo, message)


def _commit(repo: Path, message: str) -> str:
    # the message goes in on stdin (git commit -F -): no quoting issues on any shell
    proc = subprocess.run(["git", "-C", str(repo), "commit", "-q", "-F", "-"], input=message,
                          capture_output=True, text=True, encoding="utf-8", errors="replace")
    if proc.returncode != 0:
        raise GitError(f"git commit failed: {proc.stderr.strip() or proc.stdout.strip()}")
    return head(repo)
