"""The day's terminal commands on the Today page (personal mode): ``<checkout>/workshop/commands.json``, shown per
section and, unless a command needs a keyboard, run by the console with bash in the console's copy of the checkout (the
unit's worktree, ``.console-data/units/unit-01/repo``): the folder the Code, Tickets, Runs and "Check my code" pages
show, so whatever a command changes is on those pages too.

The file is made by another tool and read as it is on disk at every request (``read_commands``); the console never
writes it. Its shape::

    {"day": "20261120",
     "sections": {"kickoff": [{"id": "s6-2", "slide": 6, "slide_title": "...", "at": "09:06",
                               "cmd": "python -m harness ...", "interactive": false, "expect": "...",
                               "recorded": "recorded/s6-2.txt", "recorded_at": "19 Nov 2026",
                               "recorded_took": "3:41", "recorded_credits": "6.75"}], ...}}

A section key is the id of an ``AGENDA`` block of ``web/guide.js``. An entry needs ``id`` (``COMMAND_ID``: it goes
into a URL), ``cmd`` (text) and ``interactive`` (``true`` or ``false``); ``slide`` (a whole number), ``slide_title``,
``at``, ``expect``, ``what`` (what the command does), ``why`` (why it is run) and ``started_from`` (the page the presenter
starts it from instead, as in "the Tickets page (Run this ticket)": the Today page then offers no Run for it) (text) may be
left out, and so may ``chat`` (a list of texts: what to type, one after the other, into an interactive command that is a
chat; the page shows each with a Copy button); a top-level
``agenda`` (section -> {start, end}) gives the Today page its clock. Other keys are dropped, and so is an entry that breaks a rule or repeats
an ``id`` seen before. A missing, unreadable or malformed file is ``{"sections": {}}``: the Today page shows no
commands part then.

On the day nothing runs live on the stage: the presenter shows a command, then opens its output as it was recorded
beforehand. ``recorded`` (may be left out) names that output: a ``.txt`` file under the checkout's
``workshop/recorded/``, as a path relative to ``workshop/`` (``recorded/s6-2.txt``: forward slashes, starting with
``recorded/``, no ``..``, never absolute; ``_recorded_path``). A ``recorded`` that breaks a rule is left out (``""``),
not the entry. ``recorded_at`` (when it was recorded), ``recorded_took`` (how long it ran) and ``recorded_credits``
(what it cost) are text like the other fields. ``read_recorded`` gives the file's text, read at every request: the
first ``MAX_RECORDED`` bytes, and never a file outside ``workshop/recorded/`` (the resolved path is checked too).

``interactive: true`` means the command needs a keyboard in a real terminal (it asks y/n, it is a chat, it runs until
stopped): the page shows it and copies it, the console never runs it.

A run (``Jobs.start``) is ``bash -c <cmd>`` in the console's copy (``run_in``; ``NoCopy`` while it is not there): on
Windows Git for Windows' bash (``harness.envdetect``, as the doctor finds it), elsewhere ``bash``; never a login shell
(``-l`` changes the folder and PATH). The copy is the checkout as the console started it, without what an agent must
not read: no ``workshop/`` on a ticket branch, never ``hidden-tests/``. So the environment (``command_env``) is the
console's own with ``MSYS_NO_PATHCONV=1``, ``PYTHONUNBUFFERED=1``, the checkout's ``.venv`` (``Scripts`` on Windows,
else ``bin``) first on PATH, so ``python`` is the venv's, ``WORKSHOP`` the checkout's ``workshop/`` folder (a command
names a patch as ``"$WORKSHOP/patches/..."``) and ``HARNESS_HIDDEN_TESTS`` the checkout's ``hidden-tests/`` (the referee
an experiment started in the copy scores with; the agents' own environment never gets it: ``harness.runtime``
allowlists theirs). Paths are given with forward slashes (``C:/...`` on Windows): bash, git and Python all take that
form, also with ``MSYS_NO_PATHCONV=1``. ``workshop/stage-shell.sh`` gives a terminal the same folder and variables.
stdout and stderr are one stream, kept in memory (the
last ``MAX_OUTPUT`` bytes). The process gets a process group (Windows) or session (POSIX) of its own, so a stop
(``procs.kill_tree``: ``taskkill /T /F``, or the process group; on Windows also a job object, ``_WinJob``) takes
everything it started.

Several commands may run at once (a long experiment in the background while short gate commands run in front), at most
``MAX_RUNNING``, but one command (an ``id``) once at a time. Jobs live as long as the console: the last
``KEEP_JOBS`` are kept, and the console's end stops what still runs (``Jobs.shutdown``).
"""

