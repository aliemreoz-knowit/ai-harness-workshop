"""Hidden tests: <hidden-tests>/run.sh <checkout> <preset> <ticket> and its output format.

    PASS  <ticket> <test>  6.73 sec
    FAIL  <ticket> <test>  not run
    [hidden] PASS (2 of 2)       exit 0 | [hidden] FAIL (1 of 3)  exit 1 | exit 2: usage/build error

run.sh builds and runs them in the dev container. Without a usable Docker, or with TS_LITE=1, <hidden-tests>/lite.py
does the same on this machine with zig cc (scripts/lite_gates.py), as scripts/check.sh does for the gates; TS_LITE=0
always means the container (``default_command``).
"""
from __future__ import annotations

import functools
import os
import re
import shutil
import subprocess
import sys
import time
from dataclasses import dataclass, field
from pathlib import Path
from typing import Optional

LINE_RE = re.compile(r"^(PASS|FAIL)\s+(\S+)\s+(\S+)\s*(.*)$")
SECS_RE = re.compile(r"([0-9]+(?:\.[0-9]+)?) sec")
DEFAULT_CMD = ["{bash}", "{hidden}/run.sh", "{checkout}", "{preset}", "{ticket}"]
LITE_CMD = ["{python}", "{hidden}/lite.py", "{checkout}", "{preset}", "{ticket}"]
DOCKER_INFO_S = 8.0               # scripts/check.sh: Docker that does not answer in 8 s is not usable
LOG_TAIL = 4000


@functools.lru_cache(maxsize=1)
def docker_usable() -> bool:
    """True when ``docker info`` answers within DOCKER_INFO_S (asked once per process)."""
    exe = shutil.which("docker")
    if not exe:
        return False
    try:
        return subprocess.run([exe, "info"], capture_output=True, timeout=DOCKER_INFO_S).returncode == 0
    except (OSError, subprocess.TimeoutExpired):
        return False


def default_command(hidden_dir: Path) -> list[str]:
    """run.sh (the container); lite.py (zig cc here) with TS_LITE=1, or without a usable Docker unless TS_LITE=0."""
    lite = os.environ.get("TS_LITE", "")
    if lite != "0" and (Path(hidden_dir) / "lite.py").is_file() and (lite == "1" or not docker_usable()):
        return LITE_CMD
    return DEFAULT_CMD


@dataclass
class HiddenResult:
    status: str                       # pass | fail | error | timeout
    exit_code: Optional[int]
    duration_s: float
    tests: list[dict] = field(default_factory=list)   # {name, ticket, status, secs}
    log_tail: str = ""

    @property
    def success(self) -> bool:
        return self.status == "pass"

    def to_dict(self) -> dict:
        return {"status": self.status, "exit_code": self.exit_code, "duration_s": round(self.duration_s, 2),
                "passed": sum(1 for t in self.tests if t["status"] == "PASS"),
                "failed": sum(1 for t in self.tests if t["status"] == "FAIL"),
                "tests": self.tests}


def find_bash() -> str:
    """bash for run.sh: $HARNESS_BASH, else Git for Windows' bash (never WSL's), else bash on PATH."""
    env = os.environ.get("HARNESS_BASH")
    if env:
        return env
    if os.name == "nt":
        git = shutil.which("git")
        if git:
            root = Path(git).resolve().parent.parent
            for cand in (root / "bin" / "bash.exe", root / "usr" / "bin" / "bash.exe"):
                if cand.is_file():
                    return str(cand)
        for cand in (Path(r"C:\Program Files\Git\bin\bash.exe"),):
            if cand.is_file():
                return str(cand)
        found = shutil.which("bash")
        if found and "system32" not in found.lower():
            return found
        raise FileNotFoundError("Git Bash not found; set HARNESS_BASH to bash.exe of Git for Windows")
    return shutil.which("bash") or "/bin/bash"


def parse_output(text: str) -> list[dict]:
    tests = []
    for line in text.splitlines():
        m = LINE_RE.match(line.strip())
        if not m:
            continue
        secs = SECS_RE.search(m.group(4))
        tests.append({"status": m.group(1), "ticket": m.group(2), "name": m.group(3),
                      "secs": float(secs.group(1)) if secs else None})
    return tests


def _posix(p: Path | str) -> str:
    return Path(p).resolve().as_posix()


def build_command(template: list[str], hidden_dir: Path, checkout: Path, preset: str, ticket: str) -> list[str]:
    values = {"hidden": _posix(hidden_dir), "checkout": _posix(checkout), "preset": preset,
              "ticket": ticket, "python": sys.executable}
    if any("{bash}" in t for t in template):
        values["bash"] = find_bash()
    return [t.format(**values) for t in template]


def run_hidden(hidden_dir: Path, checkout: Path, ticket: str, preset: str = "host",
               cmd: Optional[list[str]] = None, timeout_s: float = 900.0,
               log_path: Optional[Path] = None) -> HiddenResult:
    argv = build_command(cmd or default_command(hidden_dir), hidden_dir, checkout, preset, ticket)
    env = dict(os.environ, MSYS_NO_PATHCONV="1")
    t0 = time.monotonic()
    try:
        proc = subprocess.run(argv, capture_output=True, text=True, encoding="utf-8", errors="replace",
                              timeout=timeout_s, env=env)
        out = (proc.stdout or "") + (proc.stderr or "")
        code: Optional[int] = proc.returncode
    except subprocess.TimeoutExpired as exc:
        raw = exc.stdout or ""
        out = raw.decode("utf-8", "replace") if isinstance(raw, bytes) else raw
        out += f"\n[eval] hidden tests timed out after {timeout_s:.0f} s\n"
        code = None
    except OSError as exc:
        out, code = f"cannot start {argv[0]}: {exc}", None
        dur = time.monotonic() - t0
        _write_log(log_path, argv, out)
        return HiddenResult("error", None, dur, [], out[-LOG_TAIL:])
    dur = time.monotonic() - t0
    _write_log(log_path, argv, out)
    tests = [t for t in parse_output(out) if t["ticket"] == ticket or not ticket]
    if code is None:
        status = "timeout"
    elif code == 0 and tests and all(t["status"] == "PASS" for t in tests):
        status = "pass"
    elif code in (0, 1):
        status = "fail"            # exit 0 without PASS lines is never a pass
    else:
        status = "error"
    return HiddenResult(status, code, dur, tests, out[-LOG_TAIL:])


def _write_log(path: Optional[Path], argv: list[str], out: str) -> None:
    if path is None:
        return
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        fh.write("$ " + " ".join(argv) + "\n" + out)
