"""Units: one git worktree and branch per workshop team, built from a scrubbed participant export.

Layout under ``cfg.data_dir``::

    source/                  clone of ``cfg.source_repo``; local branches for start_ref and the catch-up refs
    units/unit-NN/repo/      worktree of the clone on branch ``unit/NN``, started at ``start_ref``
    units.json               salt, admin code hash and one record per unit (written by atomic replace)

Rules this module keeps:

* Before anything is built, both the export and the clone go through :func:`check_source`; a ref or
  directory that could carry hidden material (ticket branches, golden tags, ``hidden-tests``) stops the build.
* Unit codes are never stored: only ``sha256(salt + code)``. ``login`` checks the code's shape before it
  hashes anything and compares every stored hash with ``hmac.compare_digest``.
* One ``threading.Lock`` per data dir (shared by every ``UnitStore`` on it, however many are built) guards
  every read-modify-write of ``units.json``. A lock held by a run (``lock``/``unlock``) is state in that
  file, so it is visible to every ``UnitStore`` on the data dir.
* ``reset`` also holds ``console.files.worktree_lock`` for the unit, the lock every editor save takes, so a save
  never lands between the git calls of a reset. Lock order: the store lock, then the worktree lock.
* The clone is made with ``--no-local``: only objects reachable from the export's refs are copied, never the
  unreachable ones a deleted branch leaves behind in its object store.
* Git is never pointed at a repo by a ``.git`` file inside a worktree (an agent can rewrite that file). A call
  on a unit worktree names its admin dir and work tree (``console.files.git_location``: the admin dir is found
  from the ``gitdir`` files under ``source/.git/worktrees/``, outside the worktree); a call on the clone or the
  export names its ``.git`` (``--git-dir``). So the config and hooks git uses are the clone's.
* Every git call gets ``core.hooksPath`` = ``<data>/no-hooks`` (an empty folder) and ``core.fsmonitor=false`` on its
  command line, so no hook or fsmonitor program runs whatever a repo's config says, and the allowlisted environment
  of ``procs.run_env`` (no tokens, no ``GIT_*`` variables).
* A unit whose worktree a run could not put back stays locked as ``needs-reset:<run id>`` (``hold_for_reset``);
  ``reset`` (to a ref, the old work on backup branches) or ``restore`` (back on its own branch, every save kept) is
  what clears it.
* Nothing outside ``cfg.data_dir`` is ever deleted, and git is always run without a shell.

Personal mode (``cfg.personal``): the source is the participant's own checkout, the data dir is inside it, and there
is one unit, ``PERSONAL_UNIT``, which ``init_personal`` builds on the first start and keeps from then on. The guards
above are the same, but for the referee: the workshop repository carries ``hidden-tests/`` and ``ticket/*`` on purpose
(``check_source(referee=True)``), and the unit's copy is checked out without ``hidden-tests/`` (``_hide_referee``).
The clone gets the workshop's branches as local branches (``_workshop_branches``, again on every start) and starts
on ``ticket/TS-104`` when the checkout has it, as the stage's copy does (``_start_where_the_stage_does``).
A golden tag is still refused. There is no Admin page
to give credits, so the budget is per local calendar day (``_per_day``): ``spent`` counts the day in ``spent_day``, a
record of another day starts today at 0, and ``budget`` is always ``cfg.unit_budget`` (``serve --personal --budget``).
"""

from __future__ import annotations

import contextlib
import fnmatch
import functools
import hmac
import json
import logging
import math
import os
import re
import secrets
import shutil
import stat
import subprocess
import tempfile
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Callable, Sequence

from console import procs, worktree_ops
from console.auth import ALPHABET, hash_code, new_code
from console.config import ConsoleConfig
from console.files import git_location, worktree_lock

__all__ = ["NEEDS_RESET", "PERSONAL_UNIT", "Busy", "LeakError", "Unit", "UnitStore", "check_source"]

log = logging.getLogger(__name__)

_CODE_LENGTH = 6
_MAX_UNITS = 99
_MUTABLE_FIELDS = frozenset({"name", "code_hash", "budget", "spent", "locked_by", "level"})
NEEDS_RESET = "needs-reset:"  # locked_by prefix of a unit whose worktree a run could not put back
PERSONAL_UNIT = "unit-01"  # personal mode's one unit: every request from the participant's computer is this unit
# Personal mode: the workshop's branches the clone gets as local branches (an experiment starts every run on the
# ticket's branch, the release run is on after-section-5), and the one the copy starts on, as the stage's does.
_WORKSHOP_BRANCH = re.compile(r"ticket/TS-\d+|after-section-[1-5]")
_PERSONAL_START = "ticket/TS-104"
_DATA_IGNORE = "# The console's data (personal mode). Git leaves this folder out of the checkout.\n*\n"
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")  # a full commit id
_FAILED_RESET = "failed-reset"  # after NEEDS_RESET: a free unit whose reset could not put the worktree back

# Windows refuses os.replace while another process has the target open: try again for about 2.5 s.
_REPLACE_TRIES = 50
_REPLACE_WAIT_S = 0.05

