#!/usr/bin/env python3
# SPDX-License-Identifier: MIT
"""lite_gates.py -- the tinystream gate ladder without Docker ("lite"), built with zig cc.

Usage: python scripts/lite_gates.py [gate ...]       e.g. build unit-tests loopback diff-policy (no gate: all of them)

scripts/check.sh runs this on a host where Docker is not usable (or with TS_LITE=1), after it has
prepared the change context of compliance/diff-policy. The contract is check.sh's: one result line per gate

    <gate> <PASS|FAIL|SKIP> <ms> ms (budget <s> s)  <what it checks> <reason>

then a summary line; the log of each gate is $TS_CHECK_LOG/<gate>.log (default build-check/). Exit
status: 0 when every gate PASSed or SKIPped, 1 when one FAILed, 2 for an unknown gate.

The compiler is zig cc (a clang) from the pinned ``ziglang`` pip package of the Python that runs
this script; it needs no admin rights. The container (gcc 12, cppcheck, qemu, sanitizers) stays the
reference: clang and gcc warn differently, so every result this script decides says "lite (zig)".

  build      the libraries and every test for this machine, with the warning sets of CMakeLists.txt
          (-Werror), and a cross build for aarch64-linux (the Pi): every source with the POSIX
          parts, the daemon (with the malloc guard's --wrap) and the tools linked, the tests compiled
  unit-tests/property-tests/loopback  unit / property / loopback tests: a ctest-style log that harness/baseline.py reads
  budgets      the codec budgets (tests/budget); .text/.bss and stack frames need the container's gcc
  diff-policy      the bash function of scripts/check.sh, run on this host (it needs bash and git only)
  misra      no ticket ids in the source trees, and the forbidden-function rules of the MISRA subset
          (21.3 malloc/free, 21.6 stdio, 21.8 abort/exit/getenv, 21.21 system) with the suppressions of
          scripts/cppcheck-suppressions.txt and the inline ones; the canary must trip all four first.
          The type rules (10.x, 11.x, 15.x, 17.7) need cppcheck
  no-heap    the undefined symbols of every daemon object of the aarch64-linux build (what nm -u reads),
          against check.sh's forbidden list, after its canary; on Linux also a 1 s run of tinystreamd with
          the malloc guard and a clean stop on SIGTERM
  sanitizers  every test built with UBSan (zig cc, trap on the first finding) and run; ASan needs gcc or Linux clang
  compliance     SKIP: the harness runs compliance in Python
  fuzz coverage   SKIP: libFuzzer and gcov are not part of this ladder

On Windows src/capture/capture.c (POSIX file descriptors) is replaced by scripts/lite/cap_stub.c
(the synth source only), and the two tests of the file source are reported as SKIP lines in the
gate log, never run. Linux and macOS build the real capture.c and run every test.

Build output goes to build-lite/<host|aarch64-linux-gnu>/. An object is rebuilt when its source, a
header it includes (zig cc -MMD) or its command line changed; archives and executables when an
input did, so a second run without changes only runs the tests.
"""

from __future__ import annotations

import concurrent.futures as cf
import importlib.util
import os
import platform
import re
import shutil
import signal
import subprocess
import sys
import time
import traceback
from dataclasses import dataclass, field
from pathlib import Path
from typing import Callable

ZIG_VERSION = "0.16.0"            # the pin (harness/envdetect.py ZIG_VERSION, setup): pip install ziglang==<it>
REPO = Path(__file__).resolve().parent.parent
OUT = Path("build-lite")          # paths are relative to REPO (the working directory from main() on)
MANIFEST = Path("scripts/test_manifest.txt")
STUB = "scripts/lite/cap_stub.c"
CROSS = "aarch64-linux-gnu"
LABEL = "lite (zig)"
NEEDS_DOCKER = "needs Docker (not run here)"
GATE_TIMEOUT_S = 300              # every test of one gate (gates.yaml timeout_s)
TEST_TIMEOUT_S = 120              # one test (CMakeLists.txt TIMEOUT)
STEP_TIMEOUT_S = 600              # one compile, archive or link (the first link builds zig's libc)

ALL = ["build", "misra", "no-heap", "unit-tests", "property-tests", "sanitizers", "fuzz", "loopback", "budgets", "coverage", "compliance", "diff-policy"]
OLD = {"g0": "build", "g1": "misra", "g2": "no-heap", "g3": "unit-tests", "g4": "property-tests", "g5": "sanitizers", "g6": "fuzz", "g7": "loopback", "g8": "budgets", "g9": "coverage", "g10": "compliance", "dp": "diff-policy"}   # the old codes, as arguments
NAMES = {"build": "host + Pi", "misra": "forbidden calls", "no-heap": "daemon symbols", "unit-tests": "virtual clock", "property-tests": "round trips", "sanitizers": "UBSan", "fuzz": "smoke", "loopback": "with budgets", "budgets": "codec, size, stack", "coverage": "per module", "compliance": "diff rules", "diff-policy": "protected paths"}
# budget_s of harness/gates.yaml (check.sh BUDGET_MS): printed next to the time, flagged when over
BUDGET_S = {"build": 4, "misra": 30, "no-heap": 3, "unit-tests": 2, "property-tests": 2, "sanitizers": 20, "fuzz": 90, "loopback": 4, "budgets": 2, "coverage": 15, "compliance": 1, "diff-policy": 1}

# ---- CMakeLists.txt, flag for flag ------------------------------------------------------------
WARN = ["-Wall", "-Wextra", "-Wpedantic", "-Werror", "-Wshadow", "-Wconversion", "-Wsign-conversion",
        "-Wstrict-prototypes", "-Wvla", "-Wundef", "-Wformat=2", "-Wcast-qual", "-Wdouble-promotion",
        "-D_DEFAULT_SOURCE", "-D_POSIX_C_SOURCE=200809L"]                         # ts_warnings
STRICT = WARN + ["-Wmissing-prototypes", "-Wmissing-declarations"]              # ts_strict
UNITY = ["-DUNITY_INCLUDE_DOUBLE", "-DUNITY_OUTPUT_COLOR=0"]
TEST_INC = ["-Iinclude", "-Itests/support", "-Itests/vectors", "-Ithird_party/unity"]
# zig cc on top of gcc's semantics, after every other flag: clang's -Wall has -Wparentheses-equality
# (gcc has no such warning), and zig turns UBSan traps on at -O0
CLANG = ["-Wno-parentheses-equality", "-fno-sanitize=undefined"]
# the malloc guard (CMakeLists.txt --wrap=...): zig 0.16 takes the one-dash spelling only, which GNU ld reads too
WRAP = ["-Wl,-wrap=malloc", "-Wl,-wrap=calloc", "-Wl,-wrap=realloc", "-Wl,-wrap=free"]

