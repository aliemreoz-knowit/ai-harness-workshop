"""``python -m console doctor``: the things the workshop day depends on, each with the fix when it is not right.

``run_checks(cfg, probe)`` returns one :class:`Check` per item, in the order of ``_CHECKS``; ``format_report`` prints
them as a table. The admin page will show the same list.

Rules this module keeps:

* A command runs only through ``probe``. :func:`default_probe` starts it without a shell, with the allowlisted
  environment of ``procs.run_env`` (no token of the ``.env`` ever reaches it), stdin closed, a time limit, and gives
  back the exit code and the output cut to 4 KB. A test hands in a stub, so nothing real runs.
* No value of the ``.env`` ends up in a check: it is judged by names, and by whether a value is empty.
* The report of ``python -m harness doctor --json`` is read for its ``runtime_mode`` and for the names and status
  of its checks, never for their details (those hold login names and paths).
* A check that crashes becomes a FAIL line of its own; the other checks still run.
* ``serve`` leaves ``<data>/serve.json`` (who serves, on which address) while it runs. ``console_may_be_running``
  reads it and asks the network, and gives the all-clear only when every address it knows refuses a connection:
  that is what ``units init --force`` needs before it takes the worktrees out from under a console.
  ``serving_console`` reads it for ``serve --personal``, to point to the console that runs on the checkout already.
* ``serve --personal`` holds ``ServeLock`` (an OS lock on ``<data>/serve.lock``) from before it builds anything until
  it ends: one console per checkout, however close together two starts come.
* Personal mode (``cfg.personal``, ``doctor --personal``) leaves out what only the room needs (``_ROOM_ONLY``: the
  ``.env``, the firewall rule, the LAN addresses). Docker, a busy port and a unit not built yet are warnings there:
  the gates run without Docker, ``serve --personal`` takes a free port and builds the unit on its first start.
"""

from __future__ import annotations

import contextlib
import http.client
import importlib.util
import json
import logging
import os
import platform
import re
import shutil
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
from concurrent.futures import ThreadPoolExecutor
from dataclasses import dataclass, replace
from pathlib import Path
from typing import Callable

from console import ladder, procs
from console.config import ConsoleConfig
from console.secrets import Secrets
from console.server import WEB_DIR, allowed_hosts, lan_urls
from console.units import NEEDS_RESET, UnitStore, check_source

__all__ = [
    "Check", "MIN_PYTHON", "Probe", "ServeLock", "console_answers", "console_may_be_running", "default_probe",
    "format_report", "remove_serve_record", "room_urls", "run_checks", "serving_console", "write_serve_record",
]  # fmt: skip

log = logging.getLogger(__name__)

Probe = Callable[..., tuple[int, str]]  # probe(argv, cwd=None, timeout=30) -> (exit code, output)

MIN_PYTHON = (3, 12)
MIN_FREE_GB = 10
FIREWALL_RULE = "Harness Console"
MONACO_DIR = WEB_DIR / "vendor" / "monaco"
VENDOR_SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "vendor_monaco.py"

_WINDOWS = os.name == "nt"
_GB = 1024**3
_OUTPUT_LIMIT = 4096  # what a probe hands back
_READ_LIMIT = 1024 * 1024  # what is read of a command's output before it is cut down to that
_GAP = "\n[...]\n"
_KILL_WAIT_S = 5
_HARNESS_TIMEOUT_S = 180  # it starts the Copilot CLI through the SDK
_RUNTIME_MODES = ("copilot-user", "facilitator")  # the modes in which the harness can run for the console
SERVE_FILE = "serve.json"
SERVE_LOCK = "serve.lock"
_REPLACE_TRIES = 10  # Windows refuses os.replace while a reader has the target open
_REPLACE_WAIT_S = 0.05
# A refused connection takes about 2 s to be reported on Windows (the SYN is sent again): wait longer than that.
_CONNECT_TIMEOUT_S = 4.0
_ENV_REQUIRED = ("JIRA_EMAIL", "JIRA_API_TOKEN", "GH_TOKEN", "BRIDGE_GITHUB_REPO")
_ENV_ONE_OF = ("JIRA_BASE_URL", "JIRA_SITE")
_TRUE = frozenset({"true", "yes", "on", "1"})  # what git reads as a true boolean