_store_locks: dict[str, threading.Lock] = {}  # one per units.json, so any number of UnitStores agree
_store_locks_guard = threading.Lock()


def _lock_for(units_file: Path) -> threading.Lock:
    key = os.path.normcase(os.path.realpath(units_file))
    with _store_locks_guard:
        return _store_locks.setdefault(key, threading.Lock())


class LeakError(RuntimeError):
    """The source repo holds something participants must never get."""


class Busy(RuntimeError):
    """The unit is locked by a run."""


@dataclass(frozen=True)
class Unit:
    id: str
    name: str
    branch: str
    worktree: Path
    code_hash: str
    budget: float
    spent: float
    locked_by: str | None
    level: int

    @property
    def left(self) -> float:
        """Credits not spent yet (negative when the unit is over budget)."""
        return self.budget - self.spent


# --- git ---------------------------------------------------------------------------------------


def _repo(path: Path) -> list[str]:
    """Git options for a repo with its own ``.git`` (the export, the clone): named, never looked for."""
    return ["-C", str(path), f"--git-dir={Path(path) / '.git'}"]


def _argv(where: Path | Sequence[str], hooks: str) -> list[str]:
    """``git`` with no hooks (``core.hooksPath`` is ``hooks``, an empty folder of the console's), no fsmonitor, and
    ``where``: git options (``_repo``, ``git_location``), or a plain folder (``-C``)."""
    location = ["-C", str(where)] if isinstance(where, (str, os.PathLike)) else list(where)
    return ["git", "-c", f"core.hooksPath={hooks}", "-c", "core.fsmonitor=false", *location]


def _run(where: Path | Sequence[str], args: Sequence[str], hooks: str) -> subprocess.CompletedProcess[str]:
    """Run git ``args`` at ``where`` (see ``_argv``); the output as text.

    Whatever a repo's config says, no hook runs and no fsmonitor; the environment is the one of ``procs.git_env``
    (no tokens, no ``GIT_*`` of the console's, replace refs ignored). A call that takes longer than
    ``procs.GIT_TIMEOUT_S`` is killed: ``RuntimeError("git timed out: <subcommand>")``.
    """
    return procs.run_git(_argv(where, hooks), args, text=True)


def _git(where: Path | Sequence[str], *args: str, hooks: str) -> str:
    """Stdout of git ``args`` at ``where`` (see ``_run``); ``RuntimeError`` with git's stderr when it fails."""
    done = _run(where, args, hooks)
    if done.returncode != 0:
        raise RuntimeError(done.stderr.strip() or f"git {args[0]} failed (exit {done.returncode})")
    return done.stdout


def _has_ref(where: Sequence[str], ref: str, hooks: str) -> bool:
    return _run(where, ["show-ref", "--verify", "--quiet", ref], hooks).returncode == 0


def _commit_of(where: Sequence[str], rev: str, hooks: str) -> str | None:
    """The commit ``rev`` names; ``None`` when there is none (a branch that is gone)."""
    done = _run(where, ["rev-parse", "--verify", "-q", f"{rev}^{{commit}}"], hooks)
    return (done.stdout.strip() or None) if done.returncode == 0 else None


# --- leak guard --------------------------------------------------------------------------------


def check_source(repo: Path, hooks_dir: Path | None = None, referee: bool = False) -> None:
    """Raise :class:`LeakError` naming every finding that makes ``repo`` unfit to hand to participants.

    Findings: a branch or remote ref ``ticket/*`` (or ``seed/*``, its old name), a tag ``*-golden`` (names compared ignoring case), and a
    ``hidden-tests`` directory at the top level of the working tree or of any branch, remote ref or tag.
    ``referee``: the workshop repository carries its referee on purpose (``hidden-tests/`` and the ``ticket/*``
    branches its experiments start from), so only a golden tag is a finding; personal mode passes it.
    ``hooks_dir`` is the empty folder git gets as ``core.hooksPath`` (default: a new temporary one).
    """
    repo = Path(repo)
    if not repo.is_dir():
        raise RuntimeError(f"source repo not found at {repo}")
    with contextlib.ExitStack() as stack:
        hooks = str(hooks_dir) if hooks_dir is not None else stack.enter_context(tempfile.TemporaryDirectory())
        _check_source(repo, hooks, referee)


