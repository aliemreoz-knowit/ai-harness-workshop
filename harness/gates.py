"""Gate runner: scripts/check.sh (and Python gates) -> structured feedback.

The model never sees a raw build log. Every failing gate is reduced to a few
``FeedbackItem``s (gate, file, line, rule, message, excerpt): cheap to read,
cheap in tokens, and stable enough to compare retries across variants.
"""

from __future__ import annotations

import importlib
import os
import re
import subprocess
import sys
import threading
import time
from dataclasses import asdict, dataclass, field
from pathlib import Path
from typing import Any, Callable, Mapping

from . import baseline as bl
from .config import GateDef, HarnessConfig
from .envdetect import Environment

# The SKIP reason of a gate the Docker-free ladder (scripts/lite_gates.py) cannot run: "not run here",
# not a setup problem. Such a SKIP counts like fuzz's allowed SKIP, and the ladder names those gates.
NOT_RUN_HERE = "not run here"

# "unit-tests  FAIL    123 ms (budget   2 s OVER BUDGET)  unit                   reason"
_LINE = re.compile(r"^(?P<id>[a-z][a-z0-9]*(?:-[a-z0-9]+)*|G\d+|diff-policy)\s+(?P<status>PASS|FAIL|SKIP)\s+(?P<ms>\d+) ms \(budget\s+(?P<budget>\d+) s"
                   r"(?P<over> OVER BUDGET)?\)\s+(?P<rest>.*)$")


@dataclass
class FeedbackItem:
    gate: str
    file: str = ""
    line: int = 0
    rule: str = ""
    message: str = ""
    excerpt: str = ""

    def key(self) -> tuple[str, str, int, str]:
        return (self.gate, self.file, self.line, self.rule)

    def render(self) -> str:
        loc = f"{self.file}:{self.line}" if self.file and self.line else (self.file or "-")
        rule = f" [{self.rule}]" if self.rule else ""
        text = f"- {self.gate} {loc}{rule} {self.message}".rstrip()
        if self.excerpt:
            text += "\n" + "\n".join("    " + ln for ln in self.excerpt.splitlines())
        return text


@dataclass
class GateResult:
    id: str
    name: str
    status: str                  # PASS | FAIL | SKIP | NOT_RUN | ERROR
    ms: int = 0
    budget_s: float = 0
    over_budget: bool = False
    reason: str = ""
    items: list[FeedbackItem] = field(default_factory=list)
    log: str = ""                # path of the gate log (repo-relative when possible)
    # test-type gates (gates.yaml baseline: tests | coverage): the parsed per-test results and,
    # under the baseline rule, how this run compares with the untouched base (harness/baseline.py)
    results: dict[str, Any] | None = None
    delta: dict[str, Any] = field(default_factory=dict)

    @property
    def ok(self) -> bool:
        return self.status in ("PASS", "SKIP")

    @property
    def not_run_here(self) -> bool:
        """SKIPped by the Docker-free ladder: the gate needs the container (scripts/lite_gates.py)."""
        return self.status == "SKIP" and self.reason == NOT_RUN_HERE

    @property
    def allowed_skip(self) -> bool:
        """A SKIP that does not make the ladder incomplete: fuzz (the fuzz smoke runs centrally) and a gate
        the Docker-free ladder does not run here. Every other SKIP is a setup problem."""
        return self.status == "SKIP" and (self.id == "fuzz" or self.not_run_here)


