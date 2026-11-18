"""Baseline-aware test gates: judge a change by what it changes, not by what the base already had.

A branch can carry failing tests that have nothing to do with the ticket (another open defect,
a known-red test). With absolute gates such a run can never turn green. So, at the start of a
run, the orchestrator runs the test-type gates of the workflow once on the untouched base and
records every test result (ctest test names and, where the log has them, Unity case names) in
the run state. A test-type gate (``baseline: tests`` in gates.yaml: unit-tests, property-tests, sanitizers, loopback) then passes
when

  (a) no test that passed on the base fails now (a test the base did not have counts as
      "passed on the base": a new test must pass), and
  (b) every REQUIRED test passes. Required: the test_* names in the ticket's "Acceptance
      tests" (or "Required tests") section; without such a list, when the base had failures,
      at least one of them must pass now ("progress", counted over the test gates of the
      FINAL ladder only: one fix shows up in sanitizers or budgets and not in unit-tests, and a step's own gate
      profile may not run that gate at all, so between steps (a) is enough).

coverage (``baseline: coverage``): no module below its base coverage or its threshold, whichever is
lower; a module whose own tests already failed on the base may still fail. Gates whose base was
green, gates without a parsable base (e.g. the build failed) and every other gate (build,
static, policy) stay absolute. ``loop.baseline: auto`` (default) turns the rule on only when the
base is not green; ``off`` keeps every gate absolute.

The model sees only new failures and still-failing required tests, plus the number of
pre-existing failures as context. Everything else -- the base, the delta, the rule -- goes to
the audit trail (``gates/*.json``, ``summary.json``).
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any, Iterable

# "  5/13 Test  #2: test_clients .....................***Failed    0.03 sec"
_DONE = re.compile(r"^\s*\d+/\d+\s+Test\s+#\d+:\s+(?P<name>\S+)\s+\.*\s*(?P<stars>\*{3})?"
                   r"(?P<res>Passed|Failed|Exception: \S+|Timeout|Not Run.*?|\w+)\s+[\d.]+ sec")
_CASE = re.compile(r"^(?P<file>[^\s:]+\.c):(?P<line>\d+):(?P<case>\w+):(?P<res>PASS|FAIL|IGNORE)\b")
_UNITY_SUMMARY = re.compile(r"^\d+ Tests \d+ Failures \d+ Ignored")
_END = re.compile(r"^\d+% tests passed")
_MODULE = re.compile(r"^(?P<mod>\w+) lines (?P<pct>[\d.]+|\?)% from \d+ own test\(s\) \(min (?P<min>[\d.]+)%\)")
_MODULE_FAIL = re.compile(r"^tests of (?P<mod>\w+) failed in the coverage build")
_MODULE_NONE = re.compile(r"^no (?:unit tests|objects) for (?P<mod>\w+)")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_TEST_NAME = re.compile(r"\btest_[A-Za-z0-9_]+\b")

MODES = ("auto", "off")
KINDS = ("tests", "coverage")
REQUIRED_SECTIONS = ("acceptance tests", "required tests")
COVERAGE_SLACK = 0.05       # gcovr rounds to one decimal


# ------------------------------------------------------------------ parsing ----
def parse_tests(log: str) -> dict[str, Any] | None:
    """ctest + Unity output -> {"execs": {name: {"status", "why", "complete", "cases"}}}.

    None when the log has no per-test lines (the build failed, the manifest check failed,
    the log is missing): such a run cannot be compared with anything."""
    execs: dict[str, dict[str, Any]] = {}
    current: dict[str, Any] | None = None
    for raw in _ANSI.sub("", log).splitlines():
        line = raw.strip()
        m = _DONE.match(raw)
        if m:
            res = m.group("res")
            passed = res == "Passed"
            why = res.split(":", 1)[-1].strip() if res.startswith("Exception") else res
            current = execs[m.group("name")] = {"status": "pass" if passed else "fail", "why": why,
                                                "complete": passed, "cases": {}}
            continue
        if _END.match(line):
            current = None
            continue
        if current is None:
            continue
        m = _CASE.match(line)
        if m:
            current["cases"][m.group("case")] = m.group("res").lower()
        elif _UNITY_SUMMARY.match(line):
            current["complete"] = True
    return {"execs": execs} if execs else None


def parse_coverage(log: str) -> dict[str, Any] | None:
    """check.sh coverage output -> {"modules": {name: {"pct": float | None, "min", "state"}}, "all_failed"}."""
    modules: dict[str, dict[str, Any]] = {}
    all_failed = False
    for raw in _ANSI.sub("", log).splitlines():
        line = raw.strip()
        m = _MODULE.match(line)
        if m:
            pct = None if m.group("pct") == "?" else float(m.group("pct"))
            modules[m.group("mod")] = {"pct": pct, "min": float(m.group("min")),
                                       "state": "ok" if pct is not None else "unknown"}
            continue
        m = _MODULE_FAIL.match(line)
        if m:
            modules[m.group("mod")] = {"pct": None, "min": None, "state": "tests-fail"}
            continue
        m = _MODULE_NONE.match(line)
        if m:
            modules[m.group("mod")] = {"pct": None, "min": None, "state": "no-tests"}
            continue
        if line.startswith("info: a test failed in the all-tests coverage run"):
            all_failed = True
    if not modules:
        return None
    return {"modules": modules, "all_failed": all_failed}


def parse(kind: str, log: str) -> dict[str, Any] | None:
    return parse_coverage(log) if kind == "coverage" else parse_tests(log)


def segments(log: str) -> dict[str, str]:
    """The output ctest printed for each test (--output-on-failure): name -> text."""
    out: dict[str, list[str]] = {}
    current: list[str] | None = None
    for raw in log.splitlines():
        m = _DONE.match(_ANSI.sub("", raw))
        if m:
            current = out.setdefault(m.group("name"), [])
            continue
        if _END.match(_ANSI.sub("", raw).strip()):
            current = None
            continue
        if current is not None:
            current.append(raw)
    return {k: "\n".join(v) for k, v in out.items()}


def required_tests(sections: dict[str, str]) -> list[str]:
    """test_* names of the ticket's "Acceptance tests" / "Required tests" section, in order."""
    names: list[str] = []
    for title, body in sections.items():
        if title.strip().lower() not in REQUIRED_SECTIONS:
            continue
        for name in _TEST_NAME.findall(re.sub(r"<!--.*?-->", "", body, flags=re.S)):
            if name not in names:
                names.append(name)
    return names


