# Usage guide: our jira-to-pr harness

Sample of the block-5 exercise. A usage guide is written for another team that
has never seen this harness: after reading it they can run the workflow on their
own ticket without asking us. Copy this file, replace every `<...>` with your
own numbers from the leaderboard and the run folders, and keep it to one page.

## What it does

Takes one ticket (a markdown file or a Jira key) to a reviewed local branch with
one commit, never pushed:

```
ticket ─► planner ─► implementer ◄─► gates (fast) ─► test-writer ─► ladder ─► reviewer ─► approval ─► branch + commit
```

Use it for: a bug or a small feature in one or two modules of tinystream, with a
symptom someone can reproduce. Do not use it for: changes to `include/`,
`src/pool/`, `src/malloc_guard.c`, the harness or the scripts (protected: the
run will refuse), protocol redesigns, anything that needs the Pi to verify
(hardware is a separate gate, hardware, run by the HIL station).

## Before the first run

```
python -m harness doctor                 # green = your own Copilot login works (runtime copilot-user)
MSYS_NO_PATHCONV=1 scripts/check.sh build   # on a Windows host: Docker Desktop + the dev image
```

## Run it

1. Write the ticket from `tickets/TEMPLATE.md` into `tickets/<KEY>.md`. Fill
   *Acceptance criteria* and *Out of scope*; leave guesses about the cause out.
2. `python -m harness run --workflow jira-to-pr --ticket tickets/<KEY>.md`
   (a Jira key instead of the file reads the issue through the Atlassian MCP server, read-only).
3. Approve the plan when asked (it is in `.harness/runs/<id>/plan.md`). The
   final approval shows the diff, the gate results and the review.
4. Interrupted? `python -m harness run --resume <id>` continues at the first unfinished step.

Our settings (`harness/harness.yaml`, `harness/gates.yaml`):

| knob | value | why |
|---|---|---|
| models | planner, reviewer Sonnet; implementer, test-writer Haiku (Sonnet only for a retry the budget pays for) | cheapest model that passes per role |
| retries | 3 gate-triggered retries, 2 review rounds, 2 ladder rounds | then a human decides |
| credit caps | 15 per session, 60 per run (own login) | the orchestrator stops the turn in flight |
| ladder | compliance, diff-size (diff size <= 150 production lines), diff-policy, build-sanitizers, loopback, budgets | cheap checks first |
| prompt | `prompt_template: v1`; the `team` overlay is our experiment (below) | |

## What it costs and how well it works

| measure | our value | from |
|---|---|---|
| pass rate on the eval set | `<n of m, repeats>` | leaderboard, variant `<V..>` |
| credits per run (median) | `<..>` | leaderboard, cost column |
| wall clock per run (median) | `<..> min` | leaderboard |
| human approvals per run | 2 (plan, final) | workflow |

Experiment we ran (`harness/eval/variants-unit.yaml`): `U1` = team with the `team`
prompt and one retry fewer. Result: `<what won, by how much, and why we think so>`.

## When it goes wrong

| you see | it means | do |
|---|---|---|
| run stops at `ticket` | the sanitizer found instruction-like text in the ticket | read the ticket; release it only if the text is harmless |
| `escalated` after the implement step | retry budget or credit cap used up | read `gates/` and `logs/` in the run folder; fix the ticket or split it |
| `diff-size FAIL` | the change is bigger than the budget | the agent is rewriting more than the fix needs; narrow *Out of scope* |
| `protected-path` / `diff-policy FAIL` | the agent tried to touch a protected path | expected guardrail; never lift it for an agent run |
| reviewer rejects twice | the review found a blocker with file:line | read `review.md`; usually a missing regression test |

Everything a run did is in `.harness/runs/<id>/`: `summary.json` (status,
credits per role), `events.jsonl` (every tool call), `prompts/`, `gates/`.

## Send us an improvement

One change you would make after using this guide, as a short note: what you
tried, what happened (run id), what you would change. `<where we collect it>`.