@dataclass
class LadderResult:
    gates: list[GateResult]
    duration_ms: int = 0
    exit_codes: list[int] = field(default_factory=list)
    commands: list[list[str]] = field(default_factory=list)
    output_tail: str = ""
    baseline: dict[str, Any] = field(default_factory=dict)   # the baseline rule of this ladder (if on)

    @property
    def ok(self) -> bool:
        # fuzz may SKIP (central harness only), and so may a gate the Docker-free ladder does not run
        # here (lite_note() names them); any other SKIP means the ladder is incomplete
        for g in self.gates:
            if g.status == "SKIP" and not g.allowed_skip:
                return False
            if not g.ok:
                return False
        return bool(self.gates)

    @property
    def failed(self) -> list[GateResult]:
        return [g for g in self.gates if g.status in ("FAIL", "ERROR")]

    @property
    def skipped(self) -> list[GateResult]:
        """Gates that SKIPped where a SKIP makes the ladder incomplete (not fuzz, not "not run here")."""
        return [g for g in self.gates if g.status == "SKIP" and not g.allowed_skip]

    @property
    def not_run_here(self) -> list[GateResult]:
        """Gates the Docker-free ladder did not run (they need the container)."""
        return [g for g in self.gates if g.not_run_here]

    def lite_note(self) -> str:
        """'not run here: fuzz coverage', or '' when every gate could run."""
        ids = [g.id for g in self.not_run_here]
        return f"not run here: {' '.join(ids)}" if ids else ""

    @property
    def incomplete_only(self) -> bool:
        """Nothing failed, the ladder is only incomplete: a setup problem no agent edit can fix."""
        return not self.ok and not self.failed and bool(self.skipped)             and all(g.status in ("PASS", "SKIP") for g in self.gates)

    def _statuses(self) -> str:
        return " ".join(f"{g.id}:{g.status}" for g in self.gates)

    def summary(self) -> str:
        note = self.lite_note()
        return self._statuses() + (f"; {note}" if note else "")

    def baseline_note(self) -> str:
        """Gates that passed by the baseline rule although some tests fail: what is pre-existing."""
        lines = []
        for g in self.gates:
            d = g.delta
            if g.status != "PASS" or d.get("raw_status") != "FAIL":
                continue
            fixed = f"; fixed by this change: {bl.short(d.get('fixed') or [], 6)}" if d.get("fixed") else ""
            lines.append(f"{g.id} {g.name}: PASS by the baseline rule -- {len(d.get('pre_existing') or [])} "
                         f"failure(s) were already failing on the untouched base (context, not caused by "
                         f"this change), no new failure{fixed}")
        return "\n".join(lines)

    def feedback(self, max_items: int = 6) -> str:
        """Compact feedback for the model: only failing gates, at most ``max_items`` items. Under the
        baseline rule only new failures and still-failing required tests are items; failures the
        untouched base already had are counted as context."""
        note = self.baseline_note()
        lite = self.lite_note()
        if self.ok:
            return f"GATES PASS ({self._statuses()})" + "".join(f"\n{x}" for x in (lite, note) if x)
        lines = [f"GATES FAIL ({self._statuses()})"] + ([lite] if lite else [])
        budget = max_items
        for g in self.gates:
            if g.status == "SKIP" and not g.allowed_skip:
                lines.append(f"{g.id} {g.name}: SKIPPED ({g.reason}) -- the ladder is incomplete")
            if g.status not in ("FAIL", "ERROR"):
                continue
            lines.append(f"{g.id} {g.name}: {g.reason}".rstrip(": "))
            for item in g.items[:budget]:
                lines.append(item.render())
            budget -= min(budget, len(g.items))
            if len(g.items) > max_items:
                lines.append(f"  (+{len(g.items) - max_items} more items not shown)")
            shown = {n for n, st in (g.delta.get("required") or {}).items() if st in ("fail", "missing")}
            pre = [u for u in g.delta.get("pre_existing") or []
                   if u not in shown and u.partition(":")[2] not in shown]
            if pre and not g.delta.get("no_progress"):
                lines.append(f"  ({len(pre)} other failure(s) in {g.id} were already failing on the untouched "
                             "base: context only, not caused by this change and not yours to fix)")
        if note:
            lines.append(note)
        not_run = [g.id for g in self.gates if g.status == "NOT_RUN"]
        if not_run:
            lines.append(f"not run (stopped at the first failing batch): {' '.join(not_run)}")
        return "\n".join(lines)

    def to_dict(self) -> dict[str, Any]:
        out = {"ok": self.ok, "summary": self.summary(), "duration_ms": self.duration_ms,
               "exit_codes": self.exit_codes, "commands": self.commands,
               "not_run_here": [g.id for g in self.not_run_here],
               "gates": [asdict(g) for g in self.gates]}
        if self.baseline:
            out["baseline"] = self.baseline
        return out


@dataclass
class GateContext:
    """What Python gates need to know about the run."""
    repo: Path
    allowed_writes: list[str]
    policy: dict[str, Any]
    expect_changes: bool = False
    baseline: frozenset[str] = frozenset()   # paths already dirty before the run started
    # the test results of the untouched base (state "test_baseline", when the rule is on) and the
    # ticket's required tests; final = the full ladder of a gates step (a required test must exist)
    test_baseline: dict[str, Any] | None = None
    required_tests: tuple[str, ...] = ()
    final: bool = False
    # names the gate container ts-gate-<run_id>-<n> on a host, so a timed-out ladder can be killed
    run_id: str = ""


Runner = Callable[..., subprocess.CompletedProcess]
# seconds after a gate timeout before subprocess.run gives up on its own (the watchdog has
# killed the container by then; this only bounds a container that never started)
KILL_GRACE_S = 60.0