# --------------------------------------------------------------- comparing ----
def failures(results: dict[str, Any] | None) -> list[str]:
    """Failure units: "exec:case" per failing Unity case, "exec" for a test that failed without
    running to its end (crash, sanitizer abort, timeout) or without case output."""
    out: list[str] = []
    for name, e in ((results or {}).get("execs") or {}).items():
        if e["status"] == "pass":
            continue
        units = [f"{name}:{c}" for c, s in e["cases"].items() if s == "fail"]
        if not e["complete"] or not units:
            units.append(name)
        out += units
    return out


def _covered(unit: str, base: dict[str, Any]) -> bool:
    """Did the base already have this failure (or could not tell, because it aborted earlier)?"""
    execs = base.get("execs") or {}
    name, _, case = unit.partition(":")
    b = execs.get(name)
    if b is None or b["status"] == "pass":
        return False
    if not case:
        return not b["complete"] or not [c for c, s in b["cases"].items() if s == "fail"]
    if b["cases"].get(case) == "fail":
        return True
    # the base aborted before it reached this case: unknown, so not this change's failure
    return not b["complete"] and case not in b["cases"]


def _fixed(unit: str, now: dict[str, Any]) -> bool:
    execs = now.get("execs") or {}
    name, _, case = unit.partition(":")
    n = execs.get(name)
    if n is None:
        return False
    if n["status"] == "pass":
        return True
    if case:
        return n["cases"].get(case) == "pass"
    return n["complete"] and bool(n["cases"])   # runs to its end now (its case failures are judged apart)


def compare_tests(base: dict[str, Any], now: dict[str, Any]) -> dict[str, list[str]]:
    """{"new": failures the base did not have, "pre_existing": failures it had, "fixed": base
    failures that pass now}."""
    now_units = failures(now)
    new = [u for u in now_units if not _covered(u, base)]
    pre = [u for u in now_units if u not in new]
    fixed = [u for u in failures(base) if _fixed(u, now)]
    return {"new": new, "pre_existing": pre, "fixed": fixed}