@dataclass(frozen=True)
class Check:
    """One line of the doctor's list. ``ok`` False is a failure; ``warn`` marks an ``ok`` check that deserves a look.

    ``detail`` may span lines (the unit card URLs do). ``fix`` says what to do and is empty when nothing is needed.
    """

    name: str
    ok: bool
    detail: str
    fix: str = ""
    warn: bool = False

    @property
    def status(self) -> str:
        return "FAIL" if not self.ok else "WARN" if self.warn else "OK"


# --- running commands --------------------------------------------------------------------------------------------


def default_probe(
    argv: list[str], cwd: Path | None = None, timeout: float = 30, env: dict[str, str] | None = None
) -> tuple[int, str]:
    """Run ``argv`` and return ``(exit code, combined stdout and stderr cut to 4 KB)``; never raises.

    124: it did not finish within ``timeout`` seconds (it and what it started are killed). 127: it cannot be started.
    """
    with tempfile.TemporaryDirectory(prefix="console-doctor-", ignore_cleanup_errors=True) as folder:
        out = Path(folder) / "output.txt"
        try:
            proc = procs.spawn(list(argv), cwd, procs.run_env(os.environ, env or {}), out)  # type: ignore[arg-type]
        except OSError as e:
            return 127, f"cannot run {argv[0]}: {e.strerror or e}"
        timed_out = False
        try:
            code = proc.wait(timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
        finally:
            procs.kill_tree(proc)  # does nothing once the process has ended
        if timed_out:
            try:
                proc.wait(_KILL_WAIT_S)
            except subprocess.TimeoutExpired:
                pass
            return 124, f"{argv[0]} timed out after {timeout:g} s"
        with open(out, "rb") as f:
            data = f.read(_READ_LIMIT)
    return code, _clip(data.decode("utf-8", errors="replace"))


def _clip(text: str) -> str:
    """At most 4 KB: the first and the last half (a command prints its summary first and its error last)."""
    if len(text) <= _OUTPUT_LIMIT:
        return text
    keep = (_OUTPUT_LIMIT - len(_GAP)) // 2
    return text[:keep] + _GAP + text[-keep:]


_CONTROL = re.compile(r"[\x00-\x1f\x7f]")
_CONTROL_BUT_NEWLINE = re.compile(r"[\x00-\x09\x0b-\x1f\x7f]")


def _clean(text: str, keep_newlines: bool = False) -> str:
    """``text`` with each control character (escape codes, tabs, bells ...) turned into a space; with
    ``keep_newlines`` a newline stays and a carriage return goes. For everything a command or an error printed."""
    if keep_newlines:
        return _CONTROL_BUT_NEWLINE.sub(" ", text.replace("\r", ""))
    return _CONTROL.sub(" ", text)


def _last_line(output: str, limit: int = 200) -> str:
    lines = [line.strip() for line in output.splitlines() if line.strip()]
    return _clean(lines[-1])[:limit] if lines else "no output"


# --- the checks --------------------------------------------------------------------------------------------------


def _check_python(cfg: ConsoleConfig, probe: Probe) -> Check:
    version = platform.python_version()
    if sys.version_info[:2] >= MIN_PYTHON:
        return Check("python", True, f"{version} ({sys.executable})")
    want = ".".join(map(str, MIN_PYTHON))
    return Check(
        "python", False, f"{version} is too old: the console needs Python {want} or newer",
        f"install Python {want} or newer (python.org) and start the console with it",
    )  # fmt: skip


def _check_git(cfg: ConsoleConfig, probe: Probe) -> Check:
    code, out = probe(["git", "--version"])
    if code != 0:
        return Check(
            "git", False, f"git does not run ({_last_line(out)})",
            "install Git for Windows (https://git-scm.com/download/win), then open a new terminal",
        )  # fmt: skip
    version = _last_line(out)
    code, out = probe(["git", "config", "--global", "--get", "worktree.useRelativePaths"])
    if code == 0 and out.strip().lower() in _TRUE:
        return Check(
            "git", True, f"{version}; worktree.useRelativePaths is on in the global git config",
            "git config --global worktree.useRelativePaths false", warn=True,
        )  # fmt: skip
    return Check("git", True, version)


def _check_bash(cfg: ConsoleConfig, probe: Probe) -> Check:
    from harness import envdetect  # the harness checkout is on sys.path since load_config

    bash = envdetect.find_bash("", os.environ)
    if bash:
        return Check("bash", True, str(bash))
    return Check(
        "bash", False, "no bash found (the harness runs its gate scripts with it)",
        "install Git for Windows, it brings Git Bash (the bash of WSL is not used), or set HARNESS_BASH to a bash",
    )  # fmt: skip


def _check_docker(cfg: ConsoleConfig, probe: Probe) -> Check:
    code, out = probe(["docker", "info"])
    if code == 0:
        version = re.search(r"Server Version:\s*(\S+)", out)
        return Check("docker", True, f"the engine answers (server {version[1]})" if version else "the engine answers")
    if cfg.personal:   # the workshop runs without Docker: nothing to start
        return Check("docker", True, "not needed: the gates run without Docker (zig cc)")
    return Check(
        "docker", False, f"docker info failed ({_last_line(out)})",
        "start Docker Desktop and wait until it says it is running (install it first if it is missing)",
    )  # fmt: skip


_MODE = re.compile(r'"runtime_mode":\s*"([A-Za-z-]{1,32})"')
_REPORT_OK = re.compile(r'"ok":\s*(true|false)')
_REPORT_CHECK = re.compile(r'"name":\s*"([^"\\]{1,60})",\s*"status":\s*"([A-Z]{2,8})"')


def _check_harness(cfg: ConsoleConfig, probe: Probe) -> Check:
    """``python -m harness doctor --json`` in the harness repo. Read with patterns, not a JSON parser: the probe cuts
    the output to 4 KB (the report is longer), and only ``ok``, ``runtime_mode`` and each check's name and status
    (all near the start of their parts) are wanted. No detail of the harness is repeated here."""
    # a lite personal console: no Docker for the harness doctor (it would build the image); a full one: its TS_LITE
    extra = ladder.env(cfg.ladder) if cfg.personal else {}
    argv, cwd = [*cfg.harness_argv, "doctor", "--json"], cfg.harness_repo
    code, out = probe(argv, cwd, _HARNESS_TIMEOUT_S, env=extra) if extra else probe(argv, cwd, _HARNESS_TIMEOUT_S)
    rerun = f"run `python -m harness doctor` in {cfg.harness_repo} and fix what it reports"
    mode, ok = _MODE.search(out), _REPORT_OK.search(out)
    if not mode or not ok:
        why = "it timed out" if code == 124 else f"exit {code}"
        return Check("harness runtime", False, f"the harness doctor gave no report ({why})", rerun)
    attention = ", ".join(f"{n} ({s})" for n, s in _REPORT_CHECK.findall(out) if s not in ("OK", "SKIP"))
    if ok[1] == "true" and mode[1] in _RUNTIME_MODES:
        return Check("harness runtime", True, f"runtime_mode {mode[1]}")
    if ok[1] != "true":
        detail = f"the harness doctor reports a failed check (runtime_mode {mode[1]})"
    else:
        detail = f"runtime_mode is {mode[1]}, expected {' or '.join(_RUNTIME_MODES)}"
    if attention:
        detail += f"; needs attention: {attention}"
    return Check("harness runtime", False, detail, rerun)


def _check_env(cfg: ConsoleConfig, probe: Probe) -> Check:
    path = cfg.env_file
    names_wanted = ", ".join((*_ENV_REQUIRED, " or ".join(_ENV_ONE_OF)))
    if not path.exists():
        return Check(
            ".env names", False, f"{path} not found", f"create it with a NAME=value line for each of: {names_wanted}"
        )
    try:
        secrets = Secrets.from_file(path)
    except (OSError, ValueError) as e:  # the message of a decoding error can quote the file: the type is enough
        return Check(
            ".env names", False, f"{path} cannot be read ({type(e).__name__})", "make it a readable UTF-8 text file"
        )
    names = set(secrets.names())

    def state(name: str) -> str:  # only whether a value is there is looked at; it goes nowhere
        return "missing" if name not in names else "empty" if not secrets.get(name) else "set"

    missing = [n for n in _ENV_REQUIRED if state(n) == "missing"]
    empty = [n for n in _ENV_REQUIRED if state(n) == "empty"]
    group = [state(n) for n in _ENV_ONE_OF]
    if "set" not in group:
        (empty if "empty" in group else missing).append(" or ".join(_ENV_ONE_OF))
    if not missing and not empty:
        return Check(".env names", True, "every required name is set (values are not looked at)")
    parts = [f"{label}: {', '.join(names)}" for label, names in (("missing", missing), ("empty", empty)) if names]
    return Check(".env names", False, "; ".join(parts), f"put a NAME=value line for each of these names in {path}")


def _check_source(cfg: ConsoleConfig, probe: Probe) -> Check:
    try:
        check_source(cfg.source_repo, referee=cfg.personal)
    except (RuntimeError, OSError) as e:  # LeakError is a RuntimeError
        return Check(
            "source export", False, _clean(str(e))[:400],
            "build a participant export without what is listed (ticket branches, golden tags, hidden-tests) and "
            "set source_repo in console.yaml to it",
        )  # fmt: skip
    return Check("source export", True, f"{cfg.source_repo.name} is a clean participant export")


def _check_units(cfg: ConsoleConfig, probe: Probe) -> Check:
    store = UnitStore(cfg)
    try:
        units = store.all()
    except RuntimeError:  # no units.json yet
        if cfg.personal:
            return Check("units", True, "unit-01 is not built yet: serve --personal builds it on its first start",
                         "python -m console serve --personal", warn=True)  # fmt: skip
        return Check("units", False, "no units yet", "python -m console units init")
    except (OSError, ValueError, KeyError) as e:  # units.json is damaged
        return Check(
            "units", False, f"units.json cannot be read ({type(e).__name__})",
            "python -m console units init --force (rebuilds every unit and discards their work)",
        )  # fmt: skip
    problems, running = [], []
    for unit in units:
        if unit.locked_by and unit.locked_by.startswith(NEEDS_RESET):
            run = unit.locked_by.removeprefix(NEEDS_RESET)
            problems.append(f"{unit.id}: needs a reset (run {run} could not put its worktree back)")
            continue
        try:  # through the store: git is pointed at the unit by name, never by the .git file an agent can rewrite
            branch = store.git(unit.id, "rev-parse", "--abbrev-ref", "HEAD").strip()
            status = store.git(unit.id, "--no-optional-locks", "status", "--porcelain")  # leave the run's index alone
        except (RuntimeError, OSError):
            problems.append(f"{unit.id}: git cannot read its worktree")
            continue
        changes = [line for line in status.splitlines() if line.strip()]
        if branch != unit.branch:
            where = "detached HEAD" if branch == "HEAD" else f"on {branch}"
            problems.append(f"{unit.id}: {where}, expected {unit.branch}")
        elif changes and unit.locked_by:  # a run is writing in it: not a fault
            running.append(f"{unit.id}: run in progress ({len(changes)} uncommitted change(s) are expected)")
        elif changes:
            problems.append(f"{unit.id}: {len(changes)} uncommitted change(s)")
    if problems:
        return Check(
            "units", False, "; ".join([*problems, *running]),
            "commit or discard the changes in those worktrees, reset a unit that needs it, or rebuild every unit "
            "with `python -m console units init --force` (that discards all unit work)",
        )  # fmt: skip
    if running:
        return Check(
            "units", True, f"{len(units)} units; " + "; ".join(running),
            "run the doctor again when those runs have finished", warn=True,
        )  # fmt: skip
    return Check("units", True, f"{len(units)} units, every worktree clean on its unit/NN branch")


def _check_monaco(cfg: ConsoleConfig, probe: Probe) -> Check:
    spec = importlib.util.spec_from_file_location("vendor_monaco", VENDOR_SCRIPT)
    vendor = importlib.util.module_from_spec(spec)  # type: ignore[arg-type]
    spec.loader.exec_module(vendor)  # type: ignore[union-attr]
    problems = vendor.check(MONACO_DIR)  # the script knows its own version and which files must be there
    if not problems:
        return Check("monaco", True, f"monaco-editor {vendor.VERSION} is installed")
    return Check(
        "monaco", False, "; ".join(problems)[:400],
        "python scripts/vendor_monaco.py (downloads the editor once; it is not in the repository)",
    )  # fmt: skip


def _port_problem(host: str, port: int) -> str | None:
    """``None`` when a server could bind ``host:port`` (the way ``make_server`` does), else why not."""
    host = host or "0.0.0.0"
    try:
        with socket.socket(socket.AF_INET6 if ":" in host else socket.AF_INET, socket.SOCK_STREAM) as s:
            if not _WINDOWS:  # as the server: on Windows this would let a second console share the port
                s.setsockopt(socket.SOL_SOCKET, socket.SO_REUSEADDR, 1)
            s.bind((host, port))
    except OSError as e:
        return e.strerror or str(e)
    return None


def console_answers(host: str, port: int, timeout: float = 1.0) -> bool:
    """True when a Harness Console server answers on ``host:port`` (a wildcard ``host`` is asked on loopback).

    Any other program on that port, or nothing, gives False. What a console is known by: ``GET /api/me`` is a JSON
    object with ``subject`` and ``unit``. This says "yes" when sure; a busy console may fail to answer, so for "is it
    safe to rebuild the units" ask :func:`console_may_be_running` instead.
    """
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    netloc = f"[{target}]" if ":" in target and not target.startswith("[") else target
    opener = urllib.request.build_opener(urllib.request.ProxyHandler({}))  # a proxy must not stand in for the console
    try:
        with opener.open(f"http://{netloc}:{port}/api/me", timeout=timeout) as reply:
            body = json.loads(reply.read(4096))
    except (OSError, ValueError, http.client.HTTPException):  # no answer, an error status, not JSON, not HTTP
        return False
    return isinstance(body, dict) and "subject" in body and "unit" in body


def write_serve_record(data_dir: Path, host: str, port: int) -> None:
    """Leave ``<data_dir>/serve.json``: who serves and where. ``serve`` calls this once its socket is bound.

    The file is replaced whole (written next to it, then renamed), so a reader sees the old or the new one.
    """
    data_dir = Path(data_dir)
    data_dir.mkdir(parents=True, exist_ok=True)
    record = {"pid": os.getpid(), "started": procs.start_time(os.getpid()), "host": host, "port": port}
    fd, tmp = tempfile.mkstemp(dir=data_dir, prefix="serve.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(record, f, allow_nan=False)
            f.write("\n")
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, data_dir / SERVE_FILE)
                return
            except PermissionError:  # Windows: a reader has the file open this instant
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT_S)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def remove_serve_record(data_dir: Path) -> None:
    """Remove ``serve.json`` if this process wrote it (a later console's record stays). Quiet when it is gone."""
    path = Path(data_dir) / SERVE_FILE
    try:
        record = json.loads(path.read_text(encoding="utf-8"))
        if isinstance(record, dict) and record.get("pid") == os.getpid():
            path.unlink()
    except (OSError, ValueError):
        pass


