"""Process control for runs: an allowlisted environment, a detached start, and a kill that takes the whole tree.

A run is ``python -m harness`` started as a subprocess. This module is what the runner uses to

* build the child's environment (:func:`run_env`): nothing is inherited except names on an allowlist, and a
  name that looks like a credential never gets through, whichever rule would otherwise admit it;
* start it (:func:`spawn`) in its own process group (Windows) or session (POSIX), stdout and stderr appended to a
  log file, stdin closed;
* stop it (:func:`kill_tree`, :func:`kill_containers`): the child and everything it started, then the Docker
  gate containers that carry the run's label.

The console's own git calls (``files``, ``units``, ``validate``) run through :func:`run_git`: the environment of
:func:`git_env` and at most :data:`GIT_TIMEOUT_S` seconds, after which the call and everything it started are killed.

A pid alone does not identify a process for long: the number is reused. A caller that keeps a pid (in a run
record, across a console restart) also keeps :func:`start_time` of that process, and passes it as ``started`` to
:func:`alive` and :func:`kill_tree`, which then act on a process only if it started at that moment.

Matching of variable names: Windows environment names are case-insensitive, so on Windows both the allowlist and
the ``extra`` rules ignore case, and the result keeps each name spelled as it was in ``base``. On POSIX the
allowlist is exact. The deny rules ignore case everywhere: a lower-case ``gh_token`` is still a token.

Nothing here runs through a shell, and no environment value is ever logged or put in an error message.
"""

from __future__ import annotations

import contextlib
import functools
import logging
import os
import re
import signal
import subprocess
import tempfile
import time
from pathlib import Path
from typing import Any, Mapping, Sequence

__all__ = [
    "ENV_ALLOW", "ENV_PREFIX_ALLOW", "GIT_TIMEOUT_S", "alive", "denied", "git_env", "kill_containers", "kill_tree",
    "run_env", "run_git", "spawn", "start_time",
]  # fmt: skip

_log = logging.getLogger(__name__)

_WINDOWS = os.name == "nt"

# Names a run may inherit from the console's own environment, and the prefixes it may inherit.
ENV_ALLOW = frozenset(
    {
        "PATH", "SYSTEMROOT", "SYSTEMDRIVE", "WINDIR", "COMSPEC", "PATHEXT", "TEMP", "TMP", "HOME", "USERPROFILE",
        "APPDATA", "LOCALAPPDATA", "PROGRAMDATA", "PROGRAMFILES", "PROGRAMFILES(X86)", "NUMBER_OF_PROCESSORS",
        "PROCESSOR_ARCHITECTURE", "LANG", "TZ", "DOCKER_HOST",
        "PYTHONUTF8", "PYTHONIOENCODING",  # Task 11 passes PYTHONUTF8=1
        "HOMEDRIVE", "HOMEPATH", "USERNAME",  # Windows tools resolve the profile from these
    }
)  # fmt: skip
ENV_PREFIX_ALLOW = ("COPILOT_",)

# What the console itself adds to a run's environment (``extra``), besides ENV_ALLOW names.
_EXTRA_PREFIXES = ("HARNESS_", "TS_")

# Never in a run's environment, however it got on a list above. Compared in upper case.
_DENY_PREFIXES = ("JIRA_", "GH_", "GITHUB_", "HIL_", "BRIDGE_", "CONFLUENCE_")
_DENY_SUFFIXES = ("_TOKEN", "_SECRET", "_PASSWORD", "_PASSWD", "_KEY", "_PAT", "_CREDENTIAL", "_CREDENTIALS")


def _fold(name: str) -> str:
    """The key under which two spellings of an allowlist name count as one variable."""
    return name.upper() if _WINDOWS else name


def denied(name: str) -> bool:
    """Whether ``name`` looks like a credential's (the deny rules, any case): never in a run's environment."""
    upper = name.upper()
    return upper.startswith(_DENY_PREFIXES) or upper.endswith(_DENY_SUFFIXES)


def _inherited(name: str) -> bool:
    key = _fold(name)
    return key in {_fold(n) for n in ENV_ALLOW} or key.startswith(tuple(_fold(p) for p in ENV_PREFIX_ALLOW))


def _extra_allowed(name: str) -> bool:
    key = _fold(name)
    return key.startswith(tuple(_fold(p) for p in _EXTRA_PREFIXES)) or key in {_fold(n) for n in ENV_ALLOW}