class GateRunner:
    def __init__(self, cfg: HarnessConfig, env: Environment, runner: Runner = subprocess.run,
                 clock: Callable[[], float] = time.monotonic) -> None:
        self.cfg = cfg
        self.env = env
        self.runner = runner
        self.clock = clock
        self.max_items = int(cfg.loop.get("feedback_max_items", 6))
        self.excerpt_lines = int(cfg.loop.get("feedback_excerpt_lines", 2))
        self.containers = 0          # check.sh invocations so far (the gate container's number)

    def run(self, spec: str | list[str], ctx: GateContext, parse_all: bool = False) -> LadderResult:
        """``parse_all``: parse the per-test results of every test-type gate (the baseline capture)."""
        gates = self.cfg.gates.resolve(spec)
        t0 = self.clock()
        results: list[GateResult] = []
        exit_codes: list[int] = []
        commands: list[list[str]] = []
        tail = ""
        batches = self._batches(gates)
        stopped = False
        for batch in batches:
            if stopped:
                results += [GateResult(g.id, g.name, "NOT_RUN", budget_s=g.budget_s) for g in batch]
                continue
            if batch[0].kind == "python":
                for g in batch:
                    r = self._run_python(g, ctx)
                    results.append(r)
                    if not r.ok and g.stops:
                        stopped = True
                continue
            batch_results, rc, cmd, out_tail = self._run_check(batch, ctx, parse_all)
            if cmd:                          # [] = the batch did not start (an env the harness does not allow):
                exit_codes.append(rc)        # no exit code, command or output of its own
                commands.append(cmd)
                tail = out_tail
            results += batch_results
            if any(not r.ok for r in batch_results):
                stopped = True
        ladder = LadderResult(results, int((self.clock() - t0) * 1000), exit_codes, commands, tail)
        if ctx.test_baseline:
            ladder.baseline = self._ladder_rule(ladder, ctx)
        return ladder

    def _batches(self, gates: list[GateDef]) -> list[list[GateDef]]:
        batches: list[list[GateDef]] = []
        for g in gates:
            same_env = batches and batches[-1][0].env == g.env
            if (batches and batches[-1][0].kind == g.kind == "check" and same_env
                    and not self.cfg.gates.stop_on_fail):
                batches[-1].append(g)
            else:
                batches.append([g])
        return batches

    @staticmethod
    def _refused_env(batch: list[GateDef]) -> list[GateResult]:
        """When a gate of the batch has an env key that is not allowed (disallowed_env_key): an ERROR
        result for it and NOT_RUN for the others, so the batch does not start. [] when all are fine."""
        keys = {g.id: disallowed_env_key(g.env) for g in batch}
        if all(k is None for k in keys.values()):
            return []
        return [GateResult(g.id, g.name, "NOT_RUN", budget_s=g.budget_s) if keys[g.id] is None else
                GateResult(g.id, g.name, "ERROR", budget_s=g.budget_s,
                           reason=f"gate {g.id}: env {keys[g.id]} is not allowed") for g in batch]

    # -- python gates -------------------------------------------------------------
    def _run_python(self, gate: GateDef, ctx: GateContext) -> GateResult:
        t0 = self.clock()
        try:
            mod_name, fn_name = gate.fn.split(":", 1)
            fn = getattr(importlib.import_module(mod_name), fn_name)
            result: GateResult = fn(ctx, gate)
        except Exception as exc:  # noqa: BLE001
            result = GateResult(gate.id, gate.name, "ERROR", reason=f"python gate crashed: {exc}")
        result.ms = int((self.clock() - t0) * 1000)
        result.budget_s = gate.budget_s
        result.over_budget = bool(gate.budget_s and result.ms > gate.budget_s * 1000)
        return result

    # -- check.sh gates -----------------------------------------------------------
    def _run_check(self, batch: list[GateDef], ctx: GateContext | None = None,
                   parse_all: bool = False) -> tuple[list[GateResult], int, list[str], str]:
        """Run one batch of check.sh gates: (results, exit code, command, output tail).

        A gate env the harness does not allow (disallowed_env_key) is refused here, before anything
        is built or merged into an environment: ERROR for that gate, NOT_RUN for the others, exit
        code -1, no command ([]), so every caller is covered, not only run()."""
        refused = self._refused_env(batch)
        if refused:
            return refused, -1, [], ""
        cmd = self.env.gate_command(self.cfg.gates.script, self.cfg.gates.wrapper, [g.fn for g in batch],
                                    self_wrapping=self.cfg.gates.self_wrapping)
        extra = {"TS_CHECK_LOG": str(self.cfg.paths.get("check_log", "build-check/harness")),
                 "TS_PYTHON": sys.executable}         # the Docker-free ladder runs on this Python (zig in it)
        for g in batch:
            extra.update(g.env)
        # HARNESS_RUN_ID (set by whoever started the harness, else this run's id) reaches the gate
        # scripts; dockerbuild.sh labels the container tinystream.run=<it>, so a stopped run's
        # containers can be found by label. On a host the container is also named after this
        # run (unique even when several runs share one HARNESS_RUN_ID), so that a timed-out
        # ladder -- the timeout kills bash and the docker client, never the container -- is killed.
        run_id = _container_safe(ctx.run_id if ctx is not None and ctx.run_id else "harness")
        extra["HARNESS_RUN_ID"] = os.environ.get("HARNESS_RUN_ID") or run_id
        container = ""
        if not self.env.in_container:
            self.containers += 1
            container = f"ts-gate-{run_id}-{self.containers}"
            extra["TS_CONTAINER_NAME"] = container
        timeout = sum(g.timeout_s for g in batch)
        # The timeout of subprocess.run kills bash, not the container, and the docker client keeps
        # the pipes open: on Windows run() then waits for the container to finish on its own, on
        # Linux the container runs on, orphaned. A watchdog kills the named container at the
        # timeout instead (the client exits, the pipes close); run()'s own timeout, a grace period
        # later, stays as the backstop for a container that never started.
        fired = threading.Event()
        note: list[str] = []
        timer: threading.Timer | None = None
        if container:
            def watchdog() -> None:
                fired.set()
                note.append(self._kill_container(container))
            timer = threading.Timer(timeout, watchdog)
            timer.daemon = True
            timer.start()
        try:
            proc = self.runner(cmd, cwd=str(self.cfg.repo), env=self.env.gate_env(extra=extra),
                               capture_output=True, text=True, encoding="utf-8", errors="replace",
                               timeout=timeout + (KILL_GRACE_S if container else 0))
            out, rc = (proc.stdout or "") + (proc.stderr or ""), proc.returncode
        except subprocess.TimeoutExpired as exc:
            out = str(exc.stdout or "") if not isinstance(exc.stdout, bytes) else exc.stdout.decode(errors="replace")
            if timer is not None:
                timer.cancel()
            if container and not fired.is_set():
                note.append(self._kill_container(container))
            return self._timed_out(batch, out, timeout, "".join(note)), -1, cmd, out[-2000:]
        except OSError as exc:
            return ([GateResult(g.id, g.name, "ERROR", budget_s=g.budget_s,
                                reason=f"cannot start the gate runner: {exc}") for g in batch], -1, cmd, "")
        finally:
            if timer is not None:
                timer.cancel()
        if fired.is_set():
            # killed by the watchdog: whatever the partial output says, the batch did not finish
            return self._timed_out(batch, out, timeout, "".join(note)), rc, cmd, out[-2000:]
        parsed = parse_check_output(out)
        results: list[GateResult] = []
        for g in batch:
            r = parsed.get(g.id)
            if r is None:
                reason = _last_lines(out, 3) or f"check.sh exit {rc}"
                r = GateResult(g.id, g.name, "ERROR", reason=f"no result line from check.sh: {reason}")
                r.items = parse_log("compiler", out, g.id, self.cfg.repo, self.excerpt_lines)
            r.budget_s = g.budget_s
            log_path = self.cfg.check_log_dir / f"{g.id}.log"
            log = ""
            if r.status == "FAIL":
                r.log = _rel(log_path, self.cfg.repo)
                log = _read(log_path)
                r.items = parse_log(g.parser, log, g.id, self.cfg.repo, self.excerpt_lines) or \
                    [FeedbackItem(g.id, message=r.reason or "gate failed", excerpt=_last_lines(log, 6))]
            active = bool(ctx is not None and ctx.test_baseline)
            if g.baseline and r.status in ("PASS", "FAIL") and (parse_all or active):
                r.results = bl.parse(g.baseline, log or _read(log_path))
                if active and ctx is not None:
                    self._apply_baseline(r, g, log, ctx)
            results.append(r)
        return results, rc, cmd, out[-2000:]

    @staticmethod
    def _timed_out(batch: list[GateDef], out: str, timeout: float, note: str) -> list[GateResult]:
        """A batch stopped at its timeout: the gates that printed a result line before keep it,
        the rest are ERROR."""
        parsed = parse_check_output(out)
        return [parsed.get(g.id) or GateResult(g.id, g.name, "ERROR", budget_s=g.budget_s,
                                               reason=f"ladder timed out after {timeout:.0f} s{note}")
                for g in batch]

    def _kill_container(self, name: str) -> str:
        """``docker kill`` the named gate container after a timeout (the container would otherwise
        run on, orphaned, with the build directory mounted); what happened, for the gate reason."""
        docker = self.env.docker or "docker"
        try:
            proc = self.runner([docker, "kill", name], capture_output=True, text=True, encoding="utf-8",
                               errors="replace", timeout=60)
        except Exception as exc:  # noqa: BLE001 - the ladder result matters more than the kill
            return f"; docker kill {name} failed: {exc}"
        rc = getattr(proc, "returncode", 1)
        if rc == 0:
            return f"; container {name} killed"
        return f"; container {name} not running or not killed (docker kill exit {rc})"

    # -- the baseline rule (harness/baseline.py) ------------------------------------
    def _apply_baseline(self, r: GateResult, g: GateDef, log: str, ctx: GateContext) -> None:
        """Judge one test-type gate against the untouched base: PASS when it has no new failure and
        every required test that belongs to it passes; items = only those two kinds of failure."""
        base = ((ctx.test_baseline or {}).get("gates") or {}).get(g.id) or {}
        if base.get("status") != "FAIL" or base.get("results") is None:
            return                                   # green or unparsable base: absolute
        if r.status == "FAIL" and (r.results is None or any(it.rule == "missing-tests" for it in r.items)):
            r.delta = {"rule": "absolute", "raw_status": "FAIL",
                       "why": "no per-test results to compare (build or test-manifest failure)"}
            return
        raw = r.status
        if g.baseline == "coverage":
            if raw == "FAIL" and r.results:
                cmp = bl.compare_coverage(base["results"], r.results)
            else:
                cmp = {"new": [], "pre_existing": [], "fixed": sorted(base["results"].get("modules") or {})}
            mods = {u.split(" ", 1)[0] for u in cmp["new"]}
            items = [it for it in r.items if it.rule.split(":", 1)[-1] in mods]
            covered = {it.rule.split(":", 1)[-1] for it in items}
            items += [FeedbackItem(g.id, "", 0, "coverage:" + u.split(" ", 1)[0], u)
                      for u in cmp["new"] if u.split(" ", 1)[0] not in covered]
            r.delta = {"rule": "coverage", "raw_status": raw, **cmp}
            r.status = "FAIL" if cmp["new"] else "PASS"
            r.items = items if cmp["new"] else []
            if cmp["new"]:
                r.reason = (f"baseline rule: {len(cmp['new'])} module(s) below min(base coverage, threshold) "
                            "or newly failing")
            return
        if raw == "PASS":
            cmp = {"new": [], "pre_existing": [], "fixed": bl.failures(base["results"])}
        else:
            cmp = bl.compare_tests(base["results"], r.results or {})
        required = {name: bl.required_status(name, base["results"], r.results, raw == "PASS", self.cfg.repo)
                    for name in ctx.required_tests}
        req_fail = [n for n, s in required.items() if s in ("fail", "missing")]
        items = self._items_for(g, log, cmp["new"], base["results"], False) if cmp["new"] else []
        if req_fail:
            items += self._items_for(g, log, req_fail, base["results"], True)
        seen: set[tuple[str, str, int, str]] = set()
        unique = []
        for it in items:
            if it.key() not in seen:
                seen.add(it.key())
                unique.append(it)
        r.items = unique
        r.delta = {"rule": "required" if ctx.required_tests else "progress", "raw_status": raw,
                   "new": cmp["new"], "pre_existing": cmp["pre_existing"], "fixed": cmp["fixed"],
                   "required": required}
        r.status = "FAIL" if (cmp["new"] or req_fail) else "PASS"
        if r.status == "FAIL":
            why = []
            if cmp["new"]:
                why.append(f"{len(cmp['new'])} new failure(s) (passed on the untouched base, or a new test)")
            if req_fail:
                why.append(f"required test(s) not passing: {', '.join(req_fail)}")
            r.reason = "baseline rule: " + "; ".join(why)

    def _items_for(self, g: GateDef, log: str, units: list[str], base: dict[str, Any],
                   required: bool) -> list[FeedbackItem]:
        """Structured items for failure units ("exec" or "exec:case"; with ``required``: test
        names from the ticket), parsed from each test's own section of the log."""
        segs = bl.segments(log)
        by_exec: dict[str, set[str]] = {}
        whole: set[str] = set()
        for u in units:
            exe, _, case = u.partition(":")
            if required and exe not in segs:          # a required case name: find the test that runs it
                owner = next((e for e, s in segs.items()
                              if re.search(rf":{re.escape(u)}:(?:\x1b\[[0-9;]*m)?(?:FAIL|PASS|IGNORE)", s)), None)
                owner = owner or bl.locate(u, self.cfg.repo)
                exe, case = (owner, u) if owner and owner != u else (u, "")
            b = (base.get("execs") or {}).get(exe)
            by_exec.setdefault(exe, set())
            if case:
                by_exec[exe].add(case)
            if not case or b is None or b["status"] == "pass":
                whole.add(exe)
        out: list[FeedbackItem] = []
        for exe, cases in by_exec.items():
            seg = segs.get(exe, "")
            found = parse_log(g.parser, seg, g.id, self.cfg.repo, self.excerpt_lines) if seg else []
            names = set(re.findall(r":(\w+):(?:PASS|FAIL|IGNORE)", _ANSI.sub("", seg)))
            keep = [it for it in found
                    if it.rule in cases or (exe in whole and (it.rule not in names or not cases))]
            if not keep:
                what = f"{exe}:{', '.join(sorted(cases))}" if cases else exe
                msg = ("required by the ticket (Acceptance tests) and not passing" if required
                       else "fails now; it passed on the untouched base (or it is a new test)")
                keep = [FeedbackItem(g.id, "", 0, what, msg)]
            elif required:
                for it in keep:
                    it.message = f"{it.message} (required by the ticket)"
            out += keep
        return out

    def _ladder_rule(self, ladder: LadderResult, ctx: GateContext) -> dict[str, Any]:
        """Ladder-wide part of the rule: required tests no gate runs, and "progress" (no required
        tests named: at least one base failure must pass now, over all test gates of this ladder).

        Both are judged on the final ladder only (``ctx.final``: the full ladder of a gates step).
        A step's own gate profile may not even run the gate where the fix shows (a sanitizer or
        budget failure fixed under sanitizers / budgets is invisible to unit-tests / property-tests), so between steps a test gate
        passes on no new failure and the required tests it runs: the feedback never sends an
        agent after failures the untouched base already had."""
        aware = [g for g in ladder.gates if g.delta.get("rule") in ("progress", "required")]
        info: dict[str, Any] = {
            "rule": ("required" if ctx.required_tests else "progress") if aware else "absolute",
            "required": list(ctx.required_tests),
            "final": bool(ctx.final),
            "gates": {g.id: dict(g.delta) for g in ladder.gates if g.delta},
        }
        if not aware:
            return info
        fixed = [f"{g.id} {u}" for g in aware for u in g.delta.get("fixed") or []]
        info["fixed"] = fixed
        info["new"] = [f"{g.id} {u}" for g in ladder.gates for u in g.delta.get("new") or []]
        info["pre_existing"] = sum(len(g.delta.get("pre_existing") or []) for g in ladder.gates)
        if ctx.required_tests:
            unknown = [n for n in ctx.required_tests
                       if all(g.delta.get("required", {}).get(n) == "absent" for g in aware)]
            info["not_found"] = unknown
            if unknown and ctx.final:
                g = aware[0]
                g.status = "FAIL"
                g.reason = f"baseline rule: required test(s) not run by this ladder: {', '.join(unknown)}"
                g.items += [FeedbackItem(g.id, "tests/", 0, n, "required by the ticket (Acceptance tests), but no "
                                         "gate of the ladder runs it: add the test and register it in "
                                         "scripts/test_manifest.txt") for n in unknown]
            return info
        info["progress"] = bool(fixed)
        if not fixed and ctx.final:
            pre = sorted({u for g in aware for u in g.delta.get("pre_existing") or []})
            for i, g in enumerate(aware):
                g.status = "FAIL"
                g.delta["no_progress"] = True
                g.reason = ("baseline rule: no progress -- none of the failures the untouched base had "
                            "passes now" + ("" if i == 0 else f" (see {aware[0].id})"))
                if i == 0:
                    g.items.append(FeedbackItem(
                        g.id, "", 0, "progress",
                        "the full ladder shows no test passing that failed on the untouched base, so the "
                        "symptom the ticket describes is still there (the ticket names no acceptance tests, "
                        "so the ladder looks for any base failure that passes now). Work from the ticket: "
                        f"the {len(pre)} failure(s) the base already had are context, not a list to fix -- "
                        f"{bl.short(pre, 12)}"))
        return info