def _is_number(value: object) -> bool:
    return isinstance(value, (int, float)) and not isinstance(value, bool)


def _live_record(text: str) -> tuple[int, str, int] | None:
    """``(pid, host, port)`` of a ``serve.json`` whose process still runs and is the one that wrote it, else ``None``
    (a process that is gone, a pid that is another process now, or a file that makes no sense)."""
    try:
        record = json.loads(text)
        pid, started, host, port = record["pid"], record["started"], record["host"], record["port"]
    except (ValueError, KeyError, TypeError):
        return None
    if not (isinstance(pid, int) and isinstance(port, int) and isinstance(host, str)):
        return None
    if isinstance(pid, bool) or isinstance(port, bool) or not (started is None or _is_number(started)):
        return None
    return (pid, host, port) if procs.alive(pid, started) else None


def _nothing_listens(host: str, port: int) -> bool:
    """True only when a connection to ``host:port`` is refused for every address it names (a wildcard ``host`` is
    asked on loopback) and a server could bind it. A timeout, a reset, a hang-up, an answer, or a port that cannot
    be bound all mean that something may be there."""
    target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
    try:
        addresses = socket.getaddrinfo(target, port, type=socket.SOCK_STREAM)
    except OSError:
        return False
    for family, kind, proto, _, address in addresses:
        try:
            with socket.socket(family, kind, proto) as s:
                s.settimeout(_CONNECT_TIMEOUT_S)
                s.connect(address)
        except ConnectionRefusedError:
            continue
        except OSError:
            return False
        return False  # connected: something listens
    return _port_problem(host, port) is None