LIBS: list[tuple[str, tuple[str, ...], list[str]]] = [
    ("tsproto", ("src/proto/*.c", "src/codec/*.c"), STRICT + ["-Iinclude"]),
    ("tsd", ("src/txq/*.c", "src/rate/*.c", "src/clients/*.c", "src/net/*.c", "src/pool/*.c", "src/capture/*.c",
             "src/io/*.c", "src/log/*.c"), STRICT + ["-Iinclude", "-DTS_LOG_ENABLED=0"]),
    ("tsr", ("tools/tsrecv/lib/*.c",), STRICT + ["-Iinclude", "-DTS_LOG_ENABLED=1"]),
    ("tsnetem_shim", ("tools/tsnetem/lib/*.c",), STRICT + ["-Iinclude", "-DTS_LOG_ENABLED=1"]),
    ("unity", ("third_party/unity/unity.c",), ["-w", "-Ithird_party/unity"] + UNITY),
    ("ts_testsupport", ("tests/support/*.c", "tests/vectors/*.c"),
     STRICT + ["-DTS_LOG_ENABLED=1"] + TEST_INC + UNITY),
]
TEST_LIBS = ["ts_testsupport", "tsr", "tsnetem_shim", "tsd", "tsproto", "unity"]
TEST_FLAGS = WARN + ["-DTS_LOG_ENABLED=1"] + TEST_INC + UNITY
TEST_GLOBS = [("tests/unit/test_*.c", "unit"), ("tests/loopback/*.c", "loopback"), ("tests/budget/*.c", "budget")]
EXES: list[tuple[str, tuple[str, ...], list[str], list[str], list[str]]] = [
    ("tinystreamd", ("src/*.c",), STRICT + ["-Iinclude", "-DTS_LOG_ENABLED=0"], ["tsd", "tsproto"], WRAP),
    ("tsrecv", ("tools/tsrecv/*.c",), WARN + ["-Iinclude", "-DTS_LOG_ENABLED=1"], ["tsr", "tsd", "tsproto"], []),
    ("tsnetem", ("tools/tsnetem/*.c",), WARN + ["-Iinclude", "-DTS_LOG_ENABLED=1"],
     ["tsnetem_shim", "tsd", "tsproto"], []),
]
WINDOWS_SKIP = ("not run on Windows: src/capture/capture.c needs POSIX file descriptors "
                "(the container, Linux and macOS run it)")


@dataclass
class Plan:
    """What one build makes: for this machine (host) or for the Pi (cross, build only)."""
    name: str
    target: list[str]                                   # [] or ["-target", triple]
    opt: str                                            # CMakePresets.json: host -O1, pi -O2
    exclude: set[str] = field(default_factory=set)      # sources of the globs not built here
    extra: dict[str, list[str]] = field(default_factory=dict)
    exes: list[str] = field(default_factory=list)       # EXES linked
    link_tests: bool = True                             # False: test objects only (compile check)
    skip: dict[str, str] = field(default_factory=dict)  # test -> why it is not built or run here
    suffix: str = ""
    sanitize: list[str] = field(default_factory=list)  # flags after CLANG (UBSan)

    @property
    def out(self) -> Path:
        return OUT / self.name


def host_plan() -> Plan:
    system = platform.system()
    if system == "Windows":
        return Plan("host", [], "-O1", exclude={"src/io/io_posix.c", "src/capture/capture.c"},
                    extra={"tsd": [STUB]}, skip={"test_capture": WINDOWS_SKIP, "test_lb_file_clip": WINDOWS_SKIP},
                    suffix=".exe")
    if system == "Darwin":      # io_posix.c uses Linux socket flags; the daemon is checked by the cross build
        return Plan("host", [], "-O1", exclude={"src/io/io_posix.c"})
    return Plan("host", [], "-O1", exes=[e[0] for e in EXES])


UBSAN = ["-fsanitize=undefined", "-fno-sanitize-trap=undefined", "-fno-sanitize-recover=undefined"]


def ubsan_plan() -> Plan:
    """The host plan built with UBSan (zig's runtime prints the finding and stops the test)."""
    plan = host_plan()
    plan.name, plan.sanitize, plan.exes = "host-ubsan", list(UBSAN), []
    return plan


def cross_plan() -> Plan:
    return Plan(CROSS, ["-target", CROSS], "-O2", exes=[e[0] for e in EXES], link_tests=False)


def machine() -> str:
    arch = platform.machine().lower()
    arch = {"amd64": "x86_64", "x64": "x86_64", "arm64": "aarch64"}.get(arch, arch)
    system = {"Darwin": "macos"}.get(platform.system(), platform.system().lower())
    return f"{arch}-{system}"


def sources(globs: tuple[str, ...] | list[str]) -> list[str]:
    out: list[str] = []
    for pattern in globs:
        out += sorted(p.as_posix() for p in Path(".").glob(pattern) if p.is_file())
    return out


def discover_tests() -> list[tuple[str, str, list[str]]]:
    """(name, source, labels) of every test, as CMakeLists.txt registers them."""
    tests = []
    for pattern, label in TEST_GLOBS:
        for src in sources([pattern]):
            name = Path(src).stem
            labels = [label] + (["property"] if name.startswith("test_prop_") else [])
            tests.append((name, src, labels))
    return tests


# ---------------------------------------------------------------------------- building ----
def stamp_ok(target: Path, cmd: list[str]) -> bool:
    stamp = target.with_name(target.name + ".cmd")
    try:
        return target.is_file() and stamp.read_text(encoding="utf-8") == "\n".join(cmd)
    except OSError:
        return False


def write_stamp(target: Path, cmd: list[str]) -> None:
    target.with_name(target.name + ".cmd").write_text("\n".join(cmd), encoding="utf-8")