def _check_source(repo: Path, hooks: str, referee: bool = False) -> None:
    findings: list[str] = []
    if not referee and any(p.name.lower() == "hidden-tests" for p in repo.iterdir()):
        findings.append("hidden-tests/ in the working tree")

    has_hidden: dict[str, bool] = {}  # per tree-ish: many refs share one commit
    refs = _git(_repo(repo), "for-each-ref", "--format=%(refname)%09%(objectname)%09%(*objectname)", hooks=hooks)
    for line in refs.splitlines():
        ref, obj, peeled = line.split("\t")
        if ref.startswith("refs/heads/"):
            kind, shown = "branch", ref[len("refs/heads/"):]
            name = shown
        elif ref.startswith("refs/remotes/") and "/" in ref[len("refs/remotes/"):]:
            kind, shown = "remote ref", ref[len("refs/remotes/"):]
            name = shown.split("/", 1)[1]
            if name == "HEAD":
                continue
        elif ref.startswith("refs/tags/"):
            kind, shown = "tag", ref[len("refs/tags/"):]
            name = shown
        else:
            continue
        lowered = name.lower()
        if kind != "tag" and not referee and any(fnmatch.fnmatchcase(lowered, p) for p in ("ticket/*", "seed/*")):
            findings.append(f"{kind} {shown}")
        if kind == "tag" and fnmatch.fnmatchcase(lowered, "*-golden"):
            findings.append(f"tag {shown}")
        target = peeled or obj
        if referee:
            continue
        if target not in has_hidden:
            top = _run(_repo(repo), ["ls-tree", "--name-only", target], hooks)
            has_hidden[target] = top.returncode == 0 and any(
                n.lower() == "hidden-tests" for n in top.stdout.splitlines()
            )
        if has_hidden[target]:
            findings.append(f"hidden-tests/ at the top level of {kind} {shown}")
    if findings:
        raise LeakError(f"{repo} is not a clean participant export: " + "; ".join(findings))


# --- the store ---------------------------------------------------------------------------------