# What a gate's `env` (gates.yaml) may put into the environment of scripts/check.sh: TS_* knobs
# (fuzz sets TS_FUZZ) and nothing else. These are the harness's own: TS_IN_CONTAINER=1 makes
# check.sh skip docker on a host and build and run code there, TS_CONTAINER_NAME and TS_IMAGE
# decide which container that is, TS_CHECK_LOG is set from harness.yaml's paths.check_log,
# TS_PYTHON (the harness's Python) runs the Docker-free ladder, TS_LITE=1 picks that ladder.
_GATE_ENV_KEY = re.compile(r"TS_[A-Z0-9_]+")          # matched whole: a trailing newline is no key
_HARNESS_ENV_KEYS = frozenset({"TS_IN_CONTAINER", "TS_CONTAINER_NAME", "TS_IMAGE", "TS_CHECK_LOG", "TS_PYTHON",
                               "TS_LITE"})


def disallowed_env_key(env: Mapping[str, str]) -> str | None:
    """The first key of a gate's env that must not reach check.sh, None when all may."""
    for key in env:
        if key in _HARNESS_ENV_KEYS or not _GATE_ENV_KEY.fullmatch(key):
            return key
    return None


def _container_safe(name: str) -> str:
    """A docker container name part: [a-zA-Z0-9_.-], starting with an alphanumeric."""
    s = re.sub(r"[^a-zA-Z0-9_.-]+", "-", name).strip("-.") or "harness"
    return s if s[0].isalnum() else "r" + s


