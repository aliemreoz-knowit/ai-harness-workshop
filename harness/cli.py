"""Command line: ``python -m harness <command>``.

    python -m harness run --workflow jira-to-pr --ticket <file.md | JIRA-KEY> [--mode eval]
    python -m harness run --workflow <name> --ticket <...> --run-id <id>    (the caller names the run)
    python -m harness run --resume <run-id>
    python -m harness doctor [--json] [--skip-auth] [--depth]
    python -m harness gates [--profile fast | build unit-tests ...] [--json]
    python -m harness runs [<run-id>]
    python -m harness workflows

Exit status of ``run``: 0 done, 1 failed, 3 escalated or aborted, 2 usage/config error.

Environment of ``run``: HARNESS_HITL_DIR=<dir> answers interactive approvals and escalations
through files in <dir> instead of the terminal (approvals.FileChannel; HARNESS_HITL_TIMEOUT_S,
default 900 s, is how long a question waits before it is refused / aborted).
"""

from __future__ import annotations

import argparse
import asyncio
import json
import os
import re
import sys
from pathlib import Path
from typing import Any

from . import __version__, envdetect
from .approvals import MODES, FileChannel
from .config import ConfigError, HarnessConfig, load_config
from .gates import GateContext, GateRunner
from .state import RunState

EXIT = {"done": 0, "failed": 1, "escalated": 3, "aborted": 3, "running": 1}
RUN_ID_RE = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]{0,99}")    # one safe directory name (matched whole)
HITL_TIMEOUT_S = 900.0


def _parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(prog="python -m harness", description="tinystream agent harness (Copilot SDK)")
    p.add_argument("--config", help="harness.yaml (default: harness/harness.yaml)")
    p.add_argument("--repo", help="repository root (default: paths.repo in harness.yaml)")
    p.add_argument("--version", action="version", version=f"harness {__version__}")
    sub = p.add_subparsers(dest="command", required=True)

    r = sub.add_parser("run", help="run a workflow (or resume a run)")
    r.add_argument("--workflow", "-w", help="workflow name (workflows/<name>.yaml)")
    r.add_argument("--ticket", "-t", help="ticket markdown file or Jira key")
    r.add_argument("--mode", choices=MODES, default="interactive",
                   help="interactive (ask), eval (auto-approve but count), autonomous")
    r.add_argument("--resume", metavar="RUN", help="continue run RUN from its first unfinished step")
    r.add_argument("--run-id", metavar="ID", help="id of the new run instead of a generated one (letters, digits, . _ -; "
                   "at most 100; not with --resume); an id that exists is refused")
    r.add_argument("--runtime", choices=("auto", "facilitator", "copilot-user"),
                   help="override harness.yaml runtime")
    r.add_argument("--fake", action="store_true",
                   help="no model: scripted fake runtime (plumbing check, no credits)")

    d = sub.add_parser("doctor", help="check this machine and pick the runtime mode")
    d.add_argument("--json", action="store_true")
    d.add_argument("--skip-auth", action="store_true", help="do not start the Copilot CLI")
    d.add_argument("--depth", action="store_true",
                   help="end with the lines 'Depth: <depth>' and 'Ladder: full (container) | lite (zig)'; exit 0")

    g = sub.add_parser("gates", help="run gates and print structured feedback")
    g.add_argument("spec", nargs="*", help="gate ids (build unit-tests) or one profile name")
    g.add_argument("--profile", help="profile from gates.yaml (default: fast)")
    g.add_argument("--json", action="store_true")

    rs = sub.add_parser("runs", help="list runs or show one run's summary")
    rs.add_argument("run", nargs="?")

    sub.add_parser("workflows", help="list workflows and their steps")
    return p