from __future__ import annotations

import codecs
import itertools
import json
import logging
import os
import re
import shutil
import subprocess
import threading
from dataclasses import dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from console import procs

__all__ = [
    "COMMAND_ID", "COMMANDS_FILE", "JOB_ID", "KEEP_JOBS", "LIST_JOBS", "MAX_OUTPUT", "MAX_RECORDED", "MAX_RUNNING",
    "RECORDED_DIR", "AlreadyRunning", "Jobs", "NoBash", "NoCopy", "TooMany", "command_env", "read_commands",
    "read_recorded", "validate_commands",
]  # fmt: skip

log = logging.getLogger(__name__)

COMMANDS_FILE = Path("workshop") / "commands.json"
COMMAND_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
JOB_ID = re.compile(r"j[0-9]{1,9}")
SECTION_ID = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,63}")
MAX_FILE = 1024 * 1024  # a commands file larger than this is not read
MAX_OUTPUT = 200 * 1024  # the bytes of output a job keeps (the last ones)
MAX_RECORDED = 256 * 1024  # the bytes of a recorded result the page gets (the first ones)
RECORDED_DIR = "recorded"  # the folder of the recorded results, in the checkout's workshop/
MAX_RUNNING = 4  # jobs running at once
KEEP_JOBS = 50  # jobs kept in memory, running ones always
LIST_JOBS = 20  # jobs ``list`` gives: the running ones first, then the most recent that ended
_STOP_WAIT_S = 5.0  # how long a stop waits for the job's output to end
_CHAT_MAX, _CHAT_TEXT_MAX = 5, 500  # "chat": messages to type into an interactive command
_TEXT_FIELDS = ("title", "slide_title", "at", "expect", "what", "why", "started_from", "recorded_at", "recorded_took",
                "recorded_credits")
_RECORDED_PART = re.compile(r"[A-Za-z0-9_][A-Za-z0-9._-]{0,127}")  # a folder or file name under recorded/
_MAX_RECORDED_PATH = 512
_ANSI = re.compile(r"\x1b\[[0-9;?]*[ -/]*[@-~]|\x1b\][^\x07\x1b]*(?:\x07|\x1b\\)|\x1b[@-Z\\-_]")


def _now() -> str:
    return datetime.now(timezone.utc).isoformat(timespec="seconds")


# --- the file ---------------------------------------------------------------------------------------------------------


def _entry(raw: Any) -> dict | None:
    """One command as the page gets it, or ``None`` when it breaks a rule."""
    if not isinstance(raw, dict):
        return None
    cid, cmd, interactive = raw.get("id"), raw.get("cmd"), raw.get("interactive")
    if not isinstance(cid, str) or not COMMAND_ID.fullmatch(cid):
        return None
    if not isinstance(cmd, str) or not cmd.strip() or "\x00" in cmd or not isinstance(interactive, bool):
        return None
    entry: dict[str, Any] = {"id": cid, "cmd": cmd, "interactive": interactive}
    slide = raw.get("slide")
    if slide is not None:
        if isinstance(slide, bool) or not isinstance(slide, int):
            return None
        entry["slide"] = slide
    for name in _TEXT_FIELDS:
        value = raw.get(name)
        if value is not None and not isinstance(value, str):
            return None
        entry[name] = value or ""
    entry["recorded"] = _recorded_path(raw.get("recorded"))  # one that breaks a rule is left out, not the entry
    entry["chat"] = _chat(raw.get("chat"))
    entry.setdefault("slide", None)
    return entry


