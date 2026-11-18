# tools/bridge -- team-level trigger for the agent harness

Labels in GitHub (and optionally Jira) start harness runs; the results come back as one
comment that is edited in place and a check run. The bridge polls, so it needs no webhook,
no public endpoint and no GitHub App.

| Trigger | What happens |
|---|---|
| issue labelled `agent:fix` | the label is replaced by `agent:running`, a fresh worktree is created from `origin/main`, `python -m harness run --workflow jira-to-pr --mode eval --runtime facilitator` runs there, the agent branch is pushed and a PR is opened (`Closes #n`). Afterwards the issue is labelled `agent:pr-open` or `agent:failed` |
| Jira issue in a trigger status (`Agent Ready`) or labelled `agent` | the same; the PR goes to the GitHub repo, the result is written as a comment on the Jira issue and the issue moves to `In Review` (`jira.pr_transition`) |
| PR labelled `hil-ready` | its head commit is appended to the HIL queue for `hil.py`, the PR gets a queued `hil` check and a "HIL: QUEUED (position k)" section in its bridge comment |

```
python tools/bridge/bridge.py check                 # read-only: token, repo, owner type, labels, Jira search
python tools/bridge/bridge.py labels [--dry-run]    # create the five labels if missing
python tools/bridge/bridge.py once  [--dry-run]     # one poll
python tools/bridge/bridge.py poll  [--interval 60] # until Ctrl-C (facilitator laptop, during sections 2-5)
python tools/bridge/bridge.py status                # handled items, HIL queue
python tools/bridge/bridge.py --fixtures tools/bridge/tests/fixtures/demo once   # offline demo, no network
python -m pytest tools/bridge/tests -q              # recorded responses, scripted harness
```

Run it from the repository root, on the facilitator laptop, with the facilitator's Copilot login
(`runtime: facilitator`: central login, `harness.yaml` caps of 40 credits per session and 200 per run).

## Safety

- **`--dry-run`**: reads are real (only GET and HEAD are sent), but no write leaves the machine (no
  comment, label, PR, check run or push; any other method counts as a write). Every suppressed write
  is printed as `[dry-run] POST /repos/...`. The harness
  then runs with the **fake runtime** (no model calls) unless `--live-runtime` is given. State
  and queue go to `*.dry-run` files, so a dry run never marks real work as done.
- **`--fixtures DIR`** answers GitHub and Jira from `DIR/github.json` / `DIR/jira.json` and
  implies `--dry-run`. This gives a fully offline rehearsal.
- All external calls go through `transport.py` (`UrllibTransport`, `DryRunTransport` and
  `ReplayTransport`). The clients (`github_client.py`, `jira_client.py`) contain only the calls
  listed below. The real transport talks https only (plain http only to localhost) and never follows
  a redirect: the `Authorization` header is not sent anywhere the configured URL did not point.
- Who may trigger: anyone with triage access can add labels. Use `github.allowed_actors` to
  restrict that to facilitators and team leads; the labeller is read from the issue events.
  HIL never runs a PR from a fork (`same_repo_only`).
- The harness keeps its own guardrails: it sanitizes the ticket text, applies the role
  policies and the compliance and diff-policy gates, and the harness run never pushes. The bridge pushes the branch
  only after the run has finished with `done`.
- Work items are deduplicated in `.harness/bridge/state.json`. To run an issue again,
  remove the label and add it back; the bridge re-arms an item once it stops matching its
  trigger. An item left `running` for longer than `harness.timeout_s` (the bridge was
  restarted mid-run, or the run hung) is re-armed on the next poll, still labelled or not.

## Issue -> ticket

Create the issue from the template `.github/ISSUE_TEMPLATE/agent-fix.md` ("Agent-ready fix");
it has the sections the harness reads. The bridge writes the ticket file as
`# GH-<n>: <title>`, the fields (`**Type:**` first, then the issue's own `**Name:** value`
lines and `**Source:**`) above the first section, then the issue's sections. The type is the
issue's `**Type:**` line (or an issue-form `Type` field), else its labels (`bug` -> Bug,
`enhancement`/`feature` -> Feature, `task` -> Task), else Bug. A Bug ticket makes the harness
require a regression test under `tests/`.

When the issue names no test under `## Acceptance tests`, the issue comment carries a note:
the harness then accepts any progress on a branch that already has failing tests (one of
them fixed is enough), so a partial fix can pass. The note does not stop the run; name the
tests that must pass.

## The single updating comment

Every issue or PR has at most one bridge comment. The comment is identified by a hidden marker:
`<!-- tinystream-bridge:issue -->` on issues and `<!-- tinystream-bridge:pr -->` on PRs. The PR
comment has two sections, `agent` (the harness result: status, run id, gates, review, credits,
interventions and retries, branch, commit, steps) and `hil`. The bridge keeps both sections in
its state file and rewrites the whole comment when either section changes. `hil.py` appends
its progress to the HIL queue (`running`, then `passed` / `failed` / `skipped` with the verdict
and the report path); on its next poll the bridge rewrites the `hil` section (with the report
when `hil.py` ran on the same machine) and completes the queued `hil` check run, using the
`check_run_id` stored in the queue entry (or a commit status).