def run_env(base: Mapping[str, str], extra: Mapping[str, str]) -> dict[str, str]:
    """The environment for a run: the allowed part of ``base``, then ``extra`` on top.

    ``base`` (normally ``os.environ``) contributes only names in :data:`ENV_ALLOW` or starting with a prefix in
    :data:`ENV_PREFIX_ALLOW`, and never a name that matches a deny rule (``JIRA_*``, ``GH_*``, ``GITHUB_*``,
    ``HIL_*``, ``BRIDGE_*``, ``CONFLUENCE_*``, ``*_TOKEN``, ``*_SECRET``, ``*_PASSWORD``, ``*_PASSWD``, ``*_KEY``,
    ``*_PAT``, ``*_CREDENTIAL``, ``*_CREDENTIALS``). Names keep their spelling
    from ``base``. ``extra`` is what the console sets itself: its names must start with ``HARNESS_`` or ``TS_`` or
    be in :data:`ENV_ALLOW`, and must not match a deny rule, else ``ValueError`` (naming the key, never the
    value). An ``extra`` value replaces the ``base`` one of the same variable. The arguments are not changed.
    """
    merged: dict[str, tuple[str, str]] = {}  # folded name -> (name as spelled, value)
    for name, value in base.items():
        if _inherited(name) and not denied(name):
            merged[_fold(name)] = (name, value)
    for name, value in extra.items():
        if denied(name):
            raise ValueError(f"environment variable {name!r} is never passed to a run (it matches a deny rule)")
        if not _extra_allowed(name):
            raise ValueError(
                f"environment variable {name!r} cannot be set for a run: names must start with "
                f"{' or '.join(_EXTRA_PREFIXES)} or be one of the allowlisted names"
            )
        key = _fold(name)
        merged[key] = (merged[key][0] if key in merged else name, value)
    return dict(merged.values())


# --- start --------------------------------------------------------------------------------------


def spawn(cmd: list[str], cwd: Path, env: dict[str, str], log: Path) -> subprocess.Popen:
    """Start ``cmd`` in ``cwd`` with exactly ``env`` (nothing is inherited), detached from the console.

    The child gets a new process group (Windows) or session (POSIX), so :func:`kill_tree` can take it and
    everything it starts without touching the console. stdout and stderr are appended to ``log`` (created if
    missing; its folder must exist); stdin is closed. No shell is used. To be able to stop the run safely later
    from just its pid, keep ``start_time(proc.pid)`` with it.
    """
    if _WINDOWS:
        detach: dict[str, Any] = {"creationflags": subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW}
    else:
        detach = {"start_new_session": True}
    with open(log, "ab") as out:  # the child has its own copy of the handle once it is started
        return subprocess.Popen(
            cmd, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=out, stderr=subprocess.STDOUT, **detach
        )


# --- liveness, identity and kill ----------------------------------------------------------------

_PID_LIMIT = 2**31  # a pid is a signed 32-bit number on every platform we run on
_GRACE_S = 3.0  # POSIX: how long SIGTERM gets before SIGKILL
_POLL_S = 0.05
_START_TOLERANCE_S = 1.0  # readings of one process's start time agree to far better than this
_TASKKILL_TIMEOUT_S = 30

_PROCESS_QUERY_LIMITED_INFORMATION = 0x1000
_STILL_ACTIVE = 259
_ERROR_ACCESS_DENIED = 5
_TASKKILL_NOT_FOUND = 128


@functools.cache
def _kernel32() -> Any:
    import ctypes
    from ctypes import wintypes

    lib = ctypes.WinDLL("kernel32", use_last_error=True)
    lib.OpenProcess.argtypes = [wintypes.DWORD, wintypes.BOOL, wintypes.DWORD]
    lib.OpenProcess.restype = wintypes.HANDLE
    lib.GetExitCodeProcess.argtypes = [wintypes.HANDLE, ctypes.POINTER(wintypes.DWORD)]
    lib.GetExitCodeProcess.restype = wintypes.BOOL
    lib.GetProcessTimes.argtypes = [wintypes.HANDLE, *([ctypes.POINTER(wintypes.FILETIME)] * 4)]
    lib.GetProcessTimes.restype = wintypes.BOOL
    lib.CloseHandle.argtypes = [wintypes.HANDLE]
    lib.CloseHandle.restype = wintypes.BOOL
    return lib


def start_time(pid: int) -> float | None:
    """When the process with this pid started, in seconds on a clock of the platform; ``None`` if unknown.

    Windows: the creation time. Linux: ``/proc/<pid>/stat``, seconds since boot. Elsewhere (and for a pid that is
    not running, or not readable by us) ``None``. Values are only comparable with others read on the same
    machine; the point is to tell the process a pid meant earlier from a later one that got the same number.
    """
    if not 0 < pid < _PID_LIMIT:
        return None
    return _start_time_windows(pid) if _WINDOWS else _start_time_linux(pid)


