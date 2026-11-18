"""The worktree after a run: what the run left (branch, commit, a patch), then back on the unit's branch, clean.

``wrap_up`` is what a run's finalize does with the unit worktree, under ``files.worktree_lock``:

1. A stale ``index.lock`` in the worktree's git folder (a killed git) is removed, only when the caller says that no
   process of the run is alive any more.
2. The branch and commit HEAD is on, when it is not the unit's branch.
3. A patch of everything uncommitted (staged, unstaged, untracked), best effort: if it cannot be made, the problem is
   reported and the restore still runs.
4. The unit's branch back on the commit it had when the run started (``base``), if the run moved it (an agent's
   ``git commit`` while HEAD was on it): ``update-ref`` with the moved commit as the expected old value. The moved
   commit is reported as the run's commit.
5. The restore, always: link entries removed, ``checkout -f <unit branch>``, ``clean -ffd`` (nested repositories
   too; never ``-x``: ignored files such as run folders and builds stay), then a check that HEAD is on the branch and
   nothing is left. ``restored`` is False when step 4 or 5 failed: the caller must then not hand the unit back.

Links: an agent can leave a symlink, a junction or a hard link in the worktree, and git follows a junction on Windows
as if it were a folder (``git status`` lists the files behind it; a tracked folder swapped for one makes ``checkout``
fail). A changed path that is a link, goes through one, is a file with more than one name, cannot be looked at, or is
a nested repository stays out of the patch (logged by its relative path). Symlink and junction entries are removed
before the checkout; what they lead to is never touched.

``open_plain`` and ``read_plain`` read a file of a run folder in the worktree (``events.jsonl``, ``summary.json``) only
when every folder on the way is a plain folder and the file a plain file with one name, checked on the open handle.

``git`` is ``UnitStore.git_for(unit)``, looked up before the caller takes the worktree lock.
"""

from __future__ import annotations

import logging
import os
import re
import stat
from dataclasses import dataclass, field
from pathlib import Path
from typing import BinaryIO, Callable, Sequence

__all__ = [
    "NotPlain", "WrapUp", "drop_stale_index_lock", "head_branch", "open_plain", "read_plain", "restore_to", "wrap_up",
]

log = logging.getLogger(__name__)

Git = Callable[..., str]

_REPARSE = getattr(stat, "FILE_ATTRIBUTE_REPARSE_POINT", 0x400)
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")


class NotPlain(PermissionError):
    """A file that is not a plain file reached through plain folders (a link, a hard link, a folder ...)."""


@dataclass
class WrapUp:
    """What ``wrap_up`` found and did. ``problems`` are plain sentences for the run record."""

    branch: str | None = None
    commit: str | None = None
    patch: str | None = None
    restored: bool = False
    problems: list[str] = field(default_factory=list)


# --- links ------------------------------------------------------------------------------------------------------


def _is_link(info: os.stat_result) -> bool:
    return stat.S_ISLNK(info.st_mode) or bool(getattr(info, "st_file_attributes", 0) & _REPARSE)


def _link_on(root: Path, rel: str) -> tuple[Path, bool] | None:
    """Where ``rel`` is, or goes through, a link: ``(entry, removable)``; ``None`` for a plain path or a deleted one.

    A symlink or junction (any reparse point) on the way is removable; a file with more than one name at the end and
    an entry that cannot be looked at are not: they are only left out.
    """
    parts = [part for part in rel.split("/") if part]
    here = Path(root)
    for n, part in enumerate(parts):
        here = here / part
        try:
            info = os.lstat(here)
        except FileNotFoundError:
            return None  # deleted: nothing to follow
        except OSError:
            return here, False
        if _is_link(info):
            return here, True
        if n == len(parts) - 1 and stat.S_ISREG(info.st_mode) and info.st_nlink > 1:
            return here, False
    return None


def _remove_link(path: Path) -> None:
    """Remove the link entry itself (never what it leads to); nothing when it is no longer a link."""
    try:
        if not _is_link(os.lstat(path)):
            return
    except OSError:
        return
    try:
        os.unlink(path)
    except OSError:
        os.rmdir(path)  # a directory link or junction on Windows


