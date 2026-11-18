# harness -- the tinystream agent harness

A Python orchestrator on the GitHub Copilot SDK (`github-copilot-sdk==1.0.23`, driving the
pinned Copilot CLI 1.0.100). The agents write the code; this package is the harness around them:
roles, permissions as code, deterministic gates with structured feedback, bounded loops,
approvals, resumable state and an audit trail with the AI-credit cost of every run.

```
python -m harness doctor                                    # machine ready? which runtime mode?
python -m harness run --workflow jira-to-pr --ticket tickets/KEY-1.md
python -m harness run --workflow jira-to-pr --ticket KEY-1 --mode eval     # Jira key via MCP
python -m harness run --workflow jira-to-pr --ticket KEY-1 --run-id <id>   # the caller names the run (taken: exit 2)
python -m harness run --resume <run-id>                     # continue at the first unfinished step
python -m harness gates [--profile fast | build unit-tests ...]        # the ladder with structured feedback
python -m harness runs [<run-id>]                           # list runs / show a summary
python -m harness workflows                                 # workflows and their steps
python -m harness experiment start --tickets eval --only team # = python -m harness eval (harness/eval/README.md)
python tools/bridge/bridge.py poll                          # team trigger: agent:fix / hil-ready labels (tools/bridge/README.md)
python harness/level1_runner.py <role> "<prompt>" view,edit 30  # section 1: one raw copilot -p call (30 = the CLI's minimum credit cap)
```

Run from the repository root, inside the devcontainer or on the host (Windows with Git Bash,
macOS, Linux). Where the gates run is detected automatically: inside the container
`bash scripts/check.sh ...`, on the host also `bash scripts/check.sh ...` (it reads git on the host, then re-executes itself through `scripts/dockerbuild.sh`)
with `MSYS_NO_PATHCONV=1` (Docker Desktop and the `tinystream-dev` image are needed).
Host setup: `pip install --user github-copilot-sdk==1.0.23` and the CLI
(`npm i -g @github/copilot@1.0.100`); the devcontainer image has both.

Approvals without a terminal: with `HARNESS_HITL_DIR=<dir>` an interactive run asks its approvals
and escalations through files instead (`approvals.FileChannel`). The harness writes
`<dir>/pending.json` (`id`, `kind`, `step`, `question`, `details`, `options`, `asked_at`) and waits
for `<dir>/answer.json` `{"id": <that id>, "answer": <one of options>}`; both files are removed
after a valid answer. Approvals offer `approve` / `reject` (recorded `by: console`); no answer within
`HARNESS_HITL_TIMEOUT_S` (default 900) refuses the approval or aborts the escalation
(`by: console-timeout`). Eval and autonomous runs never ask. The gate containers get `.git` read-only
(`scripts/dockerbuild.sh`), so the code they run cannot plant config the host's git would follow.

## Workflow

```
ticket ─► planner ─► implementer ◄──► gates ─► test-writer ─► ladder ─► reviewer ─► approval ─► branch + commit
(sanitized)  plan.md     │  structured feedback              │ fail       │ reject   (human|auto)   (never pushed)
                         └── retry budget ─► escalation ◄────┴───────────┘ ladder / review rounds
```

The reviewer only sees a change whose full ladder passed (the results are in its prompt), and
`write_review` applies the review policy as code: a blocker / major finding needs `file:line`
(else it counts as minor), a reject without such a finding counts as an approve, an approve with
one as a reject. A gates step may send its failures back (`on_fail: <earlier step>`, bounded by
`loop.ladder_rounds`). Model escalation (`escalation_model`) happens only when the remaining
session and run budget pays for a turn on the stronger model (estimate: the role's last turn x
`copilot.model_cost` ratio); otherwise a human decides (eval: the run ends "escalated").
Prompt lines starting with `?{run_gates}` / `?{gates}` appear only when the role has the tool /
the step is gated, so a gate-less variant never reads about gates.

| File | What it configures |
|---|---|
| `harness.yaml` | runtime (`auto` / `facilitator` / `copilot-user`), credit caps per runtime, model / tools / write scope / shell allowlist / read budget (`max_reads`) / session cap (`max_ai_credits_per_session`) per role, git subcommands any role may run (`git_allow`; history never), retry budget, review rounds, policy (protected paths, denied shell fragments, forbidden calls), ticket sources, commit format |
| `gates.yaml` | gate id -> `scripts/check.sh` function (or a Python gate), budget, timeout, log parser; profiles (`fast`, `tests`, `ladder`, `full`, `release`), cheap first |
| `workflows/*.yaml` | `single-agent`, `jira-to-pr`, `test-only`, `review-only`, `release-readiness`: steps, roles, `approval: human \| auto` |
| `prompts/` | `system.md` (harness rules), `roles/*.md` (one stable prompt per role), task templates (`plan`, `implement`, `add-tests`, `review`, `solo`, `release`, `retry`, `rework`, `ladder`); overlays `v2/` (team-prompt-v2) and `cheap/` (cheap: planner on a context pack, reviewer on the diff only) |