class UnitStore:
    """All units of one console: ``units.json`` plus their worktrees. Safe to share between threads."""

    def __init__(self, cfg: ConsoleConfig) -> None:
        self._cfg = cfg
        self._file = cfg.data_dir / "units.json"
        self._clone = cfg.data_dir / "source"
        self._units_dir = cfg.data_dir / "units"
        self._lock = _lock_for(self._file)

    def _no_hooks(self) -> str:
        """The folder every git call gets as ``core.hooksPath``: empty, in the data dir, out of every agent's reach."""
        path = self._cfg.data_dir / "no-hooks"
        path.mkdir(parents=True, exist_ok=True)
        with os.scandir(path) as entries:
            if any(True for _ in entries):
                raise RuntimeError(f"{path} must be empty: git takes its hooks from there")
        return str(path)

    # -- setup --

    def init(self, count: int, force: bool = False) -> dict[str, str]:
        """Build ``count`` units; returns each unit's code (``unit-01`` ... ) and the admin code, last.

        The codes exist only in this return value. Refuses when units already exist unless ``force``,
        which first deletes the old worktrees and clone (inside the data dir only). A failed build
        removes what it made.
        """
        if isinstance(count, bool) or not isinstance(count, int) or not 1 <= count <= _MAX_UNITS:
            raise ValueError(f"count must be a whole number from 1 to {_MAX_UNITS}, got {count!r}")
        with self._lock:
            if force:
                self._wipe()
            elif self._file.exists():
                raise RuntimeError(
                    f"{self._file} already exists: units are set up. Use force to rebuild them "
                    "(this discards every unit's work)"
                )
            elif self._clone.exists() or self._units_dir.exists():
                raise RuntimeError(f"leftovers of an earlier setup in {self._cfg.data_dir}: use force to rebuild")
            try:
                return self._build(count)
            except BaseException:
                with contextlib.suppress(Exception):
                    self._wipe()
                raise

    def _build(self, count: int) -> dict[str, str]:
        cfg = self._cfg
        hooks = self._no_hooks()
        check_source(cfg.source_repo, Path(hooks), referee=cfg.personal)
        _git(cfg.data_dir, "clone", "-q", "--no-local", "--", str(cfg.source_repo), str(self._clone), hooks=hooks)
        self._copy_identity()
        self._make_local_branches()
        if cfg.personal:
            self._workshop_branches(hooks)
            self._start_where_the_stage_does(hooks)
        check_source(self._clone, Path(hooks), referee=cfg.personal)

        salt = secrets.token_hex(16)
        codes = _distinct_codes(count + 1)
        flags = self._worktree_flags(hooks)
        records = []
        for n in range(1, count + 1):
            unit_id, branch = f"unit-{n:02d}", f"unit/{n:02d}"
            worktree = self._units_dir / unit_id / "repo"
            start = f"refs/heads/{cfg.start_ref}"
            _git(
                _repo(self._clone), "worktree", "add", "-q", *flags, "-b", branch, str(worktree), start, hooks=hooks
            )
            self._hide_referee(worktree, hooks)
            records.append(
                {
                    "id": unit_id,
                    "name": f"Unit {n:02d}",
                    "branch": branch,
                    "worktree": str(worktree),
                    "code_hash": hash_code(codes[n - 1], salt),
                    "budget": cfg.unit_budget,
                    "spent": 0.0,
                    "locked_by": None,
                    "level": 0,
                }
            )
        self._save({"salt": salt, "admin_code_hash": hash_code(codes[-1], salt), "units": records})
        return {**{r["id"]: codes[i] for i, r in enumerate(records)}, "admin": codes[-1]}

    def init_personal(self) -> bool:
        """Personal mode: build ``PERSONAL_UNIT`` from the checkout unless it is there; True when it was built now.

        A ``units.json`` that has the unit is used as it is: the unit's work stays, start after start. Without
        ``units.json`` (a build writes it last) whatever is in the data dir is what a build that stopped halfway left,
        and goes first (``init`` with ``force``: only the clone, the worktrees and ``units.json``, inside the data dir).
        The codes ``init`` makes are dropped: personal mode has no login. The data dir is inside the checkout, so it
        gets a ``.gitignore`` of ``*`` first, which keeps all of it out of the participant's own commits.
        """
        data_dir = self._cfg.data_dir
        data_dir.mkdir(parents=True, exist_ok=True)
        ignore = data_dir / ".gitignore"
        if not ignore.exists():
            ignore.write_text(_DATA_IGNORE, encoding="utf-8", newline="\n")
        if self._file.exists():
            try:
                self.get(PERSONAL_UNIT)
            except KeyError:
                raise RuntimeError(f"{self._file} has no {PERSONAL_UNIT}: it is not a personal console's") from None
            self._refresh_workshop_branches()
            return False
        self.init(1, force=True)
        return True

    def _workshop_branches(self, hooks: str) -> int:
        """Personal mode: the workshop's branches of the checkout (``ticket/TS-1xx``, ``after-section-1`` .. ``after-section-5``, which
        ``workshop/branches.sh`` makes local there) as local branches of the clone, so the unit's copy, a worktree of
        the clone, has them: the experiments of the day start their runs there. A branch that is there already moves
        to the checkout's; one checked out somewhere stays as it is. Returns how many were set."""
        listing = _git(_repo(self._clone), "for-each-ref", "--format=%(refname)", "refs/remotes/origin", hooks=hooks)
        n = 0
        for remote in listing.split():
            name = remote.removeprefix("refs/remotes/origin/")
            if _WORKSHOP_BRANCH.fullmatch(name):
                n += _run(_repo(self._clone), ["branch", "-f", "--no-track", name, remote], hooks).returncode == 0
        return n

    def _start_where_the_stage_does(self, hooks: str) -> None:
        """Personal mode: ``start_ref`` of the clone moved to ``ticket/TS-104`` when the checkout has that branch, as
        ``workshop/stage-unit.sh`` does on the stage. The day's commands then run on the tree where only TS-104 is
        open, and give what they gave on the stage; on ``main`` every ticket is open, the ladder is red before an
        agent starts, and the TS-104 run ends "escalated" whatever the agent does."""
        remote = f"refs/remotes/origin/{_PERSONAL_START}"
        if _run(_repo(self._clone), ["rev-parse", "--verify", "-q", remote], hooks).returncode != 0:
            return
        start = f"refs/heads/{self._cfg.start_ref}"
        head = _run(_repo(self._clone), ["symbolic-ref", "-q", "HEAD"], hooks).stdout.strip()
        if head == start:
            _git(_repo(self._clone), "reset", "-q", "--hard", remote, hooks=hooks)
        else:
            _git(_repo(self._clone), "update-ref", start, remote, hooks=hooks)

    def _refresh_workshop_branches(self) -> None:
        """Personal mode, a start with the unit there: the workshop's branches fetched again from the checkout (they
        may have come after the first start: ``workshop/branches.sh``, a ``git pull``). Never stops a start."""
        try:
            hooks = self._no_hooks()
            _git(
                _repo(self._clone), "fetch", "-q", "--no-tags", "origin",
                "+refs/heads/ticket/*:refs/remotes/origin/ticket/*", "+refs/heads/after-section-*:refs/remotes/origin/after-section-*",
                hooks=hooks,
            )  # fmt: skip
            self._workshop_branches(hooks)
        except (OSError, RuntimeError) as exc:
            log.warning("the workshop branches of the console's copy were not refreshed: %s", exc)

    def _hide_referee(self, worktree: Path, hooks: str) -> None:
        """The unit's copy without ``hidden-tests/`` (the referee the workshop repository carries): a sparse checkout
        of everything but that folder, so an agent working in the copy never reads the held-back tests. Nothing to do
        when the tree has no such folder. Runs at build time, before any agent has worked in the copy."""
        if not (worktree / "hidden-tests").exists():
            return
        _git(worktree, "sparse-checkout", "set", "--no-cone", "/*", "!/hidden-tests/", hooks=hooks)

    def _worktree_flags(self, hooks: str) -> list[str]:
        """``--no-relative-paths`` when this git has the option (2.48 and later), else nothing.

        With ``worktree.useRelativePaths`` on in the user's git config, ``git worktree add`` writes relative paths into
        the ``.git`` file of the worktree and the ``gitdir`` file of the clone; ``files.git_location`` reads the latter
        and needs absolute ones. An older git has neither the setting nor the option.
        """
        usage = _run(_repo(self._clone), ["worktree", "add", "-h"], hooks)  # exits 129 after printing the options
        return ["--no-relative-paths"] if "relative-paths" in usage.stdout + usage.stderr else []

    def _copy_identity(self) -> None:
        """Give the clone (and so every worktree) the git identity the source repo resolves."""
        hooks = self._no_hooks()
        for key in ("user.name", "user.email"):
            found = _run(_repo(self._cfg.source_repo), ["config", "--get", key], hooks)
            value = found.stdout.strip()
            if found.returncode == 0 and value:
                _git(_repo(self._clone), "config", key, value, hooks=hooks)

    def _make_local_branches(self) -> None:
        """Local branches for start_ref (required) and each catch-up ref that the export has."""
        cfg, hooks = self._cfg, self._no_hooks()
        listing = _git(
            _repo(self._clone), "for-each-ref", "--format=%(refname)", "refs/heads", "refs/remotes/origin", hooks=hooks
        )
        known = set(listing.split())
        for ref in dict.fromkeys((cfg.start_ref, *cfg.catchup_refs)):
            remote = f"refs/remotes/origin/{ref}"
            if remote not in known:
                if ref == cfg.start_ref:
                    raise RuntimeError(f"start_ref {ref!r} is not a branch of the source repo")
                log.info("catch-up ref %s is not in the source repo; skipped", ref)
                continue
            if f"refs/heads/{ref}" not in known:
                _git(_repo(self._clone), "branch", "--no-track", ref, remote, hooks=hooks)

    def _wipe(self) -> None:
        """Remove worktrees, clone and units.json. Only paths that resolve inside the data dir go."""
        data_dir = self._cfg.data_dir
        if (self._clone / ".git").exists() and self._is_inside(self._clone, data_dir):
            # a worktree is ours only if it is under data_dir/units, and units/ itself stays in data_dir
            ours, hooks = self._is_inside(self._units_dir, data_dir), self._no_hooks()
            listing = _run(_repo(self._clone), ["worktree", "list", "--porcelain"], hooks).stdout
            for line in listing.splitlines():
                path = line[len("worktree "):]
                if ours and line.startswith("worktree ") and self._is_inside(Path(path), self._units_dir):
                    _run(_repo(self._clone), ["worktree", "remove", "--force", path], hooks)
        self._rmtree(self._units_dir)
        self._rmtree(self._clone)
        self._file.unlink(missing_ok=True)

    def _rmtree(self, path: Path) -> None:
        if not os.path.lexists(path):
            return
        if not self._is_inside(path, self._cfg.data_dir):
            raise RuntimeError(f"refusing to delete {path}: it is outside the data dir")
        shutil.rmtree(path, onexc=_make_writable)

    @staticmethod
    def _is_inside(path: Path, folder: Path) -> bool:
        return Path(os.path.realpath(folder)) in Path(os.path.realpath(path)).parents

    # -- reading --

    def all(self) -> list[Unit]:
        with self._lock:
            return [_unit(r) for r in self._load()["units"]]

    def get(self, unit_id: str) -> Unit:
        """The unit, or ``KeyError`` when there is no such unit."""
        with self._lock:
            return _unit(_find(self._load(), unit_id))

    def holder(self, unit_id: str) -> str | None:
        """Who holds the unit (``locked_by``), read without the store lock (``KeyError``: no such unit).

        For a caller that holds a worktree lock and so must not wait on the store lock (a reset holds the store lock,
        then waits for the worktree lock). ``units.json`` is replaced atomically, so the read sees one whole version.
        """
        for attempt in range(_REPLACE_TRIES):
            try:
                return _find(self._load(), unit_id)["locked_by"]
            except PermissionError:  # Windows: the file is being replaced this instant
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT_S)
        return None

    def login(self, code: str) -> str | None:
        """The subject (``unit-NN`` or ``admin``) the code belongs to, else ``None``."""
        if not _has_code_shape(code):
            return None
        with self._lock:
            data = self._load()
        digest = hash_code(code, data["salt"])
        subject = None
        stored = [("admin", data["admin_code_hash"]), *((r["id"], r["code_hash"]) for r in data["units"])]
        for name, code_hash in stored:  # no early exit: every hash is compared
            if hmac.compare_digest(digest, code_hash):
                subject = name
        return subject

    # -- changing --

    def update(self, unit_id: str, *, budget_add: float | None = None, **fields: Any) -> Unit:
        """Set ``name``, ``code_hash``, ``budget``, ``spent``, ``locked_by`` or ``level``; returns the unit.

        ``budget_add`` raises the budget by that many credits (a positive finite number) in the same write: read and
        written under the store lock, so two top-ups both count, and a request that also sets a field changes both
        or neither.
        """
        bad = sorted(set(fields) - _MUTABLE_FIELDS)
        if bad:
            raise ValueError(f"cannot update {', '.join(bad)}")
        amount = None if budget_add is None else _top_up(budget_add)

        def edit(record: dict) -> None:
            record.update(fields)
            if amount is not None:
                record["budget"] = record["budget"] + amount

        return self._change(unit_id, edit)

    def add_spent(self, unit_id: str, credits: float) -> Unit:
        if isinstance(credits, bool) or not isinstance(credits, (int, float)) or not math.isfinite(credits):
            raise ValueError(f"credits must be a finite number, got {credits!r}")
        return self._change(unit_id, lambda record: record.update(spent=record["spent"] + credits))

    def add_budget(self, unit_id: str, credits: float) -> Unit:
        """Raise the unit's budget by ``credits``, a positive finite number (see ``update``)."""
        return self.update(unit_id, budget_add=credits)

    def regenerate_code(self, unit_id: str) -> str:
        """Give the unit a new code and return it: the one time it exists in clear. The old code stops working.

        The new code differs from every code in use (the units' and the admin's: only their hashes are known, so the
        new one is hashed and compared). ``KeyError`` when there is no such unit. Sessions are not touched here: the
        caller rotates the session key (``server.rotate_session_key``), or the old code's cookies would stay valid.
        """
        with self._lock:
            data = self._load()
            record = _find(data, unit_id)
            taken = {data["admin_code_hash"], *(r["code_hash"] for r in data["units"])}
            while True:
                code = new_code()
                digest = hash_code(code, data["salt"])
                if digest not in taken:
                    break
            record["code_hash"] = digest
            self._save(data)
            return code

    def lock(self, unit_id: str, run_id: str) -> None:
        """Mark the unit as in use by ``run_id``; ``Busy`` when another run holds it."""
        if not isinstance(run_id, str) or not run_id:
            raise ValueError(f"run id must be a non-empty string, got {run_id!r}")

        def take(record: dict) -> None:
            holder = record["locked_by"]
            if holder is not None and holder != run_id:
                raise Busy(f"{unit_id} is busy: run {holder} holds it")
            record["locked_by"] = run_id

        self._change(unit_id, take)

    def unlock(self, unit_id: str, run_id: str) -> None:
        """Release the unit if ``run_id`` holds it; otherwise do nothing."""

        def release(record: dict) -> None:
            if record["locked_by"] == run_id:
                record["locked_by"] = None

        self._change(unit_id, release)

    # -- git --

    def git(self, unit_id: str, *args: str) -> str:
        """Stdout of git ``args`` on the unit's worktree (``git_location``); ``RuntimeError`` with stderr on failure."""
        return self.git_for(unit_id)(*args)

    def git_for(self, unit_id: str) -> Callable[..., str]:
        """``git`` for one unit, with the unit and its git folders looked up once, now (``KeyError``: no such unit).

        For a caller that goes on to hold ``files.worktree_lock``: calling the returned function never waits on this
        store's lock (the order is the store lock, then the worktree lock; never the other way round).
        """
        return functools.partial(_git, git_location(self.get(unit_id).worktree), hooks=self._no_hooks())

    def git_argv(self, unit_id: str) -> list[str]:
        """The start of a git command line on the unit's worktree, as every call here has it (``_argv``, with
        ``git_location``), for the one caller that runs git with an environment of its own: the push of
        ``console.ship``, whose token goes in that environment. ``KeyError``: no such unit."""
        return _argv(git_location(self.get(unit_id).worktree), self._no_hooks())

    def bare_git_argv(self, git_dir: Path) -> list[str]:
        """The same start of a git command line for a bare repo of the console's own, named by its git dir (the
        ``ship.git`` of ``console.ship``)."""
        return _argv([f"--git-dir={git_dir}"], self._no_hooks())

    def hold_for_reset(self, unit_id: str, run_id: str) -> None:
        """Keep a unit locked after run ``run_id`` could not put its worktree back.

        If ``run_id`` holds the unit, ``locked_by`` becomes ``needs-reset:<run id>``: no run, save or keep takes the
        unit until a ``reset`` (which clears that lock) has put the worktree on a known state again.
        """

        def hold(record: dict) -> None:
            if record["locked_by"] == run_id:
                record["locked_by"] = NEEDS_RESET + run_id

        self._change(unit_id, hold)

    def reset(self, unit_id: str, ref: str) -> list[str]:
        """Put the unit's branch on ``ref`` (start_ref or a catch-up ref); returns the backup branches.

        The unit's saved work, the tip of ``unit/NN``, is kept as ``unit/NN-backup-YYYYMMDD-HHMMSS``; when HEAD is on
        another commit (an agent's branch, a detached HEAD), that one is kept too, as the same name with ``-head``.
        Uncommitted edits and untracked files are discarded (ignored files stay), so they are not in a backup.
        ``unit/NN`` is set to ``ref`` (made again if it is gone), then the worktree put on it and checked to be on
        ``unit/NN`` and clean (``worktree_ops.restore_to``). ``Busy`` while a run holds the unit; a ``needs-reset:``
        lock (``hold_for_reset``) is what a reset is for, and is cleared by it once the check passes. When the check
        (or the checkout) fails, ``RuntimeError``: a held unit stays held, and a free one is held now (``unit/NN`` has
        moved, the tree may not match it). The store lock is held throughout, so no run can take the unit mid-reset,
        and so is the worktree lock of ``console.files``, so no editor save lands between the git calls (or is lost by
        the reset). A stale ``index.lock`` (a git killed with its run) is removed first: no run of the unit is alive,
        since a run holds the unit lock until its processes are swept.
        """
        cfg = self._cfg
        if ref != cfg.start_ref and ref not in cfg.catchup_refs:
            allowed = ", ".join((cfg.start_ref, *cfg.catchup_refs))
            raise ValueError(f"unknown ref {ref!r}: reset to one of {allowed}")
        with self._lock:
            data = self._load()
            unit = _unit(_find(data, unit_id))
            if unit.locked_by is not None and not unit.locked_by.startswith(NEEDS_RESET):
                raise Busy(f"{unit_id} is busy: run {unit.locked_by} holds it")
            hooks = self._no_hooks()
            with worktree_lock(unit.worktree):  # store lock first, then worktree lock; never the other way round
                wt = git_location(unit.worktree)
                worktree_ops.drop_stale_index_lock(functools.partial(_git, wt, hooks=hooks))
                if not _has_ref(wt, f"refs/heads/{ref}", hooks):
                    raise ValueError(f"ref {ref!r} is not in this source repo")
                backups = self._back_up(wt, unit.branch, hooks)
                _git(wt, "update-ref", "-m", f"console: reset to {ref}", f"refs/heads/{unit.branch}",
                     f"refs/heads/{ref}", hooks=hooks)
                try:  # checkout -f, clean -ffd (never -x: ignored files such as builds and run logs stay), checked
                    worktree_ops.restore_to(functools.partial(_git, wt, hooks=hooks), unit.worktree, unit.branch)
                except Exception:
                    if unit.locked_by is None:  # unit/NN has moved and the tree may not match it: no run takes it
                        self._hold_after_reset(data, unit_id)
                    raise
            if unit.locked_by is not None:  # the needs-reset lock: the worktree is in a known state again
                _find(data, unit_id)["locked_by"] = None
                self._save(data)
            return backups

    @staticmethod
    def _back_up(wt: Sequence[str], branch: str, hooks: str) -> list[str]:
        """Backup branches of the unit's saved work (the tip of ``branch``) and of HEAD when it is another commit."""
        stamp = f"{branch}-backup-{datetime.now().strftime('%Y%m%d-%H%M%S')}"
        name, attempt = stamp, 1
        while _has_ref(wt, f"refs/heads/{name}", hooks) or _has_ref(wt, f"refs/heads/{name}-head", hooks):
            attempt += 1
            name = f"{stamp}-{attempt}"
        tip, head = _commit_of(wt, f"refs/heads/{branch}", hooks), _commit_of(wt, "HEAD", hooks)
        backups = []
        if tip is not None:
            _git(wt, "branch", "--no-track", name, tip, hooks=hooks)
            backups.append(name)
        if head is not None and head != tip:
            head_name = f"{name}-head" if backups else name  # the only backup when unit/NN is gone
            _git(wt, "branch", "--no-track", head_name, head, hooks=hooks)
            backups.append(head_name)
        return backups

    def _hold_after_reset(self, data: dict, unit_id: str) -> None:
        """A free unit whose reset could not put its worktree back is held for a reset, as a run's would be."""
        _find(data, unit_id)["locked_by"] = NEEDS_RESET + _FAILED_RESET
        try:
            self._save(data)
        except OSError:
            log.exception("%s could not be held for a reset after its reset failed", unit_id)
        else:
            log.error("%s stays locked (needs a reset): its reset could not put the worktree back", unit_id)

    def restore(self, unit_id: str, base: str | None = None) -> bool:
        """Put the worktree of a unit that waits for a reset (``hold_for_reset``) back on its own branch, and free it.

        ``base``: the commit ``unit/NN`` had when the held run started (its ``unit_head``), when it is known. Unlike
        ``reset``, ``unit/NN`` is not moved and nothing goes to a backup branch: every save the unit made stays. That
        holds also when a run moved ``unit/NN`` away from ``base`` (an agent's commit on it that could not be taken
        back): the branch is kept as it is, a warning is logged, and the answer is True. A ``unit/NN`` that is gone is
        made again at ``base`` (without ``base`` the restore fails). A stale ``index.lock`` is removed, then
        ``worktree_ops.restore_to``: HEAD on ``unit/NN`` (``checkout -f``), untracked files and nested repositories
        removed (``clean -ffd``; never ``-x``: ignored files such as run folders and builds stay), and a check that the
        worktree is on ``unit/NN`` and clean. Only then is the hold cleared. Store lock, then worktree lock, as in
        ``reset``. ``Busy`` while a run holds the unit, ``ValueError`` for a unit that does not wait for a reset; when
        git fails or the check does not pass (``RuntimeError``) the unit stays held.
        """
        base = base if isinstance(base, str) and _SHA.fullmatch(base) else None
        with self._lock:
            data = self._load()
            unit = _unit(_find(data, unit_id))
            if unit.locked_by is None:
                raise ValueError(f"{unit_id} does not wait for a reset")
            if not unit.locked_by.startswith(NEEDS_RESET):
                raise Busy(f"{unit_id} is busy: run {unit.locked_by} holds it")
            hooks, wt = self._no_hooks(), git_location(unit.worktree)
            git = functools.partial(_git, wt, hooks=hooks)
            branch = f"refs/heads/{unit.branch}"
            with worktree_lock(unit.worktree):
                worktree_ops.drop_stale_index_lock(git)
                tip = git("rev-parse", "--verify", "-q", branch).strip() if _has_ref(wt, branch, hooks) else None
                if tip is None and base is not None:  # gone: made again where the run started ("": it must not exist)
                    git("update-ref", "-m", "console: restore where the run started", branch, base, "")
                    tip = base
                    log.warning("%s was gone: made again at %s, where the held run started", unit.branch, base)
                worktree_ops.restore_to(git, unit.worktree, unit.branch)
            moved = base is not None and tip != base
            if moved:
                log.warning("%s is at %s, not at %s where the held run started: kept (a commit the run made is on it)",
                            unit.branch, tip, base)
            _find(data, unit_id)["locked_by"] = None
            self._save(data)
            return moved

    # -- units.json --

    def _change(self, unit_id: str, edit: Callable[[dict], None]) -> Unit:
        with self._lock:
            data = self._load()
            record = _find(data, unit_id)
            edit(record)
            self._save(data)
            return _unit(record)

    def _load(self) -> dict:
        try:
            text = self._file.read_text(encoding="utf-8")
        except FileNotFoundError:
            raise RuntimeError(f"no units yet ({self._file} is missing): run `python -m console units init`") from None
        data = json.loads(text)
        if self._cfg.personal:
            _per_day(data, self._cfg.unit_budget, _today())
        return data

    def _save(self, data: dict) -> None:
        self._file.parent.mkdir(parents=True, exist_ok=True)
        fd, tmp = tempfile.mkstemp(dir=self._file.parent, prefix="units.", suffix=".tmp")
        try:
            with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
                json.dump(data, f, indent=2, allow_nan=False)
                f.write("\n")
                f.flush()
                os.fsync(f.fileno())
            for attempt in range(_REPLACE_TRIES):
                try:
                    os.replace(tmp, self._file)
                    break
                except PermissionError:
                    if attempt == _REPLACE_TRIES - 1:
                        raise
                    time.sleep(_REPLACE_WAIT_S)
        except BaseException:
            with contextlib.suppress(OSError):
                os.unlink(tmp)
            raise