class ServeLock:
    """The exclusive lock of ``<data_dir>/serve.lock``: one ``serve --personal`` per data folder (so per checkout).

    An OS lock on an open file (``msvcrt.locking`` on Windows, ``flock`` elsewhere), held until ``release`` or until the
    process ends, however it ends: a console that crashed or was killed leaves no lock behind. Each ``acquire`` opens
    the file anew, so a second ``ServeLock`` does not get it either, in another process or in this one.
    """

    def __init__(self, data_dir: Path) -> None:
        self.path = Path(data_dir) / SERVE_LOCK
        self._fd: int | None = None

    def acquire(self) -> bool:
        """Take the lock now, without waiting: True when this ``ServeLock`` holds it, False when another one does."""
        if self._fd is not None:
            return True
        self.path.parent.mkdir(parents=True, exist_ok=True)
        fd = os.open(self.path, os.O_RDWR | os.O_CREAT | getattr(os, "O_BINARY", 0), 0o600)
        try:
            _lock_file(fd)
        except (BlockingIOError, PermissionError):  # held: flock says EWOULDBLOCK, msvcrt EACCES
            os.close(fd)
            return False
        except BaseException:
            os.close(fd)
            raise
        self._fd = fd
        return True

    def release(self) -> None:
        """Give the lock up (nothing when it is not held)."""
        fd, self._fd = self._fd, None
        if fd is None:
            return
        try:
            _unlock_file(fd)
        except OSError:
            pass  # closing the file releases it too
        finally:
            os.close(fd)