def depfile_ok(obj: Path, dep: Path) -> bool:
    """Every prerequisite zig cc -MMD listed exists and is not newer than the object."""
    try:
        text = dep.read_text(encoding="utf-8", errors="replace").replace("\\\r\n", " ").replace("\\\n", " ")
        built = obj.stat().st_mtime
    except OSError:
        return False
    m = re.search(r":\s", text)                 # "<target>: <prereq> ..." (the target may hold a drive colon)
    if not m:
        return False
    for raw in re.split(r"(?<!\\)\s+", text[m.end():].strip()):
        if not raw:
            continue
        try:
            if Path(raw.replace("\\ ", " ")).stat().st_mtime > built:
                return False
        except OSError:
            return False
    return True


def newer(target: Path, inputs: list[Path]) -> bool:
    try:
        built = target.stat().st_mtime
        return any(p.stat().st_mtime > built for p in inputs)
    except OSError:
        return True


def run_step(cmd: list[str]) -> tuple[int, str]:
    try:
        p = subprocess.run(cmd, stdout=subprocess.PIPE, stderr=subprocess.STDOUT, timeout=STEP_TIMEOUT_S)
    except subprocess.TimeoutExpired:
        return 1, f"timed out after {STEP_TIMEOUT_S} s: {' '.join(cmd[-3:])}"
    except OSError as exc:
        return 1, f"cannot run {cmd[0]}: {exc}"
    return p.returncode, p.stdout.decode("utf-8", "replace").replace("\r\n", "\n")


@dataclass
class BuildResult:
    ok: bool
    text: list[str]                                  # the build's log lines
    tests: dict[str, Path] = field(default_factory=dict)      # linked test executables
    seconds: float = 0.0


class Builder:
    def __init__(self, zig: list[str], jobs: int) -> None:
        self.zig = zig
        self.jobs = jobs
        self.done: dict[str, BuildResult] = {}

    def build(self, plan: Plan) -> BuildResult:
        if plan.name not in self.done:
            self.done[plan.name] = self._build(plan)
        return self.done[plan.name]

    def _cc(self, plan: Plan, flags: list[str]) -> list[str]:
        return self.zig + ["cc"] + plan.target + ["-std=c11", plan.opt, "-g"] + flags + CLANG + plan.sanitize

    def _build(self, plan: Plan) -> BuildResult:
        t0 = time.monotonic()
        text: list[str] = []
        out = plan.out
        # 1. objects: one compile job per source, grouped by the unit it belongs to
        jobs: list[tuple[str, Path, list[str]]] = []          # (source, object, command)
        units: dict[str, list[Path]] = {}

        def add(unit: str, src: str, flags: list[str]) -> None:
            obj = out / "obj" / unit.split(":")[0] / (src + ".o")
            dep = obj.with_name(obj.name + ".d")
            jobs.append((src, obj, self._cc(plan, flags) + ["-MMD", "-MF", dep.as_posix(), "-c", src,
                                                            "-o", obj.as_posix()]))
            units.setdefault(unit, []).append(obj)

        for name, globs, flags in LIBS:
            for src in [s for s in sources(globs) if s not in plan.exclude] + plan.extra.get(name, []):
                add(name, src, flags)
        exes = [e for e in EXES if e[0] in plan.exes]
        for name, globs, flags, _, _ in exes:
            for src in sources(globs):
                add(name, src, flags)
        tests = [(n, s) for n, s, _ in discover_tests() if n not in plan.skip]
        for name, src in tests:
            add("test:" + name, src, TEST_FLAGS)

        def compile_one(job: tuple[str, Path, list[str]]) -> tuple[Path, int, str, bool]:
            src, obj, cmd = job
            if stamp_ok(obj, cmd) and depfile_ok(obj, obj.with_name(obj.name + ".d")):
                return obj, 0, "", False
            obj.parent.mkdir(parents=True, exist_ok=True)
            rc, msg = run_step(cmd)
            if rc == 0:
                write_stamp(obj, cmd)
            else:
                obj.unlink(missing_ok=True)
            return obj, rc, msg, True

        bad: set[Path] = set()                                 # objects, archives, executables not made
        compiled = 0
        with cf.ThreadPoolExecutor(self.jobs) as ex:
            for (src, _, _), (obj, rc, msg, ran) in zip(jobs, ex.map(compile_one, jobs)):
                compiled += ran
                if msg.strip():
                    text.append(msg.rstrip())
                if rc != 0:
                    bad.add(obj)
                    text.append(f"[{plan.name}] FAILED: {src}")
        # 2. one static archive per library (a test links only the objects it pulls in)
        archives: dict[str, Path] = {}
        for name, _, _ in LIBS:
            lib = archives[name] = out / f"lib{name}.a"
            objs = units.get(name, [])
            cmd = self.zig + ["ar", "rcs", lib.as_posix()] + [o.as_posix() for o in objs]
            if bad & set(objs):
                bad.add(lib)
            elif not (stamp_ok(lib, cmd) and not newer(lib, objs)):
                lib.unlink(missing_ok=True)
                rc, msg = run_step(cmd)
                if rc == 0:
                    write_stamp(lib, cmd)
                else:
                    bad.add(lib)
                    text.append(msg.rstrip())
                    text.append(f"[{plan.name}] FAILED: lib{name}.a")
        # 3. executables: the daemon and the tools where the plan has them, every test where it links them
        links: list[tuple[str, Path, list[Path], list[str]]] = []
        for name, _, _, libs, ldflags in exes:
            links.append((name, units[name], [archives[n] for n in libs], ldflags))
        if plan.link_tests:
            for name, _ in tests:
                links.append((name, units["test:" + name], [archives[n] for n in TEST_LIBS], []))

        def link_one(item: tuple[str, list[Path], list[Path], list[str]]) -> tuple[str, Path, int, str, bool]:
            what, objs, libs, ldflags = item
            exe = out / "bin" / (what + plan.suffix)
            if bad & set(objs + libs):
                return what, exe, 1, "", False
            inputs = objs + libs
            cmd = self._cc(plan, []) + [p.as_posix() for p in inputs] + ldflags + ["-o", exe.as_posix()]
            if stamp_ok(exe, cmd) and not newer(exe, inputs):
                return what, exe, 0, "", False
            exe.parent.mkdir(parents=True, exist_ok=True)
            rc, msg = run_step(cmd)
            if rc == 0:
                write_stamp(exe, cmd)
            else:
                exe.unlink(missing_ok=True)
                msg += f"[{plan.name}] FAILED: link {what}"
            return what, exe, rc, msg, True

        linked = 0
        made: dict[str, Path] = {}
        with cf.ThreadPoolExecutor(self.jobs) as ex:
            for what, exe, rc, msg, ran in ex.map(link_one, links):
                linked += ran
                if msg.strip():
                    text.append(msg.rstrip())
                if rc == 0:
                    made[what] = exe
                else:
                    bad.add(exe)
        secs = time.monotonic() - t0
        what = (f"{len(exes)} tool(s) and {len(tests)} test(s) linked" if plan.link_tests
                else f"{len(exes)} tool(s) linked, {len(tests)} test(s) compiled only")
        text.insert(0, f"[{plan.name}] {len(jobs)} objects ({compiled} compiled now), {len(LIBS)} libraries, {what} "
                       f"({linked} linked now) in {secs:.1f} s, {out.as_posix()}/")
        text[1:1] = [f"[{plan.name}] {name}: not built ({why})" for name, why in plan.skip.items()]
        if bad:
            text.append(f"[{plan.name}] build FAILED")
        return BuildResult(not bad, text, {n: made[n] for n, _ in tests if n in made}, secs)