# --- helpers -----------------------------------------------------------------------------------


def _today() -> str:
    """The local calendar day, ``YYYY-MM-DD``: the day a personal unit's ``spent`` counts."""
    return datetime.now().date().isoformat()


def _per_day(data: dict, budget: float, today: str) -> None:
    """Personal mode, on every read of ``units.json``: a unit whose ``spent`` is of another day than ``today``
    (``spent_day``) starts the day at 0, and ``budget`` is the config's. So a day's credits are the budget, whatever
    earlier days spent; the change is written with the next write of the file (a charge, a lock)."""
    for record in data["units"]:
        if record.get("spent_day") != today:
            record["spent"], record["spent_day"] = 0.0, today
        record["budget"] = budget


def _find(data: dict, unit_id: str) -> dict:
    for record in data["units"]:
        if record["id"] == unit_id:
            return record
    raise KeyError(unit_id)


def _unit(record: dict) -> Unit:
    return Unit(
        id=record["id"],
        name=record["name"],
        branch=record["branch"],
        worktree=Path(record["worktree"]),
        code_hash=record["code_hash"],
        budget=float(record["budget"]),
        spent=float(record["spent"]),
        locked_by=record["locked_by"],
        level=int(record["level"]),
    )


def _top_up(credits: object) -> float:
    """``credits`` as a float if positive and finite (no bool, no integer beyond a float); else ``ValueError``."""
    if isinstance(credits, bool) or not isinstance(credits, (int, float)):
        raise ValueError(f"credits must be a positive finite number, got {credits!r}")
    try:
        amount = float(credits)
    except OverflowError:
        raise ValueError("credits must be a positive finite number, got an integer too large") from None
    if not math.isfinite(amount) or amount <= 0:
        raise ValueError(f"credits must be a positive finite number, got {credits!r}")
    return amount


def _has_code_shape(code: object) -> bool:
    """6 ASCII characters, all from the code alphabet once upper-cased."""
    return (
        isinstance(code, str)
        and len(code) == _CODE_LENGTH
        and code.isascii()
        and all(ch in ALPHABET for ch in code.upper())
    )


def _distinct_codes(n: int) -> list[str]:
    codes: list[str] = []
    while len(codes) < n:
        code = new_code()
        if code not in codes:
            codes.append(code)
    return codes


def _make_writable(func: Callable[[str], object], path: str, _exc: BaseException) -> None:
    """``shutil.rmtree`` error hook: git's read-only pack files need the flag cleared on Windows."""
    os.chmod(path, stat.S_IWRITE)
    func(path)