def _lock_file(fd: int) -> None:
    if _WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_NBLCK, 1)  # the first byte; a lock may lie past the end of the file
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_EX | fcntl.LOCK_NB)  # per open file, so per ServeLock, not per process


def _unlock_file(fd: int) -> None:
    if _WINDOWS:
        import msvcrt

        os.lseek(fd, 0, os.SEEK_SET)
        msvcrt.locking(fd, msvcrt.LK_UNLCK, 1)
    else:
        import fcntl

        fcntl.flock(fd, fcntl.LOCK_UN)


def serving_console(data_dir: Path) -> tuple[str, int] | None:
    """``(host, port)`` of the console that ``<data_dir>/serve.json`` names, while that process runs and something
    listens on its address; else ``None``. ``serve --personal`` asks it when another start holds the folder's lock
    (``ServeLock``; setup run twice): it points to that console instead of serving the same unit a second time."""
    try:
        text = (Path(data_dir) / SERVE_FILE).read_bytes().decode("utf-8", errors="replace")
    except OSError:
        return None
    live = _live_record(text)
    if live is None or _nothing_listens(live[1], live[2]):
        return None
    return live[1], live[2]


def console_may_be_running(cfg: ConsoleConfig) -> str | None:
    """Why a console may be running (words for the person), or ``None`` when none is.

    The all-clear needs every address it knows to refuse a connection: ``cfg.host``, loopback, both on
    ``cfg.port``; and where ``serve.json`` of the data dir names a live process, the address it serves on (``serve
    --host/--port`` may differ from console.yaml). Unlike ``console_answers`` this does not need an answer to an HTTP
    request: a console at its connection cap, or one that hangs, still accepts the connection. A ``serve.json`` of a
    process that is gone is ignored and removed.
    """
    candidates: dict[tuple[str, int], tuple[str, str]] = {}  # (address to connect to, port) -> (host to bind, words)

    def add(host: str, port: int, words: str) -> None:
        target = "127.0.0.1" if host in ("", "0.0.0.0", "::") else host
        candidates.setdefault((target, port), (host, f"{words}: {target}:{port} does not refuse connections"))

    if cfg.port:
        add(cfg.host, cfg.port, "console.yaml")
        add("127.0.0.1", cfg.port, "console.yaml")
    path = cfg.data_dir / SERVE_FILE
    try:
        text = path.read_bytes().decode("utf-8", errors="replace")
    except FileNotFoundError:
        pass
    except OSError:  # a console is replacing it this instant, or it cannot be read: be careful
        return f"{path} cannot be read"
    else:
        live = _live_record(text)
        if live is None:
            with contextlib.suppress(OSError):
                path.unlink()
        else:
            add(live[1], live[2], f"{SERVE_FILE} says a console (pid {live[0]}) serves")
    if not candidates:
        return None
    asked = [(host, port) for (_, port), (host, _) in candidates.items()]
    with ThreadPoolExecutor(len(asked)) as pool:  # a refusal takes 2 s on Windows: ask all at once
        verdicts = list(pool.map(lambda address: _nothing_listens(*address), asked))
    for (_, why), nothing in zip(candidates.values(), verdicts):
        if not nothing:
            return why
    return None