def main(argv: list[str] | None = None) -> int:
    args = _parser().parse_args(argv)
    try:
        cfg = load_config(args.config, repo=args.repo)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2
    try:
        return {"run": cmd_run, "doctor": cmd_doctor, "gates": cmd_gates, "runs": cmd_runs,
                "workflows": cmd_workflows}[args.command](cfg, args)
    except ConfigError as exc:
        print(f"config error: {exc}", file=sys.stderr)
        return 2


def _environment(cfg: HarnessConfig) -> envdetect.Environment:
    return envdetect.detect(cfg.repo, str(cfg.paths.get("bash", "") or ""))


def resolve_runtime_mode(cfg: HarnessConfig, override: str | None, doctor_dir: Path | None = None,
                         default: str = "copilot-user") -> str:
    """``override`` (a CLI flag, a variant's ``extra.runtime_mode``), else harness.yaml /
    HARNESS_RUNTIME; ``auto`` means what the last ``harness doctor`` decided on this machine
    (``doctor_dir``: the runs folder holding doctor-<host|container>.json, default the
    configured one), else ``default``."""
    mode = override or cfg.runtime
    if mode != "auto":
        return mode
    try:
        return str(json.loads(_doctor_file(cfg, doctor_dir).read_text(encoding="utf-8"))["runtime_mode"])
    except (OSError, ValueError, KeyError):
        return default


def _doctor_file(cfg: HarnessConfig, runs_dir: Path | None = None) -> Path:
    # inside the git-ignored runs folder; host and devcontainer share the checkout, not the login
    where = "container" if envdetect.in_container() else "host"
    return (runs_dir or cfg.runs_dir) / f"doctor-{where}.json"


def _hitl_timeout(raw: str | None) -> float | None:
    """HARNESS_HITL_TIMEOUT_S: seconds, default 900; None when it is not a number >= 0."""
    if raw is None or not raw.strip():
        return HITL_TIMEOUT_S
    try:
        value = float(raw)
    except ValueError:
        return None
    return value if value >= 0 else None                  # also False for nan