def locate(name: str, repo: Path | None) -> str | None:
    """The ctest name that runs test ``name``: itself when tests/**/<name>.c exists, else the
    stem of the test file that defines ``void <name>(void)``. None: not in the tree."""
    if repo is None:
        return None
    tests = Path(repo) / "tests"
    if not tests.is_dir():
        return None
    files = sorted(tests.rglob("*.c"))
    for f in files:
        if f.stem == name:
            return name
    pat = re.compile(r"\bvoid\s+" + re.escape(name) + r"\s*\(\s*void\s*\)")
    for f in files:
        try:
            if pat.search(f.read_text(encoding="utf-8", errors="replace")):
                return f.stem
        except OSError:
            continue
    return None


def required_status(name: str, base: dict[str, Any], now: dict[str, Any] | None,
                    now_ok: bool, repo: Path | None) -> str:
    """pass | fail | missing (in this gate, but not run) | absent (not a test of this gate)."""
    nexec = (now or {}).get("execs") or {}
    bexec = base.get("execs") or {}
    owner: str | None = name if (name in nexec or name in bexec) else None
    if owner is None:
        owner = next((exe for src in (nexec, bexec) for exe, e in src.items() if name in e["cases"]), None)
    if owner is None:
        owner = locate(name, repo)
    if owner is None or (owner not in nexec and owner not in bexec):
        return "absent"
    n = nexec.get(owner)
    if n is None:
        # no per-test results now: a passing gate ran every test of the base green
        return "pass" if now is None and now_ok else "missing"
    if owner == name or n["status"] == "pass":
        return "pass" if n["status"] == "pass" else "fail"
    state = n["cases"].get(name)
    if state == "pass":
        return "pass"
    return "fail" if state == "fail" or not n["complete"] else "missing"


def compare_coverage(base: dict[str, Any], now: dict[str, Any]) -> dict[str, Any]:
    """Modules below min(base coverage, threshold) or newly failing; base problems stay context."""
    bad: list[str] = []
    pre: list[str] = []
    fixed: list[str] = []
    bmods = base.get("modules") or {}
    for mod, n in (now.get("modules") or {}).items():
        b = bmods.get(mod)
        if n["pct"] is not None:
            if b is None:
                floor = n["min"] or 0.0
            elif b["pct"] is None:
                floor = 0.0
                fixed.append(mod)
            else:
                floor = min(b["pct"], n["min"] if n["min"] is not None else b["pct"])
            if n["pct"] + COVERAGE_SLACK < floor:
                bad.append(f"{mod} {n['pct']:.1f}% (needs {floor:.1f}%: its base coverage or its threshold, "
                           "whichever is lower)")
            elif b is not None and b["pct"] is not None and n["min"] is not None and b["pct"] < n["min"]:
                pre.append(f"{mod} {n['pct']:.1f}% (below its {n['min']:.0f}% threshold on the base too)")
            continue
        if b is not None and b["pct"] is None and b["state"] == n["state"]:
            pre.append(f"{mod} ({n['state']})")
        else:
            bad.append(f"{mod} ({n['state']})")
    if now.get("all_failed"):
        (pre if base.get("all_failed") else bad).append("all-tests coverage run (a test fails)")
    return {"new": bad, "pre_existing": pre, "fixed": fixed}


def gate_ids(cfg: Any, workflow: Any) -> list[str]:
    """Test-type gates (gates.yaml ``baseline:``) of every profile the workflow's steps use, in
    ladder order."""
    seen: list[str] = []
    for step in workflow.steps:
        spec = step.gates or ("fast" if step.role and "run_gates" in cfg.role(step.role).tools else None)
        if not spec:
            continue
        for g in cfg.gates.resolve(spec):
            if g.baseline and g.id not in seen:
                seen.append(g.id)
    order = list(cfg.gates.gates)
    return sorted(seen, key=order.index)


def describe(baseline: dict[str, Any] | None) -> str:
    """One line on the base for prompts and the console."""
    if not baseline:
        return ""
    if not baseline.get("active"):
        return ""
    parts = []
    for gid, g in (baseline.get("gates") or {}).items():
        if g.get("status") == "FAIL" and g.get("results") is not None:
            parts.append(f"{gid} {len(failures(g['results'])) if g.get('kind') == 'tests' else 'coverage'}")
    return "base failures: " + ", ".join(parts)


def short(units: Iterable[str], limit: int = 12) -> str:
    units = list(units)
    text = ", ".join(units[:limit])
    return text + (f" (+{len(units) - limit} more)" if len(units) > limit else "")