def _check_port(cfg: ConsoleConfig, probe: Probe) -> Check:
    problem = _port_problem(cfg.host, cfg.port)
    if problem is None:
        return Check("port", True, f"port {cfg.port} is free")
    if console_answers(cfg.host, cfg.port):
        return Check("port", True, f"port {cfg.port}: the console is already running here")
    if cfg.personal:
        return Check("port", True, f"port {cfg.port} cannot be used ({problem}): serve --personal takes a free one "
                     "and prints its address", warn=True)  # fmt: skip
    who = f"netstat -ano | findstr :{cfg.port}" if _WINDOWS else f"ss -ltnp | grep :{cfg.port}"
    return Check(
        "port", False, f"port {cfg.port} cannot be used ({problem}) and no console answers on it",
        f"stop the program that holds the port (find it with `{who}`) or set another `port:` in console.yaml",
    )  # fmt: skip


def _covers(value: str, port: int) -> bool:
    """Whether a ``LocalPort`` value of netsh (``Any``, ``8765``, ``80,8765``, ``8000-9000``) includes ``port``."""
    for part in (p.strip() for p in value.split(",")):
        if part.lower() == "any":
            return True
        low, dash, high = part.partition("-")
        if low.isdigit() and (not dash or high.isdigit()) and int(low) <= port <= int(high or low):
            return True
    return False