# ------------------------------------------------------------------------------ tests ----
SIGNALS = {getattr(signal, n): w for n, w in (("SIGSEGV", "SegFault"), ("SIGBUS", "BusError"),
                                             ("SIGILL", "Illegal"), ("SIGFPE", "Numerical"),
                                             ("SIGABRT", "Aborted")) if hasattr(signal, n)}
NT_CRASH = {0xC0000005: "SegFault", 0xC00000FD: "SegFault", 0xC00000FF: "SegFault", 0xC0000374: "SegFault",
            0xC000001D: "Illegal", 0xC0000096: "Illegal", 0xC0000094: "Numerical", 0xC0000095: "Numerical",
            0xC000008E: "Numerical", 0xC0000090: "Numerical", 0xC0000409: "Aborted"}


def outcome(rc: int | None) -> tuple[str, str]:
    """(ctest's result word on the test line, its word in "The following tests FAILED")."""
    if rc is None:
        return "Timeout", "Timeout"
    if rc == 0:
        return "Passed", ""
    crash = ""
    if os.name == "nt" and (rc & 0xFFFFFFFF) >= 0xC0000000:
        crash = NT_CRASH.get(rc & 0xFFFFFFFF, "Other")
    elif rc < 0:
        crash = SIGNALS.get(-rc, "Other")
    if crash:
        return f"Exception: {crash}", "SEGFAULT" if crash == "SegFault" else crash
    return "Failed", "Failed"


def run_one(exe: Path, cwd: Path, timeout: float) -> tuple[int | None, float, str]:
    t0 = time.monotonic()
    if timeout <= 0:
        return None, 0.0, ""
    try:
        p = subprocess.run([str(exe.resolve())], cwd=str(cwd), stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=timeout)
        rc: int | None = p.returncode
        raw = p.stdout
    except subprocess.TimeoutExpired as exc:
        rc, raw = None, exc.stdout or b""
    except OSError as exc:
        return 1, time.monotonic() - t0, f"cannot start {exe}: {exc}\n"
    return rc, time.monotonic() - t0, raw.decode("utf-8", "replace").replace("\r\n", "\n")


@dataclass
class Ctx:
    log_dir: Path
    log_shown: str                 # the log dir as the reason names it
    builder: Builder | None
    zig_note: str                  # zig version line, or why zig is missing
    jobs: int


def zig_command() -> tuple[list[str] | None, str]:
    """(zig command, version) from the ziglang package of this Python; (None, why) without it."""
    hint = f"{sys.executable} -m pip install ziglang=={ZIG_VERSION}"
    try:
        spec = importlib.util.find_spec("ziglang")
    except (ImportError, ValueError):
        spec = None
    if spec is None or not spec.origin:
        return None, f"zig not found: {hint}"
    exe = Path(spec.origin).parent / ("zig.exe" if os.name == "nt" else "zig")
    cmd = [str(exe)] if exe.is_file() else [sys.executable, "-m", "ziglang"]
    try:
        p = subprocess.run(cmd + ["version"], capture_output=True, text=True, timeout=120)
    except (OSError, subprocess.TimeoutExpired) as exc:
        return None, f"zig does not start ({exc}): {hint}"
    if p.returncode != 0:
        return None, f"zig does not start ({(p.stderr or p.stdout).strip()[-200:]}): {hint}"
    return cmd, p.stdout.strip()


def manifest(label: str) -> list[str]:
    names = []
    for line in MANIFEST.read_text(encoding="utf-8").splitlines():
        parts = line.split()
        if len(parts) >= 2 and not line.lstrip().startswith("#") and label in parts[1:]:
            names.append(parts[0])
    return names


def header(ctx: Ctx, log: Callable[[str], None]) -> None:
    log(f"{LABEL}: zig cc {ctx.zig_note} on {machine()} -- the Docker-free ladder")


def built(ctx: Ctx, log: Callable[[str], None], gid: str, plan: Plan | None = None) -> BuildResult | tuple[str, str]:
    """The host build (made once per run), its lines in this gate's log; a (status, reason) when it
    cannot be made."""
    if ctx.builder is None:
        return "SKIP", ctx.zig_note
    header(ctx, log)
    res = ctx.builder.build(plan or host_plan())
    for line in res.text:
        log(line)
    if not res.ok:
        return "FAIL", f"{LABEL}: build failed, see {ctx.log_shown}/{gid}.log"
    return res