Step kinds: `ticket`, `agent`, `agent_gated` (agent <-> gates loop), `gates`
(`on_fail: fail | escalate | continue | <earlier step>`), `review` (`on_reject: <earlier step>`),
`approval`, `commit`.

`require_test_change: bug | always` on an `agent_gated` step (jira-to-pr `tests`: bug; test-only:
always): after the gates pass, the orchestrator checks that the change adds or modifies a file
under `tests/` (for `bug`, only when the ticket's **Type** is Bug). If not, the step is not done:
the agent gets structured feedback (`GATES FAIL (TESTS:FAIL)`, `[regression-test]`) within the
same retry budget, and the reviewer never sees an untested bug fix. The reviewer prompt treats a
missing regression test as a major finding.

**Baseline-aware test gates** (`baseline.py`, `loop.baseline: auto | off`). A branch may already
fail tests that have nothing to do with the ticket (other open defects). Before the first agent
turn the orchestrator runs the workflow's test-type gates (`baseline: tests` in gates.yaml: unit-tests,
property-tests, sanitizers, loopback; `baseline: coverage`: coverage) once on the untouched base and stores every result (ctest
names, Unity case names) in `state.json` (`test_baseline`; a resumed run reuses it). When the base
is green nothing changes: every gate is absolute. When it is not, a test gate passes when no test
that passed on the base fails now (a new test counts: it must pass) and every required test
passes: the `test_*` names under the ticket's `## Acceptance tests` (or `## Required tests`),
else at least one base failure must pass now ("progress", over the test gates of the final
ladder only: a step's own gate profile may not run the gate where the fix shows, so between
steps no new failure is enough, and no feedback ever names a base failure as something to fix).
coverage: no module below its base coverage or its threshold, whichever is lower. Build, static and
policy gates (build, misra, no-heap, budgets, compliance, diff-policy) and a test gate whose base could not be parsed stay absolute.
The agent sees only new failures and still-failing required tests plus the number of
pre-existing ones; the reviewer gets the delta (fixed / new / pre-existing); `gates/*.json`,
`events.jsonl` (`baseline`, `gates`) and `summary.json` (`baseline`, `baseline_delta`) record the
base, the delta and the rule. The eval runner keeps absolute gates (`extra.baseline: auto` opts a
variant in); `HARNESS_BASELINE=off` switches the rule off for one run.

## Runtime and SDK usage

One role = one SDK session (`CopilotClient.create_session`), reused across retries and review
rounds so the prompt prefix stays cached:

- `custom_agents=[{name, prompt, tools, model}]` + `agent=<role>`, per-role `model`,
- `available_tools` = exactly the role's tools (built-ins + harness tools), the cheapest token saving,
- `tools` = harness custom tools via `@define_tool`: `run_gates`, `read_ticket`, `write_plan`, `write_review`,
- `hooks` = `on_pre_tool_use` (tool allowlist, protected paths, write scope, shell allowlist) and
  `on_post_tool_use` (MCP results wrapped as untrusted data),
