# Harness Console

Your own Harness Console: a web page on your laptop where you edit code and harness files, run the agent, read the
live log, approve steps and compare runs. The repository is
[ai-harness-workshop](https://github.com/aliemreoz-knowit/ai-harness-workshop). Python standard library only on the
server; no build step.

## Starting it

The setup script in the repository's root starts the console and prints its address (for example
`http://127.0.0.1:8765`). Open that address in your browser. To start it yourself, from this folder:

    python -m console serve --personal [--port P] [--budget N]

Only your own laptop can reach it, and there is no login. It works on a copy of your checkout (kept in
`.console-data/units/unit-01/repo` inside the checkout), so your own files stay as they are until you copy something
back. One console runs per checkout: a second start prints the address of the one that runs.

## The pages

- **Today**: your setup (the depth, the gate ladder) and the day, section by section: the story of each section (what
  was wrong, what the harness gained and why, how it was tested, what that showed), the commands on the stage (Run them
  here: they work in this copy) and your steps.
- **Blueprint**: the Harness Blueprint, thirteen boxes in six parts, filled in section by section.
- **Harness**: the harness as cards (roles, skills, hooks, gates, workflows, policy), each with a link to edit its file.
- **Code**: the files of the copy, an editor, Save (one commit) and what changed since the start.
- **Tickets**: the agent-ready tickets of the repository (`tickets/*.agent.md`) and your own.
- **Runs**: start a run (workflow, ticket, mode) or "Check my code"; one run's live log, steps, approval
  card, gate matrix, plan, review, diff and credits.
- **Evals**: the leaderboard of the experiments run from this checkout, and two runs side by side.
- **Guides**: the usage guide.

## The ladder line

The top of the Today page says which gate ladder the console uses, decided once when it starts: the full ladder runs
the gates in Docker; the lite ladder runs them without Docker, and the gates that need Docker are not run. The setup
records its choice; `TS_LITE=1` in the console's environment forces lite, `TS_LITE=0` never picks it.

## Credits

A run is capped at 30 credits and a day at 150 (the top bar shows "N of 150 credits today"). `--budget N` sets the
daily amount; what earlier days spent does not count. Runs use your own Copilot login, or the harness's fake model
(no model, 0 credits) when the setup found no usable login.

## The CLI drawer

The drawer at the bottom of the page shows the command line of what you are doing. Its commands start in the console's
copy of your checkout (`.console-data/units/unit-01/repo`), not in the checkout itself, so run them there.

## If something goes wrong

- Run the setup again; it prints the address of the console that runs, or starts one.
- The Today page's "Check again" repeats the setup check.
- Move, reset and removal of `.console-data`, proxies and the other setup problems are covered in the repository's
  README (the "If something goes wrong" section).
- Settings are in `console.yaml` next to this file (`CONSOLE_PORT` overrides the port).