def cmd_run(cfg: HarnessConfig, args: Any) -> int:
    from .orchestrator import HarnessError, Orchestrator

    if not args.resume and not args.workflow:
        print("run: --workflow is required (or --resume RUN)", file=sys.stderr)
        return 2
    if args.run_id is not None:
        if args.resume:
            print("run: --run-id names a new run and cannot be combined with --resume", file=sys.stderr)
            return 2
        if not RUN_ID_RE.fullmatch(args.run_id):
            print(f"run: bad run id {args.run_id!r}: letters, digits, '.', '_' and '-' only, "
                  "starting with a letter or digit, at most 100 characters", file=sys.stderr)
            return 2
    channel = None
    if os.environ.get("HARNESS_HITL_DIR"):
        timeout = _hitl_timeout(os.environ.get("HARNESS_HITL_TIMEOUT_S"))
        if timeout is None:
            print("run: HARNESS_HITL_TIMEOUT_S must be a number of seconds, 0 or more", file=sys.stderr)
            return 2
        channel = FileChannel(Path(os.environ["HARNESS_HITL_DIR"]), timeout_s=timeout)
    env = _environment(cfg)
    runtime_mode = resolve_runtime_mode(cfg, args.runtime)
    if args.fake:
        from .fake import FakeRuntime
        runtime: Any = FakeRuntime()
    else:
        from .runtime import CopilotRuntime, env_passthrough, sdk_available
        if not sdk_available():
            print("github-copilot-sdk is not installed (pip install --user github-copilot-sdk==1.0.23)",
                  file=sys.stderr)
            return 2
        cli = envdetect.find_copilot_cli(str(cfg.copilot.get("cli_path", "") or ""))
        runtime = CopilotRuntime(cli, str(cfg.repo), log_level=str(cfg.copilot.get("log_level", "warning")),
                                 passthrough=env_passthrough(cfg))
    orch = Orchestrator(cfg, runtime, env, mode=args.mode, runtime_mode=runtime_mode, channel=channel)
    try:
        state = asyncio.run(orch.run(workflow=args.workflow, ticket_ref=args.ticket, resume=args.resume,
                                     run_id=args.run_id))
    except HarnessError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    except FileNotFoundError as exc:
        print(f"run: {exc}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        st = getattr(orch, "state", None)
        print("\n[harness] interrupted" + (f"; resume with: python -m harness run --resume {st.run_id}" if st else ""),
              file=sys.stderr)
        return 130
    summary = json.loads((state.dir / "summary.json").read_text(encoding="utf-8"))
    print(f"[harness] run {state.run_id}: {summary['status']}"
          + (f" at {summary['stopped_at']} ({summary['reason']})" if summary.get("stopped_at") else "")
          + (f" -- error: {summary['error']}" if summary.get("error") else ""))
    print(f"[harness] credits {summary['credits']:.2f}, interventions {summary['interventions']}, "
          f"retries {summary['counters'].get('retries', 0)}, folder {state.dir}")
    if summary["status"] != "done":
        print(f"[harness] resume with: python -m harness run --resume {state.run_id}")
    return EXIT.get(summary["status"], 1)


def cmd_doctor(cfg: HarnessConfig, args: Any) -> int:
    from . import doctor

    report = doctor.run_doctor(cfg, skip_auth=args.skip_auth)
    if not args.skip_auth:
        try:
            _doctor_file(cfg).parent.mkdir(parents=True, exist_ok=True)
            _doctor_file(cfg).write_text(json.dumps(report.to_dict(), indent=2), encoding="utf-8")
        except OSError:
            pass
    print(json.dumps(report.to_dict(), indent=2) if args.json else report.render())
    if args.depth:
        if not args.json:                    # the setup scripts and the console read these two lines
            print(f"Depth: {doctor.depth(report)}")
            print(f"Ladder: {doctor.LADDER.get(report.ladder, report.ladder)}")
        return 0
    return 0 if report.ok else 1


def cmd_gates(cfg: HarnessConfig, args: Any) -> int:
    env = _environment(cfg)
    spec: Any = args.profile or (args.spec[0] if len(args.spec) == 1 else args.spec) or "fast"
    allowed: list[str] = []
    for r in cfg.roles.values():
        allowed += [w for w in r.writes if w not in allowed]
    ladder = GateRunner(cfg, env).run(spec, GateContext(cfg.repo, allowed, cfg.policy))
    if args.json:
        print(json.dumps(ladder.to_dict(), indent=2))
    else:
        print(ladder.feedback(int(cfg.loop.get("feedback_max_items", 6))))
        print(f"({env.where}, {ladder.duration_ms} ms)")
    return 0 if ladder.ok else 1


def cmd_runs(cfg: HarnessConfig, args: Any) -> int:
    if args.run:
        path = cfg.runs_dir / args.run / "summary.json"
        if not path.is_file():
            print(f"no summary for run {args.run} in {cfg.runs_dir}", file=sys.stderr)
            return 1
        print(path.read_text(encoding="utf-8"))
        return 0
    runs = RunState.list_runs(cfg.runs_dir)
    if not runs:
        print(f"no runs in {cfg.runs_dir}")
    for r in runs:
        credits = r.get("credits")
        print(f"{r['run_id']:<44} {r.get('workflow') or '':<18} {r.get('status') or '':<9} "
              f"{r.get('ticket') or '-':<10} {credits if credits is not None else '-'}")
    return 0


def cmd_workflows(cfg: HarnessConfig, args: Any) -> int:
    for name in cfg.workflow_names():
        wf = cfg.workflow(name)
        steps = " -> ".join(f"{s.id}" + (f"[{s.role}]" if s.role else "") + ("*" if s.approval == "human" else "")
                            for s in wf.steps)
        print(f"{name:<18} {steps}")
    print("(* = approval: human)")
    return 0


if __name__ == "__main__":  # pragma: no cover
    sys.exit(main())
