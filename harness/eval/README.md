# harness/eval: eval runner, leaderboard and mission-board data

Section 4 of the workshop (design doc 4.4 and 12): run the experiment matrix
the eleven variants over the eval tickets, score every run with the hidden tests, show the
result on `tools/board/leaderboard.html`.

## Run an experiment

```
python -m harness eval --variants variants.yaml --tickets eval --parallel 4 --repeats 3
python -m harness eval --tickets KEY-1,KEY-2 --only gates,team --parallel 2
python -m harness eval --variants variants-section1.yaml --tickets KEY-1 --only raw,gates   # section 1: gates with run cap 12
python -m harness eval --tickets eval --fake --base-ref main      # whole pipeline, no models, no docker
python -m harness eval --resume --experiment exp-20261120-121500 --tickets eval   # after Ctrl-C
python -m harness eval --tickets eval --dry-run                   # print the plan
python -m harness experiment start --tickets eval --only team       # the agenda's wording: same as "eval"
python -m harness experiment leaderboard                          # same as "leaderboard"
```

Per (variant, ticket, repeat) the runner

1. creates a git worktree `<repo>/../<repo name>-eval/worktrees/<experiment>/<V>-<ticket>-r<k>`
   (next to the repository, so a copy of the checkout never includes them;
   `--worktree-root` overrides; pre-register that folder in the Copilot CLI
   `trustedFolders` or hooks do not fire) on branch `eval/<experiment>/<V>-<ticket>-r<k>`, reset to the ticket's branch
   branch (`ticket_branch` in `<hidden-tests>/tickets/<ID>/meta.yaml`, default
   `ticket/<ID>`); a missing ticket branch stops the experiment before anything starts;
2. calls `harness.api.run_workflow(workflow, ticket, settings, "eval")` with the
   variant's settings (`harness.api.RunSettings`: models per role, roles on/off,
   retry budget, custom instructions, prompt template, autonomy, credit cap);
3. reads the run's audit trail (`events.jsonl`, schema in `harness/api.py`) for
   credits, tokens, approvals, escalations, retries, gate results and the
   reviewer score; the `RunResult` fields are the fallback;