def parse_check_output(out: str) -> dict[str, GateResult]:
    results: dict[str, GateResult] = {}
    for raw in out.splitlines():
        m = _LINE.match(raw.strip())
        if not m:
            continue
        rest = m.group("rest")
        name, reason = rest[:22].strip(), rest[23:].strip()
        gid = m.group("id")
        results[gid] = GateResult(gid, name, m.group("status"), ms=int(m.group("ms")),
                                  budget_s=float(m.group("budget")), over_budget=bool(m.group("over")),
                                  reason=reason)
    return results


# ------------------------------------------------------------------ parsers ----
_CC = re.compile(r"^(?P<file>[^\s:][^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>fatal error|error|warning):"
                 r"\s*(?P<msg>.*?)(?:\s*\[(?P<rule>-W[^\]]+|-Werror[^\]]*)\])?$")
_LD = re.compile(r"(?P<file>[^\s:]+\.c):(?:\(\.text[^)]*\)|(?P<line>\d+)):?\s*(?P<msg>undefined reference to .*)$")
_CPPCHECK = re.compile(r"^(?P<file>[^\s:][^:\n]*?):(?P<line>\d+):(?:(?P<col>\d+):)?\s*(?P<sev>\w+):\s*(?P<msg>.*?)"
                       r"\s*\[(?P<rule>[\w.\-]+)\]$")