def _changed_paths(porcelain: str) -> list[str]:
    """Paths in ``git status --porcelain=v1 -z`` output; a rename or copy gives both of its paths."""
    items, paths, i = porcelain.split("\0"), [], 0
    while i < len(items):
        entry = items[i]
        i += 1
        if len(entry) < 4:
            continue
        paths.append(entry[3:])
        if entry[0] in "RC" and i < len(items):
            paths.append(items[i])
            i += 1
    return list(dict.fromkeys(path for path in paths if path))


def _status(git: Git) -> list[str]:
    return _changed_paths(git("status", "--porcelain=v1", "-z", "--untracked-files=all"))


def _sort_out(root: Path, changed: Sequence[str]) -> tuple[list[str], list[str], list[Path]]:
    """Of the changed paths: those a patch takes, the entries it leaves out (relative), the link entries to remove."""
    taken, left_out, links = [], [], []
    for rel in changed:
        found = _link_on(root, rel)
        if found is None and rel.endswith("/"):  # with --untracked-files=all only a nested repository is a folder
            left_out.append(rel.rstrip("/"))
            continue
        if found is None:
            taken.append(rel)
            continue
        entry, removable = found
        left_out.append(entry.relative_to(root).as_posix())
        if removable:
            links.append(entry)
    return taken, list(dict.fromkeys(left_out)), list(dict.fromkeys(links))


# --- the steps --------------------------------------------------------------------------------------------------


def head_branch(git: Git) -> str | None:
    """The branch HEAD is on; ``None`` when it is detached (or git cannot say)."""
    try:
        return git("symbolic-ref", "-q", "--short", "HEAD").strip() or None
    except RuntimeError:
        return None


def drop_stale_index_lock(git: Git) -> None:
    """Remove ``index.lock`` from the worktree's git folder (outside the worktree): a git that was killed left it.

    Only for a caller that knows no git of the worktree runs any more: removing a live git's lock breaks its index.
    """
    try:
        lock = Path(git("rev-parse", "--absolute-git-dir").strip()) / "index.lock"
        if stat.S_ISREG(os.lstat(lock).st_mode):
            lock.unlink()
            log.warning("removed a stale %s", lock.name)
    except FileNotFoundError:
        pass
    except (RuntimeError, OSError):
        log.warning("a stale index.lock could not be looked at or removed", exc_info=True)


def _save_patch(git: Git, folder: Path, left_out: Sequence[str]) -> str | None:
    """Stage everything but ``left_out``, write the staged diff to ``folder/console-diff.patch``, unstage.

    ``add -A`` on the whole tree takes deletions that are staged already (``git rm``) and renames as they are; the
    paths left out are excluded literally. The pathspecs go through a file: no command line limit, no quoting.
    """
    spec, patch = folder / "console-paths.txt", folder / "console-diff.patch"
    specs = [".", *(f":(exclude,literal){rel}" for rel in left_out)]
    spec.write_bytes(b"\0".join(item.encode("utf-8") for item in specs))
    try:
        git("add", "-A", f"--pathspec-from-file={spec}", "--pathspec-file-nul")
        git("diff", "--cached", "--binary", "--no-color", "--no-ext-diff", "--no-textconv", f"--output={patch}")
    finally:
        spec.unlink(missing_ok=True)
        git("reset", "-q")
    if patch.is_file() and patch.stat().st_size:
        return str(patch)
    patch.unlink(missing_ok=True)
    return None


def restore_to(git: Git, root: Path, branch: str) -> None:
    """Put the worktree on ``branch``, clean, and check that it is (step 5 of ``wrap_up``; also an admin's restore or
    reset): link entries out, ``checkout -f <branch> --`` (the ``--``: a missing branch is an error, never a path to
    check out), ``clean -ffd``; ``RuntimeError`` unless HEAD is then on ``branch`` with nothing left in
    ``status --porcelain``. Ignored files stay."""
    try:
        links = _sort_out(root, _status(git))[2]
    except RuntimeError:
        links = []  # status failed: the checkout below says whether the tree can be put back
    for link in links:
        _remove_link(link)
    git("checkout", "-q", "-f", branch, "--")
    git("clean", "-ffdq")  # -ff: nested repositories too; never -x: ignored files (run folders, builds) stay
    left = git("status", "--porcelain=v1", "--untracked-files=all").strip()
    if head_branch(git) != branch or left:
        raise RuntimeError(f"the worktree is not clean on {branch} after the checkout: {left[:200]}")