4. moves the workflow's commit (the harness commits on `agent/<key>-<id>`) onto
   the eval branch, measures the diff against the ticket branch (size, files outside the
   ticket's `allowed_paths`) and the golden diff size (`--golden-ref`) and writes
   the full patch against the ticket branch (commits, uncommitted and untracked files)
   to `diff.patch` in the run folder;
5. runs `<hidden-tests>/run.sh <worktree> <preset> <ticket>` (Git Bash on
   Windows, never WSL bash; `HARNESS_BASH` overrides) - success = every hidden
   test of the ticket passes;
6. appends one JSON line to `results/<experiment>.jsonl`, refreshes
   `results/results.json` and deletes the `agent/*` branch (its commit is on
   the eval branch).

Per-run artefacts: `results/<experiment>/<V>-<ticket>-r<k>/` (`settings.json`,
`run_result.json`, `events.jsonl`, `hidden.log`, `diff.patch`, `record.json`).
`--cleanup` removes each worktree after its run; the eval branch (with the
commit, if any) stays for inspection or a PR.

The orchestrator is found through `harness.api` (`set_backend`, then
`HARNESS_BACKEND=module:function`, then `harness.orchestrator.run_workflow`,
which is `harness/backend.py`: one real orchestrator run per job, the variant
applied to `harness.yaml` and the workflow, the ticket text read from
`<hidden-tests>/tickets/<ID>/ticket.md` (passed as `settings.extra["ticket_file"]`,
never copied into the worktree), the orchestrator's run folder under
`<run_dir>/orchestrator/` and its events translated into the eval schema in
`<run_dir>/events.jsonl`).

Backends (`--backend`):

| value | orchestrator | runtime | gates / hidden tests |
|---|---|---|---|
| `api` (default), `orchestrator` | real (`harness/backend.py`) | Copilot SDK, credits | real |
| `orchestrator-fake` | real | scripted `harness/fake.py` (no model, no credits) | real: the plumbing check before a paid batch |
| `fake` / `--fake` | toy model `harness/eval/fake.py` | none | `--fake` also swaps the hidden tests for `fake_hidden.py` |

## Overlay: a unit's harness on the eval tickets

A variant with `overlay_from: <dir>` (a workshop unit's worktree) runs on the harness structure of
`<dir>` instead of the ticket branch's, so a unit can evaluate its own harness on hidden tickets:

```yaml
variants:
  U3: {label: unit 3's harness, extends: team, overlay_from: C:/units/unit-3}   # null clears an inherited one
```

After the worktree is created from its ticket branch and before the run starts, the runner copies these paths
from `<dir>` into it and commits them as `eval: harness overlay from <name of dir>` (the repository's
git identity): `AGENTS.md`, `.github/agents/**`, `.github/skills/**`, `.github/instructions/**`,
`.github/copilot-instructions.md`, `.github/hooks/*.json`, `harness/harness.yaml`, `harness/gates.yaml`,
`harness/workflows/**`, `harness/prompts/**`. Of the files under these paths only `.md`, `.yaml`, `.yml` and
`.json` files are copied (the extension is matched without regard to case); a script, a binary or a file
without extension is skipped, and named in the run record as `overlay_skipped` and counted as
`overlay_skipped_total` (below).

What the run then uses of it:

- The orchestrator reads `harness.yaml` from the eval worktree, and `gates.yaml`, `workflows/` and
  `prompts/` from beside it (a file the overlay lacks is the ticket branch's copy), instead of the package's own;
  the variant's settings go on top, but only the ones it sets itself (below).
  A variant without `overlay_from` keeps loading the package's files. `overlay_from` and `extra.config`
  exclude each other (`extra.config` is how the runner points the backend at the worktree's file).
- The host-side keys never come from the unit's `harness.yaml`: the whole `copilot` section (`cli_path`,
  `env_passthrough`, versions, `model_cost`, ...), `paths.bash`, `paths.check_log`, `ticket.jira`, `runtime`
  and `runtimes` (the credit caps) are the package's own, whatever the unit's file says; roles, loop, policy,
  the rest of ticket and paths, commit and the workflows / gates / prompts it points at are the unit's
  (`backend.load_run_config`). `extra.runtime_mode: auto` reads the doctor file of the main checkout (the
  package's), not of the worktree.
- `gates.yaml` and the hooks stay the unit's; what they may run on the host is locked by the console's save
  rules, not here.
- The Python code of the harness (orchestrator, gates, backend) is always the package that started the
  eval, never the worktree's `harness/*.py`, even though the worktree has a copy of it.
- The Copilot CLI finds `AGENTS.md` and `.github/**` in its working directory, the worktree.

Which settings of the variant apply: an overlay variant applies only the settings it names itself, so that
evaluating the unit's own harness does not quietly evaluate the defaults of the variants file. A setting is
named when the variant's entry, or an entry of its `extends` chain, sets it; a value that only comes from
`defaults:` or from the `RunSettings` defaults is left out. The settings are `retry_budget`,
`custom_instructions`, `prompt_template`, `autonomy`, `gates` and, one each per role, `roles.<role>` and
`models.<role>` (so `models: {implementer: ...}` names only the implementer's model); everything else keeps the
value of the unit's `harness.yaml` and workflow (for instance its `loop.retry_budget` and its roles). The run
cap `max_ai_credits` is the exception: the default's or the variant's always applies, so a unit's file can
never raise an eval run's cost beyond the run cap. However many retries, rounds or pricier models the
unit's file asks for, the credit guard ends the run `budget_exceeded` at most one model call above the cap
(0.04-0.4 credits); a variant with no cap of its own is bounded by the package's `runtimes.<mode>` cap, which
the unit's file cannot change either (above). `timeout_s` and the `extra` knobs -- `escalation`,
`role_overrides`, `runtime_mode`, `baseline` -- are not harness.yaml settings: they apply to an overlay
variant exactly as before, on top of the unit's roles (`escalation: false` keeps every role on its own model,
`role_overrides` narrows a role's tools, read budget or session cap). The names that apply, with
`max_ai_credits` when a cap is set, are
passed to the backend as `extra.applied_knobs` and recorded as `applied_knobs` (sorted; null for a variant
without `overlay_from`, which applies everything as before); a variants file cannot set `extra.applied_knobs`.

How the copy is done:

- `<dir>` must be an existing directory (checked when the variants file is read); a relative path is
  relative to the variants file.
- Files the worktree lacks are added, files it has are replaced, nothing is deleted. Only regular files
  are copied: symlinks are skipped, and so is anything named `.git` (a nested repository or a gitdir
  pointer). A skipped file (wrong extension) is not looked at any further and counts for none of the limits
  below. Refused, and the run's setup fails (`run_status: setup_error`, nothing committed): a path that
  resolves outside `<dir>`, a file with several hard links, an unreadable directory, more than 500 files or
  5 MB in total (`OVERLAY_MAX_FILES`, `OVERLAY_MAX_BYTES` in `worktree.py`), a file that stopped being a
  regular file while the overlay was applied, or a worktree that is not clean after the commit.
- When nothing differs from the ticket branch there is no commit.
- The agent starts on a clean tree (`git status` is empty), so diff-policy and compliance see only its own change. For
  the same reason the record measures the run from the overlay commit: `diff`, `changed_files`,
  `path_violations`, `commits` / `workflow_committed` and `diff.patch` leave the overlay out, while
  `seed_commit` still names the ticket branch's commit and the eval branch keeps the overlay commit in its history.

## Record fields (results/*.jsonl)

`success` = `hidden_pass` (hidden tests), `workflow_committed` (the workflow
ended with a commit: `commits` > 0, sha in `commit`), `branch` (the eval branch,
which carries that commit; `agent_branch` = the deleted harness branch),
`status` / `workflow_success` (the workflow's own verdict), `gate_violations` (failed gates on the final attempt),
`gate_violation_score` (weighted by `gate_weights` in variants.yaml),
`gate_failures_total` (gate catches over all attempts), `reviewer_score`,
`duration_s`, `approvals`, `escalations`, `interventions` (= approvals +
escalations), `retries`, `ai_credits` (usage checkpoints, nanoAiu / 1e9),
`tokens` {in, out, cached}, `model_calls`, `diff`, `golden_diff`,
`path_violations`, `hidden` (per-test results), `settings`, `run_status`
(`done` | `setup_error` | `runner_error`), `overlay_from` (the variant's `overlay_from` as written, else
null), `overlay_commit` (sha of the overlay commit; null without an overlay or when it changed nothing),
`overlay_skipped` (the files under the overlay paths that were not copied because of their extension: sorted
paths relative to `overlay_from`, at most the first 50; `[]` without an overlay),
`overlay_skipped_total` (how many files that is in all, also beyond the 50 named; 0 without an overlay) and
`applied_knobs` (the settings the overlay variant applied, see above; null without an overlay).

## Leaderboard

```
python -m harness leaderboard                    # results/*.jsonl -> results/results.json (+ table)
python -m http.server 8000                       # repository root
http://localhost:8000/tools/board/leaderboard.html
```

Pass rate with a 95 % Wilson interval, credits per run and per pass,
interventions, retries, duration, gate violations, reviewer score, a pass matrix
per ticket; filters per experiment and ticket. The variant rows in `results.json` also carry
`overlay_from`, `overlay_commit` and `applied_knobs` (one value per variant: its last run's); the per-run rows
carry their own run's values; nothing is aggregated from them.

## Mission board

```
python -m harness board init                     # .harness/board/tickets.json + units.json from hidden-tests
python -m harness board ticket KEY-1 review --unit U3
python -m harness board unit U3 --level 3 --ticket KEY-1 --variant team
tsrecv ... --metrics .harness/board/metrics.json --frame .harness/board/frame.pgm
http://localhost:8000/tools/board/mission.html   # ?demo=1 without a receiver
```

Board data lives in `.harness/board/` (git-ignored), never under `tools/`: it
carries ticket ids, and misra rejects ticket ids anywhere in `tools/`.

## Tests

```
python -m pytest harness/eval/tests -q           # fake orchestrator, temporary git repos, no docker
```