def _chat(value: Any) -> list[str]:
    """``chat`` as the page gets it: the messages to type into an interactive command (a chat), in order; at most
    ``_CHAT_MAX`` texts of at most ``_CHAT_TEXT_MAX`` characters each. Anything else is ``[]``."""
    if not isinstance(value, list) or len(value) > _CHAT_MAX:
        return []
    if not all(isinstance(text, str) and text.strip() and len(text) <= _CHAT_TEXT_MAX and "\x00" not in text for text in value):
        return []
    return list(value)


def _recorded_path(value: Any) -> str:
    """``recorded`` as the page gets it: a path relative to ``workshop/`` that starts with ``recorded/``, ends ``.txt``
    and has only plain names (``_RECORDED_PART``: no ``..``, no ``\\``, no drive, not absolute); ``""`` otherwise."""
    if not isinstance(value, str) or len(value) > _MAX_RECORDED_PATH or ".." in value or not value.endswith(".txt"):
        return ""
    first, *rest = value.split("/")
    if first != RECORDED_DIR or not rest or not all(_RECORDED_PART.fullmatch(part) for part in rest):
        return ""
    return value


def validate_commands(data: Any) -> dict:
    """The file's content as the page gets it: ``{"day", "sections": {section: [entry, ...]}}`` with what breaks a rule
    left out (see the module docstring). Anything that is not such an object is ``{"sections": {}}``."""
    if not isinstance(data, dict) or not isinstance(data.get("sections"), dict):
        return {"sections": {}}
    seen: set[str] = set()
    sections: dict[str, list[dict]] = {}
    for key, entries in data["sections"].items():
        if not isinstance(key, str) or not SECTION_ID.fullmatch(key) or not isinstance(entries, list):
            continue
        kept = []
        for raw in entries:
            entry = _entry(raw)
            if entry is None or entry["id"] in seen:
                continue
            seen.add(entry["id"])
            kept.append(entry)
        sections[key] = kept
    result: dict[str, Any] = {"sections": sections}
    if isinstance(data.get("day"), str):
        result["day"] = data["day"]
    agenda = _agenda(data.get("agenda"))
    if agenda:
        result["agenda"] = agenda
    return result


_HHMM = re.compile(r"\d\d:\d\d")


def _agenda(raw: Any) -> dict[str, dict[str, str]]:
    """The deck plan's section times (``{section: {"start": "HH:MM", "end": "HH:MM"}}``): the clock of the Today page.
    A section id that breaks ``SECTION_ID`` or a time that is not ``HH:MM`` is left out."""
    if not isinstance(raw, dict):
        return {}
    kept: dict[str, dict[str, str]] = {}
    for key, times in raw.items():
        if not isinstance(key, str) or not SECTION_ID.fullmatch(key) or not isinstance(times, dict):
            continue
        start, end = times.get("start"), times.get("end")
        if isinstance(start, str) and isinstance(end, str) and _HHMM.fullmatch(start) and _HHMM.fullmatch(end):
            kept[key] = {"start": start, "end": end}
    return kept


def read_commands(checkout: Path) -> dict:
    """``<checkout>/workshop/commands.json`` validated (``validate_commands``); ``{"sections": {}}`` when it is missing,
    too large, unreadable or not JSON."""
    path = Path(checkout) / COMMANDS_FILE
    try:
        if path.stat().st_size > MAX_FILE:
            log.warning("%s is larger than %s bytes: not read", COMMANDS_FILE.as_posix(), MAX_FILE)
            return {"sections": {}}
        data = json.loads(path.read_text(encoding="utf-8-sig"))
    except FileNotFoundError:
        return {"sections": {}}
    except (OSError, ValueError, RecursionError):
        log.warning("%s cannot be read as JSON: no commands on Today", COMMANDS_FILE.as_posix(), exc_info=True)
        return {"sections": {}}
    return validate_commands(data)


def find_command(checkout: Path, command_id: str) -> dict | None:
    """The command with this id in the file as it is on disk now, or ``None``."""
    for entries in read_commands(checkout)["sections"].values():
        for entry in entries:
            if entry["id"] == command_id:
                return entry
    return None


