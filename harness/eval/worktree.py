"""git worktrees for eval runs: one per (variant, ticket, repeat), from the ticket's branch."""
from __future__ import annotations

import bisect
import fnmatch
import os
import shutil
import subprocess
import threading
from dataclasses import dataclass
from pathlib import Path
from typing import Iterator, Optional

# git worktree add/remove/prune touch shared files under .git (worktrees/, refs,
# packed-refs); concurrent calls race on their lock files, so they are serialised.
_GIT_LOCK = threading.Lock()


class GitError(RuntimeError):
    pass


class OverlayError(RuntimeError):
    pass


def git(cwd: Path | str, *args: str, check: bool = True) -> str:
    env = dict(os.environ, GIT_TERMINAL_PROMPT="0", LC_ALL="C")
    proc = subprocess.run(["git", *args], cwd=str(cwd), capture_output=True, text=True,
                          encoding="utf-8", errors="replace", env=env)
    if check and proc.returncode != 0:
        raise GitError(f"git {' '.join(args)}: {proc.stderr.strip() or proc.stdout.strip()}")
    return proc.stdout.strip()


def resolve_commit(repo: Path, ref: str) -> str:
    try:
        return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}")
    except GitError:
        raise GitError(f"ref {ref!r} not found in {repo}") from None


def ref_exists(repo: Path, ref: str) -> bool:
    return git(repo, "rev-parse", "--verify", "--quiet", f"{ref}^{{commit}}", check=False) != ""


def _registered(repo: Path) -> set[str]:
    out = git(repo, "worktree", "list", "--porcelain")
    return {os.path.normcase(os.path.realpath(line[len("worktree "):]))
            for line in out.splitlines() if line.startswith("worktree ")}


def create_worktree(repo: Path, path: Path, branch: str, base_ref: str) -> str:
    """Fresh worktree at `path` on `branch` reset to `base_ref`; returns the base commit."""
    base = resolve_commit(repo, base_ref)
    with _GIT_LOCK:
        if os.path.normcase(os.path.realpath(path)) in _registered(repo):
            git(repo, "worktree", "remove", "--force", str(path))
        elif path.exists() and any(path.iterdir()):
            raise GitError(f"{path} exists and is not a worktree of {repo}")
        git(repo, "worktree", "prune")
        path.parent.mkdir(parents=True, exist_ok=True)
        git(repo, "worktree", "add", "--quiet", "-B", branch, str(path), base)
    return base


# The harness structure of a workshop unit: what a variant's overlay_from copies into the eval worktree.
# `dir/**` = every file below dir, `dir/*.ext` = the files of dir itself, anything else = one file.
OVERLAY_PATHS = ("AGENTS.md", ".github/agents/**", ".github/skills/**", ".github/instructions/**",
                 ".github/copilot-instructions.md", ".github/hooks/*.json", "harness/harness.yaml",
                 "harness/gates.yaml", "harness/workflows/**", "harness/prompts/**")
# Of the files under those paths only text the harness reads is copied (the extension is matched without
# regard to case): a script, a binary or a file without extension would run on the eval host or in its
# gates. What is left out is named in the run record (the first OVERLAY_SKIPPED_MAX names, sorted)
# and counted there (all of them).
OVERLAY_EXTENSIONS = frozenset({".md", ".yaml", ".yml", ".json"})
OVERLAY_SKIPPED_MAX = 50
OVERLAY_MAX_FILES = 500                # an overlay is a handful of text files: anything bigger is refused
OVERLAY_MAX_BYTES = 5 * 1024 * 1024
_OVERLAY_BATCH = 200       # paths per `git add`, well below any command-line limit


def _unreadable(exc: OSError) -> None:
    raise OverlayError(f"cannot read the overlay source: {exc}")        # never a silent partial overlay


def _is_git_entry(name: str) -> bool:
    return name.lower() == ".git"


class SkippedNames(list):
    """What apply_overlay reports about the files it left out: the list holds their names (sorted,
    at most OVERLAY_SKIPPED_MAX per call) and ``total`` how many files it left out in all. Both add up
    when the same object is passed to several calls."""
    total: int = 0


class _FirstNames:
    """The ``limit`` smallest names added so far (sorted) and how many were added in all: memory stays
    at ``limit`` names, however many files the overlay source has. The overlay paths do not overlap
    and a walk yields a file once, so a name is added once."""

    def __init__(self, limit: int) -> None:
        self.limit = limit
        self.names: list[str] = []
        self.total = 0

    def add(self, name: str) -> None:
        self.total += 1
        if len(self.names) < self.limit or (self.names and name < self.names[-1]):
            bisect.insort(self.names, name)
            del self.names[self.limit:]


