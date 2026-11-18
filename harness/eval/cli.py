"""Command line: python -m harness eval ... / python -m harness leaderboard ..."""
from __future__ import annotations

import argparse
import os
import shlex
import sys
from datetime import datetime
from pathlib import Path
from typing import Optional

from .. import api
from . import results as resmod
from . import tickets as ticketmod
from .runner import EvalConfig, Runner, plan
from .variants import VariantError, find_variants_file, load_matrix

REPO_DEFAULT = Path(__file__).resolve().parents[2]
FAKE_HIDDEN_CMD = ["{python}", str(Path(__file__).resolve().parent / "fake_hidden.py"),
                   "{checkout}", "{preset}", "{ticket}"]


def find_hidden_tests(repo: Path) -> Optional[Path]:
    """$HARNESS_HIDDEN_TESTS, else <repo>/hidden-tests, else a sibling 'hidden-tests' of the repo or of any parent."""
    env = os.environ.get("HARNESS_HIDDEN_TESTS")
    if env:
        return Path(env)
    if (repo / "hidden-tests" / "run.sh").is_file():       # the workshop repository carries its referee
        return repo / "hidden-tests"
    for parent in [repo, *repo.parents]:
        cand = parent.parent / "hidden-tests" if parent.parent != parent else None
        if cand is not None and (cand / "run.sh").is_file():
            return cand
    return None


def default_worktree_root(repo: Path) -> Path:
    """Next to the repository, not inside it: a checkout copied by the hidden-test runner
    (tar of the whole tree minus top-level build dirs) must not drag 80 eval worktrees along."""
    return repo.parent / f"{repo.name}-eval" / "worktrees"


def _backend(spec: str) -> Optional[api.WorkflowFn]:
    if spec == "api":
        return None                      # discovery inside harness.api.run_workflow
    if spec == "fake":
        from .fake import run_workflow
        return run_workflow
    if spec == "orchestrator":
        from ..backend import run_workflow as real
        return real
    if spec == "orchestrator-fake":
        from ..backend import run_workflow_fake_runtime
        return run_workflow_fake_runtime
    return api.load_backend(spec)


def eval_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m harness eval",
                                description="Run variants x tickets in parallel git worktrees and score them "
                                            "with the hidden tests.")
    p.add_argument("--variants", default=None, help="variants.yaml (default: harness/eval/variants.yaml)")
    p.add_argument("--tickets", required=True,
                   help="comma list of Jira keys (KEY-1,KEY-2); 'eval' = every eval: true ticket in the hidden-tests repo")
    p.add_argument("--only", default="", help="comma list of variants to run (default: all)")
    p.add_argument("--parallel", type=int, default=1, help="runs at the same time (default 1)")
    p.add_argument("--hidden-parallel", type=int, default=None, help="hidden-test containers at the same time")
    p.add_argument("--repeats", type=int, default=None, help="override every variant's repeats")
    p.add_argument("--experiment", default=None, help="experiment id (default exp-<timestamp>)")
    p.add_argument("--resume", action="store_true", help="skip runs already done in <experiment>.jsonl")
    p.add_argument("--repo", type=Path, default=REPO_DEFAULT, help="tinystream repository")
    p.add_argument("--hidden-tests", type=Path, default=None, help="hidden-tests repository")
    p.add_argument("--preset", choices=("host", "asan"), default="host", help="hidden-test preset")
    p.add_argument("--results-dir", type=Path, default=None, help="default <repo>/results")
    p.add_argument("--worktree-root", type=Path, default=None, help="default <repo>/../<repo name>-eval/worktrees")
    p.add_argument("--board-out", type=Path, default=None, help="leaderboard file (default <results>/results.json)")
    p.add_argument("--base-ref", default=None, help="start every run from this ref instead of the ticket branch")
    p.add_argument("--golden-ref", default="auto",
                   help="ref for 'diff size vs golden' (auto = the tag the ticket branch is built on; '' = off)")
    p.add_argument("--backend", default="api",
                   help="api (discovery: the orchestrator) | orchestrator | orchestrator-fake (real orchestrator, "
                        "gates and hidden tests, scripted runtime: no model, no credits) | fake (toy model) | "
                        "module:function")
    p.add_argument("--hidden-cmd", default=None,
                   help="hidden-test command template, placeholders {bash} {hidden} {checkout} {preset} "
                        "{ticket} {python}")
    p.add_argument("--hidden-timeout", type=float, default=900.0, help="seconds per hidden-test run")
    p.add_argument("--skip-hidden", action="store_true", help="do not run the hidden tests (success = false)")
    p.add_argument("--fake", action="store_true",
                   help="fake orchestrator + fake hidden tests (no models, no docker): a dry run of the pipeline")
    p.add_argument("--cleanup", action="store_true", help="remove each worktree after its run (branch stays)")
    p.add_argument("--allow-missing-branches", "--allow-missing-seeds", action="store_true",
                   help="run anyway when a ticket branch is missing (those runs are recorded as setup errors)")
    p.add_argument("--dry-run", action="store_true", help="print the plan and exit")
    return p