_TICKET_ID = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<text>.*TS-[0-9]{3}.*)$")
_NM = re.compile(r"^(?P<obj>\S+?\.dir/(?P<src>\S+?)\.o):\s+U\s+(?P<sym>\w+)")
_UNITY = re.compile(r"^(?P<file>[^\s:]+\.c):(?P<line>\d+):(?P<test>\w+):(?P<res>FAIL|IGNORE)(?::\s*(?P<msg>.*))?$")
_CTEST_FAIL = re.compile(r"^\s*\d+\s+-\s+(?P<test>\S+)\s+\((?P<why>[^)]+)\)")
_ASAN = re.compile(r"ERROR: (?P<tool>AddressSanitizer|LeakSanitizer|MemorySanitizer|ThreadSanitizer): (?P<kind>[\w-]+)")
_FRAME = re.compile(r"#\d+\s+0x[0-9a-f]+\s+in\s+(?P<fn>\S+)\s+(?P<file>[^\s:]+):(?P<line>\d+)")
_UBSAN = re.compile(r"^(?P<file>[^\s:]+):(?P<line>\d+):(?P<col>\d+): runtime error: (?P<msg>.*)$")
_LIBFUZZER = re.compile(r"==\d+== ERROR: libFuzzer: (?P<kind>[\w-]+)|SUMMARY: \w+Sanitizer: (?P<sum>.*)")
_COVERAGE = re.compile(r"coverage below (?P<pct>[\d.]+)% for (?P<mod>\w+) \((?P<dir>[^)]+)\)")
_NO_TESTS = re.compile(r"no unit tests for (?P<mod>\w+) \((?P<path>[^)]+)\)")
_MISSING = re.compile(r"tests in \S+ but not registered in preset \S+:(?P<names>.*)$")
_ANSI = re.compile(r"\x1b\[[0-9;]*[A-Za-z]")
_BUDGET_OVER = re.compile(r"^\s*(?P<what>\w+): (?P<msg>.*over budget.*|unbounded stack frames:?)$")