def _field(block: str, name: str) -> str | None:
    found = re.search(rf"^\s*{name}:\s*(.+?)\s*$", block, re.MULTILINE)
    return found[1] if found else None


def _rule_faults(block: str, port: int) -> list[tuple[str, str]]:
    """What is wrong with one rule of netsh's English output: ``(in words, the setting of ``netsh set rule``)``.

    Only a field that is there counts, so output in another language (no such fields) passes on the exit code.
    """
    faults = []
    enabled, action = _field(block, "Enabled"), _field(block, "Action")
    if enabled is not None and enabled.lower() != "yes":
        faults.append(("is disabled", "enable=yes"))
    if action is not None and action.lower() != "allow":
        faults.append((f"does not allow ({action})", "action=allow"))
    ports = re.findall(r"^\s*LocalPort:\s*(.+?)\s*$", block, re.MULTILINE)
    if ports and not any(_covers(value, port) for value in ports):
        faults.append((f"lets in port {', '.join(ports)}, not {port}", f"localport={port}"))
    return faults


def _check_firewall(cfg: ConsoleConfig, probe: Probe) -> Check:
    if not _WINDOWS:
        return Check("firewall", True, "skipped: the Windows firewall rule is only checked on Windows", warn=True)
    rule = f'name="{FIREWALL_RULE}"'
    add = f"netsh advfirewall firewall add rule {rule} dir=in action=allow protocol=TCP localport={cfg.port}"
    admin = "(the fix needs an administrator shell)"
    code, out = probe(["netsh", "advfirewall", "firewall", "show", "rule", f"name={FIREWALL_RULE}"])
    if code != 0:
        detail = f'no firewall rule "{FIREWALL_RULE}": other computers cannot reach port {cfg.port} {admin}'
        return Check("firewall", False, detail, add)
    starts = [m.start() for m in re.finditer(r"^\s*Rule Name:", out, re.MULTILINE)]
    blocks = [out[a:b] for a, b in zip(starts, [*starts[1:], len(out)])] or [out]  # one block per rule of that name
    faults = [_rule_faults(block, cfg.port) for block in blocks]
    if all(faults):  # no rule that is on, allows, and covers the port
        words = " and ".join(what for what, _ in faults[0])
        settings = " ".join(setting for _, setting in faults[0])
        return Check(
            "firewall", False, f'the rule "{FIREWALL_RULE}" {words} {admin}',
            f"netsh advfirewall firewall set rule {rule} new {settings}",
        )  # fmt: skip
    return Check("firewall", True, f'rule "{FIREWALL_RULE}" is in place')


def _is_loopback(name: str) -> bool:
    return name in ("localhost", "[::1]", "::1") or name.startswith("127.")


def room_urls(cfg: ConsoleConfig) -> list[str]:
    """The addresses a browser on the room network can open (the route to the outside first); none for a loopback bind."""
    return [u for u in lan_urls(cfg.host, cfg.port) if not _is_loopback(u.removeprefix("http://").rsplit(":", 1)[0])]


def _check_lan(cfg: ConsoleConfig, probe: Probe) -> Check:
    urls = room_urls(cfg)
    if not urls:
        if _is_loopback(cfg.host):
            return Check(
                "lan addresses", False, f"host is {cfg.host} in console.yaml: only this computer can connect",
                "set host: 0.0.0.0 in console.yaml",
            )  # fmt: skip
        return Check(
            "lan addresses", False, "this computer has no network address besides loopback",
            "connect this laptop to the room network (Wi-Fi or cable) and run the doctor again",
        )  # fmt: skip
    accepted = ", ".join(sorted(allowed_hosts(cfg.host, cfg)))
    note = "unit cards: use the address on the room network (usually the first)"
    return Check("lan addresses", True, "\n".join([*urls, note, f"accepted Host names: {accepted}"]))


