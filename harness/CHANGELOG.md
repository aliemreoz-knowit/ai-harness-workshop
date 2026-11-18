# Changelog -- tinystream harness

Release notes for the `v*-harness` tags, newest first. Earlier releases are in the messages of their
tags (`git tag -n --list "v*-harness"`).

## 2.9.5 -- 2026-11-12

Tools

- `tools/bridge`: the comments in `sources.py`, the bridge tests and the recorded demo fixtures use
  `PROJ` as the example Jira project key (`PROJ-7`, branch `agent/proj-7-ab12`). Only example data
  changes; the bridge behaves as before.

## 2.9.4 -- 2026-11-12

Docs

- The README, `docs/ARCHITECTURE.md` and the header comment of `.devcontainer/Dockerfile` name the
  workshop as the AI Harness Workshop; the example queue file in the `tools/hil/hilqueue.py`
  docstring lists a neutral ref (`refs/heads/my-fix`). Only words change; the image and the tools
  behave as before.

## 2.9.3 -- 2026-11-12

Tools

- The HIL report, `tools/hil/hil.py`, `tools/hil/measure.py`, tsrecv's comments,
  `scripts/opening_demo.sh` and the README describe the keys check and `tsrecv --heartbeat-ms` by
  what they do: the report line reads "viewer presses 'k' every 2000 ms with the rate heartbeat on
  (every 1000 ms)"; `--heartbeat-ms N` sends a SET_RATE at the current rate every N ms. What the check
  sends is unchanged.

## 2.9.2 -- 2026-11-12

Tools

- `scripts/device.sh`: viewer 1 runs with a rate heartbeat and reads its keys from a FIFO in the
  container; `send keyframe` writes `k` into it, so the board's "viewer keys" tile shows k sent,
  answered and ignored (as the hil keys check presses it). The one-shot request stays as
  `send keyframe --one-shot`. `start --second-viewer --viewer2-rate <kbps>` gives viewer 2 a rate it
  keeps (`tsrecv --kbps <kbps> --heartbeat-ms 1000`).
- `scripts/device.sh` runs on bash older than 4.4 too (macOS ships 3.2): an empty option list no
  longer stops it with "unbound variable".
- `scripts/device.sh`'s usage lists `--viewer2-rate <kbps>` (with `--second-viewer`, for `start` and
  `pipe-demo`) and `send keyframe --one-shot` on a line of its own.
- `tools/hil/README.md` and the comments in `tools/hil/hil.yaml` describe each check by what it
  measures (the keys check: keys sent, presses answered and ignored, with the rate heartbeat on).

## 2.9.1 -- 2026-11-11

Harness

- `TS_LITE=0` never runs the Docker-free ladder; the stage and the central eval batches set it. On a
  host, `scripts/check.sh` then waits for `docker info` without the 8 s bound (the caller's gate
  timeout is the limit) and runs the container ladder. When Docker does not work it prints
  `check.sh: TS_LITE=0 and Docker is not usable ...` and exits 2 without a result line, so the harness
  reports the batch's gates as ERROR, as for the container path without Docker. `TS_LITE=1` (the
  Docker-free ladder) and no `TS_LITE` (the 8 s probe, then the Docker-free ladder) are unchanged.
- `harness doctor` under `TS_LITE=0` waits up to 60 s for `docker info` and reports the container
  ladder (`Ladder: full (Docker)`). Without Docker its environment and gates lines are a FAIL, instead
  of a WARN that points at the Docker-free ladder.

Tools

- `scripts/device.sh send keyframe|set-rate=<kbps>` sends one control message to the running device,
  as the viewer's keys did: tsrecv's one-shot `--send`, run in `ts-device` with `docker exec`, with
  the device's own build of the ref (`start` labels the container with it). The one-shot is a client
  of its own for about a second: a keyframe request gives every viewer a keyframe, a rate is that
  client's own (the viewers keep theirs). `--dry-run` prints the `docker exec` line.