def read_recorded(checkout: Path, command: dict) -> dict | None:
    """The recorded result of ``command`` (an entry of ``read_commands``), read from disk now: ``{id, recorded, text,
    truncated, bytes, max_bytes}``. ``text`` is the first ``MAX_RECORDED`` (``max_bytes``) bytes as the page shows
    output (``_text``: UTF-8, line ends as ``\\n``, no terminal colours), ``truncated`` says the file is longer,
    ``bytes`` is its size. ``None`` when the command names none, when the file is not there, or when its real path is
    not under ``workshop/recorded/`` (a link out of it)."""
    rel = _recorded_path(command.get("recorded"))
    if not rel:
        return None
    try:
        workshop_dir = (Path(checkout) / COMMANDS_FILE.parent).resolve()
        path = (workshop_dir / rel).resolve()
        if not path.is_relative_to(workshop_dir / RECORDED_DIR) or not path.is_file():
            return None
        with path.open("rb") as f:
            size = os.fstat(f.fileno()).st_size
            data = f.read(MAX_RECORDED + 1)
    except OSError:
        return None
    truncated = len(data) > MAX_RECORDED or size > MAX_RECORDED
    decoder = codecs.getincrementaldecoder("utf-8-sig")(errors="replace")  # a cut in a character drops that character
    text = decoder.decode(data[:MAX_RECORDED], final=not truncated)
    return {"id": command["id"], "recorded": rel, "text": _clean(text), "truncated": truncated,
            "bytes": max(size, len(data)), "max_bytes": MAX_RECORDED}  # fmt: skip


# --- running ----------------------------------------------------------------------------------------------------------


class AlreadyRunning(Exception):
    """This command runs already."""


class TooMany(Exception):
    """``MAX_RUNNING`` commands run already."""


class NoBash(Exception):
    """No bash was found to run a command with."""


class NoCopy(Exception):
    """The console's copy, the folder a command runs in, is not there."""


def _find_bash() -> str | None:
    """Git for Windows' bash on Windows (never WSL's), ``bash`` elsewhere: as the harness doctor finds it."""
    try:
        from harness import envdetect  # the checkout's harness: on sys.path since load_config
    except ImportError:
        found = shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    return envdetect.find_bash("", os.environ)


def command_env(checkout: Path, base: dict[str, str] | None = None) -> dict[str, str]:
    """The environment of a command: the console's own, ``MSYS_NO_PATHCONV=1``, ``PYTHONUNBUFFERED=1``, the checkout's
    ``.venv`` first on PATH when it is there, ``WORKSHOP`` (the checkout's ``workshop/``) and, when the checkout carries
    the referee and the environment names none, ``HARNESS_HIDDEN_TESTS`` (its ``hidden-tests/``)."""
    checkout = Path(checkout).resolve()
    env = dict(os.environ if base is None else base)
    path_key = next((name for name in env if name.upper() == "PATH"), "PATH")
    venv = checkout / ".venv" / ("Scripts" if os.name == "nt" else "bin")
    if venv.is_dir():
        env[path_key] = os.pathsep.join(p for p in (str(venv), env.get(path_key, "")) if p)
    env["MSYS_NO_PATHCONV"] = "1"
    env["PYTHONUNBUFFERED"] = "1"
    env["WORKSHOP"] = (checkout / "workshop").as_posix()
    referee = checkout / "hidden-tests"
    if (referee / "run.sh").is_file() and not any(name.upper() == "HARNESS_HIDDEN_TESTS" for name in env):
        env["HARNESS_HIDDEN_TESTS"] = referee.as_posix()
    return env


