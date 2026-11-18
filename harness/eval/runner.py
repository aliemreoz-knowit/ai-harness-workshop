"""The eval runner: variant x ticket x repeat, each in its own git worktree.

Per run:
  1. git worktree <worktree_root>/<experiment>/<V>-<ticket>-r<k> on branch
     eval/<experiment>/<V>-<ticket>-r<k>, reset to the ticket's branch
     (tickets/<ID>/meta.yaml: ticket_branch, default ticket/<ID>)
  1b. a variant with overlay_from takes its harness structure from that directory: the files are
     copied in and committed (worktree.apply_overlay), so the run starts on a clean tree; the diff,
     the patch and the commit count below are measured from that commit, not from the seed
  2. harness.api.run_workflow(workflow, ticket, settings, "eval") with the
     variant's settings, workdir = the worktree, run_dir = the run's results folder
  3. metrics from the run's audit trail (events.jsonl), RunResult as fallback
  4. the commit the workflow made (on agent/<key>-<id>) moves onto the eval branch;
     diff against the seed (size, files outside the ticket's allowed paths) and the
     full patch in <run_dir>/diff.patch (written before the worktree can go)
  5. hidden tests: <hidden-tests>/run.sh <worktree> <preset> <ticket>
  6. one JSON line in <results_dir>/<experiment>.jsonl (hidden_pass and
     workflow_committed), results.json refreshed; the agent/* branch is deleted
"""
from __future__ import annotations

import json
import shutil
import threading
import time
import traceback
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Callable, Optional

from .. import api, names
from ..api import RunResult
from . import audit as auditmod
from . import hidden as hiddenmod
from . import results as resmod
from . import tickets as ticketmod
from . import worktree as wt
from .variants import Variant

DEFAULT_GATE_WEIGHT = 1.0


@dataclass(frozen=True)
class Job:
    variant: Variant
    ticket: str
    repeat: int

    @property
    def slug(self) -> str:
        return f"{self.variant.name}-{self.ticket}-r{self.repeat}"


@dataclass
class EvalConfig:
    repo: Path
    hidden_dir: Optional[Path]
    variants: list[Variant]
    tickets: list[str]
    experiment: str
    results_dir: Path
    worktree_root: Path
    gate_weights: dict[str, float] = field(default_factory=dict)
    parallel: int = 1
    hidden_parallel: Optional[int] = None    # default: = parallel
    repeats: Optional[int] = None            # overrides every variant's repeats
    preset: str = "host"
    base_ref: Optional[str] = None           # overrides every ticket's branch
    golden_ref: Optional[str] = "auto"       # "auto": the tag the ticket branch is built on (wt.base_tag)
    hidden_cmd: Optional[list[str]] = None   # default: bash <hidden>/run.sh <checkout> <preset> <ticket>
    hidden_timeout_s: float = 900.0
    skip_hidden: bool = False
    cleanup: bool = False                    # remove the worktree after the run (the branch stays)
    resume: bool = False                     # skip runs already "done" in <experiment>.jsonl
    backend: Optional[api.WorkflowFn] = None # None: harness.api.run_workflow (discovery)
    board_out: Optional[Path] = None         # default: <results_dir>/results.json
    log: Callable[[str], None] = print

    @property
    def results_file(self) -> Path:
        return self.results_dir / f"{self.experiment}.jsonl"


def plan(cfg: EvalConfig) -> list[Job]:
    jobs = []
    for v in cfg.variants:
        for t in cfg.tickets:
            for k in range(cfg.repeats or v.repeats):
                jobs.append(Job(v, t, k))
    return jobs