def normalise_path(path: str, repo: Path) -> str:
    p = path.strip().replace("\\", "/")
    for prefix in ("/work/", "./"):
        if p.startswith(prefix):
            p = p[len(prefix):]
    m = re.match(r"^/tmp/ts-g\d\.[A-Za-z0-9]+/(.*)$", p)
    if m:
        p = m.group(1)
    m = re.match(r"^/tmp/[^/]+/(src|tests|tools|fuzz|include)/(.*)$", p)
    if m:
        p = f"{m.group(1)}/{m.group(2)}"
    try:
        rp = Path(p)
        if rp.is_absolute():
            p = str(rp.resolve().relative_to(repo.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        pass
    return p


def excerpt(repo: Path, file: str, line: int, context: int) -> str:
    if not file or line <= 0:
        return ""
    path = repo / file
    try:
        lines = path.read_text(encoding="utf-8", errors="replace").splitlines()
    except OSError:
        return ""
    lo, hi = max(1, line - context), min(len(lines), line + context)
    width = len(str(hi))
    out = []
    for n in range(lo, hi + 1):
        mark = ">" if n == line else " "
        out.append(f"{mark}{n:>{width}} | {lines[n - 1][:160]}")
    return "\n".join(out)


def parse_log(parser: str, log: str, gate: str, repo: Path, context: int = 2) -> list[FeedbackItem]:
    fn = _PARSERS.get(parser, _parse_none)
    log = _ANSI.sub("", log)  # Unity and ctest colour their output
    items = fn(log, gate, repo)
    if parser not in ("compiler", "none"):
        items += _parse_compiler(log, gate, repo, errors_only=True)
    seen: set[tuple[str, str, int, str]] = set()
    out: list[FeedbackItem] = []
    for it in items:
        if it.key() in seen:
            continue
        seen.add(it.key())
        if not it.excerpt and it.file and it.line:
            it.excerpt = excerpt(repo, it.file, it.line, context)
        out.append(it)
    return out


def _parse_none(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    return []


def _parse_compiler(log: str, gate: str, repo: Path, errors_only: bool = False) -> list[FeedbackItem]:
    items = []
    for raw in log.splitlines():
        m = _CC.match(raw.strip())
        if m:
            if errors_only and m.group("sev") == "warning":
                continue
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line")),
                                      (m.group("rule") or m.group("sev")).strip(), m.group("msg").strip()))
            continue
        m = _LD.search(raw)
        if m:
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line") or 0),
                                      "link", m.group("msg").strip()))
    return items


def _parse_cppcheck(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    for raw in log.splitlines():
        line = raw.strip()
        m = _CPPCHECK.match(line)
        if m:
            rule = m.group("rule")
            msg = m.group("msg")
            if msg.startswith("misra violation"):
                msg = f"MISRA C:2012 rule {rule.replace('misra-c2012-', '')} violated"
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line")),
                                      rule, msg))
            continue
        m = _TICKET_ID.match(line)
        if m:
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line")),
                                      "no-ticket-ids", "ticket ids must not appear in code, tests, tools or fuzz"))
        elif line.startswith("misra canary"):
            items.append(FeedbackItem(gate, "scripts/misra_canary/canary.c", 0, "canary", line))
    return items


def _parse_nm(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    for raw in log.splitlines():
        line = raw.strip()
        m = _NM.match(line)
        if m:
            src = m.group("src")
            items.append(FeedbackItem(gate, src, 0, f"no-malloc:{m.group('sym')}",
                                      f"daemon object references forbidden call {m.group('sym')}()"))
        elif "aborted (SIGABRT): allocation after init" in line:
            items.append(FeedbackItem(gate, "", 0, "no-malloc:runtime",
                                      line + " -- something allocates after ts_init_done()"))
        elif line.startswith("no-heap canary"):
            items.append(FeedbackItem(gate, "scripts/no_heap_canary/canary.c", 0, "canary", line))
        elif re.match(r"^(host|pi): exit \d+", line):
            items.append(FeedbackItem(gate, "", 0, "run", line))
    return items


def _parse_ctest(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    failed_tests = []
    for raw in log.splitlines():
        line = raw.strip()
        m = _UNITY.match(line)
        if m and m.group("res") == "FAIL":
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line")),
                                      m.group("test"), (m.group("msg") or "assertion failed").strip()))
            continue
        m = _CTEST_FAIL.match(raw)
        if m:
            failed_tests.append((m.group("test"), m.group("why")))
            continue
        m = _MISSING.search(line)
        if m:
            items.append(FeedbackItem(gate, "scripts/test_manifest.txt", 0, "missing-tests",
                                      "tests listed in the manifest are not registered:" + m.group("names")))
    named = {Path(it.file).stem for it in items if it.file}
    for test, why in failed_tests:
        if test not in named:
            items.append(FeedbackItem(gate, "", 0, test, f"ctest: {why}"))
    items += _parse_sanitizer_only(log, gate, repo)
    return items