class _WinJob:
    """A Windows job object holding a command and everything it starts (Windows only).

    ``taskkill /T`` follows parent pids, and an MSYS program that bash started (``sleep``, ``cat``) can have a parent
    that is gone already: it outlives the tree kill and holds the output pipe. A job object takes every descendant, so a
    stop ends them too (``terminate``). The process is created suspended, put in the job, then resumed: nothing it starts
    escapes. Closing the job (``close``, or the console's end) ends what is still in it.
    """

    # No BREAKAWAY_OK: MSYS leaves a job when it may (its sleep outlived a stop then). A program that insists on leaving
    # (CREATE_BREAKAWAY_FROM_JOB) cannot start in a command; the harness asks for no such thing.
    _KILL_ON_JOB_CLOSE = 0x2000
    _EXTENDED_LIMITS = 9

    def __init__(self) -> None:
        import ctypes
        from ctypes import wintypes

        self._ctypes = ctypes
        self._k32 = ctypes.WinDLL("kernel32", use_last_error=True)
        self._k32.CreateJobObjectW.restype = wintypes.HANDLE
        self._k32.CreateJobObjectW.argtypes = [wintypes.LPVOID, wintypes.LPCWSTR]
        self._k32.SetInformationJobObject.argtypes = [wintypes.HANDLE, ctypes.c_int, wintypes.LPVOID, wintypes.DWORD]
        self._k32.AssignProcessToJobObject.argtypes = [wintypes.HANDLE, wintypes.HANDLE]
        self._k32.TerminateJobObject.argtypes = [wintypes.HANDLE, wintypes.UINT]
        self._k32.CloseHandle.argtypes = [wintypes.HANDLE]
        self._ntdll = ctypes.WinDLL("ntdll")
        self._ntdll.NtResumeProcess.argtypes = [wintypes.HANDLE]
        handle = self._k32.CreateJobObjectW(None, None)
        if not handle:
            raise OSError(ctypes.get_last_error(), "CreateJobObject failed")
        self.handle = handle

        class Basic(ctypes.Structure):
            _fields_ = [("user_time", ctypes.c_int64), ("job_time", ctypes.c_int64), ("flags", wintypes.DWORD),
                        ("min_ws", ctypes.c_size_t), ("max_ws", ctypes.c_size_t), ("processes", wintypes.DWORD),
                        ("affinity", ctypes.c_size_t), ("priority", wintypes.DWORD), ("scheduling", wintypes.DWORD)]  # fmt: skip

        class Extended(ctypes.Structure):
            _fields_ = [("basic", Basic), ("io", ctypes.c_uint64 * 6), ("process_memory", ctypes.c_size_t),
                        ("job_memory", ctypes.c_size_t), ("peak_process", ctypes.c_size_t),
                        ("peak_job", ctypes.c_size_t)]  # fmt: skip

        info = Extended()
        info.basic.flags = self._KILL_ON_JOB_CLOSE
        if not self._k32.SetInformationJobObject(handle, self._EXTENDED_LIMITS, ctypes.byref(info), ctypes.sizeof(info)):
            error = ctypes.get_last_error()
            self.close()
            raise OSError(error, "SetInformationJobObject failed")

    def adopt(self, proc: subprocess.Popen) -> None:
        """Put the (suspended) process in the job, then let it run."""
        process = int(proc._handle)  # type: ignore[attr-defined]
        try:
            if not self._k32.AssignProcessToJobObject(self.handle, process):
                log.warning("a workshop command is not in a job object (error %s)", self._ctypes.get_last_error())
        finally:
            self._ntdll.NtResumeProcess(process)

    def terminate(self) -> None:
        if self.handle:
            self._k32.TerminateJobObject(self.handle, 1)

    def close(self) -> None:
        if self.handle:
            self._k32.CloseHandle(self.handle)
            self.handle = None


def _spawn(argv: list[str], cwd: Path, env: dict[str, str]) -> subprocess.Popen:
    """Start the command in a process group (Windows: and a job object, ``_WinJob``, as ``proc.winjob``) or session
    (POSIX) of its own."""
    if os.name != "nt":
        return subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.STDOUT, start_new_session=True)  # fmt: skip
    flags = subprocess.CREATE_NEW_PROCESS_GROUP | subprocess.CREATE_NO_WINDOW
    try:
        job: _WinJob | None = _WinJob()
    except OSError:
        log.warning("no job object for a workshop command: a stop takes what taskkill /T finds", exc_info=True)
        job = None
    suspended = 0x4  # CREATE_SUSPENDED
    proc = subprocess.Popen(argv, cwd=cwd, env=env, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                            stderr=subprocess.STDOUT, creationflags=flags | (suspended if job else 0))  # fmt: skip
    if job is not None:
        job.adopt(proc)
    proc.winjob = job  # type: ignore[attr-defined]
    return proc