def _put_branch_back(git: Git, branch: str, base: str) -> str | None:
    """``branch`` back on ``base`` if it moved; returns the commit it had moved to, else ``None``; ``RuntimeError``."""
    now = git("rev-parse", "--verify", "-q", f"refs/heads/{branch}").strip()
    if now == base:
        return None
    git("update-ref", "-m", "console: back where the run started", f"refs/heads/{branch}", base, now)
    log.warning("%s had moved during the run (to %s): put back on %s", branch, now, base)
    return now


def wrap_up(
    git: Git, root: Path, branch: str, folder: Path, *, index_free: bool, base: str | None = None
) -> WrapUp:
    """Record what the run left and put the worktree back on ``branch`` (see the module doc). Never raises.

    ``folder`` is the run's console folder (outside the worktree) for the patch. ``index_free``: no process of the run
    is alive, so an ``index.lock`` is stale. ``base``: the commit ``branch`` had when the run started.
    """
    out = WrapUp()
    root = Path(root)
    if index_free:
        drop_stale_index_lock(git)
    head = head_branch(git)
    if head != branch:
        out.branch = head
        try:
            out.commit = git("rev-parse", "--verify", "HEAD").strip()
        except RuntimeError:
            log.warning("the run's HEAD could not be read", exc_info=True)
    try:
        taken, left_out, _ = _sort_out(root, _status(git))
        for rel in left_out:
            log.warning("%s left out of the patch: a link, a file with more than one name or a nested repository", rel)
        if taken:
            folder.mkdir(parents=True, exist_ok=True)
            out.patch = _save_patch(git, folder, left_out)
    except (RuntimeError, OSError):
        log.warning("the run's uncommitted changes could not be saved", exc_info=True)
        out.problems.append("what the run left uncommitted could not be saved as a patch")
    branch_back = True
    if base is not None:
        try:
            if not _SHA.fullmatch(base):
                raise RuntimeError(f"not a commit id: {base!r}")
            moved = _put_branch_back(git, branch, base)
            out.commit = out.commit or moved
        except RuntimeError:
            log.error("%s could not be put back where the run started", branch, exc_info=True)
            out.problems.append(f"{branch} moved during the run and could not be put back")
            branch_back = False
    try:
        restore_to(git, root, branch)
        out.restored = branch_back
    except (RuntimeError, OSError):
        log.error("the worktree could not be put back on %s", branch, exc_info=True)
        out.problems.append(f"the worktree could not be put back on {branch}: it needs a reset by the facilitator")
    return out


# --- reading a run folder in the worktree ---------------------------------------------------------------------


def open_plain(root: Path, parts: Sequence[str]) -> BinaryIO:
    """Open ``root/parts...`` for reading, binary. ``FileNotFoundError`` when it is not there; ``NotPlain`` unless every
    folder on the way is a plain folder (no link, no junction) and the file a plain file with one name.

    The open handle is checked against what was looked at (same device and file id), so a swap in between is caught.
    """
    here = Path(root)
    for part in parts[:-1]:
        here = here / part
        info = os.lstat(here)
        if _is_link(info) or not stat.S_ISDIR(info.st_mode):
            raise NotPlain(f"{'/'.join(parts)}: {part} is not a plain folder")
    leaf = here / parts[-1]
    info = os.lstat(leaf)
    if _is_link(info) or not stat.S_ISREG(info.st_mode) or info.st_nlink != 1:
        raise NotPlain(f"{'/'.join(parts)} is not a plain file with one name")
    fh = open(leaf, "rb")
    try:
        got = os.fstat(fh.fileno())
        if (got.st_dev, got.st_ino) != (info.st_dev, info.st_ino) or got.st_nlink != 1:
            raise NotPlain(f"{'/'.join(parts)} changed while it was opened")
    except BaseException:
        fh.close()
        raise
    return fh


def read_plain(root: Path, parts: Sequence[str], limit: int) -> bytes | None:
    """The bytes of ``open_plain(root, parts)``; ``None`` when it is missing, not plain, or larger than ``limit``."""
    try:
        with open_plain(root, parts) as fh:
            data = fh.read(limit + 1)
    except FileNotFoundError:
        return None
    except OSError as err:
        log.warning("%s not read: %s", "/".join(parts), err)
        return None
    if len(data) > limit:
        log.warning("%s not read: larger than %d bytes", "/".join(parts), limit)
        return None
    return data