def _parse_sanitizer_only(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    lines = log.splitlines()
    for i, raw in enumerate(lines):
        m = _ASAN.search(raw)
        if m:
            kind = m.group("kind")
            frame = None
            for nxt in lines[i + 1:i + 40]:
                fm = _FRAME.search(nxt)
                if fm and re.match(r"^(/work/)?(src|tools|tests|fuzz)/", fm.group("file").replace("\\", "/")):
                    frame = fm
                    break
            if frame:
                items.append(FeedbackItem(gate, normalise_path(frame.group("file"), repo), int(frame.group("line")),
                                          f"{m.group('tool')}:{kind}", f"{kind} in {frame.group('fn')}()"))
            else:
                items.append(FeedbackItem(gate, "", 0, f"{m.group('tool')}:{kind}", raw.strip()[:200]))
            continue
        m = _UBSAN.match(raw.strip())
        if m:
            items.append(FeedbackItem(gate, normalise_path(m.group("file"), repo), int(m.group("line")),
                                      "UBSan", m.group("msg").strip()))
            continue
        m = _LIBFUZZER.search(raw)
        if m and m.group("kind"):
            items.append(FeedbackItem(gate, "", 0, f"libFuzzer:{m.group('kind')}", raw.strip()[:200]))
    return items


def _parse_sanitizer(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = _parse_sanitizer_only(log, gate, repo)
    items += [it for it in _parse_ctest(log, gate, repo) if not it.rule.startswith(("Address", "Leak", "UBSan"))]
    return items


def _parse_budget(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    for raw in log.splitlines():
        line = raw.strip()
        if line.startswith("BUDGET") and "FAIL" in line:
            items.append(FeedbackItem(gate, "", 0, "codec-budget", line))
        m = _BUDGET_OVER.match(line)
        if m:
            items.append(FeedbackItem(gate, "", 0, f"budget:{m.group('what')}", m.group("msg")))
        if line.startswith("STACK") or line.startswith("SIZE"):
            if items and items[-1].rule.startswith("budget:") and not items[-1].excerpt:
                items[-1].excerpt = line
    items += _parse_ctest(log, gate, repo)
    return items


def _parse_coverage(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    items = []
    for raw in log.splitlines():
        line = raw.strip()
        m = _COVERAGE.search(line)
        if m:
            items.append(FeedbackItem(gate, m.group("dir"), 0, f"coverage:{m.group('mod')}",
                                      f"line coverage of {m.group('mod')} from its own unit tests is below "
                                      f"{m.group('pct')}%"))
            continue
        m = _NO_TESTS.search(line)
        if m:
            items.append(FeedbackItem(gate, m.group("path"), 0, f"coverage:{m.group('mod')}",
                                      f"no unit tests for {m.group('mod')}"))
        elif re.match(r"^lines: [\d.]+%", line) and items and not items[-1].excerpt:
            items[-1].excerpt = line
    items += _parse_ctest(log, gate, repo)
    return items


def _parse_dp(log: str, gate: str, repo: Path) -> list[FeedbackItem]:
    """check.sh diff-policy: 'protected: ... paths: a b', 'calls: ...' + '  path<TAB>line', 'tlv: new TLV type(s): X'."""
    items: list[FeedbackItem] = []
    in_calls = False
    for raw in log.splitlines():
        line = raw.rstrip()
        if line.startswith("protected: the change touches protected paths:"):
            for p in line.split(":", 2)[2].split():
                items.append(FeedbackItem(gate, p, 0, "protected-path", f"{p} is a protected path; revert this change"))
        elif line.startswith("calls: new process creation"):
            in_calls = True
            continue
        elif in_calls and line.startswith("  ") and "\t" in line:
            path, code = line.strip().split("\t", 1)
            items.append(FeedbackItem(gate, path, 0, "process-call", f"new process creation / code loading: {code.strip()}"))
            continue
        elif line.startswith("tlv: new TLV type(s):"):
            names = line.split(":", 2)[2].split("(")[0].split()
            for n in names:
                items.append(FeedbackItem(gate, "", 0, "new-tlv", f"new TLV type {n}: the control channel is closed"))
        in_calls = False
    return items


_PARSERS: dict[str, Callable[[str, str, Path], list[FeedbackItem]]] = {
    "dp": _parse_dp,
    "none": _parse_none,
    "compiler": _parse_compiler,
    "cppcheck": _parse_cppcheck,
    "nm": _parse_nm,
    "ctest": _parse_ctest,
    "sanitizer": _parse_sanitizer,
    "budget": _parse_budget,
    "coverage": _parse_coverage,
}


def _read(path: Path) -> str:
    try:
        return path.read_text(encoding="utf-8", errors="replace")
    except OSError:
        return ""


def _rel(path: Path, repo: Path) -> str:
    try:
        return str(path.resolve().relative_to(repo.resolve())).replace("\\", "/")
    except (ValueError, OSError):
        return str(path)


def _last_lines(text: str, n: int) -> str:
    lines = [ln.rstrip() for ln in text.splitlines() if ln.strip()]
    return "\n".join(ln[:200] for ln in lines[-n:])
