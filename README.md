# AI Harness Workshop

The harness that is built, run and measured on the stage in Knowit's AI Harness Workshop, together with the practice project it works on.

> **The agents write the code. You build the harness.**

A harness is everything around an agent's loop that makes its "done" mean done, at a known cost.
Like any test setup: whatever is under test never grades itself. The setup feeds it, limits it, measures it and decides.
Here, the agent is what is under test.

**At the workshop you need no laptop:** everything in this repository is opened and run live on the stage.
If you want, set it up on your own laptop ([Run it yourself](#run-it-yourself-optional-about-5-minutes)) and run each stage demo yourself as it happens.
Afterwards it is here to read what you saw, to run it again, and to start on your own harness.

**Reading this before the day?** There is nothing to prepare. If you want to follow along on your laptop, you can run the setup
before the day or during the kick-off: it takes about 5 minutes. One thing helps: have one recurring task of your own team in mind, coding or not
(a kind of bug ticket, test-log triage, release notes, reviews), and keep asking what each part of the harness would look like for that task.

**Jump to:**
[What was shown](#what-was-shown-and-where-it-lives) ·
[The Blueprint](#the-harness-blueprint) ·
[Beyond this example](#beyond-this-example) ·
[Run it yourself](#run-it-yourself-optional-about-5-minutes) ·
[Words](#words-you-will-hear) ·
[If something goes wrong](#if-something-goes-wrong)

## The story: fix the broken stream

`tinystream` is a small C11 streaming daemon with a receiver and a network chaos relay. It is broken on purpose.
Every defect is a ticket (the texts are in `tickets/`), and every ticket has a symptom you can see in the stream.

The workshop starts with a raw agent that says "completed" on a ticket and is wrong.
Section by section, one part of the harness is added, until agents fix the tickets and a human approves the release.
Nobody writes the C code by hand: it changes only through an agent run.

The tickets mention a Raspberry Pi camera board. In the workshop we work only on its code and its tests, on a laptop.

## What was shown, and where it lives

A harness has six parts. Each section of the workshop adds one.

| Section | Part | What it adds | Where it lives | Try it |
|---|---|---|---|---|
| Kick-off | | A raw agent: one model, no rules, no checks. It says "completed" and is wrong. | `tickets/` | Read the ticket the raw agent got, TS-107: after the connection comes back, the picture stays frozen for up to ten seconds. |
| 1 | **Inputs** | An agent-ready ticket, the context every call gets, prompt templates, skills, tools. | `tickets/`, `AGENTS.md`, `.github/copilot-instructions.md`, `.github/instructions/`, `.github/skills/`, `harness/prompts/` | [Write an agent-ready ticket](#1-write-an-agent-ready-ticket) |
| 2 | **Checks** | A ladder of gates with no model in them, short feedback to the agent, retry budgets, credit caps. | `scripts/check.sh`, `harness/gates.yaml`, `harness/harness.yaml` (`loop`) | [Break the code and watch a gate catch it](#2-break-the-code-and-watch-a-gate-catch-it) |
| 3 | **Limits** | Four roles with their own tools, guardrails in three layers, untrusted ticket text, an audit trail. | `harness/harness.yaml` (`roles`, `policy`), `.github/agents/`, `.github/hooks/`, `harness/workflows/`, `harness/orchestrator.py` | [Send in a ticket with a hidden injection](#3-send-in-a-ticket-with-a-hidden-injection) |
| 4a | **Humans** | Approval where a mistake is expensive; everything else runs alone, with a cap. | `approval:` in `harness/workflows/*.yaml` | Start a run in Interactive mode in the console. |
| 4b | **Evidence** | Evals: the same tickets, many runs, several harness variants, pass rate and cost per pass. | `harness/eval/`, `tools/board/leaderboard.html` | Console, Evals page: compare two of your runs. |
| 5 | **Team** | A label on an issue starts a run; an owner; a usage guide another person can follow. | `tools/bridge/`, `.github/workflows/agent-fix.yml`, `.github/ISSUE_TEMPLATE/agent-fix.md` | Console, Guides page: start a usage guide from the template. |

The held-back acceptance tests that judge the eval runs are in `hidden-tests/`, and the fixes for the open tickets are on the `after-section-1` .. `after-section-5` branches.
The agent never sees either: every run starts on a `ticket/TS-1xx` branch, which has neither. An eval is only worth something if the agent cannot see what it is judged by.

## The Harness Blueprint

One page that describes a whole harness: thirteen boxes in the same six parts.
This is the page filled in for tinystream. For your own work, answer the same questions for one task your team does again and again.

| Part | Box | The question | In tinystream |
|---|---|---|---|
| Inputs | 1 · Task spec | What does done look like, and which check proves it? | A ticket with expected and actual in numbers, and the names of the tests that must pass. |
| | 2 · Context | What does the agent read every time? | `AGENTS.md` on every call, C rules per file type, `PROTOCOL.md` as the source of truth. |
| | 3 · Reusable prompts | Which instructions repeat? | One prompt per role, and templates for plan, implement, review and retry. |
| Checks | 6 · Verification gates | Which checks with no model in them decide? | Fourteen gates, the ones without a build first, among them compliance, style, comments, diff-policy, build, MISRA subset, unit tests, sanitizers and budgets. |
| | 7 · Iteration loop | How many tries, and what does the agent get back? | Three retries. At most six feedback items with gate, file, line and message. Then it escalates to a person. |
| Limits | 4 · Roles | Who does what, and what may each one touch? | Planner, implementer, test-writer, reviewer, each with its own tools and write scope. |
| | 5 · Orchestration | Which steps, in which order? | Plan, implement, tests, gates, review, commit. A reject goes back twice at most. |
| | 8 · Guardrails | What must never be touched or run? | Protected paths, forbidden calls, a shell deny list. Ticket text is data. |
| | 9 · Audit | What is on record afterwards? | Every prompt, tool call, gate result and credit, in the run folder. |
| Humans | 10 · Autonomy | Which step needs a person? | A person approves the plan and the merge. A flagged ticket stops and waits. The rest runs alone, with a cap. |
| Evidence | 11 · Evals | What is the eval set? | Three hard tickets, held-back tests, repeated runs, one leaderboard. |
| | 12 · Cost | What may a run cost, and how do we compare? | A cap on every run. Variants are compared by credits per pass, not per run. |
| Team | 13 · Team workflow | Who starts a run, who owns it, can a colleague run it without us? | A label on an issue starts the run; one named owner; a one-page usage guide, tested by a handover. |

The box numbers follow the printed sheet, so they are not in section order.

Two rules when you fill it in for your own task:

- The minimum viable version in every box. One concrete line beats a complete, empty template.
- If a rule lives only in a prompt, write down where the code check for it will go.

### From the Blueprint to a pilot

One page, two to six weeks:

- the task, and how a wrong result is caught;
- a baseline, measured before the pilot starts: how long the task takes people today, and how often it comes back;
- an eval set of real past tasks with a known good result, each variant run three times;
- a credit cap per run and a budget per month. The cap stops a runaway run; it is not a savings plan;
- risks, approvals, one named owner, a sponsor;
- a decision date: stop, extend or roll out, on pass rate, quality, time, interventions and credits per pass.

## Beyond this example

tinystream is small on purpose. A harness for real work often needs parts this one does not have.

- **Inputs:** memory that carries lessons from one run to the next; search over documents and old tickets, fetched when needed; logs, traces, crash dumps or screenshots as input; specifications the change must follow; two or three past changes of the same kind as a pattern; other systems as tools through MCP (an issue tracker, a database, a browser, your own test rig).
- **Checks:** security, secret and licence scanners; performance budgets; screenshot comparison for a user interface; a second model as judge where no test can decide (calibrate it against people first); mutation testing, which catches an agent that weakens a test to get to green; real hardware in the loop.
- **Limits:** a sandbox without network access, thrown away after the run; short-lived secrets with a narrow scope; rules for which data may reach a model at all; a kill switch for every running agent; other team shapes (several attempts in parallel with a judge, or one coordinator with specialists); a different model for each step, with a fallback.
- **Humans and evidence:** approval in the team chat or on the pull request, with the evidence attached; traces and dashboards; the eval set rerun on every change to a prompt, a gate or the model; reverted changes and reopened tickets as new eval cases; a staged rollout of a new harness version.
- **Team:** other triggers (a schedule, a failed CI job, an alert, a comment on a pull request), and other kinds of agent under the same harness: review on every pull request, ticket triage, test generation, documentation, the same mechanical change across many repositories, log analysis.

Different agents, the same thirteen boxes.

## Run it yourself (optional, about 5 minutes)

You get the folder, run one command, and open a web page on your own laptop: the Harness Console.
It needs no admin rights. Your own GitHub Copilot is optional: without it, agent runs use a scripted fake model.

What you need:

- A laptop with Windows, macOS or Linux. A new laptop with only VS Code on it is enough.
- You do not need git: on Windows the setup brings a portable Git for Windows along when there is none (about 60 MB,
  no install). macOS and Linux usually have git; on a Mac without it, `xcode-select --install` first.
- You do not need Python: if there is no Python 3.12 or newer, the setup gets one.

### 1. Get the folder

Download it: **Code > Download ZIP**, then unzip it. Or clone it with git (the green **Code** button gives its address).
Both work for the whole day: in a ZIP folder the setup fetches the workshop's branches from GitHub.

- On Windows, the folder path must be short: at most 80 characters. Windows allows 260 characters for a file path,
  and a run writes its files deep inside the folder. A longer path (a ZIP unpacked in Downloads, say) is fine: the
  setup copies the folder to `C:\Users\<you>\ws\ai-harness-workshop` and goes on there. Open that folder in VS Code
  from then on.
- Do not put the folder under OneDrive or another synced folder: keep it in a plain local folder.
- Pick the place now. If you move the folder after the first start, you have to delete `.console-data` in it
  (this deletes everything you made in the console) and run the setup again.

> [!NOTE]
> Unzipping in Explorer makes a folder inside a folder of the same name.
> Use the inner one: the folder that holds `setup.ps1`.

### 2. Run the one setup command

Open a terminal in that folder.

Windows (PowerShell):

```
powershell -ExecutionPolicy Bypass -File setup.ps1
```

Linux and macOS (and Git Bash on Windows):

```
bash setup.sh
```

- It takes about 5 minutes, a minute or two more on Windows when it brings git along.
- It needs no admin rights. It changes no system setting and no shell profile.
- Run it again at any time. A second run changes nothing that works, and it finishes what a stopped run left.

| Windows | Linux and macOS | What it does |
|---|---|---|
| `-Check` | `--check` | Only looks: it reports what is there and installs nothing. |
| `-NoConsole` | `--no-console` | Does not start the Harness Console. |

### 3. Read the last line

The last line is always `Depth: ...`. It says how much your laptop can run.
There are two depths: **Look** (the setup could not finish) and **Try** (everything).

| Last line | What you can run |
|---|---|
| `Depth: Look` | The setup could not finish. The lines above it say why. You can still read everything here on GitHub. |
| `Depth: Try (fake model)` | The console, the harness and the gates. Agent runs use the fake model. |
| `Depth: Try (your Copilot)` | The same, and agent runs use your own Copilot. |

> [!TIP]
> To move from "fake model" to "your Copilot": run the Copilot CLI command the setup prints in its last lines (the one in `.tools/copilot`), type `/login`, then run the setup again.

### 4. Open the Harness Console

The setup starts the console and prints its address, for example `http://127.0.0.1:8765`.
Open it in your browser. Only your own laptop can reach it, and there is no login.

The pages are Today, Harness, Code, Tickets, Runs, Evals and Guides.

- **Runs > Check my code** builds and tests the code with the gates, with no agent and no credits. Some gates are red on purpose: those are the open tickets.
- **Harness** shows the roles, the gates, the policy, the workflows and the loop, and validates your changes to them.
- **Today** has the workshop day section by section, with every command the stage showed (see *Follow the day* below).
- A save in the console is a commit in the console's own copy of your checkout, inside `.console-data`. Nothing is pushed anywhere, and nothing leaves your laptop except your own Copilot calls.

> [!IMPORTANT]
> **Before your first real agent run.** The console stops a run at 30 credits (1 credit is 1 US cent), and the default harness gives the planner and the reviewer the larger model, which can spend that on the plan alone.
> Either press **Continue** when the run reaches its cap, or raise `run_cap` in `console/console.yaml` and start the console again,
> or put the planner and the reviewer on the small model in `harness/harness.yaml` (Code page). The fake model and Check my code cost nothing.

### Follow the day

Every command the stage runs on the workshop day was also run once on 18 Nov 2026, on a fresh clone of this repository
set up with the command above. That recording is the stage's backup, and your reference when you run the same commands.

- Console, **Today**: every command of the day, section by section, in the order the stage showed them. Each one has
  **Recorded** (what it printed for us, how long it took, how many credits) and **Run** (runs it in the
  console's copy of this folder). That copy starts where the stage's does: on `ticket/TS-104`, where only TS-104 is open.
- The gates are built with zig on your laptop, the same as on the stage: build, misra (ticket ids and
  the forbidden calls of the MISRA subset), no-heap (the symbols of the daemon objects), the tests, UBSan, loopback and
  budgets. Only fuzz and coverage say `not run here`, and the day does not use them. The held-back tests that judge each
  experiment run are built with zig too (`hidden-tests/lite.py`).
- The same recordings are in `workshop/recorded/day/`: `<id>.txt` per command, `index.json` with the times and
  credits, `results/` with the experiments' result files. `workshop/commands.txt` has every command in the order of
  the clock.
- The gates give the same answer every time on the same code. A model run does not: the same ticket can pass for
  you where it failed for us, or the other way round, and cost a different number of credits. That is why the day
  compares harnesses by pass rate over repeated runs, not by one run.
- What it cost us: about 307 credits for the whole day (1 credit is 1 US cent). The lunch experiment is 271 of them
  (24 runs, 39 minutes on our laptop); everything else together is about 36. The day's numbers at the end count the
  experiment runs only (279). The fake model and the gates cost nothing.
- The setup installs Copilot CLI 1.0.100 into `.tools/copilot` and the harness always uses that one, with
  auto-update off. Newer CLI versions speak a protocol the Copilot SDK does not know yet. Your own CLI stays as it is.

### Three things to try

The commands below run from the folder you set up. Use the Python the setup made: `.venv\Scripts\python.exe` on Windows, `.venv/bin/python` on Linux and macOS, written here as `python`.

#### 1. Write an agent-ready ticket

Console, **Tickets**: open a ticket and fill in "Your version for the agent": context, steps, expected and actual in numbers, acceptance criteria, the tests that must pass, what is out of scope, a definition of done.
Write what can be checked. Leave guesses about the cause out: a wrong guess costs the agent more turns than no guess.

#### 2. Break the code and watch a gate catch it

Add a variable that is never used to any function in `src/rate/rate.c`, then run the cheapest gate:

```
python -m harness gates build
```

It fails in seconds, and prints what an agent would get back: gate, file, line, message and a few lines of context. Nothing more.
Undo it with `git checkout -- src/rate/rate.c`. Then try a `malloc` call in the same file and run `python -m harness gates compliance`: the policy check names the forbidden call before anything is built.

`python -m harness gates --profile fast` runs the gates an agent gets inside its loop.

#### 3. Send in a ticket with a hidden injection

One ticket in `tickets/` asks for verbose control-channel diagnostics and looks like a normal request. Read its raw text and find the lines that give the agent orders. Then, with that file's name:

```
python -m harness run --workflow jira-to-pr --ticket tickets/<that file> --mode interactive
```

The ticket is flagged before any model sees it, and the run asks you whether to continue. Answer `n`: no model call, no credits.

<details>
<summary><b>What the setup changes and downloads</b></summary>

What it makes stays in this folder: `.venv`, `.tools` and `.console-data`. These are the exceptions:

- If you do not have the GitHub Copilot CLI, the setup installs it for your user only
  (winget on Windows; in Git Bash npm when Node.js is there, else winget; GitHub's install script into `~/.local/bin`
  on Linux and macOS). winget may install a Copilot CLI newer than 1.0.100; that is fine. winget also installs PowerShell 7
  for your user if it is missing: the Copilot CLI package needs it.
- On Windows, if the setup has to fetch Python 3.12, that Python lives in `%APPDATA%\uv`
  and uv's cache in `%LOCALAPPDATA%\uv\cache`.
- pip's download cache, and zig's compiler cache (`%LOCALAPPDATA%\zig` on Windows, `~/.cache/zig` on Linux and macOS),
  stay in your user profile.
- If the folder came from a ZIP, the setup makes it a git repository with one commit, "workshop start".
- If git has no name and email here, the setup sets `Workshop participant <participant@localhost>` for this folder only.

It downloads only these, and it sends no data anywhere:

- From PyPI: `github-copilot-sdk` 1.0.23 and `ziglang` 0.16.0 (a C compiler, about 100 MB).
- Only if no Python 3.12 or newer is there: `uv` 0.13.5 from GitHub (its SHA-256 is checked), which fetches Python 3.12.
- Only if it is missing: the GitHub Copilot CLI (on Windows with winget, and PowerShell 7 with it if that is missing too).
- The Monaco code editor for the console, from npm (checked).
- Only on Windows when git is missing: a portable Git for Windows 2.56.1 from GitHub (its SHA-256 is checked), unpacked
  into `.tools/git`.
- Only for a ZIP folder: the workshop's branches, fetched from this repository on GitHub.

</details>

## Words you will hear

| Word | What it means |
|---|---|
| **Agent** | A model in a loop with tools (read, edit, run), working toward a goal. The loop ends when the model says it is done: that is an opinion, not a test. |
| **Harness** | Everything around that loop that makes "done" mean done, at a known cost: inputs, checks, limits, human approvals, evidence, and the team workflow. |
| **Agent-ready ticket** | A ticket rewritten for an agent: what can be observed and checked, the tests that must pass, what is out of scope. No guesses about the cause. |
| **Tool, MCP** | A tool is something the agent can call: read a file, edit, run a command. An MCP server plugs another system in as a bundle of tools. Every tool costs tokens on every call and adds risk. |
| **Gate** | A pass/fail check with no model in it: build, tests, static analysis, policy. Each gate has a short name, such as `build`, `misra`, `unit-tests` or `diff-policy`. |
| **Gate ladder** | The gates in a fixed order, the ones without a build first. A failing gate is feedback for the agent, not an error. |
| **Lite ladder** | The gates built with zig on your laptop, here and on the stage. A gate it cannot run (fuzz, coverage) says `not run here`: neither a pass nor a fail. The `tinystream-dev` container (`TS_LITE=0`) is not used in the workshop. |
| **Retry budget** | How many gate-triggered attempts an agent gets before the harness escalates: to a stronger model, or to a person. |
| **Role** | An agent with one job, its own prompt, model, tools and write scope: planner, implementer, test-writer, reviewer. |
| **Guardrail** | A rule enforced in code, not asked of the model: tool permissions, protected paths, a shell deny list, the diff policy. |
| **Orchestrator** | The code that runs the workflow step by step: roles, gates, approvals, caps, the audit trail. |
| **Eval** | Measuring the harness, not one change: the same tickets, many runs, several variants. A test checks the code; an eval compares ways of working. |
| **Held-back tests** | Acceptance tests kept out of the agent's copy (`hidden-tests/`, never on a ticket branch), so that no agent can tune its change to them. They judge the eval runs on the stage. |
| **Variant** | One harness configuration in an experiment. Change one thing at a time, or you cannot tell what helped. |
| **AI credits** | What an agent run on your own Copilot costs; 1 credit is 1 US cent. The console shows "N of 150 credits today". |
| **Fake model** | A scripted stand-in for the agents: no model, no credits, no code changes. A fake run shows how the harness behaves, not a fix. |
| **Depth** | How much your laptop can run: Look, Try or Full. The last line of the setup says which. |

## If something goes wrong

First, run the setup again. It picks up where it stopped.

<details>
<summary><b>During the setup</b></summary>

| What you see | What to do |
|---|---|
| PowerShell says running scripts is disabled on this system. | Use the whole line, with `-ExecutionPolicy Bypass`: `powershell -ExecutionPolicy Bypass -File setup.ps1`. If a company policy still blocks it, the setup cannot run on this laptop; everything can still be read here on GitHub. |
| `this folder's path has N characters: too long for the runs` | The setup copies the folder to `C:\Users\<you>\ws\ai-harness-workshop` and goes on there by itself. Use that folder from then on. If the copy fails, move the folder to a path of at most 80 characters and run the setup again. |
| Typing `bash` in PowerShell starts WSL or says it is not installed. | Use `workshop\shell` in the repository root instead: it opens Git Bash in the console's copy, with the portable git the setup brought. |
| `winget is not available on this computer`, then `the Copilot CLI is not installed, so you stay on the fake model.` | You are at Try (fake model), and that is fine. Later, install the CLI from https://gh.io/install-copilot-readme and run the setup again. |
| `Depth: Try (fake model)`, but you have Copilot. | Run the Copilot CLI command the setup prints in its last lines, type `/login`, then run the setup again. |
| `checking your Copilot login: this can take up to 5 minutes; please wait, do not close the window` | Wait. If the check does not answer, the setup tries once more without it. |
| `WARN  copilot cli   1.0.1xx at ... (pinned 1.0.100)` | The Copilot CLI updated itself when you started it. That is fine: the result still says `ready`. |
| `WARN  repo trust    ... is not in trusted_folders` | Start `copilot` in this folder and, when it asks whether you trust the folder, choose the answer that remembers it. Without it the hooks in `.github/hooks` do not fire. |
| You want Copilot to use the Jira or GitHub server of `.github/mcp.json`. | They are examples and start switched off (`disabledMcpServers` in `.github/copilot/settings.json`), so `copilot` opens no sign-in page. Type `/mcp` in Copilot to turn one on. The workshop reads its tickets from `tickets/` and needs neither. |
| `no Python 3.12 or newer with a venv module on PATH` | Nothing to do: the setup gets uv and lets it fetch Python 3.12. If that is blocked you see `there is no working Python 3.12 here.`: install Python 3.12 or newer from python.org ("for me only" on Windows), then run the setup again. |
| `could not download ... PortableGit ...` or `could not unpack the portable Git` | A network or proxy problem: run the setup again. Or install Git for Windows yourself (https://git-scm.com/downloads, it installs for your user too) and run the setup again. |
| `git is missing.` (macOS, Linux) | Install git (on macOS: `xcode-select --install`), then run the setup again. |
| `pip could not install the packages` | A network or proxy problem, or on Windows a path that is too long. Run the setup again later: it retries. Behind a company proxy, set `HTTPS_PROXY` before you run the setup. |
| `macOS: install a newer bash (brew install bash) so every gate runs; until then a check is skipped` | The full lite ladder needs bash 4 or later. Run `brew install bash`, then run the setup again. With the bash that macOS ships (3.2), one check is skipped and agent runs stop before they finish. |
| `Depth: Look` | Read the lines above it: each `FAIL` line says what to fix. Run the setup again. |

</details>

<details>
<summary><b>In the console</b></summary>

| What you see | What to do |
|---|---|
| The address does not open. | Open the address the setup printed: the port can differ from 8765. Run the setup again to see it. |
| `the console is already running: http://127.0.0.1:8765` | That is your console: open the address. |
| `the console is still starting: its address will be in .tools/console.log` | Open that file for the address (`.tools\console.log` on Windows), or run the setup again in a moment. |
| `the console did not start (log: ...). The harness works without it.` | Read the log it names, in `.tools`. Run the setup again. |
| You moved or renamed the folder after the first start. | Delete `.console-data` in the folder (this deletes everything you made in the console: copy out what you want to keep first), then run the setup again. |
| Today says `Your setup could not be checked.` | Press **Check again**. The line under it says why. |
| `This run reached its credit cap (30). Continue adds up to N more credits of today's 150.` | Press **Continue** to let the run go on, or **Stop**. See "Before your first real agent run" above. |
| `This run reached its step limit on the fake model. Continue or Stop.` | Press **Continue** or **Stop**: the fake model costs nothing. |
| `Not enough credits left for an agent run (it needs 1). The fake model needs none. Check my code stays free.` | You used today's credits. Choose "Fake model (no credits)", or use Check my code. |
| A card says `The run needs your approval`. | The workflow stopped at a step that waits for a person. Read the plan or the diff, then press **Approve** or **Reject (stops the run)**. Nothing is committed on reject. |
| A save is refused with a sentence about a rule. | The file stays as it was. The sentence names the rule: a protected path, a read-only file, a file that cannot be created. Fix the line and save again. |
| `A run is using your files. Save again when it has finished.` | Wait for the run, or stop it, then save again. |
| `lite ladder: coverage not run here` | Not an error: coverage (and fuzz) are not part of the lite ladder, and the day does not use them. |
| `The editor is not installed (run scripts/vendor_monaco.py): a plain text box is used.` | It works without it. Run the setup again later for the code editor. |
| You want to stop the console. | Use the command the setup printed next to the address: `Stop-Process -Id <id>` on Windows, `kill <id>` on Linux and macOS. Or restart your laptop. |

</details>

## Remove everything

> [!WARNING]
> Deleting `.console-data` deletes everything you made in the console: copy out what you want to keep first.

1. Stop the console: use the command the setup printed next to the address, or restart your laptop.
2. Delete `.venv`, `.tools` and `.console-data` in this folder, or delete the whole folder.
3. On Windows, if the setup fetched Python, also delete `%APPDATA%\uv` and `%LOCALAPPDATA%\uv\cache`.
4. The Copilot CLI, and PowerShell 7 if winget installed it, stay installed for your user. Keep them, or remove them the way you remove any program.

## What is in this repository

- tinystream: a C11 streaming daemon with a receiver and a network chaos relay, and a ladder of quality gates. The practice project.
  - `PROTOCOL.md`: the wire format.
  - `docs/ARCHITECTURE.md`: the architecture, the gates and their budgets.
  - `AGENTS.md`: the rules every agent follows in this repository.
- `harness/`: the harness that drives AI agents on tinystream (`harness/README.md`).
- `console/`: the Harness Console, a web page that runs on your own laptop.
- `tickets/`: the ticket texts.
- `tools/bridge/`: the label trigger that turns an issue into a run and a pull request.
- `setup.ps1` and `setup.sh`: the one setup command, for Windows and for Linux and macOS.
- `scripts/`: helper scripts for the project and the gates.

## Licence

MIT. See `LICENSE`.
Third-party notices are in `console/NOTICE`. The font licences are next to the fonts, in `console/web/fonts/`.
