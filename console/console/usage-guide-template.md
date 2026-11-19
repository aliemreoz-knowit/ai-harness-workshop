# Usage guide: <harness name>

A usage guide lets a colleague who was not there run your workflow without asking you. In the
console, Guides > "Start from the template" saves this file as `docs/usage-guide.md`; then edit it
like any file. Keep it to one page and replace every `<...>` with your own numbers, taken from
your runs and your run folders.

The test of a guide is the handover: someone follows it literally, alone, and you watch where
they get stuck. Every place they get stuck is a gap in the guide, not in the reader.

## 1. What this harness does

<One or two sentences: which kind of ticket, which workflow, which variant.>

- **Use it for:** <the kind of task it handles well>
- **Do not use it for:** <protected areas, changes it refuses, anything that needs real hardware or
  a gate your ladder does not run (fuzz and coverage are not run on this laptop)>

```
<the workflow steps, e.g. ticket -> planner -> implementer <-> gates -> test-writer -> ladder -> reviewer -> approval -> branch + commit>
```

## 2. Prerequisites

- The repository set up with the one setup command; note the `Depth:` and `Ladder:` lines it
  printed (which model the runs can use, and whether the gates run in Docker).
- The Harness Console running (Today > Your setup shows the same two facts).
- A ticket with its agent-ready version in `tickets/<KEY>.agent.md`.
- <anything else your harness needs: a branch, a variant file, an environment variable>

## 3. Run it

In the console: Runs > Check my code (gates only), then Tickets > the ticket > Run this ticket
(model "Fake model (no credits)" or "Your Copilot"). The same from a terminal (CLI equivalent):

```
python -m harness gates --profile fast                                               # the gates your change adds or tightens
python -m harness run --workflow <workflow> --ticket tickets/<KEY>.agent.md --fake --mode eval   # scripted model, no credits
python -m pytest harness/tests -q                                                    # the harness's own tests
python -m harness runs <run-id>        # summary: status, retries, interventions, credits
```

Runs in the console show how the harness behaves on one ticket. Whether a variant is better is a
question for an eval: the same tickets, several variants, repeated, judged by tests the agents never see.

Our settings (the files we changed and why):

| Knob | Value | Why |
|---|---|---|
| Models per role | <..> | <..> |
| Retry budget, review rounds | <..> | <..> |
| Credit caps (run / day) | <..> | <..> |
| Gate profile in the loop / before review | <..> | <..> |
| Prompt template | <..> | <..> |
| Guardrails we added | <..> | <..> |

## 4. What to expect

- **Approvals you will be asked for:** <plan, final commit, device stage, ...>
- **Gates that typically fail first, and what the feedback looks like:** <..>
- **Rough cost of a warm run:** <n> AI credits; **wall clock:** <n> min.

| Measure | Our value | From |
|---|---|---|
| Pass rate | <n of m, repeats> | leaderboard, variant <..> |
| Credits per run (median) / per pass | <..> | leaderboard, or Compare two runs |
| Wall clock per run (median) | <..> | leaderboard, or Compare two runs |
| Human interventions per run | <..> | leaderboard |

When it goes wrong:

| You see | It means | Do |
|---|---|---|
| Run stops at the `ticket` step | the sanitizer flagged instruction-like text in the ticket | read the ticket; release it only if the text is harmless |
| `escalated` | retry budget or credit cap used up | read `gates/` and `logs/` in the run folder; sharpen or split the ticket |
| `diff-policy FAIL` / protected path denied | the agent tried to touch a protected path | expected guardrail; never lift it for an agent run |
| <..> | <..> | <..> |

Everything a run did is in `.harness/runs/<run-id>/`: `summary.json`, `events.jsonl`,
`prompts/`, `gates/`, `plan.md`, `review.md`.

## 5. Handover checklist

A colleague should reach the expected result using only sections 2 to 4.

- [ ] Followed by: <who>, ticket: <KEY>, run id: <run-id>, result: <as expected / not as expected>
- [ ] Steps they marked as unclear (each one is a gap in this guide): <..>
- [ ] One improvement they suggested: <what they tried, what happened (run id), what they would change>
- [ ] What we changed in response: <..>