- `scripts/device.sh start --second-viewer` (also `pipe-demo`) starts a second `tsrecv` in the
  container: a second client of the daemon from the same host, on udp 5611 straight to the daemon
  (the slider's relay carries the first viewer only), writing `.harness/board2/` next to
  `.harness/board/` in the web root. The printed board address carries
  `?feed2=../../.harness/board2`, the "rx 2" thumbnail of `mission.html`. The board is still published
  on 127.0.0.1 only and still serves only the board files. `--dry-run` also prints the viewers'
  commands.
- `scripts/device.sh start --ref` reuses the build of a commit on Windows too: the cache looks for the
  three binaries, not for an exec bit Git Bash does not see on them, so a ref that was started once
  starts again without the archive and the build.

## 2.9.0 -- 2026-11-11

Harness

- The gate ladder runs without Docker. When `docker info` does not answer within 8 s (Docker not
  installed, or Docker Desktop stopped), or with `TS_LITE=1`, `scripts/check.sh` on a host prepares
  the change context as before and then hands off to `scripts/lite_gates.py`. It builds with `zig cc`
  from the pinned `ziglang==0.16.0` pip package (user scope, no admin rights) into `build-lite/`,
  prints `check.sh`'s result lines and writes the same `<G>.log` files:
  - build: the libraries and every test for this machine with the warning sets of `CMakeLists.txt`
    (`-Werror`), plus a cross build for aarch64-linux: every source including the POSIX ones, the
    daemon (with the malloc guard's `-wrap`) and the tools linked, the tests compiled;
  - unit-tests, property-tests, loopback: the tests, in a ctest-style log that the baseline rule reads test for test;
  - budgets: the codec budgets (`tests/budget`) only; `.text`/`.bss` and stack frames need the container;
  - diff-policy: `check.sh`'s own function, run on the host;
  - misra, no-heap, sanitizers, fuzz, coverage: `SKIP` with `needs Docker (not run here)`; compliance: `SKIP` with
    `harness runs compliance in Python`.
  The exit status is 0 when every gate PASSed or SKIPped. Every result it decides says `lite (zig)`:
  the container ladder stays the reference (clang and gcc 12 can warn differently). On Windows
  `src/capture/capture.c` (POSIX file descriptors) is replaced by `scripts/lite/cap_stub.c` (synth
  source only) and `test_capture` and `test_lb_file_clip` are reported as `SKIP` lines in the log;
  Linux and macOS build the real `capture.c` and run every test. Objects are rebuilt when a source,
  an included header or the command line changes, so a run without changes only runs the tests.
- The lite ladder's Python is the harness's own (`TS_PYTHON`, now in the environment of every gate
  run), else the `.venv` of the checkout (or of a worktree's main checkout), else `python3` or
  `python`. `TS_PYTHON` and `TS_LITE` join the names a gate's `env` in `gates.yaml` cannot set.
- `envdetect.docker_usable()`: `(True, "")`, `(False, "Docker not installed")` or
  `(False, "Docker installed but not running")` (`docker info` fails or takes longer than 8 s).
- `python -m harness doctor --depth [--skip-auth]` ends with `Depth: <depth>` -- `Try (fake model)`,
  `Try (your Copilot)`, `Full (fake model)` or `Full (your Copilot)` -- and `Ladder: full (Docker)` or
  `Ladder: lite (no Docker)`, and exits 0. "Full" is the container ladder, "Try" the Docker-free one;
  "your Copilot" needs a working Copilot login (auth OK), so `--skip-auth` gives "fake model".
  `--json` carries `ladder` and `depth` too. Without Docker the doctor's environment line is a WARN
  that names why, and its gates line checks zig instead of the container's toolchain. The "copilot
  cli not found" hint is `winget install GitHub.Copilot (Windows) or the GitHub install script (Linux,
  macOS)`.
- A gate the Docker-free ladder cannot run is "not run here", not "ladder incomplete". A `SKIP` with
  the reason `needs Docker (not run here)` (`gates.NOT_RUN_HERE`) counts like fuzz's allowed SKIP: the
  ladder can pass on the gates that ran, and a run does not stop for a human ("gate ladder
  incomplete"). The ladder says so:
  - `LadderResult.summary()` ends with `; lite ladder: misra no-heap sanitizers coverage not run here (needs Docker)`;
  - the feedback has that line under `GATES PASS (...)` / `GATES FAIL (...)`;
  - the gates JSON of the audit trail has `"not_run_here": ["misra", ...]`;
  - the `gates` event, the run state's `last_ladder` and `summary.json` have `"not_run_here": "lite
    ladder: ..."`.
  Every other SKIP still makes the ladder incomplete, and with Docker nothing changes.
- `envdetect.find_copilot_cli` also finds the CLI where the setup installs it, for a process whose PATH
  predates the install: on Windows `%LOCALAPPDATA%\Microsoft\WinGet\Links\copilot.exe`, then
  `%LOCALAPPDATA%\Microsoft\WinGet\Packages\GitHub.Copilot*\copilot.exe` (or one folder below); on Linux
  and macOS `~/.local/bin/copilot`. `COPILOT_CLI_PATH`, the npm install and a native binary on PATH
  (now on Windows too: a `copilot.exe`, never the `copilot.cmd` shim) come first. A `#!` script is
  never taken for the CLI.
- `check.sh diff-policy` matches C files by `[.](c|h)$` instead of `\.(c|h)$`: gawk (Git Bash, where the lite
  ladder runs diff-policy) drops the backslash from an `awk -v` value, mawk does not; both now read the same.

Tools

- `scripts/device.sh`: a simulated device for the projector. `start [--source file|synth] [--ref REF]
  [--port 8000]` runs one `tinystream-dev` container (`ts-device`) with `tinystreamd` on the recorded
  clip, `tsnetem` with its loss slider on the attached terminal, `tsrecv` writing the mission-board
  files and a web server for `tools/board/mission.html`; `pipe-demo` runs the camera-stall demo
  (`--source pipe` fed by `pipe_source.py`); `stop` removes the container. `--ref` runs the host build
  of that ref, `--dry-run` prints the `docker run` line.

## 2.8.1 -- 2026-10-27

Harness

- The `env` of a `kind: check` gate in `gates.yaml` can only set `TS_` knobs. A name that does not
  match `TS_[A-Z0-9_]+` (whole name), or that is one of `TS_IN_CONTAINER`, `TS_CONTAINER_NAME`,
  `TS_IMAGE` and `TS_CHECK_LOG` (the harness sets those itself), makes that gate an ERROR,
  `gate <id>: env <KEY> is not allowed`, and `check.sh` does not start for that batch. The check is
  made where the gate command is built, so every way into it is covered, not only a full ladder run.
  The `env` of a `kind: python` gate is settings its function reads, never the environment of
  `check.sh`: any name works there.
- The shell path check now reads Windows and PowerShell spellings of an operand, so an allowlisted
  reader (`cat`, `head`, `grep`, `find`, `sed -n`, `git add`, ...) can no longer reach a file outside
  the checkout. A drive path (`C:\...`, `c:/...`), a UNC share (`\\host\share`), a drive-relative name
  (`C:foo`), a rooted name (`\x`) and a PowerShell provider (`env:`, `HKCU:`, `variable:`, ...) are
  refused the same way a `/etc` path is. A backslash is read as a separator on every platform, and
  `Policy.rel()` places the same operand the same way on Windows and POSIX. Which operands are paths
  is decided by each command's position rules (a `cat`/`head`/`ls` operand, a `grep` file after its
  pattern, a `find` starting point or `-newer*` argument, a `git` pathspec), not by quoting -- on
  PowerShell a single-quoted operand is a literal string, so a quoted path is still checked and a
  `grep` pattern may hold any colon or backslash.
- An unquoted `,` `(` `)` `{` `}` `@` `[` `]` or `::` in a shell word is refused (`shell-grammar`):
  PowerShell builds arrays, expressions and script blocks from them, and bash brace lists, so the
  command would get other arguments than the words show. Quote the character when it is part of a
  pattern (`grep -n 'a,b' src`, `sed -n '1,20p' src/x.c`); an unquoted `sed -n 1,20p` is now refused.
- Before any other check, a shell command is refused (`shell-grammar`) when it holds a backslash before
  a double quote (`\"`: bash reads it as an escaped quote, PowerShell ends the string there), or any
  character outside printable ASCII and tab -- PowerShell reads Unicode quotes as quotes and Unicode
  dashes as the `-` of a parameter. This also refuses a line break, carriage return or NUL inside a
  command (a trailing line break is ignored): one command per call, chained with `&&` or `;`.
- Outside quotes a backslash may come only before a letter, a digit, `. _ - * ? /` or another
  backslash, and inside double quotes never before `$` (`shell-grammar`): bash reads it as an escape
  of the next character, PowerShell as a plain one, so `src\ ..\x` is one word to bash and two to
  PowerShell, and `"\$env:X"` is a variable to PowerShell. Single-quoted patterns and Windows paths
  (`src\net.c`) are unaffected.
- An option value that names a file is path-checked: a path-like inline value (`--file=`,
  `--pathspec-from-file=`, `--files0-from=`, `--exclude-from=`, `-Path:`) and the next word after
  `--file`, `--pathspec-from-file`, `--files0-from`, `--exclude-from`, `grep -f`, `-Path` and
  `-LiteralPath`.

Eval

- An overlay (`overlay_from`) copies only text the harness reads: `.md`, `.yaml`, `.yml` and `.json`
  files (the extension is matched without regard to case). A script, a binary or a file without an
  extension is left out.
- The run record names what was left out in `overlay_skipped` (sorted, relative to `overlay_from`, the
  first 50) and says how many files that is in all in the new `overlay_skipped_total`. The scan keeps
  only those 50 names in memory, however many files the unit has.

Bridge

- The real transport never follows a redirect: a 3xx answer is an error (`redirect refused`), so the
  `Authorization` header is not sent to another location. A repository that was renamed or moved now
  needs its new name in `bridge.yaml`.
- The real transport talks https only; plain http is for `localhost` and `127.0.0.1` (the tests'
  local servers). Any other URL is refused before anything is sent.
- `--dry-run` sends only GET and HEAD. Every other method, `OPTIONS` and `TRACE` included, is recorded
  as a write and not sent.