def _start_time_windows(pid: int) -> float | None:
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return None
    try:
        created, exited, kernel, user = (wintypes.FILETIME() for _ in range(4))
        if not kernel32.GetProcessTimes(
            handle, ctypes.byref(created), ctypes.byref(exited), ctypes.byref(kernel), ctypes.byref(user)
        ):
            return None
        return ((created.dwHighDateTime << 32) | created.dwLowDateTime) / 1e7  # 100 ns ticks since 1601
    finally:
        kernel32.CloseHandle(handle)


def _start_time_linux(pid: int) -> float | None:
    fields = _proc_stat(pid)
    if fields is None or len(fields) < 20:
        return None
    try:
        return int(fields[19]) / os.sysconf("SC_CLK_TCK")  # field 22 of stat; ``fields`` starts at field 3
    except (ValueError, OSError):
        return None


def _proc_stat(pid: int) -> list[str] | None:
    """Fields 3 and up of ``/proc/<pid>/stat`` (the state letter first); ``None`` without such an entry."""
    try:
        stat = Path(f"/proc/{pid}/stat").read_text(encoding="utf-8", errors="replace")
    except OSError:
        return None
    return stat.rpartition(")")[2].split()  # fields 1 and 2 end at the last ")": the command name may hold spaces


def _same_start(pid: int, started: float) -> bool:
    current = start_time(pid)
    return current is not None and abs(current - started) <= _START_TOLERANCE_S


def alive(pid: int, started: float | None = None) -> bool:
    """True while a process with this pid is running.

    With ``started`` (a :func:`start_time` kept from earlier) it must also be the process that started then: a
    different start time, or one that cannot be read, is a different process that took the pid, so False.

    Never sends a signal that can end the process (``os.kill(pid, 0)`` is a plain existence check on POSIX
    only; on Windows it would terminate, so the process is queried instead). A process that has exited is not
    alive, a zombie that nobody has waited for included where ``/proc`` shows it (Linux). A pid that cannot be a
    process (zero, negative, too large) is not alive. (Windows: a process that exited with code 259, the value
    ``STILL_ACTIVE``, looks alive; the harness never exits with it.)
    """
    if not 0 < pid < _PID_LIMIT:
        return False
    running = _alive_windows(pid) if _WINDOWS else _alive_posix(pid)
    return running and (started is None or _same_start(pid, started))


def _alive_windows(pid: int) -> bool:
    import ctypes
    from ctypes import wintypes

    kernel32 = _kernel32()
    handle = kernel32.OpenProcess(_PROCESS_QUERY_LIMITED_INFORMATION, False, pid)
    if not handle:
        return ctypes.get_last_error() == _ERROR_ACCESS_DENIED  # there, just not ours to query; else no such pid
    try:
        code = wintypes.DWORD()
        if not kernel32.GetExitCodeProcess(handle, ctypes.byref(code)):
            return True  # cannot tell: assume it runs, so the caller still tries to stop it
        return code.value == _STILL_ACTIVE
    finally:
        kernel32.CloseHandle(handle)


def _alive_posix(pid: int) -> bool:
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # it exists; it is another user's
    fields = _proc_stat(pid)
    return not fields or fields[0] != "Z"  # a zombie has exited, only nobody has waited for it yet


def kill_tree(target: int | subprocess.Popen, started: float | None = None) -> None:
    """Kill a process and everything it started. Quiet when it is already gone.

    ``target`` is the :class:`subprocess.Popen` that :func:`spawn` returned, or its pid. A ``Popen`` that has
    exited is left alone (nothing to kill, and its pid may already belong to someone else). Pass the ``Popen``
    when there is one: on POSIX it is also what lets the exited leader be reaped, so the wait below ends early.

    ``started`` is the process's :func:`start_time` from when it was started. When given, a process that holds
    the pid now with a different start time is not touched (a warning is logged): the pid was reused. On Windows
    a pid whose start time cannot be read counts as not ours as well.

    Windows: ``taskkill /PID <pid> /T /F``. POSIX: ``spawn`` made the child a session leader, so its group id is
    its pid; SIGTERM goes to that group (children that outlive their leader are reached; a pid that leads no group
    gives nothing), then up to 3 seconds for the whole group to be gone, then SIGKILL to what is left.

    ``ValueError`` for a pid that is not a process, the console's own, or (POSIX) the console's own process group.
    """
    if isinstance(target, subprocess.Popen):
        if target.poll() is not None:
            return
        pid, leader = target.pid, target
    else:
        pid, leader = target, None
    if not 0 < pid < _PID_LIMIT:
        raise ValueError(f"not a process id: {pid!r}")
    if pid == os.getpid():
        raise ValueError("refusing to kill the console's own process")
    if started is not None and not _is_the_process(pid, started):
        return
    if _WINDOWS:
        _kill_tree_windows(pid)
    else:
        _kill_tree_posix(pid, leader)


