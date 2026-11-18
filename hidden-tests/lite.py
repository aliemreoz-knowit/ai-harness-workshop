"""lite.py <checkout> [host] [ticket-id ...] -- the hidden tests without Docker, on this machine.

run.sh's job and output format, with the Docker-free build of scripts/lite_gates.py (zig cc) instead of the dev
container. The harness runs it in place of run.sh when Docker is not usable or TS_LITE=1 (harness/eval/hidden.py),
the same choice scripts/check.sh makes for the gates; TS_LITE=0 never runs it.

    python hidden-tests/lite.py <checkout> [host] [TS-1xx ...]

The checkout is copied to a temporary folder (build-* and .git left out) and the selected hidden tests go into the
copy's tests/unit/ and tests/loopback/, so nothing is written to the checkout. Only the host preset: ASan needs the
container. The container (gcc 12) stays the reference: clang and gcc can disagree, and on Windows a test that needs
POSIX file descriptors is not run and counts as FAIL ("not run here").

Output: one line per hidden test, `PASS|FAIL <ticket> <test> <time>`, the output of the failed tests, then
`[hidden] PASS` or `[hidden] FAIL`. Exit status: 0 every selected hidden test passed, 1 at least one failed,
2 usage / unknown ticket / build error.
"""
from __future__ import annotations

import importlib.util
import os
import shutil
import sys
import tempfile
from pathlib import Path
from types import ModuleType

HT_DIR = Path(__file__).resolve().parent
KINDS = ("unit", "loopback")
LABEL = "lite (zig)"


def fail(msg: str) -> int:
    print(f"lite.py: {msg}", file=sys.stderr)
    return 2


def selection(tickets: list[str]) -> list[tuple[str, str, Path]]:
    """(ticket label, unit|loopback, test source) of every selected hidden test, as run.sh selects them."""
    out: list[tuple[str, str, Path]] = []

    def add(label: str, base: Path) -> None:
        for kind in KINDS:
            out.extend((label, kind, f) for f in sorted((base / kind).glob("test_*.c")) if f.is_file())

    if not tickets:
        add("shared", HT_DIR)
        for t in sorted(p for p in (HT_DIR / "tickets").iterdir() if p.is_dir()):
            add(t.name, t / "tests")
    for t in tickets:
        add(t, HT_DIR / "tickets" / t / "tests")
    return out


def _skip(_folder: str, names: list[str]) -> list[str]:
    return [n for n in names if n == ".git" or n == "build" or n.startswith("build-")]


def load_lite_gates(work: Path) -> ModuleType:
    """The copy's scripts/lite_gates.py: the build the gates of that tree use."""
    spec = importlib.util.spec_from_file_location("lite_gates_hidden", work / "scripts" / "lite_gates.py")
    if spec is None or spec.loader is None:
        raise ImportError(f"cannot load {work / 'scripts' / 'lite_gates.py'}")
    mod = importlib.util.module_from_spec(spec)
    sys.modules[spec.name] = mod            # dataclasses look their module up while the module runs
    spec.loader.exec_module(mod)
    return mod


def run(checkout: Path, tickets: list[str], sel: list[tuple[str, str, Path]], work: Path) -> int:
    """Copy, build and run in ``work``; prints run.sh's lines and returns its exit status."""
    shutil.copytree(checkout, work, ignore=_skip)
    for _, kind, src in sel:
        shutil.copyfile(src, work / "tests" / kind / src.name)
    if not (work / "scripts" / "lite_gates.py").is_file():
        return fail(f"{checkout} has no scripts/lite_gates.py: the Docker-free build is not there")
    lg = load_lite_gates(work)
    os.chdir(work)                          # lite_gates works with paths relative to the repository root
    if os.name == "nt":
        lg.quiet_crashes()
    names = [src.stem for _, _, src in sel]
    print(f"[hidden] {LABEL}, preset host, tickets: {' '.join(tickets) or 'all'}, tests: {' '.join(names)}", flush=True)
    zig, note = lg.zig_command()
    if zig is None:
        return fail(note)
    plan = lg.host_plan()
    res = lg.Builder(zig, os.cpu_count() or 4).build(plan)
    if not res.ok:
        print("\n".join(res.text)[-6000:])
        print("[hidden] build failed", file=sys.stderr)
        return 2
    failed: list[tuple[str, str]] = []
    for ticket, _, src in sel:
        name = src.stem
        if name not in res.tests:
            why = plan.skip.get(name, "not built")
            print(f"FAIL  {ticket:<8} {name:<34} not run here ({why})")
            failed.append((name, why))
            continue
        rc, secs, out = lg.run_one(res.tests[name], plan.out, lg.TEST_TIMEOUT_S)
        word, _ = lg.outcome(rc)
        print(f"{'PASS' if word == 'Passed' else 'FAIL'}  {ticket:<8} {name:<34} {secs:.2f} sec")
        if word != "Passed":
            failed.append((name, f"{word}\n{out.rstrip()}"))
    if failed:
        print(f"---- output of the failed tests ({LABEL}) ----")
        for name, text in failed:
            print(f"{name}: {text}")
        print(f"[hidden] FAIL ({len(failed)} of {len(sel)})")
        return 1
    print(f"[hidden] PASS ({len(sel)} of {len(sel)})")
    return 0


def main(argv: list[str]) -> int:
    for stream in (sys.stdout, sys.stderr):     # paths may hold any character; the harness reads UTF-8
        try:
            stream.reconfigure(encoding="utf-8", errors="replace")  # type: ignore[union-attr]
        except (AttributeError, ValueError):
            pass
    if not argv or not (Path(argv[0]) / "CMakePresets.json").is_file():
        return fail("usage: lite.py <path-to-tinystream checkout> [host] [ticket-id ...]")
    checkout, rest = Path(argv[0]).resolve(), argv[1:]
    if rest and rest[0] in ("host", "asan"):
        if rest.pop(0) == "asan":
            return fail("the asan preset needs Docker (the container's ASan/UBSan build): run.sh runs it")
    for t in rest:
        if not t or "/" in t or "\\" in t or t.startswith(".") or not (HT_DIR / "tickets" / t).is_dir():
            return fail(f"unknown ticket '{t}' (no tickets/{t}/)")
    sel = selection(rest)
    if not sel:
        return fail("no hidden tests selected")
    stems = [src.name for _, _, src in sel]
    dups = sorted({n for n in stems if stems.count(n) > 1})
    if dups:
        return fail(f"hidden test file name used more than once: {' '.join(dups)}")
    here = os.getcwd()
    tmp = tempfile.mkdtemp(prefix="hidden-lite-")
    try:
        return run(checkout, rest, sel, Path(tmp) / "ts")
    finally:
        os.chdir(here)
        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