def _overlay_candidates(source: Path, pattern: str) -> Iterator[Path]:
    head_dir, _, tail = pattern.rpartition("/")
    base = source / head_dir if head_dir else source
    if tail == "**":
        if not base.is_dir():
            return
        for dirpath, dirs, names in os.walk(base, onerror=_unreadable):    # followlinks=False: linked dirs stay out
            dirs[:] = [d for d in dirs if not _is_git_entry(d)]            # a nested repository: not part of a harness
            for name in names:
                if not _is_git_entry(name):                                # nor a gitdir pointer file
                    yield Path(dirpath) / name
    elif any(c in tail for c in "*?["):
        try:
            names = sorted(os.listdir(base)) if base.is_dir() else []
        except OSError as exc:
            _unreadable(exc)
        for name in names:
            if fnmatch.fnmatchcase(name, tail):
                yield base / name
    else:
        yield source / pattern


def _vet(real_source: Path, path: Path, rel: str) -> Optional[int]:
    """The size of `path` when it is a regular file of the source with one hard link; None when it
    is to be skipped (a symlink, a directory, gone). A file that resolves outside the source or has
    several hard links (its content may be someone else's) is refused."""
    if os.path.islink(path) or not os.path.isfile(path):
        return None
    if not Path(os.path.realpath(path)).is_relative_to(real_source):
        raise OverlayError(f"overlay path escapes the source {real_source}: {rel}")
    st = os.stat(path)
    if st.st_nlink > 1:
        raise OverlayError(f"overlay file has {st.st_nlink} hard links, refused: {rel}")
    return st.st_size


def _overlay_files(source: Path) -> tuple[list[str], list[str], int]:
    """The files of `source` under OVERLAY_PATHS to copy, the first OVERLAY_SKIPPED_MAX of the ones to
    leave out because their extension is not in OVERLAY_EXTENSIONS, and how many files that is in all:
    paths as sorted posix paths relative to `source`. Only those names are held, not all of them.

    A symlink is never copied or entered, nor is anything named .git; a file that still resolves
    outside `source` (a linked parent directory, a junction) or has several hard links is refused,
    and so is an overlay of more than OVERLAY_MAX_FILES files or OVERLAY_MAX_BYTES bytes. A file left
    out is not looked at any further, so it counts for none of that."""
    real_source = Path(os.path.realpath(source))
    sizes: dict[str, int] = {}
    left_out = _FirstNames(OVERLAY_SKIPPED_MAX)
    for pattern in OVERLAY_PATHS:
        for path in _overlay_candidates(source, pattern):
            rel = path.relative_to(source).as_posix()
            if path.suffix.lower() not in OVERLAY_EXTENSIONS:
                if os.path.lexists(path):
                    left_out.add(rel)
                continue
            size = _vet(real_source, path, rel)
            if size is None:
                continue
            sizes[rel] = size
            if len(sizes) > OVERLAY_MAX_FILES:
                raise OverlayError(f"overlay has more than {OVERLAY_MAX_FILES} files")
            if sum(sizes.values()) > OVERLAY_MAX_BYTES:
                raise OverlayError(f"overlay is larger than the limit of {OVERLAY_MAX_BYTES} bytes")
    return sorted(sizes), left_out.names, left_out.total


def apply_overlay(workdir: Path, source: Path, skipped: Optional[list[str]] = None) -> Optional[str]:
    """Copy the harness structure of `source` (OVERLAY_PATHS) into the worktree and commit it.

    Only files with an extension of OVERLAY_EXTENSIONS are copied. The others under the overlay paths
    are left out; when `skipped` is given, their names (sorted, relative to `source`, the first
    OVERLAY_SKIPPED_MAX) are appended to it as soon as the scan is done, so also when nothing is
    copied; an overlay refused by the scan appends nothing. A `skipped` that is a SkippedNames also
    has the number of files left out in all added to its ``total`` (the names beyond the first
    OVERLAY_SKIPPED_MAX are counted, not kept); like the names, the total adds up over calls.

    Files the worktree lacks are added, files it has are replaced, nothing is deleted. The commit
    ("eval: harness overlay from <source name>", the repository's identity) leaves the tree clean,
    so a run that starts here sees only its own changes: when the worktree is not clean afterwards
    the overlay fails (OverlayError). Returns the commit sha, or None when the overlay changed
    nothing (no commit)."""
    source, workdir = Path(source), Path(workdir)
    if not source.is_dir():
        raise OverlayError(f"overlay source {source} is not a directory")
    files, left_out, left_out_total = _overlay_files(source)     # refusals come before the first copy
    if skipped is not None:
        skipped.extend(left_out)
        if isinstance(skipped, SkippedNames):
            skipped.total += left_out_total
    if not files:
        return None
    real_source = Path(os.path.realpath(source))
    for rel in files:
        if _vet(real_source, source / rel, rel) is None:        # the source is someone else's: look again
            raise OverlayError(f"overlay source changed while it was applied: {rel} is no longer a regular file")
        dest = workdir / rel
        dest.parent.mkdir(parents=True, exist_ok=True)
        shutil.copy(source / rel, dest)
    sha = None
    with _GIT_LOCK:
        for i in range(0, len(files), _OVERLAY_BATCH):
            git(workdir, "--literal-pathspecs", "add", "--force", "--", *files[i:i + _OVERLAY_BATCH])
        if git(workdir, "diff", "--cached", "--name-only"):
            name = Path(os.path.abspath(source)).name or "overlay"
            git(workdir, "commit", "-q", "-m", f"eval: harness overlay from {name}")
            sha = head(workdir)
        dirty = git(workdir, "status", "--porcelain").splitlines()
    if dirty:
        raise OverlayError(f"the worktree is not clean after the overlay: {'; '.join(dirty[:5])}")
    return sha