Check runs need a GitHub App token or the Actions `GITHUB_TOKEN`. When the bridge runs with a
personal access token, it posts a commit status with the same name instead (`agent-harness` and
`hil`).

## HIL queue (`.harness/hil/queue.jsonl`)

The queue is an append-only JSON-lines file. The state of an entry is the fold of all lines that
carry its `id`:

```json
{"op": "enqueue", "id": "pr31-1a2b3c4", "pr": 31, "sha": "<40 hex>", "branch": "agent/gh-9-c0de",
 "repo": "owner/name", "url": "...", "title": "...", "status": "queued", "enqueued_at": "...",
 "check_run_id": 123, "comment_tag": "pr", "dry_run": false}
{"op": "update", "id": "pr31-1a2b3c4", "status": "running|passed|failed|skipped", "updated_at": "...",
 "verdict": "PASS|FAIL|DECLINED", "report": "build-hil/runs/<stamp>-pr-31/report.md"}
```

`store.HilQueue` (`pending()`, `update()`, `position()`) defines the format; `hil.py`
(`tools/hil/hilqueue.py`, `hil.yaml queue.bridge`) reads the pending entries and appends its
`update` lines in the same format. A new commit on a PR that is still labelled `hil-ready` is queued again, and the same
commit is never queued twice. A refused PR (fork, or a labeller who is not allowed) is
recorded with `status: refused`.

## Configuration (`bridge.yaml`)

Secrets stay in the environment and never go in `bridge.yaml`:

| Variable | Used for |
|---|---|
| `GITHUB_TOKEN` (or `GH_TOKEN`) | a fine-grained PAT on the workshop repo: Issues R/W, Pull requests R/W, Contents R/W (push), Commit statuses R/W, Metadata R |
| `BRIDGE_GITHUB_REPO` | `owner/name`, when `github.repo` is empty and `origin` is not on github.com |
| `JIRA_SITE`, `JIRA_EMAIL`, `JIRA_API_TOKEN` | Jira Cloud REST v3 with basic auth (an Atlassian API token of the facilitator account); only when `jira.enabled: true` |

The run worktrees live in `<repo>/../<repo name>-bridge/worktrees/<key>-<timestamp>`, and the
ticket files sit next to them. Add that folder to the Copilot CLI `trustedFolders` before the
first live run, as for the eval worktrees, or the hooks do not fire. Failed runs keep their
worktree; continue one with `python -m harness run --resume <run-id>` inside it.

API calls used: GitHub `GET repos/{r}`, `GET/POST labels`, `GET issues?labels=`,
`GET issues/{n}`, `GET issues/{n}/events`, `GET/POST issues/{n}/comments`,
`PATCH issues/comments/{id}`, `POST/DELETE issues/{n}/labels`, `GET/POST/PATCH pulls`,
`POST/PATCH check-runs`, `POST statuses/{sha}`; Jira `GET myself`, `GET search/jql`,
`GET/POST/PUT issue/{k}/comment`, `PUT issue/{k}` (label removal),
`GET/POST issue/{k}/transitions`.

## GitHub Actions variant (optional, `.github/workflows/agent-fix.yml`)

The Actions variant does the same job in CI. Labelling an issue `agent:fix` triggers this sequence:

1. `bridge.py ci-start` writes the ticket and labels the issue.
2. The harness runs with `python -m harness run ...` on the runner. Gates run in the
   `tinystream-dev` image, which the workflow builds from `.devcontainer/Dockerfile`.
3. `bridge.py ci-finish` pushes the branch and opens the PR, the check run and the comments.

The run folder is uploaded as an artifact. The workflow uses only the job's `GITHUB_TOKEN`
with `contents`, `pull-requests`, `issues`, `checks` and `copilot-requests: write`. No PAT or
model key is needed.

**Org-owned repository requirement.** The `copilot-requests: write` permission lets the
Copilot CLI in Actions authenticate with `GITHUB_TOKEN`. These requests are billed to the
organization's Copilot plan, so the repository has to be owned by an organization with
Copilot Business or Enterprise, and the org policy has to allow Copilot from Actions. A
personal repository has no such plan, so the job is guarded and stays **inert** (skipped)
unless both of these hold:

- `github.event.repository.owner.type == 'Organization'`
- the repository variable `AGENT_FIX_ENABLED` is `true` (an explicit opt-in, so an org fork
  does not start spending)

The workshop repository lives on the facilitator's personal account, so on the day the
workflow stays skipped and the polling bridge is the trigger. To show the Actions variant,
transfer or mirror the repository to an org that has Copilot Enterprise, set the variable, and
label an issue. Note that PRs opened with `GITHUB_TOKEN` do not trigger other workflows.
