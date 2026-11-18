"""Where are we running? Devcontainer or host, and how gates are executed there.

Inside the devcontainer (``TS_IN_CONTAINER=1`` or ``/.dockerenv``) the gate
ladder runs directly: ``bash scripts/check.sh build unit-tests``. On a host (Windows with
Git Bash, macOS, Linux) ``scripts/check.sh`` re-executes itself through ``scripts/dockerbuild.sh``, which
mounts the checkout into the tinystream-dev image; ``MSYS_NO_PATHCONV=1``
keeps Git Bash from rewriting ``/work``. A host without a usable Docker
(``docker_usable``) runs the Docker-free ladder instead: check.sh hands off to
``scripts/lite_gates.py`` (zig cc, ``ZIG_VERSION``).
"""

from __future__ import annotations

import os
import platform
import shutil
import subprocess
from dataclasses import dataclass
from pathlib import Path
from typing import Mapping

# zig cc for the Docker-free ladder (scripts/lite_gates.py, same pin there): pip install ziglang==<it>
ZIG_VERSION = "0.16.0"


@dataclass
class Environment:
    in_container: bool
    system: str                # platform.system(): Linux | Windows | Darwin
    bash: str | None           # bash used to run the gate scripts
    docker: str | None         # docker CLI on PATH (host only matters)
    devcontainer: bool         # .devcontainer/devcontainer.json in the repo
    image: str = "tinystream-dev:latest"

    @property
    def where(self) -> str:
        return "devcontainer" if self.in_container else f"host ({self.system})"

    def gate_command(self, script: str, wrapper: str, args: list[str], self_wrapping: bool = False) -> list[str]:
        """Command line that runs ``script args...`` in the right place.

        ``self_wrapping``: the script enters the container on its own when started on
        the host (scripts/check.sh: it reads git on the host first, which a worktree
        needs), so the host starts it directly instead of through the wrapper."""
        bash = self.bash or "bash"
        if self.in_container or self_wrapping:
            return [bash, script, *args]
        return [bash, wrapper, script, *args]

    def gate_env(self, base: Mapping[str, str] | None = None, extra: Mapping[str, str] | None = None) -> dict[str, str]:
        env = dict(os.environ if base is None else base)
        env["MSYS_NO_PATHCONV"] = "1"
        if extra:
            env.update(extra)
        return env


def in_container(env: Mapping[str, str] | None = None) -> bool:
    env = os.environ if env is None else env
    return env.get("TS_IN_CONTAINER") == "1" or Path("/.dockerenv").exists()


def find_bash(configured: str = "", env: Mapping[str, str] | None = None) -> str | None:
    """Git Bash on Windows (never WSL's System32 bash.exe), plain bash elsewhere."""
    env = os.environ if env is None else env
    for cand in (env.get("HARNESS_BASH"), configured):
        if cand and Path(cand).exists():
            return str(cand)
    if platform.system() != "Windows":
        return shutil.which("bash")
    candidates: list[Path] = []
    git = shutil.which("git")
    if git:
        # <Git>/cmd/git.exe or <Git>/mingw64/bin/git.exe -> <Git>/bin/bash.exe
        for up in Path(git).resolve().parents:
            candidates.append(up / "bin" / "bash.exe")
            if (up / "git-bash.exe").exists():
                break
    for var in ("ProgramFiles", "ProgramW6432", "ProgramFiles(x86)", "LOCALAPPDATA"):
        root = env.get(var)
        if root:
            candidates.append(Path(root) / "Git" / "bin" / "bash.exe")
            candidates.append(Path(root) / "Programs" / "Git" / "bin" / "bash.exe")
    for c in candidates:
        if c.exists():
            return str(c)
    which = shutil.which("bash")
    if which and "system32" not in which.lower():
        return which
    return None


def detect(repo: Path, configured_bash: str = "", env: Mapping[str, str] | None = None) -> Environment:
    env = os.environ if env is None else env
    return Environment(
        in_container=in_container(env),
        system=platform.system(),
        bash=find_bash(configured_bash, env),
        docker=shutil.which("docker"),
        devcontainer=(repo / ".devcontainer" / "devcontainer.json").is_file(),
        image=env.get("TS_IMAGE", "tinystream-dev:latest"),
    )


def docker_usable(timeout: float = 8.0) -> tuple[bool, str]:
    """Can a host run the gates in the container? ``docker info`` must answer within ``timeout``
    seconds, as scripts/check.sh asks it before it picks the container or the Docker-free ladder.
    (True, "") | (False, "Docker not installed") | (False, "Docker installed but not running")."""
    docker = shutil.which("docker")
    if not docker:
        return False, "Docker not installed"
    try:
        proc = subprocess.Popen([docker, "info"], stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL,
                                stderr=subprocess.DEVNULL)
    except OSError:
        return False, "Docker installed but not running"
    try:
        rc = proc.wait(timeout=timeout)
    except subprocess.TimeoutExpired:      # the daemon does not answer (Docker Desktop starting or hung)
        proc.kill()
        try:
            proc.wait(timeout=5)
        except subprocess.TimeoutExpired:
            pass
        return False, "Docker installed but not running"
    return (True, "") if rc == 0 else (False, "Docker installed but not running")


