# workshop/

Everything the workshop day runs on the stage, so that you can run the same thing at home.

**One folder.** The stage runs in the Harness Console's copy of this repository, `.console-data/units/unit-01/repo`
(the console makes it on its first start): the folder the console's Code, Tickets, Runs and "Check my code" pages show.
The console's Today page runs its commands there, and every terminal of the day works there too, so whatever a command
changes, the console shows. Open a terminal (Git Bash) in the repository root and type once:

    source workshop/stage-shell.sh

It takes the terminal into the copy and sets `TS_LITE=1`, `MSYS_NO_PATHCONV=1`, the repository's `.venv`, `WORKSHOP`
(this folder: the copy has no `workshop/` on a ticket branch, so a command names a patch as
`"$WORKSHOP/patches/fault1-unused-variable.patch"`) and `HARNESS_HIDDEN_TESTS` (the root's `hidden-tests/`, which is never
in the copy). The console gives its own commands the same folder and variables.

| File | What it is |
|---|---|
| `commands.txt`, `commands.json` | Every command of the day in the order of the clock. The Harness Console shows the commands of each section on its Today page and can run them. |
| `shell.cmd` | Windows: `workshop\shell` in the repository root (PowerShell or cmd) opens Git Bash with `stage-shell.sh` sourced. It needs no Git Bash of your own: it takes the portable git the setup brought. |
| `stage-shell.sh` | `source` it once per terminal, in the repository root: the terminal goes into the console's copy, with the variables the commands need (see above). |
| `branches.sh` | Makes the workshop's branches local: `ticket/TS-1xx` (one per ticket, only that ticket open) and `after-section-1` .. `after-section-5` (the harness and the fixes after each section). `setup.sh` runs it. |
| `TS-104.agent.md` | The agent-ready version of TS-104 written in section 1. |
| `patches/` | The faults planted in section 2 and the injected change of section 3. |
| `stage-unit.sh`, `day-numbers.py`, `clean-day.sh` | The stage's helpers: the console's start point (`stage-unit.sh`, in the root with the console stopped: the copy on `ticket/TS-104`, with the ticket and `after-section-*` branches), the day's numbers, a clean start after a rehearsal. In the copy they are `python "$WORKSHOP/day-numbers.py"` and so on. |
| `recorded/` | Recorded runs the day shows. `recorded/day/`: every stage command run once on a fresh clone, with its output (`<id>.txt`), when, how long and how many credits (`index.json`), and the experiments' result files (`results/`); the Today page shows them. The `calib-*` / `verify2-*` files: the calibration of the variants, used by the leaderboard. |

The held-back tests are in `../hidden-tests/`. An experiment (`python -m harness experiment start ...`) starts every
run in a separate worktree from the ticket's branch (`ticket/TS-1xx`), where `hidden-tests/` is not: the agent never sees them.
Started in the console's copy, an experiment finds them through `HARNESS_HIDDEN_TESTS` (or, without it, as the
`hidden-tests/` beside a folder above the copy); its results go to the copy's `results/`, its worktrees next to the copy
(`.console-data/units/unit-01/repo-eval/`).