def remove_worktree(repo: Path, path: Path) -> None:
    with _GIT_LOCK:
        git(repo, "worktree", "remove", "--force", str(path), check=False)
        git(repo, "worktree", "prune", check=False)


def head(workdir: Path) -> str:
    return git(workdir, "rev-parse", "HEAD", check=False)


def commits_since(workdir: Path, base: str) -> int:
    out = git(workdir, "rev-list", "--count", f"{base}..HEAD", check=False)
    return int(out) if out.isdigit() else 0


@dataclass
class DiffStat:
    files: list[str]
    insertions: int
    deletions: int

    def to_dict(self) -> dict:
        return {"files": len(self.files), "insertions": self.insertions, "deletions": self.deletions,
                "lines": self.insertions + self.deletions}


def _numstat(out: str) -> tuple[list[str], int, int]:
    files, ins, dels = [], 0, 0
    for line in out.splitlines():
        parts = line.split("\t", 2)
        if len(parts) != 3:
            continue
        a, d, name = parts
        ins += int(a) if a.isdigit() else 0      # "-" for binary files
        dels += int(d) if d.isdigit() else 0
        files.append(name)
    return files, ins, dels


def diff_vs(workdir: Path, base: str) -> DiffStat:
    """Committed + uncommitted + untracked changes of the worktree against `base`."""
    files, ins, dels = _numstat(git(workdir, "diff", "--numstat", "--no-renames", base, check=False))
    untracked = git(workdir, "ls-files", "--others", "--exclude-standard", check=False).splitlines()
    for name in untracked:
        files.append(name)
        try:
            with open(Path(workdir) / name, "rb") as fh:
                ins += fh.read().count(b"\n")
        except OSError:
            pass
    return DiffStat(files=files, insertions=ins, deletions=dels)


# tags the ticket branches are built on, newest first by `git describe`
BASE_TAG_PATTERNS = ("v*-harness", "v*-golden")


def base_tag(repo: Path, ref: str, patterns: tuple[str, ...] = BASE_TAG_PATTERNS) -> Optional[str]:
    """The tag a ticket branch is built on: the nearest ``v*-harness`` (else ``v*-golden``) tag
    reachable from ``ref``, i.e. the golden tree the seed was cut from. None when there is none."""
    for pat in patterns:
        tag = git(repo, "describe", "--tags", "--abbrev=0", "--match", pat, ref, check=False)
        if tag and ref_exists(repo, tag):
            return tag
    return None


def diff_between(repo: Path, a: str, b: str) -> Optional[DiffStat]:
    if not (ref_exists(repo, a) and ref_exists(repo, b)):
        return None
    files, ins, dels = _numstat(git(repo, "diff", "--numstat", "--no-renames", a, b, check=False))
    return DiffStat(files=files, insertions=ins, deletions=dels)


def current_branch(workdir: Path) -> str:
    """The branch checked out in `workdir` ("" when detached)."""
    return git(workdir, "branch", "--show-current", check=False)


def patch_vs(workdir: Path, base: str) -> str:
    """The full patch of the worktree against `base`: commits, uncommitted and untracked files.

    Built with a throw-away index (GIT_INDEX_FILE), so the worktree's own index stays as it is."""
    workdir = Path(workdir)
    gitdir = Path(git(workdir, "rev-parse", "--absolute-git-dir"))
    index = gitdir / "eval-patch.index"
    env = dict(os.environ, GIT_INDEX_FILE=str(index), GIT_TERMINAL_PROMPT="0", LC_ALL="C")

    def run(*args: str) -> subprocess.CompletedProcess:
        return subprocess.run(["git", *args], cwd=str(workdir), capture_output=True, text=True,
                              encoding="utf-8", errors="replace", env=env)
    try:
        run("read-tree", "HEAD")
        run("add", "-A")
        out = run("diff", "--cached", "--no-renames", "--binary", base)
        return out.stdout if out.returncode == 0 else ""
    finally:
        try:
            index.unlink()
        except OSError:
            pass


def adopt_branch(workdir: Path, branch: str) -> str:
    """The run left `workdir` on another branch (the harness commit step makes agent/<key>-<id>):
    point `branch` at that commit and check it out again. Returns the other branch ("" if none)."""
    other = current_branch(workdir)
    if not other or other == branch:
        return ""
    with _GIT_LOCK:
        git(workdir, "checkout", "-q", "-B", branch)
    return other


def delete_branch(repo: Path, name: str, keep_tip_of: str) -> bool:
    """Delete branch `name` when its tip is also the tip of `keep_tip_of` (nothing is lost)."""
    tip = git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False)
    if not tip or tip != git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{keep_tip_of}", check=False):
        return False
    with _GIT_LOCK:
        git(repo, "branch", "-D", name, check=False)
    return git(repo, "rev-parse", "--verify", "--quiet", f"refs/heads/{name}", check=False) == ""