NPM_GLOBAL_ROOTS = (Path("/usr/lib/node_modules"), Path("/usr/local/lib/node_modules"))
# The setup installs the pinned CLI in the checkout's .tools/copilot: an npm install (--prefix) on Windows, GitHub's
# install script (PREFIX) elsewhere. It goes first, before any CLI of the user's own, so every laptop runs the same
# version. The harness may run from a folder inside the checkout (the console's copy, .console-data/units/<u>/repo),
# so the nearest .tools/copilot above this file counts.
def _local_cli_dir() -> Path:
    here = Path(__file__).resolve().parent
    for folder in here.parents:
        if (folder / ".tools" / "copilot").is_dir():
            return folder / ".tools" / "copilot"
    return here.parent / ".tools" / "copilot"


LOCAL_CLI_DIR = _local_cli_dir()
# The CLI downloads newer versions by itself and then runs them, even when the pinned binary is the one started. The
# SDK speaks one protocol version, so the harness always starts it with auto-update off (runtime.copilot_env too).
NO_AUTO_UPDATE = {"COPILOT_AUTO_UPDATE": "false"}


def _native(path: Path | str) -> bool:
    """A file that is not a ``#!`` script (the npm node shim): the CLI binary itself."""
    try:
        with open(path, "rb") as fh:
            return fh.read(2) != b"#!"
    except OSError:
        return False


def _install_places(system: str, env: Mapping[str, str], exe: str) -> list[Path]:
    """Where the setup installs the CLI, for a process whose PATH does not have it yet: winget's
    user-scope link folder and package folder on Windows, ~/.local/bin (GitHub's install script)
    on Linux and macOS."""
    places: list[Path] = []
    if system == "Windows":
        if env.get("LOCALAPPDATA"):
            winget = Path(env["LOCALAPPDATA"]) / "Microsoft" / "WinGet"
            places.append(winget / "Links" / exe)
            try:
                packages = sorted((winget / "Packages").glob("GitHub.Copilot*"))
            except OSError:
                packages = []
            for pkg in packages:
                places += [pkg / exe] + sorted(pkg.glob(f"*/{exe}"))
    else:
        home = env.get("HOME") or str(Path.home())
        places.append(Path(home) / ".local" / "bin" / exe)
    return places


def find_copilot_cli(configured: str = "", env: Mapping[str, str] | None = None) -> str | None:
    """Native Copilot CLI binary: config > COPILOT_CLI_PATH > npm global install > a native binary
    on PATH > where the setup installs it (winget's Links / Packages folder, ~/.local/bin).

    The npm ``copilot`` shim is a node loader (``copilot.cmd`` on Windows); the
    SDK needs the native binary next to it, so the platform package is located.
    """
    env = os.environ if env is None else env
    for cand in (configured, env.get("COPILOT_CLI_PATH")):
        if cand and Path(cand).is_file():
            return str(cand)
    system = platform.system()
    osname = {"Windows": "win32", "Darwin": "darwin", "Linux": "linux"}.get(system, system.lower())
    machine = platform.machine().lower()
    arch = {"amd64": "x64", "x86_64": "x64", "arm64": "arm64", "aarch64": "arm64"}.get(machine, machine)
    exe = "copilot.exe" if system == "Windows" else "copilot"
    for local in (LOCAL_CLI_DIR / "node_modules" / "@github" / f"copilot-{osname}-{arch}" / exe,
                  LOCAL_CLI_DIR / "bin" / exe):
        if local.is_file() and _native(local):
            return str(local)
    roots: list[Path] = []
    shim = shutil.which("copilot")
    if shim:
        sp = Path(shim).resolve()
        roots += [sp.parent / "node_modules", sp.parent.parent / "lib" / "node_modules", sp.parent.parent]
    if env.get("APPDATA"):
        roots.append(Path(env["APPDATA"]) / "npm" / "node_modules")
    roots += list(NPM_GLOBAL_ROOTS)
    for root in roots:
        pkg = root / "@github" / "copilot" / "node_modules" / "@github" / f"copilot-{osname}-{arch}" / exe
        if pkg.is_file():
            return str(pkg)
        if root.name == "copilot" and (root / exe).is_file():
            return str(root / exe)
    # a native binary on PATH (not the node shim; on Windows an .exe such as winget's, never copilot.cmd)
    if shim and Path(shim).suffix.lower() in ((".exe",) if system == "Windows" else ("", ".exe")) and _native(shim):
        return shim
    # where the setup puts it, when this process's PATH predates the install
    for place in _install_places(system, env, exe):
        if place.is_file() and _native(place):
            return str(place)
    return None


def cli_version(cli: str, timeout: float = 20) -> str | None:
    """``copilot --version`` (no prompt, no network)."""
    try:
        out = subprocess.run([cli, "--version"], capture_output=True, text=True, timeout=timeout,
                             encoding="utf-8", errors="replace", env={**os.environ, **NO_AUTO_UPDATE})
    except (OSError, subprocess.TimeoutExpired):
        return None
    for token in (out.stdout or "").replace("\n", " ").split():
        token = token.rstrip(".")
        if token[:1].isdigit() and token.count(".") >= 2:
            return token
    return None