def _is_the_process(pid: int, started: float) -> bool:
    """False (with a warning) when the pid now belongs to a process that did not start at ``started``."""
    current = start_time(pid)
    if current is None:
        # Windows: gone, or not ours to read: nothing to kill either way. Elsewhere the leader may be gone while
        # its children run on in the group; no process holds the number, so signalling the group is safe.
        return not _WINDOWS
    if abs(current - started) > _START_TOLERANCE_S:
        _log.warning("not killing pid %s: it started at a different time than the process meant (pid reused)", pid)
        return False
    return True


def _kill_tree_windows(pid: int) -> None:
    taskkill = Path(os.environ.get("SystemRoot", r"C:\Windows")) / "System32" / "taskkill.exe"
    try:
        done = subprocess.run(
            [str(taskkill), "/PID", str(pid), "/T", "/F"],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            timeout=_TASKKILL_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW,
            check=False,
        )
    except subprocess.TimeoutExpired:
        _log.warning("taskkill for pid %s did not finish within %s s", pid, _TASKKILL_TIMEOUT_S)
        return
    if done.returncode not in (0, _TASKKILL_NOT_FOUND):
        detail = done.stderr.decode("utf-8", errors="replace").strip()
        _log.warning("taskkill for pid %s exited %s: %s", pid, done.returncode, detail)


def _kill_tree_posix(pid: int, leader: subprocess.Popen | None) -> None:
    if pid == os.getpgrp():
        raise ValueError(f"pid {pid} is the console's own process group; refusing to signal it")
    if not _signal_group(pid, signal.SIGTERM):
        return  # nothing leads a group with this number: gone already, or not a process of ours to take down
    deadline = time.monotonic() + _GRACE_S
    while time.monotonic() < deadline:
        if _group_gone(pid, leader):
            return
        time.sleep(_POLL_S)
    _signal_group(pid, signal.SIGKILL)  # whatever is left, including children that ignored SIGTERM


def _signal_group(pgid: int, sig: int) -> bool:
    """Signal the group; False when there is no such group."""
    try:
        os.killpg(pgid, sig)
    except ProcessLookupError:
        return False
    return True


def _group_gone(pgid: int, leader: subprocess.Popen | None) -> bool:
    if leader is not None:
        leader.poll()  # an exited leader stays a zombie, and keeps its group in existence, until it is waited for
    try:
        os.killpg(pgid, 0)
    except ProcessLookupError:
        return True
    except PermissionError:
        return False  # the group exists, with a member we may not signal
    return False


# --- the console's own git calls ----------------------------------------------------------------

GIT_TIMEOUT_S = 120.0  # how long one git call of the console may take before it is killed
_AFTER_KILL_S = 5.0  # how long the output of a killed call is waited for


def git_env(extra: Mapping[str, str] | None = None) -> dict[str, str]:
    """The environment of a git call of the console: :func:`run_env` of the console's own (no tokens, no ``GIT_*``),
    git never asks for input, and replace refs are ignored (``GIT_NO_REPLACE_OBJECTS``: an object an agent put under
    ``refs/replace`` cannot change what the console reads or commits). Then ``extra``, as it is."""
    return {**run_env(os.environ, {}), "GIT_TERMINAL_PROMPT": "0", "GIT_NO_REPLACE_OBJECTS": "1", **(extra or {})}