class Runner:
    def __init__(self, cfg: EvalConfig):
        self.cfg = cfg
        self.writer = resmod.ResultsWriter(cfg.results_file)
        self._hidden_sem = threading.Semaphore(max(1, cfg.hidden_parallel or cfg.parallel))
        self._board_lock = threading.Lock()
        self._ticket_cache: dict[str, ticketmod.Ticket] = {}
        self._golden_cache: dict[str, Optional[dict]] = {}
        self._cache_lock = threading.Lock()

    # ------------------------------------------------------------ helpers --
    def ticket(self, tid: str) -> ticketmod.Ticket:
        with self._cache_lock:
            if tid not in self._ticket_cache:
                self._ticket_cache[tid] = ticketmod.load_ticket(self.cfg.hidden_dir, tid)
            return self._ticket_cache[tid]

    def golden_diff(self, tid: str, base_ref: str) -> Optional[dict]:
        if not self.cfg.golden_ref:
            return None
        with self._cache_lock:
            if tid not in self._golden_cache:
                golden = self.cfg.golden_ref
                if golden == "auto":
                    golden = wt.base_tag(self.cfg.repo, base_ref)
                d = wt.diff_between(self.cfg.repo, base_ref, golden) if golden else None
                self._golden_cache[tid] = d.to_dict() if d else None
            return self._golden_cache[tid]

    def refresh_board(self) -> None:
        out = self.cfg.board_out or (self.cfg.results_dir / "results.json")
        with self._board_lock:
            recs = resmod.load_records(sorted(self.cfg.results_dir.glob("*.jsonl")))
            resmod.write_board(recs, out)

    def missing_bases(self) -> dict[str, str]:
        """ticket -> base ref that does not exist in the repo (checked before any worktree is made)."""
        out = {}
        for tid in self.cfg.tickets:
            ref = self.cfg.base_ref or self.ticket(tid).ticket_branch
            if not wt.ref_exists(self.cfg.repo, ref):
                out[tid] = ref
        return out

    def done_keys(self) -> set[tuple[str, str, int]]:
        if not self.cfg.resume:
            return set()
        recs = resmod.load_records([self.cfg.results_file])
        return {resmod.run_key(r) for r in recs if r.get("run_status") in resmod.FINAL_STATUSES}

    # ------------------------------------------------------------- a run --
    def run_one(self, job: Job) -> dict[str, Any]:
        cfg = self.cfg
        v = job.variant
        workdir = cfg.worktree_root / cfg.experiment / job.slug
        run_dir = cfg.results_dir / cfg.experiment / job.slug
        branch = f"eval/{cfg.experiment}/{job.slug}"
        rec: dict[str, Any] = {
            "schema": resmod.SCHEMA, "experiment": cfg.experiment, "variant": v.name,
            "variant_label": v.label, "variant_tests": v.tests, "workflow": v.workflow,
            "ticket": job.ticket, "repeat": job.repeat, "branch": branch, "worktree": str(workdir),
            "run_dir": str(run_dir), "started_at": resmod.now_iso(), "error": None,
            "overlay_from": v.overlay_from, "overlay_commit": None, "overlay_skipped": [],
            "overlay_skipped_total": 0,
            "applied_knobs": list(v.applied_knobs) if v.applied_knobs is not None else None,
        }
        if run_dir.exists():
            shutil.rmtree(run_dir)            # a re-run starts from a clean artefact folder
        run_dir.mkdir(parents=True, exist_ok=True)

        try:
            ticket = self.ticket(job.ticket)
            base_ref = cfg.base_ref or ticket.ticket_branch
            rec["seed_ref"] = base_ref
            base = wt.create_worktree(cfg.repo, workdir, branch, base_ref)
            rec["seed_commit"] = base
            if v.overlay_dir is not None:
                skipped = wt.SkippedNames()
                try:
                    rec["overlay_commit"] = wt.apply_overlay(workdir, v.overlay_dir, skipped)
                finally:      # what the scan found is in the record also when the overlay fails afterwards
                    rec["overlay_skipped"], rec["overlay_skipped_total"] = list(skipped), skipped.total
        except Exception as exc:  # noqa: BLE001 - recorded, the experiment goes on
            rec.update(run_status="setup_error", success=False, status="error", error=f"setup: {exc}",
                       finished_at=resmod.now_iso())
            self.writer.append(rec)
            return rec

        settings = v.run_settings(str(workdir), str(run_dir))
        settings.experiment, settings.repeat = cfg.experiment, job.repeat
        ticket_md = (cfg.hidden_dir / "tickets" / job.ticket / "ticket.md") if cfg.hidden_dir else None
        if ticket_md is not None and ticket_md.is_file() and "ticket_file" not in settings.extra:
            settings.extra["ticket_file"] = str(ticket_md)   # outside the worktree: never part of the diff
        rec["settings"] = {k: val for k, val in settings.to_dict().items() if k not in ("workdir", "run_dir")}
        _write_json(run_dir / "settings.json", settings.to_dict())

        t0 = time.monotonic()
        try:
            result = api.run_workflow(v.workflow, job.ticket, settings, "eval", backend=cfg.backend)
        except Exception as exc:  # noqa: BLE001
            result = RunResult(status="error", success=False, error=f"{type(exc).__name__}: {exc}")
            (run_dir / "workflow_error.txt").write_text(traceback.format_exc(), encoding="utf-8")
        wall = time.monotonic() - t0
        _write_json(run_dir / "run_result.json", result.to_dict())
        rec.update(self._metrics(result, run_dir, wall))

        # the harness commits on its own branch (agent/<key>-<id>); the record's branch must be
        # the one with the commit, so the eval branch takes it over and the agent branch goes
        agent_branch = wt.adopt_branch(workdir, branch)
        if agent_branch:
            rec["agent_branch"] = agent_branch
        start = rec["overlay_commit"] or base       # what the run started from: the overlay is not its work
        rec["head_commit"] = wt.head(workdir)
        rec["commits"] = wt.commits_since(workdir, start)
        rec["workflow_committed"] = rec["commits"] > 0
        rec["commit"] = rec["head_commit"] if rec["workflow_committed"] else None
        (run_dir / "diff.patch").write_text(wt.patch_vs(workdir, start), encoding="utf-8", newline="\n")
        diff = wt.diff_vs(workdir, start)
        rec["diff"] = diff.to_dict()
        rec["changed_files"] = diff.files[:200]
        rec["path_violations"] = ticketmod.path_violations(diff.files, ticket.allowed_paths)
        rec["golden_diff"] = self.golden_diff(job.ticket, base_ref)

        if cfg.skip_hidden or cfg.hidden_dir is None and cfg.hidden_cmd is None:
            rec["hidden"] = {"status": "skipped"}
            rec["success"] = False
            rec["hidden_pass"] = False
        else:
            with self._hidden_sem:
                h = hiddenmod.run_hidden(cfg.hidden_dir or Path("."), workdir, job.ticket, cfg.preset,
                                         cfg.hidden_cmd, cfg.hidden_timeout_s, run_dir / "hidden.log")
            rec["hidden"] = dict(h.to_dict(), preset=cfg.preset)
            rec["success"] = h.success              # success = the hidden tests pass
            rec["hidden_pass"] = h.success
            if h.status in ("error", "timeout") and not rec["error"]:
                rec["error"] = f"hidden tests: {h.status} (exit {h.exit_code})"
        rec["run_status"] = "done"
        rec["finished_at"] = resmod.now_iso()
        self.writer.append(rec)
        _write_json(run_dir / "record.json", rec)
        if agent_branch:
            wt.delete_branch(cfg.repo, agent_branch, keep_tip_of=branch)
        if cfg.cleanup:
            wt.remove_worktree(cfg.repo, workdir)
        return rec

    def _metrics(self, result: RunResult, run_dir: Path, wall: float) -> dict[str, Any]:
        """Audit trail first, RunResult where the trail is silent."""
        candidates = [Path(result.audit_path)] if result.audit_path else []
        if result.run_dir:
            candidates.append(Path(result.run_dir) / "events.jsonl")
        candidates.append(run_dir / "events.jsonl")
        src = next((p for p in candidates if p.is_file()), None)
        if src is not None and src.resolve() != (run_dir / "events.jsonl").resolve():
            shutil.copyfile(src, run_dir / "events.jsonl")   # keep the trail when the worktree goes
        a = auditmod.parse_audit(src)

        def pick(audit_val: Any, result_val: Any) -> Any:
            return audit_val if a is not None and audit_val is not None else result_val

        gate_results = (a.gate_results if a is not None and a.gate_results else result.gate_results) or {}
        gate_violations = pick(a.gate_violations if a else None, result.gate_violations)
        weights = self.cfg.gate_weights
        score = sum(weights.get(names.gate(g), DEFAULT_GATE_WEIGHT) for g, s in gate_results.items()
                    if str(s).upper() == "FAIL")
        has_tokens = a is not None and (a.model_calls > 0 or a.tokens_in > 0)
        approvals = a.approvals if a is not None and a.events else result.approvals
        escalations = a.escalations if a is not None and a.events else result.escalations
        out = {
            "status": result.status,
            "workflow_success": bool(result.success),
            "run_id": result.run_id,
            "audit": {"path": str(src) if src else None, "events": a.events if a else 0,
                      "bad_lines": a.bad_lines if a else 0, "models": a.models if a else {}},
            "metrics_source": "audit" if a is not None and a.events else "result",
            "duration_s": round(result.duration_s if result.duration_s > 0 else wall, 2),
            "wall_s": round(wall, 2),
            "gate_results": gate_results,
            "gate_violations": int(gate_violations or 0),
            "gate_violation_score": round(score, 2),
            "gate_failures_total": a.gate_failures_total if a else int(gate_violations or 0),
            "reviewer_score": pick(a.reviewer_score if a else None, result.reviewer_score),
            "approvals": int(approvals),
            "escalations": int(escalations),
            "interventions": int(approvals) + int(escalations),
            "retries": int(a.retries if a is not None and a.events else result.retries),
            "ai_credits": round(float(pick(a.ai_credits if a else None, result.ai_credits) or 0.0), 4),
            "tokens": {"in": a.tokens_in if has_tokens else result.tokens_in,
                       "out": a.tokens_out if has_tokens else result.tokens_out,
                       "cached": a.tokens_cached if has_tokens else result.tokens_cached},
            "model_calls": a.model_calls if has_tokens else result.model_calls,
        }
        if result.error:
            out["error"] = result.error
        return out

    # ------------------------------------------------------- the matrix --
    def run(self) -> list[dict[str, Any]]:
        cfg = self.cfg
        jobs = plan(cfg)
        done = self.done_keys()
        todo = [j for j in jobs if (j.variant.name, j.ticket, j.repeat) not in done]
        cfg.log(f"[eval] experiment {cfg.experiment}: {len(jobs)} runs "
                f"({len(cfg.variants)} variants x {len(cfg.tickets)} tickets), "
                f"{len(jobs) - len(todo)} already done, {len(todo)} to run, parallel {cfg.parallel}")
        out: list[dict[str, Any]] = []
        if not todo:
            self.refresh_board()
            return out
        pool = ThreadPoolExecutor(max_workers=max(1, cfg.parallel), thread_name_prefix="eval")
        try:
            futs = {pool.submit(self._guarded, j): j for j in todo}
            for i, fut in enumerate(as_completed(futs), 1):
                rec = fut.result()
                out.append(rec)
                self.refresh_board()
                cfg.log(_progress_line(i, len(todo), rec))
        except KeyboardInterrupt:
            pool.shutdown(wait=False, cancel_futures=True)
            cfg.log("[eval] interrupted: finished runs are saved; rerun with --resume "
                    f"--experiment {cfg.experiment}")
            raise
        pool.shutdown(wait=True)
        return out

    def _guarded(self, job: Job) -> dict[str, Any]:
        try:
            return self.run_one(job)
        except Exception as exc:  # noqa: BLE001 - never lose the other runs
            rec = {"schema": resmod.SCHEMA, "experiment": self.cfg.experiment, "variant": job.variant.name,
                   "variant_label": job.variant.label, "ticket": job.ticket, "repeat": job.repeat,
                   "run_status": "runner_error", "success": False, "status": "error",
                   "error": f"runner: {type(exc).__name__}: {exc}", "finished_at": resmod.now_iso()}
            self.writer.append(rec)
            return rec


def _progress_line(i: int, n: int, rec: dict[str, Any]) -> str:
    verdict = "PASS" if rec.get("success") else "FAIL"
    if rec.get("run_status") != "done":
        verdict = "ERR "
    extra = f" error={rec['error']}" if rec.get("error") else ""
    return (f"[eval] {i:>3}/{n} {verdict} {rec.get('variant'):<4} {rec.get('ticket'):<8} r{rec.get('repeat')}"
            f"  status={rec.get('status')} credits={rec.get('ai_credits', 0)}"
            f" interventions={rec.get('interventions', 0)} retries={rec.get('retries', 0)}"
            f" {rec.get('duration_s', 0)}s{extra}")


def _write_json(path: Path, obj: Any) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with open(path, "w", encoding="utf-8", newline="\n") as fh:
        json.dump(obj, fh, indent=1, sort_keys=True, default=str)
        fh.write("\n")