def build_config(args: argparse.Namespace) -> EvalConfig:
    repo = args.repo.resolve()
    hidden = args.hidden_tests.resolve() if args.hidden_tests else find_hidden_tests(repo)
    matrix = load_matrix(find_variants_file(args.variants))
    only = [s.strip() for s in args.only.split(",") if s.strip()]
    tickets = ticketmod.parse_ticket_list(args.tickets, hidden)
    if not tickets:
        raise ValueError("no tickets given")
    if args.parallel < 1:
        raise ValueError("--parallel must be >= 1")
    if args.repeats is not None and args.repeats < 1:
        raise ValueError("--repeats must be >= 1")
    hidden_cmd = shlex.split(args.hidden_cmd) if args.hidden_cmd else None
    backend_spec = args.backend
    if args.fake:
        backend_spec = "fake" if backend_spec == "api" else backend_spec
        hidden_cmd = hidden_cmd or FAKE_HIDDEN_CMD
    if hidden is None and hidden_cmd is None and not args.skip_hidden:
        raise ValueError("hidden-tests repository not found: pass --hidden-tests or set HARNESS_HIDDEN_TESTS")
    return EvalConfig(
        repo=repo,
        hidden_dir=hidden,
        variants=matrix.select(only),
        tickets=tickets,
        experiment=args.experiment or datetime.now().strftime("exp-%Y%m%d-%H%M%S"),
        results_dir=(args.results_dir or repo / "results").resolve(),
        worktree_root=(args.worktree_root or default_worktree_root(repo)).resolve(),
        gate_weights=matrix.gate_weights,
        parallel=args.parallel,
        hidden_parallel=args.hidden_parallel,
        repeats=args.repeats,
        preset=args.preset,
        base_ref=args.base_ref,
        golden_ref=args.golden_ref or None,
        hidden_cmd=hidden_cmd,
        hidden_timeout_s=args.hidden_timeout,
        skip_hidden=args.skip_hidden,
        cleanup=args.cleanup,
        resume=args.resume,
        backend=_backend(backend_spec),
        board_out=args.board_out,
    )


def main_eval(argv: Optional[list[str]] = None) -> int:
    args = eval_parser().parse_args(argv)
    if args.resume and not args.experiment:
        print("--resume needs --experiment <id>", file=sys.stderr)
        return 2
    try:
        cfg = build_config(args)
    except (ValueError, VariantError, api.BackendUnavailable) as exc:
        print(f"eval: {exc}", file=sys.stderr)
        return 2
    if args.dry_run:
        for job in plan(cfg):
            t = ticketmod.load_ticket(cfg.hidden_dir, job.ticket)
            print(f"{job.slug:<24} workflow={job.variant.workflow} base={cfg.base_ref or t.ticket_branch} "
                  f"roles={','.join(r for r, on in job.variant.settings['roles'].items() if on)}")
        return 0
    if cfg.backend is None:
        try:
            api._discover()   # fail before creating 80 worktrees
        except api.BackendUnavailable as exc:
            print(f"eval: {exc}", file=sys.stderr)
            return 2
    run = Runner(cfg)
    missing = run.missing_bases()
    if missing and not args.allow_missing_branches:
        print("eval: base ref(s) not found in " + str(cfg.repo) + ": " +
              ", ".join(f"{t} -> {r}" for t, r in missing.items()) +
              " (create the ticket branches, use --base-ref, or --allow-missing-branches)", file=sys.stderr)
        return 2
    try:
        run.run()
    except KeyboardInterrupt:
        return 130
    recs = resmod.latest(resmod.load_records([cfg.results_file]))
    print()
    print(resmod.format_table(resmod.aggregate(recs)))
    print(f"\n[eval] results: {cfg.results_file}\n[eval] board:   {cfg.board_out or cfg.results_dir / 'results.json'}")
    errors = [r for r in recs if r.get("run_status") != "done"]
    return 1 if errors else 0


def main_leaderboard(argv: Optional[list[str]] = None) -> int:
    p = argparse.ArgumentParser(prog="python -m harness leaderboard",
                                description="Merge result files into results.json for tools/board/leaderboard.html")
    p.add_argument("files", nargs="*", help="result files or globs (default <repo>/results/*.jsonl)")
    p.add_argument("-o", "--out", type=Path, default=None, help="default <repo>/results/results.json")
    p.add_argument("--quiet", action="store_true", help="do not print the table")
    args = p.parse_args(argv)
    patterns = args.files or [str(REPO_DEFAULT / "results" / "*.jsonl")]
    files = resmod.expand(patterns)
    if not files:
        print(f"leaderboard: no result files match {' '.join(patterns)}", file=sys.stderr)
        return 2
    out = args.out or REPO_DEFAULT / "results" / "results.json"
    board = resmod.write_board(resmod.load_records(files), out)
    if not args.quiet:
        print(resmod.format_table(board["variants"]))
        print(f"\n{len(board['runs'])} runs from {len(files)} file(s) -> {out}")
    return 0
