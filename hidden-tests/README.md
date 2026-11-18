# hidden-tests/: the referee

The held-back tests of every ticket. They decide whether an agent's change really fixed the ticket.

`python -m harness experiment start --tickets TS-107 --only raw` starts each run in its own worktree next to this
repository, on the ticket's branch (`ticket/TS-107`: only that ticket's defect is open). That worktree does not
contain this folder, so the agent never sees these tests and cannot tune its change to them. When the run has ended,
`run.sh` copies the agent's tree into the dev container, adds the ticket's tests from `tickets/<ID>/tests/` and runs
them: `[hidden] PASS` or `[hidden] FAIL`.

    bash hidden-tests/run.sh <checkout> [host|asan] [TS-1xx ...]

Without a usable Docker, or with `TS_LITE=1`, the harness runs `lite.py` instead: the same tests and the same output,
built with zig cc on this machine (`scripts/lite_gates.py`, the Docker-free build of the gates). Only the host preset;
the container stays the reference.

    python hidden-tests/lite.py <checkout> [host] [TS-1xx ...]

`tickets/<ID>/meta.yaml` holds what the eval code reads: the ticket branch, the paths a change may touch, the test files.
