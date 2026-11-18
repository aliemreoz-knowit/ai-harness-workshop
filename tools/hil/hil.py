#!/usr/bin/env python3
"""hil.py -- the HIL station (gate hardware): candidate -> Pi (or local stand-in) -> checks -> report.

    python tools/hil/hil.py run --ref <git ref> [--target local|pi] [--checks file,camera,keys]
                                [--yes] [--dry-run] [--config tools/hil/hil.yaml]
    python tools/hil/hil.py queue [--target pi] [--github] [--watch SECONDS] [--dry-run]
                                  [--recorded DIR] [--repo OWNER/NAME] [--yes]
    python tools/hil/hil.py check-config [--config ...]

run    one candidate: export the ref, cross-compile the daemon in the dev
       container, approval (hil.yaml targets.<t>.approval), deploy, the checks
       of hil.yaml: file (10 s, pkt_seq wraps inside a 1 s link cut through
       tsnetem, then 2 % loss), camera (source pause), keys (scripted 'k'
       presses with the rate heartbeat on), budgets, report.md. Without --checks: file + every enabled check.
       --target pi: scp + systemctl on the Pi (ssh host from hil.yaml).
       --target local: the same checks with everything in the dev container
       (the aarch64 binary under qemu, or the host build); the camera is
       replaced by tools/hil/pipe_source.py (same byte stream as rpicam-vid).
queue  every candidate of the queue file not run yet at its current commit,
       plus (--github or github.enabled) open PRs labelled github.label; the
       report goes back to the PR as a comment. --watch repeats the pass.
--dry-run   nothing touches the Pi or GitHub: ssh/scp/systemctl and every
            GitHub write are listed instead (GitHub reads still happen unless
            --recorded DIR answers them from recorded responses).
--yes       approve every deploy (takes the human out of the loop).

Exit status: 0 every candidate PASS (or DRY-RUN), 1 a FAIL / DECLINED, 2 an error.
"""
from __future__ import annotations

import argparse
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))

import hilcore  # noqa: E402
import hilqueue  # noqa: E402
from ghclient import GitHubClient, GitHubError, RecordedTransport  # noqa: E402


def _exit_code(verdicts: list[str]) -> int:
    if any(v == "ERROR" for v in verdicts):
        return 2
    if any(v in ("FAIL", "DECLINED") for v in verdicts):
        return 1
    return 0


def cmd_run(a: argparse.Namespace, cfg: dict) -> int:
    shell = hilcore.Shell(dry_run=a.dry_run, bash=cfg["station"].get("bash", ""),
                          echo=(lambda s: print("  $ " + s)) if a.verbose else None)
    approve = hilcore.make_approver(cfg, yes=a.yes)
    cand = hilcore.Candidate(ref=a.ref)
    checks = a.checks.split(",") if a.checks else None
    res = hilcore.run_candidate(cfg, cand, a.target, shell, approve, checks=checks, receiver=a.receiver)
    print("hil: %s %s -> %s" % (a.ref, res.verdict, res.report_path or "(no report)"))
    if res.error:
        print("hil: error: %s" % res.error, file=sys.stderr)
    return _exit_code([res.verdict])


def _github(a: argparse.Namespace, cfg: dict, out_dir: str):
    g = cfg["github"]
    if not (a.github or g.get("enabled")):
        return None
    names = g.get("token_env") or ["HIL_GITHUB_TOKEN"]
    names = [names] if isinstance(names, str) else list(names)
    transport = RecordedTransport.from_dir(a.recorded) if a.recorded else None
    return GitHubClient(a.repo or g.get("repo", ""), token=GitHubClient.token_from_env(names), api=g.get("api", ""),
                        dry_run=a.dry_run, transport=transport, out_dir=out_dir)


def cmd_queue(a: argparse.Namespace, cfg: dict) -> int:
    shell = hilcore.Shell(dry_run=a.dry_run, bash=cfg["station"].get("bash", ""),
                          echo=(lambda s: print("  $ " + s)) if a.verbose else None)
    approve = hilcore.make_approver(cfg, yes=a.yes)
    checks = a.checks.split(",") if a.checks else None
    verdicts: list[str] = []
    while True:
        try:
            gh = _github(a, cfg, str(shell.root / cfg["station"]["work_dir"]))
            results = hilqueue.process(cfg, a.target, shell, approve, gh=gh, queue_file=a.file, checks=checks,
                                       receiver=a.receiver)
        except GitHubError as exc:
            print("hil: github: %s" % exc, file=sys.stderr)
            return 2
        for r in results:
            print("hil: %s%s %s -> %s" % (("PR #%d " % r["pr"]) if r["pr"] else "", r["ref"], r["verdict"],
                                          r["report"] or "(no report)"))
            verdicts.append(r["verdict"])
        if not results:
            print("hil: queue: nothing new")
        if not a.watch:
            break
        time.sleep(a.watch)
    return _exit_code(verdicts)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--config", default=None, help="default tools/hil/hil.yaml")
    sub = ap.add_subparsers(dest="cmd", required=True)
    for name in ("run", "queue"):
        p = sub.add_parser(name)
        p.add_argument("--target", default="local", choices=["local", "pi"])
        p.add_argument("--checks", default="", help="comma list of hil.yaml checks, e.g. file,camera,keys (default: file + enabled)")
        p.add_argument("--dry-run", action="store_true")
        p.add_argument("--yes", action="store_true", help="approve every deploy (no human in the loop)")
        p.add_argument("-v", "--verbose", action="store_true", help="print every command")
        p.add_argument("--receiver", default=None, choices=["station", "candidate"],
                       help="whose tsrecv measures (default hil.yaml station.receiver)")
        if name == "run":
            p.add_argument("--ref", required=True)
        else:
            p.add_argument("--file", default=None, help="queue file (default hil.yaml queue.file)")
            p.add_argument("--github", action="store_true", help="also take labelled PRs")
            p.add_argument("--recorded", default=None, help="answer GitHub reads from recorded responses")
            p.add_argument("--repo", default=None, help="owner/name (default hil.yaml github.repo)")
            p.add_argument("--watch", type=float, default=0.0, help="repeat every N seconds")
    sub.add_parser("check-config")
    a = ap.parse_args(argv)
    try:
        cfg = hilcore.load_config(a.config)
    except (hilcore.HilError, OSError, ValueError) as exc:
        print("hil: config: %s" % exc, file=sys.stderr)
        return 2
    if a.cmd == "check-config":
        print("hil: config OK: checks %s, targets %s" % (", ".join(cfg["checks"]), ", ".join(cfg["targets"])))
        return 0
    if a.cmd == "run":
        return cmd_run(a, cfg)
    return cmd_queue(a, cfg)


if __name__ == "__main__":
    sys.exit(main())