def tests_gate(ctx: Ctx, log: Callable[[str], None], gid: str, label: str,
              show_all: bool = False, plan: Plan | None = None, name_re: str = "") -> tuple[str, str]:
    """Run the tests of one label (``*``: every test) from the plan's build, ctest style; ``name_re`` keeps the
    tests whose name it matches (ctest -R)."""
    plan = plan or host_plan()
    res = built(ctx, log, gid, plan)
    if isinstance(res, tuple):
        return res
    if not MANIFEST.is_file():
        return "FAIL", f"missing {MANIFEST.as_posix()}"
    registered = [(n, s) for n, s, labels in discover_tests() if label == "*" or label in labels]
    names = {n for n, _ in registered}
    if name_re:
        registered = [(n, s) for n, s in registered if re.search(name_re, n)]
    wanted = sorted({t for lab in ("unit", "loopback", "budget") for t in manifest(lab)}) if label == "*" else manifest(label)
    missing = [] if name_re else [t for t in wanted if t not in names]
    if missing:
        log(f"tests in {MANIFEST.as_posix()} but not registered in preset lite: {' '.join(missing)}")
        return "FAIL", f"missing tests (label {label}): {' '.join(missing)}"
    skipped = [n for n, _ in registered if n in plan.skip]
    run = [n for n, _ in registered if n not in plan.skip]
    log(f"Test project {plan.out.as_posix()} ({LABEL}, label {label})")
    for n in skipped:
        log(f"SKIP {n}: {plan.skip[n]}")
    deadline = time.monotonic() + GATE_TIMEOUT_S
    number = {n: i + 1 for i, (n, _, _) in enumerate(discover_tests())}

    def one(name: str) -> tuple[str, int | None, float, str]:
        rc, secs, out = run_one(res.tests[name], plan.out, min(TEST_TIMEOUT_S, deadline - time.monotonic()))
        return name, rc, secs, out

    results = []
    with cf.ThreadPoolExecutor(ctx.jobs) as ex:
        results = list(ex.map(one, run))
    width = max([len(n) for n in run] + [20]) + 4
    failed: list[tuple[str, str]] = []
    for i, (name, rc, secs, out) in enumerate(results, 1):
        word, listed = outcome(rc)
        dots = " " + "." * max(3, width - len(name))
        mark = "   " if word == "Passed" else "***"
        log(f"{i:>2}/{len(results)} Test {('#' + str(number[name])):>3}: {name}{dots}{mark}{word:<8} {secs:6.2f} sec")
        if word != "Passed" or show_all:
            log(out.rstrip("\n"))
            log("")
        if word != "Passed":
            failed.append((name, listed))
    log("")
    if results:
        pct = (100 * (len(results) - len(failed)) + len(results) // 2) // len(results)
        log(f"{pct}% tests passed, {len(failed)} tests failed out of {len(results)}")
    else:
        log("No tests were found!!!")
    if failed:
        log("")
        log("The following tests FAILED:")
        for name, listed in failed:
            log(f"\t{number[name]:>3} - {name} ({listed})")
    note = f"; not run on Windows: {' '.join(skipped)}" if skipped else ""
    if failed or not results:
        return "FAIL", f"{LABEL}: see {ctx.log_shown}/{gid}.log{note}"
    return "PASS", f"{LABEL}{note}"


# ------------------------------------------------------------------------------ gates ----
def g0(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    res = built(ctx, log, "build")
    if isinstance(res, tuple):
        return res
    assert ctx.builder is not None
    cross = ctx.builder.build(cross_plan())
    for line in cross.text:
        log(line)
    if not cross.ok:
        return "FAIL", f"{LABEL}: {CROSS} build failed, see {ctx.log_shown}/build.log"
    return "PASS", f"{LABEL}: host {machine()} + {CROSS.rsplit('-', 1)[0]}"


def g3(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    return tests_gate(ctx, log, "unit-tests", "unit")


def g4(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    return tests_gate(ctx, log, "property-tests", "property")


def g7(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    return tests_gate(ctx, log, "loopback", "loopback")


def g8(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    status, reason = tests_gate(ctx, log, "budgets", "budget", show_all=True)
    log(".text/.bss and stack budgets: not checked here (they need the container's gcc and binutils)")
    if status != "PASS":
        return status, reason
    ratio = ""
    try:
        m = re.search(r"ratio_q1_x100=(\d+)", (ctx.log_dir / "budgets.log").read_text(encoding="utf-8"))
    except OSError:
        m = None
    if m and len(m.group(1)) > 2:
        ratio = f" ratio {m.group(1)[:-2]}.{m.group(1)[-2:]};"
    return "PASS", f"{LABEL}: codec budgets only;{ratio} size and stack need Docker"


# ---- misra: ticket ids and the forbidden-function rules of the subset, without cppcheck ------------------
MISRA_TREES = ("src", "include", "tools", "tests/support")
MISRA_CALLS = {"21.3": ("malloc", "calloc", "realloc", "free"), "21.8": ("abort", "exit", "getenv", "_Exit", "quick_exit"),
               "21.21": ("system",)}
MISRA_SUPPRESSIONS = Path("scripts/cppcheck-suppressions.txt")
_COMMENT_OR_TEXT = re.compile(r'/\*.*?\*/|//[^\n]*|"(?:\\.|[^"\\\n])*"|\'(?:\\.|[^\'\\\n])*\'', re.S)


def _blank(match: re.Match) -> str:
    """A comment or a literal as spaces, newlines kept: the line numbers stay, nothing inside it is a call."""
    return re.sub(r"[^\n]", " ", match.group(0))


def misra_findings(path: Path, shown: str) -> list[tuple[str, int, str]]:
    """(rule, line, what) of one C file: a call of a forbidden function, an include of stdio.h or wchar.h."""
    raw = path.read_text(encoding="utf-8", errors="replace")
    lines = raw.splitlines()
    code = _COMMENT_OR_TEXT.sub(_blank, raw)
    found: list[tuple[str, int, str]] = []
    for rule, names in MISRA_CALLS.items():
        for m in re.finditer(r"(?<![\w.>])(" + "|".join(names) + r")\s*\(", code):
            found.append((rule, code.count("\n", 0, m.start()) + 1, m.group(1) + "()"))
    for m in re.finditer(r"^[ \t]*#[ \t]*include[ \t]*<(stdio|wchar)\.h>", code, re.M):
        found.append(("21.6", code.count("\n", 0, m.start()) + 1, f"#include <{m.group(1)}.h>"))

    def inline(rule: str, line: int) -> bool:    # cppcheck-suppress on the line or the line above
        tag = f"cppcheck-suppress misra-c2012-{rule}"
        return any(tag in lines[i - 1] for i in (line, line - 1) if 0 < i <= len(lines))
    return [(r, n, w) for r, n, w in found if not inline(r, n)]


def misra_suppressed() -> set[tuple[str, str]]:
    """(rule, file) pairs of scripts/cppcheck-suppressions.txt; ``*`` as the rule for a whole-file entry."""
    out: set[tuple[str, str]] = set()
    for line in MISRA_SUPPRESSIONS.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        m = re.match(r"^(misra-c2012-([\d.]+)|\*):([^:\s]+)", line)
        if m:
            out.add((m.group(2) or "*", m.group(3)))
    return out


def g1(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    log(f"{LABEL}: no ticket ids in the source trees; MISRA C:2012 forbidden-function rules 21.3 21.6 21.8 21.21 "
        "on " + " ".join(MISRA_TREES) + " (the type rules 10.x 11.x 15.x 17.7 need cppcheck)")
    ids = []
    for tree in ("src", "include", "tests", "tools", "fuzz", "PROTOCOL.md"):
        root = Path(tree)
        files = [root] if root.is_file() else sorted(q for q in root.rglob("*") if q.is_file()) if root.is_dir() else []
        for f in files:
            try:
                text = f.read_text(encoding="utf-8", errors="replace")
            except OSError:
                continue
            for n, line in enumerate(text.splitlines(), 1):
                if re.search(r"TS-[0-9]{3}", line):
                    ids.append(f"{f.as_posix()}:{n}:{line.strip()}")
    if ids:
        log("ticket ids must not appear in code, tests, tools, fuzz or PROTOCOL.md:")
        for line in ids:
            log(line)
        return "FAIL", f"ticket ids in the tree ({len(ids)} lines, see {ctx.log_shown}/misra.log)"
    canary = {r for r, _, _ in misra_findings(Path("scripts/misra_canary/canary.c"), "canary")}
    miss = [r for r in ("21.3", "21.6", "21.8", "21.21") if r not in canary]
    if miss:
        log("misra canary: rule(s) not detected on scripts/misra_canary/canary.c: " + " ".join(miss))
        return "FAIL", "MISRA canary failed: a forbidden-function rule is not detected"
    log("canary: 21.3 21.6 21.8 21.21 detected")
    skip = misra_suppressed()
    bad: list[str] = []
    count = 0
    for tree in MISRA_TREES:
        for f in sorted(Path(tree).rglob("*")):
            if f.suffix not in (".c", ".h") or not f.is_file():
                continue
            name = f.as_posix()
            if ("*", name) in skip:
                continue
            count += 1
            for rule, line, what in misra_findings(f, name):
                if (rule, name) not in skip:      # cppcheck's line format: harness/gates.py _parse_cppcheck
                    bad.append(f"{name}:{line}:0: style: {what} is forbidden (MISRA C:2012 rule {rule}) "
                               f"[misra-c2012-{rule}]")
    if bad:
        log("findings:")
        for line in bad:
            log(line)
        rules = sorted({b.rsplit("[misra-c2012-", 1)[1].rstrip("]") for b in bad})
        first = bad[0].split(":0: style")[0]
        return "FAIL", f"{LABEL}: {len(bad)} finding(s) of rule(s) {' '.join(rules)}, first {first} (see {ctx.log_shown}/misra.log)"
    log(f"no finding in {count} files")
    return "PASS", f"{LABEL}: ticket ids + 21.3 21.6 21.8 21.21 in {count} files; type rules need cppcheck"


# ---- no-heap: the undefined symbols of the daemon objects (nm -u), from the aarch64-linux build ----------
NO_HEAP_ALLOC = ("malloc free calloc realloc reallocarray strdup strndup wcsdup posix_memalign aligned_alloc memalign "
                 "valloc pvalloc asprintf vasprintf getline getdelim open_memstream open_wmemstream fmemopen fopen fdopen "
                 "freopen tmpfile opendir fdopendir scandir popen system execl execle execlp execv execve execvp execvpe "
                 "execveat fexecve fork vfork _Fork posix_spawn posix_spawnp dlopen dlmopen").split()
NO_HEAP_CANARY = ("malloc popen system execl execle execlp execv execve execvp execvpe fexecve fork vfork posix_spawn "
                  "posix_spawnp dlopen").split()
DAEMON_UNITS = ("tsproto", "tsd", "tinystreamd")


def elf_undefined(path: Path) -> set[str]:
    """The undefined global or weak symbols of an ELF64 little-endian object (``nm -u``)."""
    import struct
    data = path.read_bytes()
    if data[:4] != b"\x7fELF" or data[4] != 2 or data[5] != 1:
        raise ValueError(f"{path}: not an ELF64 little-endian object")
    shoff, = struct.unpack_from("<Q", data, 0x28)
    shentsize, shnum = struct.unpack_from("<HH", data, 0x3A)
    sections = [struct.unpack_from("<IIQQQQIIQQ", data, shoff + i * shentsize) for i in range(shnum)]
    names: set[str] = set()
    for sh in sections:
        if sh[1] != 2:                                   # SHT_SYMTAB
            continue
        strtab = sections[sh[6]]
        str_off = strtab[4]
        for off in range(sh[4], sh[4] + sh[5], sh[9] or 24):
            st_name, st_info, _, st_shndx = struct.unpack_from("<IBBH", data, off)
            if st_shndx != 0 or st_name == 0 or (st_info >> 4) not in (1, 2):
                continue
            end = data.index(b"\0", str_off + st_name)
            names.add(data[str_off + st_name:end].decode("ascii", "replace"))
    return names


def g2(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    if ctx.builder is None:
        return "SKIP", ctx.zig_note
    header(ctx, log)
    plan = cross_plan()
    res = ctx.builder.build(plan)
    for line in res.text:
        log(line)
    if not res.ok:
        return "FAIL", f"{LABEL}: {CROSS} build failed, see {ctx.log_shown}/no-heap.log"
    canary = plan.out / "no_heap_canary.o"
    rc, msg = run_step(ctx.builder.zig + ["cc", "-target", CROSS, "-std=c11", "-O0", "-w", "-c",
                                          "scripts/no_heap_canary/canary.c", "-o", canary.as_posix()])
    if rc != 0:
        log(msg)
        return "FAIL", "no-heap canary: scripts/no_heap_canary/canary.c does not compile"
    hits = elf_undefined(canary) & set(NO_HEAP_ALLOC)
    miss = [n for n in NO_HEAP_CANARY if n not in hits]
    if miss:
        log("no-heap canary: forbidden list misses: " + " ".join(miss))
        return "FAIL", "no-heap canary not detected: " + " ".join(miss)
    log(f"canary: {len(NO_HEAP_CANARY)} forbidden calls detected")
    objs = [o for unit in DAEMON_UNITS for o in sorted((plan.out / "obj" / unit).rglob("*.o"))
            if o.name != "io_fake.c.o"]
    if not objs:
        return "FAIL", "no daemon objects found"
    found: list[tuple[str, str]] = []
    for o in objs:
        unit, src = o.relative_to(plan.out / "obj").as_posix().split("/", 1)
        for sym in sorted(elf_undefined(o) & set(NO_HEAP_ALLOC)):
            found.append((f"{plan.out.as_posix()}/obj/{unit}.dir/{src}", sym))   # nm -A's line: _parse_nm
    if found:
        log("forbidden calls referenced by daemon objects:")
        for obj, sym in found:
            log(f"{obj}:                 U {sym}")
        return "FAIL", "forbidden call referenced: " + " ".join(sorted({s for _, s in found}))
    log(f"no forbidden call in {len(objs)} daemon objects ({CROSS})")
    if platform.system() != "Linux":
        log("the 1 s run of tinystreamd under the malloc guard needs Linux (POSIX sockets and signals): not run here")
        return "PASS", f"{LABEL}: {len(objs)} daemon objects clean; the daemon run needs Linux"
    host = ctx.builder.build(host_plan())
    exe = host_plan().out / "bin" / "tinystreamd"
    if not host.ok or not exe.is_file():
        return "FAIL", f"{LABEL}: host build failed, see {ctx.log_shown}/no-heap.log"
    import random
    port = 20000 + random.randrange(20000) * 2
    proc = subprocess.Popen([str(exe.resolve()), "--udp-port", str(port), "--tcp-port", str(port + 1)],
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT)
    time.sleep(1.0)
    if proc.poll() is None:
        proc.send_signal(signal.SIGTERM)
    try:
        out, _ = proc.communicate(timeout=4)
    except subprocess.TimeoutExpired:
        proc.kill()
        out, _ = proc.communicate()
    log(out.decode("utf-8", "replace"))
    if proc.returncode == -signal.SIGABRT or proc.returncode == 134:
        return "FAIL", "tinystreamd aborted: allocation after init (malloc guard)"
    if proc.returncode != 0:
        return "FAIL", f"tinystreamd exit {proc.returncode} (expected 0 after SIGTERM)"
    log("host: ran 1 s, clean stop on SIGTERM")
    return "PASS", f"{LABEL}: {len(objs)} daemon objects clean, 1 s run clean"


_ZIG_PANIC = re.compile(r"panic: (?P<msg>.*)$")
_ZIG_FRAME = re.compile(r"^(?P<file>.*?[\\/](?:src|tools|tests|fuzz)[\\/][^:]+):(?P<line>\d+): 0x[0-9a-f]+ in ")


def ubsan_lines(text: str) -> list[str]:
    """zig's UBSan runtime reports a finding as a panic and a stack; each one as the line clang's runtime prints
    (``file:line:col: runtime error: ...``), the first frame in this repository as its place."""
    out: list[str] = []
    lines = text.splitlines()
    for i, raw in enumerate(lines):
        m = _ZIG_PANIC.search(raw)
        if not m:
            continue
        for frame in lines[i + 1:i + 30]:
            f = _ZIG_FRAME.match(frame.strip())
            if f:
                place = Path(f.group("file"))
                try:
                    place = place.resolve().relative_to(REPO)
                except ValueError:
                    pass
                out.append(f"{place.as_posix()}:{f.group('line')}:0: runtime error: {m.group('msg')}")
                break
    return out


def g5(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    log("every test built with UBSan (-fsanitize=undefined, zig's runtime: the first finding stops the test)")
    status, reason = tests_gate(ctx, log, "sanitizers", "*", plan=ubsan_plan())
    try:
        found = ubsan_lines((ctx.log_dir / "sanitizers.log").read_text(encoding="utf-8"))
    except OSError:
        found = []
    if found:
        log("")
        log("UBSan findings:")
        for line in dict.fromkeys(found):
            log(line)
    if status == "PASS":
        return "PASS", reason + ": UBSan; ASan needs gcc or Linux clang"
    return status, reason


def g10(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    log("compliance is a Python gate of the harness (harness.compliance:check); scripts/check.sh compliance "
        "runs in the container")
    return "SKIP", "harness runs compliance in Python"


def needs_docker(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    log("libFuzzer and gcov are not part of the Docker-free ladder: this gate is not run")
    return "SKIP", NEEDS_DOCKER


_LINE = re.compile(r"^(?P<id>[a-z][a-z-]*)\s+(?P<status>PASS|FAIL|SKIP)\s+(?P<ms>\d+) ms \(budget\s+(?P<budget>\d+) s"
                   r"(?P<over> OVER BUDGET)?\)\s+(?P<rest>.*)$")


def find_bash() -> str | None:
    """Git Bash on Windows (never the WSL bash.exe in System32), bash elsewhere."""
    try:
        sys.path.insert(0, str(REPO))
        from harness.envdetect import find_bash as harness_find_bash   # noqa: PLC0415
        return harness_find_bash()
    except Exception:  # noqa: BLE001 - a checkout without the harness: PATH
        found = shutil.which("bash")
        return found if found and "system32" not in found.lower() else None
    finally:
        sys.path.remove(str(REPO))


def dp(ctx: Ctx, log: Callable[[str], None]) -> tuple[str, str]:
    """scripts/check.sh diff-policy on this host (TS_IN_CONTAINER=1: no hand-off); check.sh writes diff-policy.log. The
    time printed is this script's: a host's date(1) may not know check.sh's %3N (uutils on Ubuntu)."""
    bash = find_bash()
    if bash is None:
        log("no bash found (Windows: Git for Windows)")
        return "SKIP", "no bash on this host (Windows: install Git for Windows)"
    if platform.system() == "Darwin":        # check.sh needs bash 4 (associative arrays); macOS ships 3.2
        old = subprocess.run([bash, "-c", 'test "${BASH_VERSINFO[0]}" -ge 4'], capture_output=True, timeout=60)
        if old.returncode != 0:
            log(f"{bash} is older than bash 4")
            return "SKIP", "scripts/check.sh needs bash 4 or later (macOS: brew install bash)"
    env = dict(os.environ, TS_IN_CONTAINER="1", TS_CHECK_LOG=ctx.log_dir.resolve().as_posix())
    try:
        p = subprocess.run([bash, "scripts/check.sh", "diff-policy"], env=env, stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                           timeout=120)
        out = p.stdout.decode("utf-8", "replace")
    except (OSError, subprocess.TimeoutExpired) as exc:
        out = f"scripts/check.sh diff-policy did not finish: {exc}"
    for raw in out.splitlines():
        m = _LINE.match(raw.strip())
        if m and m.group("id") == "diff-policy":
            return m.group("status"), m.group("rest")[23:].strip()
    log(out)
    return "FAIL", f"no result line from scripts/check.sh diff-policy, see {ctx.log_shown}/diff-policy.log"


GATES: dict[str, Callable[..., tuple]] = {"build": g0, "misra": g1, "no-heap": g2, "unit-tests": g3, "property-tests": g4,
                                          "sanitizers": g5, "loopback": g7, "budgets": g8, "compliance": g10,
                                          "diff-policy": dp}


def host_path(raw: str) -> Path:
    """A path from the environment; a Git Bash spelling (/c/Users/...) on Windows becomes C:/Users/..."""
    m = re.match(r"^/([A-Za-z])(/.*)?$", raw) if os.name == "nt" else None
    return Path(f"{m.group(1)}:{m.group(2) or '/'}") if m else Path(raw)


def appender(path: Path) -> Callable[[str], None]:
    def log(text: str) -> None:
        with open(path, "a", encoding="utf-8", newline="\n") as fh:
            fh.write(text + "\n")
    return log


def quiet_crashes() -> None:
    """Windows: a crashing test exits at once instead of waiting for Windows Error Reporting."""
    try:
        import ctypes
        ctypes.windll.kernel32.SetErrorMode(0x0001 | 0x0002)   # SEM_FAILCRITICALERRORS | SEM_NOGPFAULTERRORBOX
    except Exception:  # noqa: BLE001
        pass


LABELS = {"unit": "unit", "property": "property", "loopback": "loopback", "budget": "budget"}


def scripts_mode(mode: str, argv: list[str]) -> int:
    """``--build <preset>`` (scripts/build.sh) and ``--ctest <preset> [-L label] [-R regex]`` (scripts/test.sh): the
    build, or the tests, of a preset with zig cc, the log on stdout. host, coverage, fuzz: the host build; asan:
    the UBSan build; pi: the aarch64 cross build (build only)."""
    preset = argv[0] if argv and not argv[0].startswith("-") else "host"
    rest = argv[1:] if argv and not argv[0].startswith("-") else argv
    label, name_re = "*", ""
    i = 0
    while i < len(rest):
        if rest[i] == "-L" and i + 1 < len(rest):
            label = LABELS.get(rest[i + 1], rest[i + 1])
            i += 1
        elif rest[i] == "-R" and i + 1 < len(rest):
            name_re = rest[i + 1]
            i += 1
        i += 1
    zig, note = zig_command()
    jobs = os.cpu_count() or 4
    ctx = Ctx(Path("build-check"), "build-check", Builder(zig, jobs) if zig else None, note, jobs)
    ctx.log_dir.mkdir(parents=True, exist_ok=True)
    plan = cross_plan() if preset == "pi" else ubsan_plan() if preset == "asan" else host_plan()
    if mode == "--build" or preset == "pi":
        res = built(ctx, print, "build", plan)
        if isinstance(res, tuple):
            print(f"{res[0]}: {res[1]}")
            return 0 if res[0] == "PASS" else 1
        print(f"build {'PASS' if res.ok else 'FAIL'} ({LABEL}, preset {preset})")
        return 0 if res.ok else 1
    status, reason = tests_gate(ctx, print, "tests", label, plan=plan, name_re=name_re)
    print(f"tests {status}: {reason}")
    return 0 if status == "PASS" else 1


def main(argv: list[str]) -> int:
    os.chdir(REPO)
    for stream in (sys.stdout, sys.stderr):     # paths may hold any character; the harness reads UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if argv and argv[0] in ("--build", "--ctest"):
        if os.name == "nt":
            quiet_crashes()
        return scripts_mode(argv[0], argv[1:])
    wanted = [OLD.get(a.lower(), a.lower()) for a in argv] or list(ALL)
    for g in wanted:
        if g not in ALL:
            print(f"unknown gate: {g}")
            return 2
    raw_log = os.environ.get("TS_CHECK_LOG") or "build-check"
    log_dir = host_path(raw_log)
    log_dir.mkdir(parents=True, exist_ok=True)
    if os.name == "nt":
        quiet_crashes()
    builds = {"build", "no-heap", "unit-tests", "property-tests", "sanitizers", "loopback", "budgets"} & set(wanted)
    zig, note = zig_command() if builds else (None, "")
    jobs = os.cpu_count() or 4
    ctx = Ctx(log_dir, raw_log.replace("\\", "/").rstrip("/"), Builder(zig, jobs) if zig else None, note, jobs)
    counts = {"PASS": 0, "FAIL": 0, "SKIP": 0}
    t_all = time.monotonic()
    for g in wanted:
        gid = g
        t0 = time.monotonic()
        log_file = log_dir / f"{gid}.log"
        if g != "diff-policy":                            # check.sh diff-policy writes its log itself; lite only adds to it
            log_file.write_text("", encoding="utf-8")
        log = appender(log_file)
        try:
            status, reason = GATES.get(g, needs_docker)(ctx, log)
        except Exception as exc:  # noqa: BLE001 - one gate's crash is that gate's FAIL
            log(traceback.format_exc())
            status, reason = "FAIL", f"lite_gates: {type(exc).__name__}: {exc}"
        ms = int((time.monotonic() - t0) * 1000)
        counts[status] += 1
        budget = BUDGET_S[g]
        over = " OVER BUDGET" if status != "SKIP" and ms > budget * 1000 else ""
        print(f"{gid:<14} {status:<4} {ms:6d} ms (budget {budget:3d} s{over})  {NAMES[g]:<22} {reason}", flush=True)
    total = int((time.monotonic() - t_all) * 1000)
    print(f"---- ladder: {counts['PASS']} PASS, {counts['FAIL']} FAIL, {counts['SKIP']} SKIP in {total} ms "
          f"(logs in {ctx.log_shown}) -- {LABEL}, no Docker")
    return 1 if counts["FAIL"] else 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