- `on_permission_request` = the role policy (never a human prompt, never `approve_all`),
- no `session_limits`: the per-session and per-run credit caps are enforced by the orchestrator, which
  aborts the turn in flight when one is reached (with a limit set, the runtime shows the model its
  remaining credits and tells it to be frugal, and agents give up early); a role's own
  `max_ai_credits_per_session` (harness.yaml or a variant's `role_overrides`) lowers its session cap.
  The guard checks on every usage event, so a capped session or run overshoots by at most the
  model call in flight: 0.04-0.4 credits (one Haiku call .. one Sonnet call),
- `system_message` in `customize` mode (sections removed, harness rules appended),
- `session.on(...)` events -> audit (`assistant.message`, `tool.execution_start/complete`) and cost
  (`assistant.usage.copilot_usage.total_nano_aiu`, `session.usage_checkpoint.total_nano_aiu`;
  1 AI credit = 1e9 nano AIU),
- `send_and_wait` per turn, `set_model` to the role's `escalation_model` after the first failed attempt
  (only when the budget pays for it),
  `resume_session` on `--resume`,
- Jira tickets: a throw-away session with only the Jira MCP server; the tool runs through
  `session.rpc.tools.execute` (no prompt, no model call).

`doctor` uses `get_auth_status` and `list_models` only: it never sends a prompt.

Runtime modes: `copilot-user` (participant's own Copilot login) or `facilitator` (the
facilitator's login, central runs). `runtime: auto` uses what `harness doctor` decided on this
machine (`.harness/runs/doctor-<host|container>.json`).

## Guardrails (three independent layers)

1. `on_pre_tool_use` hook -- denies tools outside the role, writes to protected paths
   (`include/`, `src/pool/`, `.github/`, `scripts/`, `harness/`, ...) or outside the role's write
   scope, shell commands off the allowlist, redirections, network. It fails closed (a raising SDK
   hook would fail open).
2. `on_permission_request` -- the runtime's own permission requests (shell, write, read, url, mcp,
   custom-tool) answered by the same policy; MCP tools must be read-only (no network writes).
3. Gate compliance (`compliance.py`) on the resulting diff -- allowed paths, protected paths, forbidden
   calls in added lines, license header and SPDX identifier on new files, `PROTOCOL_VERSION` bump
   with PROTOCOL.md -- and gate diff-policy (`scripts/check.sh diff-policy`, the same check CI and the facilitator
   run): protected paths, new process creation / code loading, new TLV types. Every profile
   runs compliance and diff-policy first.

Ticket text (file or Jira) is sanitized (hidden characters, control characters, forged
delimiters) and scanned for instruction-like text; it reaches a model only inside an
`<untrusted-data>` block. A flagged ticket stops at the `ticket` step until a human releases it.

The CLI process -- and so every agent shell -- gets an allowlisted environment (`runtime.py`
`ENV_ALLOW`: PATH, HOME, TEMP, TLS / proxy settings, `COPILOT_*`, `TS_*`, `HARNESS_*`;
`GITHUB_TOKEN` only in Actions or with `HARNESS_COPILOT_ENV_TOKEN=1`), never the harness's
whole environment: the bridge and HIL start the harness with their Jira / GitHub tokens set.
`copilot.env_passthrough` in harness.yaml names extra variables; `JIRA_*`, `GH_TOKEN`, `HIL_*`
and `GITHUB_MCP_TOKEN` never pass. `harness doctor` lists what stays behind (names only).

## Modes, approvals, escalation

`--mode interactive` asks on the terminal (no terminal -> refuse, never hang). `--mode eval`
auto-approves every `approval: human` step but counts it (interventions metric); escalations end
the run. `--mode autonomous` passes approvals without counting (variant team-autonomous).
Escalation happens when the retry budget, the review rounds or the run credit cap is exhausted.

## Run folder `.harness/runs/<id>/`

`state.json` (resumable; saved after every change), `events.jsonl` (harness events and every SDK
session event), `prompts/` (every prompt verbatim, system prompts included), `gates/` (every ladder
result with structured feedback; `baseline-01-base.json`: the untouched base), `logs/` (copies of
failing gate logs), `ticket.json`, `plan.md`, `review.md`, `review.json`, `summary.json` (status,
interventions, retries, credits per role, tokens, branch, commit, baseline and its delta).

Structured feedback, as the model sees it after a failing gate:

```
GATES FAIL (compliance:PASS build:PASS no-heap:PASS unit-tests:FAIL property-tests:PASS)
unit-tests unit: see build-check/harness/unit-tests.log
- unit-tests src/proto/crc32.c:66 [-Werror=unused-variable] unused variable 'x'
     65 | {
    >66 |     int x = 0;
     67 |     return TS_CRC_INIT;
```

## Eval backend (`harness/backend.py`)

The eval runner (`python -m harness eval`, `harness/eval/`) calls only `harness.api.run_workflow`;
discovery finds `harness.orchestrator.run_workflow`, which is `backend.run_workflow`: one real
orchestrator run in the eval worktree, with the variant applied (models per role, roles on/off,
retry budget, credit cap, prompt template `prompts/<v>/` overlaying `prompts/`, custom
instructions off = `skip_custom_instructions`, gates off, autonomy `auto` = `--mode autonomous`).
Its events are translated into the eval schema (`harness/api.py`). `--backend orchestrator-fake`
runs the same path with the scripted runtime: no model, no credits, real gates and hidden tests.

## Tests

`python -m pytest harness/tests -q` -- uses a FAKE session (`harness/fake.py`): scripted turns
whose tool calls go through the real hooks, permission handler and custom tools; events are real
SDK `SessionEvent` objects. No test starts the Copilot CLI or sends a prompt. PyYAML is optional
(the devcontainer venv has none; `yamlmini.py` reads the config subset).
`python -m harness run --fake ...` runs a workflow with the fake runtime and the real gates.