def run_git(
    git: Sequence[str],
    args: Sequence[str],
    *,
    stdin: bytes | None = None,
    env: Mapping[str, str] | None = None,
    text: bool = False,
) -> subprocess.CompletedProcess:
    """Run ``[*git, *args]`` (``git`` is the command with its global options, ``args`` the subcommand and what
    follows) without a shell, in the environment of ``git_env(env)``, and wait for it; stdout and stderr captured.

    ``stdin`` is what it reads (from a temporary file, so a call that never reads cannot block the write); without
    it the input is empty. ``text``: the output decoded as UTF-8 (what cannot be decoded replaced) with ``\\r\\n``
    and ``\\r`` read as ``\\n``, as ``subprocess.run(text=True)`` does; else bytes.

    ``RuntimeError("git timed out: <subcommand>")`` when it has not ended after :data:`GIT_TIMEOUT_S` seconds: the
    process and everything it started are killed first (:func:`kill_tree`; on POSIX the call runs in a session of
    its own for that), and its output is waited for a few seconds at most, so a process that got away and holds
    the pipes cannot hold the caller.
    """
    argv = [*git, *args]
    detach: dict[str, Any] = {} if _WINDOWS else {"start_new_session": True}
    with contextlib.ExitStack() as stack:
        source: Any = subprocess.DEVNULL
        if stdin is not None:
            source = stack.enter_context(tempfile.TemporaryFile())
            source.write(stdin)
            source.seek(0)
        proc = subprocess.Popen(
            argv, stdin=source, stdout=subprocess.PIPE, stderr=subprocess.PIPE, env=git_env(env), **detach
        )
    try:
        out, err = proc.communicate(timeout=GIT_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        kill_tree(proc)
        if proc.poll() is None:  # the tree kill did not take it (taskkill failed): the process itself at least
            proc.kill()
        with contextlib.suppress(subprocess.TimeoutExpired):
            proc.communicate(timeout=_AFTER_KILL_S)
        raise RuntimeError(f"git timed out: {args[0] if args else git[0]}") from None
    except BaseException:  # interrupted while waiting: the call does not outlive it
        proc.kill()
        raise
    if text:
        return subprocess.CompletedProcess(argv, proc.returncode, _text(out), _text(err))
    return subprocess.CompletedProcess(argv, proc.returncode, out, err)


def _text(data: bytes) -> str:
    return data.decode("utf-8", errors="replace").replace("\r\n", "\n").replace("\r", "\n")


# --- docker -------------------------------------------------------------------------------------

_RUN_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")
_DOCKER_TIMEOUT_S = 30
_RUNNABLE_EXTENSIONS = (".com", ".exe", ".bat", ".cmd")  # what CreateProcess starts; PATHEXT also lists scripts


def _find_docker() -> str | None:
    """The first ``docker`` in the absolute folders of PATH.

    Not ``shutil.which``: on Windows that looks in the current directory first, and an empty or relative PATH
    entry means the current directory too; the console must never run a ``docker`` that a unit or a run left
    lying in the folder it happens to be in.
    """
    extensions = [""]
    if _WINDOWS:
        listed = [e.strip().lower() for e in os.environ.get("PATHEXT", "").split(";")]
        extensions = [e for e in listed if e in _RUNNABLE_EXTENSIONS] or list(_RUNNABLE_EXTENSIONS)
    cwd = os.path.normcase(os.path.realpath(os.getcwd()))
    for folder in os.environ.get("PATH", "").split(os.pathsep):
        folder = folder.strip('"')
        if not folder or not os.path.isabs(folder) or os.path.normcase(os.path.realpath(folder)) == cwd:
            continue
        for extension in extensions:
            candidate = os.path.join(folder, "docker" + extension)
            if os.path.isfile(candidate) and (_WINDOWS or os.access(candidate, os.X_OK)):
                return candidate
    return None


def kill_containers(run_id: str) -> int:
    """``docker kill`` the gate containers labelled ``tinystream.run=<run_id>``; returns how many it asked for.

    ``ValueError`` for an id that is not 1 to 100 characters of ``A-Z a-z 0-9 . _ -`` starting with a letter or
    digit (it goes into a docker filter, so nothing else is accepted). 0 when ``docker`` is not on PATH (the
    current directory is never searched), when ``docker ps`` fails, or when nothing carries the label. A failing
    ``docker kill`` is logged, not raised: the containers may have exited since ``ps``.
    """
    if not isinstance(run_id, str) or not _RUN_ID.fullmatch(run_id):
        raise ValueError(f"not a valid run id: {run_id!r}")
    docker = _find_docker()
    if docker is None:
        return 0
    listing = _docker(docker, "ps", "-q", "--filter", f"label=tinystream.run={run_id}")
    if listing is None or listing.returncode != 0:
        return 0
    ids = listing.stdout.split()
    if not ids:
        return 0
    killed = _docker(docker, "kill", *ids)
    if killed is None or killed.returncode != 0:
        _log.warning("docker kill for run %s did not succeed (exit %s)", run_id, getattr(killed, "returncode", "n/a"))
    return len(ids)


def _docker(docker: str, *args: str) -> subprocess.CompletedProcess[str] | None:
    """Run ``docker args`` without a shell; ``None`` when it cannot be run or does not answer in time."""
    try:
        return subprocess.run(
            [docker, *args],
            stdin=subprocess.DEVNULL,
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            timeout=_DOCKER_TIMEOUT_S,
            creationflags=subprocess.CREATE_NO_WINDOW if _WINDOWS else 0,
            check=False,
        )
    except (OSError, subprocess.TimeoutExpired):
        return None