@dataclass
class Job:
    id: str
    command_id: str
    cmd: str
    started_at: str
    state: str = "running"  # running, done (exit 0), failed (another exit), stopped
    exit_code: int | None = None
    ended_at: str | None = None
    proc: subprocess.Popen | None = field(default=None, repr=False)
    output: bytearray = field(default_factory=bytearray, repr=False)
    stop_asked: bool = False
    ended: threading.Event = field(default_factory=threading.Event, repr=False)

    def summary(self) -> dict:
        return {"id": self.id, "command_id": self.command_id, "cmd": self.cmd, "state": self.state,
                "exit_code": self.exit_code, "started_at": self.started_at, "ended_at": self.ended_at}  # fmt: skip


def _text(data: bytes) -> str:
    """Output as the page shows it: UTF-8 (what cannot be decoded replaced), line ends as ``\\n``, no terminal colours."""
    return _clean(data.decode("utf-8", errors="replace"))


def _clean(text: str) -> str:
    """Line ends as ``\\n``, no terminal colours."""
    return _ANSI.sub("", text.replace("\r\n", "\n").replace("\r", "\n"))


class Jobs:
    """The commands this console runs, at most ``MAX_RUNNING`` at once and each command once at a time. Thread-safe.

    ``checkout`` is the participant's checkout: its ``workshop/commands.json`` names the commands, its ``.venv`` and
    folders are in their environment (``command_env``). ``run_in()`` is the folder they run in, asked at every start:
    the console's copy (the server passes the unit's worktree); without it, the checkout. ``find_bash`` and
    ``spawn(argv, cwd, env)`` are for tests.
    """

    def __init__(
        self,
        checkout: Path,
        *,
        run_in: Callable[[], Path] | None = None,
        find_bash: Callable[[], str | None] = _find_bash,
        spawn: Callable[[list[str], Path, dict[str, str]], subprocess.Popen] = _spawn,
    ) -> None:
        self.checkout = Path(checkout)
        self._run_in = run_in
        self._find_bash = find_bash
        self._spawn = spawn
        self._lock = threading.Lock()
        self._jobs: list[Job] = []  # oldest first
        self._ids = itertools.count(1)

    def folder(self) -> Path:
        """The folder commands run in: ``run_in()`` (the console's copy), else the checkout. ``NoCopy`` when it is not
        there."""
        if self._run_in is None:
            return self.checkout
        try:
            folder = Path(self._run_in())
        except (KeyError, OSError, RuntimeError) as err:
            raise NoCopy(f"the console's copy is not there ({err}): start the console again") from None
        if not folder.is_dir():
            raise NoCopy(f"the console's copy is not there ({folder}): start the console again")
        return folder

    def start(self, command: dict) -> Job:
        """Run ``command`` (an entry of ``read_commands``, not interactive) in ``folder()``. ``AlreadyRunning``,
        ``TooMany``, ``NoBash``, ``NoCopy``."""
        if command["interactive"]:
            raise ValueError("an interactive command runs in a terminal, not in the console")
        bash = self._find_bash()
        if not bash:
            raise NoBash("no bash found: install Git for Windows (it brings Git Bash) or set HARNESS_BASH")
        folder = self.folder()
        with self._lock:
            running = [job for job in self._jobs if job.state == "running"]
            if any(job.command_id == command["id"] for job in running):
                raise AlreadyRunning(f"{command['id']} is running already")
            if len(running) >= MAX_RUNNING:
                raise TooMany(f"{MAX_RUNNING} commands are running already: stop one or wait")
            job = Job(f"j{next(self._ids)}", command["id"], command["cmd"], _now())
            job.proc = self._spawn([bash, "-c", command["cmd"]], folder, command_env(self.checkout))
            self._jobs.append(job)
            self._trim()
        log.info("workshop command %s started as %s", job.command_id, job.id)
        threading.Thread(target=self._read, args=(job,), name=f"workshop-{job.id}", daemon=True).start()
        return job

    def _trim(self) -> None:
        """Keep at most ``KEEP_JOBS`` jobs, dropping the oldest that ended (the lock is held)."""
        extra = len(self._jobs) - KEEP_JOBS
        if extra > 0:
            drop = {id(job) for job in self._jobs if job.state != "running"}
            kept = []
            for job in self._jobs:
                if extra > 0 and id(job) in drop:
                    extra -= 1
                    continue
                kept.append(job)
            self._jobs = kept

    def _read(self, job: Job) -> None:
        """Take the job's output until it ends, then its exit code."""
        proc = job.proc
        assert proc is not None and proc.stdout is not None
        try:
            while chunk := proc.stdout.read1(65536):
                with self._lock:
                    job.output += chunk
                    if len(job.output) > MAX_OUTPUT:
                        del job.output[: len(job.output) - MAX_OUTPUT]
        except (OSError, ValueError):  # the pipe broke: the process is ending or gone
            pass
        finally:
            code = proc.wait()
            proc.stdout.close()
            winjob = getattr(proc, "winjob", None)
            if winjob is not None:
                winjob.close()  # what it left running in the background ends with it
            with self._lock:
                job.exit_code = code
                job.ended_at = _now()
                job.state = "stopped" if job.stop_asked else ("done" if code == 0 else "failed")
            job.ended.set()
            log.info("workshop command %s (%s) ended: %s", job.command_id, job.id, job.state)

    def get(self, job_id: str) -> Job | None:
        with self._lock:
            return next((job for job in self._jobs if job.id == job_id), None)

    def view(self, job: Job) -> dict:
        """The job with its output (the last ``MAX_OUTPUT`` bytes, as text)."""
        with self._lock:
            return {**job.summary(), "output": _text(bytes(job.output))}

    def list(self) -> list[dict]:
        """The running jobs (oldest first), then the most recent that ended; ``LIST_JOBS`` at most."""
        with self._lock:
            running = [job for job in self._jobs if job.state == "running"]
            ended = [job for job in reversed(self._jobs) if job.state != "running"]
            return [job.summary() for job in (running + ended)[:LIST_JOBS]]

    def stop(self, job_id: str) -> Job:
        """Stop a running job and everything it started; ``KeyError`` for no such job, ``ValueError`` when it has
        ended. Waits a few seconds for it to end."""
        job = self.get(job_id)
        if job is None:
            raise KeyError(job_id)
        with self._lock:
            if job.state != "running":
                raise ValueError("it is not running")
            job.stop_asked = True
        self._kill(job)
        job.ended.wait(_STOP_WAIT_S)
        return job

    @staticmethod
    def _kill(job: Job) -> None:
        proc = job.proc
        if proc is None:
            return
        if proc.poll() is not None:  # bash has ended; what it started may still hold the output
            winjob = getattr(proc, "winjob", None)
            if winjob is not None:
                winjob.terminate()
            return
        try:
            procs.kill_tree(proc)
        except (OSError, ValueError):
            log.warning("stopping workshop command %s failed", job.id, exc_info=True)
        winjob = getattr(proc, "winjob", None)
        if winjob is not None:  # what taskkill /T did not find (an MSYS program whose parent had gone)
            winjob.terminate()
        if proc.poll() is None:  # the tree kill did not take it: the process itself at least
            try:
                proc.kill()
            except OSError:
                pass

    def shutdown(self) -> None:
        """Stop every job that still runs (the console is ending)."""
        with self._lock:
            running = [job for job in self._jobs if job.state == "running"]
            for job in running:
                job.stop_asked = True
        for job in running:
            self._kill(job)
        for job in running:
            job.ended.wait(_STOP_WAIT_S)
