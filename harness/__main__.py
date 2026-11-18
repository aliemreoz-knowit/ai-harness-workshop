"""python -m harness <command> ...

Commands owned by the eval package (harness/eval):
    eval         run a variant x ticket experiment (see harness/eval/README.md)
    leaderboard  merge results/*.jsonl into results.json for tools/board/leaderboard.html
    board        write / update tickets.json and units.json for tools/board/mission.html
    experiment   the agenda's wording: "experiment start ..." = "eval ...",
                 "experiment leaderboard ..." = "leaderboard ..."

Every other command is handed to the orchestrator CLI (harness.cli.main) when
that module exists.
"""
from __future__ import annotations

import importlib
import sys

EVAL_COMMANDS = {
    "eval": "harness.eval.cli:main_eval",
    "leaderboard": "harness.eval.cli:main_leaderboard",
    "board": "harness.eval.board:main",
}

# "python -m harness experiment start --tickets eval ..." (the agenda's wording) -> eval
EXPERIMENT_COMMANDS = {"start": "eval", "leaderboard": "leaderboard"}
EXPERIMENT_USAGE = ("usage: python -m harness experiment {start,leaderboard} [args]\n"
                    "  start        run a variant x ticket experiment (= python -m harness eval)\n"
                    "  leaderboard  merge result files into results.json (= python -m harness leaderboard)")


def _call(target: str, argv: list[str]) -> int:
    mod_name, fn_name = target.split(":")
    fn = getattr(importlib.import_module(mod_name), fn_name)
    return int(fn(argv) or 0)


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    if argv and argv[0] == "experiment":
        sub = argv[1] if len(argv) > 1 else ""
        if sub not in EXPERIMENT_COMMANDS:
            print(EXPERIMENT_USAGE, file=sys.stderr)
            return 0 if sub in ("-h", "--help") else 2
        argv = [EXPERIMENT_COMMANDS[sub], *argv[2:]]
    if argv and argv[0] in EVAL_COMMANDS:
        return _call(EVAL_COMMANDS[argv[0]], argv[1:])
    try:
        cli = importlib.import_module("harness.cli")
    except ModuleNotFoundError as exc:
        if exc.name != "harness.cli":
            raise
        print(__doc__.strip(), file=sys.stderr)
        print("\n(harness.cli not installed: only the eval commands are available)", file=sys.stderr)
        return 2
    return int(cli.main(argv) or 0)


if __name__ == "__main__":
    sys.exit(main())