def _check_disk(cfg: ConsoleConfig, probe: Probe) -> Check:
    where = cfg.data_dir
    while not where.exists() and where != where.parent:  # the data dir is made by `units init`: ask its nearest parent
        where = where.parent
    free = shutil.disk_usage(where).free
    detail = f"{free / _GB:.1f} GB free on the disk of {where}"
    if free >= MIN_FREE_GB * _GB:
        return Check("disk space", True, detail)
    return Check(
        "disk space", False, f"{detail}; at least {MIN_FREE_GB} GB are needed (clones, worktrees, build output, logs)",
        "free some space (old exports and run logs; `docker system prune` for old images) or set data_dir in "
        "console.yaml to a bigger disk",
    )  # fmt: skip


_CHECKS: tuple[tuple[str, Callable[[ConsoleConfig, Probe], Check]], ...] = (
    ("python", _check_python),
    ("git", _check_git),
    ("bash", _check_bash),
    ("docker", _check_docker),
    ("harness runtime", _check_harness),
    (".env names", _check_env),
    ("source export", _check_source),
    ("units", _check_units),
    ("monaco", _check_monaco),
    ("port", _check_port),
    ("firewall", _check_firewall),
    ("lan addresses", _check_lan),
    ("disk space", _check_disk),
)
_ROOM_ONLY = frozenset({".env names", "firewall", "lan addresses"})  # personal mode: no .env, no room network


def run_checks(cfg: ConsoleConfig, probe: Probe = default_probe) -> list[Check]:
    """Every check, in order (in personal mode not those of ``_ROOM_ONLY``). ``probe(argv, cwd=None, timeout=30)``
    runs a command: ``(exit code, output)``; the harness doctor of a personal console gets ``env=`` (``ladder.env``)
    when that adds anything (lite, or ``TS_LITE`` in full)."""
    if cfg.personal and not cfg.ladder:  # personal mode decides the ladder as the console's start does
        kind, why = ladder.decide(cfg.source_repo)
        cfg = replace(cfg, ladder=kind, ladder_why=why)
    results = []
    for name, check in _CHECKS:
        if cfg.personal and name in _ROOM_ONLY:
            continue
        try:
            results.append(check(cfg, probe))
        except Exception as e:  # one broken check must not hide the rest of the list
            log.debug("doctor check %s crashed", name, exc_info=True)
            message = _clean(str(e))[:200]
            detail = f"the check crashed: {type(e).__name__}: {message}"
            results.append(Check(name, False, detail, "run it again; if it keeps crashing, the console has a bug"))
    return results


# --- the table ---------------------------------------------------------------------------------------------------


def _ascii(text: str) -> str:
    """Text that any console prints: other characters as ``\\xNN`` escapes, control characters as spaces."""
    return _clean(text.encode("ascii", "backslashreplace").decode("ascii"), keep_newlines=True)


def format_report(checks: list[Check]) -> str:
    """``[OK] name  detail`` per check; a failure or warning that has a fix gets an indented ``fix:`` line below it."""
    width = max((len(c.name) for c in checks), default=0)
    indent = " " * (len("[FAIL]") + 1 + width + 2)  # where the detail starts
    lines: list[str] = []
    for c in checks:
        first, *more = _ascii(c.detail).splitlines() or [""]
        lines.append(f"{'[' + c.status + ']':<6} {c.name:<{width}}  {first}".rstrip())
        lines.extend(indent + line for line in more)
        if c.fix and c.status != "OK":
            first, *more = _ascii(c.fix).splitlines()
            lines.append(f"{' ' * 7}fix: {first}")
            lines.extend(" " * 12 + line for line in more)
    problems = sum(not c.ok for c in checks)
    warnings = sum(c.ok and c.warn for c in checks)
    tail = f" ({warnings} warning{'s' if warnings != 1 else ''})" if warnings else ""
    lines.append("")
    lines.append(
        f"{problems} problem{'s' if problems != 1 else ''} to fix{tail}" if problems else f"All checks passed{tail}"
    )
    return "\n".join(lines)
