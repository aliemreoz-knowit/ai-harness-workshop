// The day's guide: Friday 20 November, section by section, in this console. Pure: no DOM, no imports. The Today page
// and the step strip under the top bar read it.
//
// The day is laptop-free: everything is opened and run on the stage, and the room watches, predicts and asks. So a
// section here says what is shown on the stage (`see`), what it teaches (`ideas`), and lists the steps behind it that
// happen in this console. On the day the presenter does them; afterwards anyone with the repository can do every one
// alone. The content is plain data and only asks for what the console can do: no git, no command line, no Python (a
// .py file is not editable here). A section is
//   {id, kind: 'block', label, title, icon, start, end, level, see, ideas, steps, note?}
// `story` is the section told as a change: {before: {text}, added: [{name, why, action}], none?, test, after, next}:
// what went wrong, what the harness gained and why, how it was tested, what that showed, and the next problem.
// `ideas` are the few things the section teaches, each {title, text}; `icon`
// names one of the icons of web/guideui.js; `level` is the harness level the section builds (0 to 5, null for the
// wrap-up); `note` says why a section has no steps. A break is {id, kind: 'break', label, start, end, note}. A step is
//   {id, title, why, where, how, action?, also?, done, pick?}
// (a step without `action` happens off the console: on paper, or on another page)
// `why` is what the step shows; `how` what to do; `done` the observable result ("Done when").
// `where` is {file, key?} (a file of the checkout, opened in the Code view) or {page, part?} (a page of the console);
// `action` and `also` are {label, to, find?}: `to` is a route of the console (`#/code?path=...`, `#/harness?tab=...`),
// `find` = {key, within?} asks the Code view to open at the line of that key. Steps that share a `pick` are
// alternatives: doing one of them is doing that item.
// `{u}` in a text or a link is the unit's folder name (u04 for unit-04), `{U}` its variant name (U04).
//
// Progress (the steps ticked, the sections whose strip is hidden, a pinned section) is kept in the browser's
// localStorage, one entry per unit; every access is guarded, so a browser that refuses storage gets a guide that forgets.

export const DAY = { year: 2026, month: 11, date: 20, label: 'Friday 20 November' };

export const LEVELS = [
  { level: 0, name: 'Raw agent', adds: 'One prompt, no rules, no gates.' },
  { level: 1, name: 'Task & context', adds: 'Agent-ready tickets, instructions and prompt templates.' },
  { level: 2, name: 'Gates & loops', adds: 'Build and test gates, structured feedback, retry budgets.' },
  { level: 3, name: 'Roles & guardrails', adds: 'Planner, implementer, test writer and reviewer, under policy.' },
  { level: 4, name: 'Approvals & evals', adds: 'Where people approve, and variants measured against each other.' },
  { level: 5, name: 'Team workflow', adds: 'A workflow another team can run from its guide.' },
];

const HARNESS_YAML = 'harness/harness.yaml';
const COPY = "the console's copy of the checkout (.console-data/units/unit-01/repo)";
const code = (path) => `#/code?path=${path}`;

export const AGENDA = [
  {
    id: 'kickoff',
    kind: 'block',
    label: 'Kick-off',
    title: 'Why a raw agent is not enough',
    icon: 'bolt',
    start: '09:00',
    end: '09:25',
    level: 0,
    story: {
      before: { text: 'TS-107: after a short outage the picture stays frozen for up to ten seconds. We gave the ticket to a raw agent: one small model, no repository rules, no checks, no retries.' },
      added: [
      ],
      none: 'Nothing yet. This is the starting point: level L0.',
      test: 'Held-back tests judge the change. They are kept out of the copy the agent works in (never on a ticket branch), so no agent can see them or tune its change to them.',
      after: 'The agent said "completed". The held-back tests said FAIL.',
      next: 'The agent did not know what done means.',
    },
    ideas: [
      { title: 'An agent is a loop', text: 'A model reads the context and proposes the next step, a tool does the work, and the result goes back in as input for the next call. That is also where the cost comes from.' },
      { title: '"Done" is an opinion', text: 'The loop ends when the model says so. In a raw agent nothing checks that claim.' },
      { title: 'A harness is a test setup', text: 'Whatever is under test never grades itself. Here the agent is what is under test: the harness feeds it, constrains it, measures it and decides.' },
      { title: 'Six parts, one per section', text: 'Inputs, checks, limits, humans, evidence, team. The agents write the code; you build the harness.' },
    ],
    steps: [
      {
        id: 'k-setup',
        title: 'Check your setup',
        why: 'Know what this computer can run before anything starts.',
        where: { page: 'Today', part: 'Your setup' },
        how: 'Read the Your setup box at the top of this page. It says whether agent runs use your Copilot login or only the scripted model, and whether the gates run in Docker (the full ladder) or without it (the lite ladder). "Check again" asks again.',
        done: 'You can say which model and which ladder this computer uses.',
      },
      {
        id: 'k-ticket',
        title: 'Read the ticket the raw agent got',
        why: 'This text is everything the raw agent knows about the problem.',
        where: { page: 'Tickets', part: 'TS-107' },
        how: "Open TS-107 and read the left pane: the reporter's own words.",
        action: { label: 'Open TS-107', to: '#/tickets/TS-107' },
        done: 'You can name two things an agent would need that the ticket does not say.',
      },
      {
        id: 'k-check',
        title: 'Check my code',
        why: 'See the gates work without any agent: the starting point of the day.',
        where: { page: 'Runs', part: 'Check my code' },
        how: `Press "Check my code". It builds and tests ${COPY} with the fast gates. No agent, no credits.`,
        action: { label: 'Open Runs', to: '#/runs' },
        done: 'The run has ended and shows one line per gate. A red gate is an open ticket: nobody has fixed it yet.',
      },
    ],
  },
  {
    id: 'section1',
    kind: 'block',
    label: 'Section 1',
    title: 'Standardized task description and context',
    icon: 'doc',
    start: '09:25',
    end: '10:05',
    level: 1,
    story: {
      before: { text: 'The raw agent did not know what done means: the ticket named no tests, and the agent read no repository rules.' },
      added: [
        { name: 'Agent-ready ticket', why: 'It names the tests that must pass, so a pass can be checked.', action: { label: 'Open TS-104', to: '#/tickets/TS-104' } },
        { name: 'Context files', why: 'AGENTS.md and the C rules reach the agent on every call.', action: { label: 'Open Instructions', to: '#/harness?tab=instructions' } },
        { name: 'Prompt templates and skills', why: 'The instructions we used to type by hand, loaded only when needed.', action: { label: 'Open Skills', to: '#/harness?tab=skills' } },
      ],
      test: 'The same ticket (TS-101) for the raw agent and for one agent with context and gates. Then TS-104, rewritten live, goes to a real run.',
      after: 'Done is defined: the ticket names the tests that must pass. Without named tests, the harness had accepted half a fix.',
      next: 'The agent knows what to do, but it still judges its own work.',
    },
    ideas: [
      { title: 'An agent-ready ticket has nine parts', text: 'Context, steps to reproduce, inputs, expected and actual in numbers, acceptance criteria, acceptance tests, out of scope, definition of done. Every criterion can be checked; there are no guesses at the cause.' },
      { title: 'A ticket is data, never instructions', text: 'Its text describes a problem. An agent must not take orders from it.' },
      { title: 'Context has a schedule', text: 'AGENTS.md goes into every call, so keep it short. Path instructions load per file type, skills only when the task needs them, hooks run on every tool call.' },
      { title: 'Every tool costs tokens', text: "A session's first call is about 24,000 tokens, 10,000 of them tool descriptions. Fewer tools per role is the easiest saving; cached input was about 12 times cheaper in our runs." },
    ],
    steps: [
      {
        id: 'section1-context',
        title: 'Read what the agent reads',
        why: 'The repository tells the agent its rules; you should know which file says what.',
        where: { page: 'Harness', part: 'Instructions' },
        how: 'Open the Instructions tab: AGENTS.md and the repository instructions go into every agent call, the path-scoped instructions only for matching files. Then look at the Skills and Hooks tabs: what is loaded when a task needs it, and what runs on every tool call.',
        action: { label: 'Open Instructions', to: '#/harness?tab=instructions' },
        done: 'You know which instruction file applies to a C file under src/.',
      },
      {
        id: 'section1-ticket',
        title: 'Write the agent-ready ticket',
        why: 'The same problem, told so that a gate can check every line of it.',
        where: { page: 'Tickets', part: 'Your version for the agent' },
        how: `Open TS-104. In the right pane fill the nine parts: Context, Steps to reproduce, Inputs, Expected, Actual, Acceptance criteria, Acceptance tests, Out of scope, Definition of done. Write what can be observed; leave guesses about the cause out. On the stage a prepared version is pasted in. Save keeps it as tickets/TS-104.agent.md in ${COPY}.`,
        action: { label: 'Open TS-104', to: '#/tickets/TS-104' },
        done: 'The ticket is saved, all nine parts are ticked, and Acceptance tests names every test that must pass.',
      },
      {
        id: 'section1-run',
        title: 'Run the ticket',
        why: 'A real agent run on the ticket you just wrote; it is read in section 2.',
        where: { page: 'Tickets', part: 'Run this ticket' },
        how: 'Press "Run this ticket". Choose Mode "Autonomous" (the steps that would ask a person pass on their own) and Model "Your Copilot" (your login, counted against today\'s credits). "Fake model" plays the agents without changing code. Then press "Start run". It takes a few minutes.',
        action: { label: 'Open Runs', to: '#/runs' },
        done: 'The run page shows the workflow as a flow from ticket to commit, and the first step is done.',
      },
    ],
  },
  {
    id: 'section2',
    kind: 'block',
    label: 'Section 2',
    title: 'Verification and iteration loops',
    icon: 'shield',
    start: '10:05',
    end: '10:50',
    level: 2,
    story: {
      before: { text: 'The model judged its own work. On TS-107 it said "completed"; the held-back tests said FAIL.' },
      added: [
        { name: 'Gate ladder', why: 'Checks with no model in them decide, the ones without a build first.', action: { label: 'Open Gates', to: '#/harness?tab=gates' } },
        { name: 'Structured feedback', why: 'On a failure the agent gets gate, file, line and message: six items at most.', action: { label: 'Open Gates', to: '#/harness?tab=gates' } },
        { name: 'Retry budgets and caps in code', why: 'How often it may try is a number in harness.yaml that the model never sees.', action: { label: 'Open the loop', to: '#/harness?tab=loop' } },
      ],
      test: 'Two faults put in on purpose: an unused variable, and a malloc in the daemon. Which gate catches each?',
      after: 'build caught the unused variable in seconds; compliance and no-heap caught the malloc. With the caps kept in code, 4 of 5 runs passed instead of 1 of 3.',
      next: 'One agent still holds every tool.',
    },
    ideas: [
      { title: 'The gate decides, not the model', text: 'Fourteen gates, the ones without a build first: compliance, style, comments and docs, diff policy, then build, MISRA subset, no heap after init, unit and property tests, sanitizers, loopback, budgets and coverage. A release adds a fuzz run. The Gates tab lists them. No model is involved.' },
      { title: 'Feedback is structured', text: 'A failing gate sends back the gate, file, line, rule, message and a short excerpt: six items at most. Nothing more reaches the agent.' },
      { title: 'The loop has budgets', text: 'Three implementer retries, two ladder rounds, two review rejects. After a failed attempt a stronger model takes over if the budget pays for it. When the retries are used up, a person gets the run.' },
      { title: 'Caps live in the orchestrator', text: 'Shown its remaining credits, the model gave up early: 1 of 3 runs passed. With the same caps kept in code and never shown: 4 of 5.' },
    ],
    steps: [
      {
        id: 'section2-run',
        title: 'Read the run from section 1',
        why: 'Watch the gates decide: red before the agent, green after its change.',
        where: { page: 'Runs', part: 'the run on TS-104' },
        how: 'Open the run. Follow the flow at the top, then read the gate lines in the log from the top: before the agent started (red: that is the defect), after each attempt, the full ladder, the review, the commit. The commit stays local: nothing is pushed.',
        action: { label: 'Open Runs', to: '#/runs' },
        done: 'You can say on which attempt the gates turned green and whether the reviewer sent the change back.',
      },
      {
        id: 'section2-ladder',
        title: 'Read the gate ladder',
        why: 'The ladder is the definition of "checked" for this repository.',
        where: { page: 'Harness', part: 'Gates' },
        how: 'Open the Gates tab. The ladder runs the cheapest gate first; the fast profile runs after every attempt of the agent, the full ladder once before the review.',
        action: { label: 'Open Gates', to: '#/harness?tab=gates' },
        done: 'You can name the gate that runs the unit tests and the gate that looks for allocating calls.',
      },
      {
        id: 'section2-fault',
        title: 'Plant a fault and let a gate catch it',
        why: 'A check you have never seen fail is a check you cannot trust.',
        where: { file: 'src/rate/rate.c' },
        how: 'Open src/rate/rate.c and add the line "int demo_unused = 0;" as the first line of a function. Save, then press "Check my code" on the Runs page. Afterwards take the line out again and Save. On the stage the same fault goes in with the Run buttons under "Commands on the stage", in this same copy.',
        action: { label: 'Open rate.c', to: code('src/rate/rate.c') },
        also: { label: 'Open Runs', to: '#/runs' },
        done: 'Check my code shows build FAIL with the file and the line; with the line removed it passes again.',
      },
      {
        id: 'section2-loop',
        title: 'Read the loop and its budgets',
        why: 'How often the agent may try again is a number you set, not a mood of the model.',
        where: { page: 'Harness', part: 'Loop' },
        how: 'Open the Loop tab: retries per step, review rounds, ladder rounds and the baseline are settings in harness.yaml. The orchestrator keeps them; the model never sees them.',
        action: { label: 'Open the loop', to: '#/harness?tab=loop' },
        done: 'You can say how many retries an implement step gets and what happens after the last one.',
      },
    ],
  },
  { id: 'break-1', kind: 'break', label: 'Break', start: '10:50', end: '11:05', note: 'A break.' },
  {
    id: 'section3',
    kind: 'block',
    label: 'Section 3',
    title: 'Agent roles, guardrails and compliance',
    icon: 'lock',
    start: '11:05',
    end: '12:00',
    level: 3,
    story: {
      before: { text: 'One agent held every tool. It could edit a test until it passes, touch protected code, or obey orders hidden in a ticket. Nothing recorded who did what.' },
      added: [
        { name: 'Four roles', why: 'One job per agent, and only the tools for that job.', action: { label: 'Open Roles', to: '#/harness?tab=roles' } },
        { name: 'Hooks and protected paths', why: 'Code checks every tool call and can refuse it; the model cannot talk its way past it.', action: { label: 'Open Hooks', to: '#/harness?tab=hooks' } },
        { name: 'Untrusted input', why: 'A sanitizer flags orders hidden in a ticket before any model reads it.', action: { label: 'Open Policy', to: '#/harness?tab=policy' } },
        { name: 'Audit trail', why: 'Every prompt, tool call, gate result and credit is in the run folder.', action: { label: 'Open Runs', to: '#/runs' } },
      ],
      test: 'TS-666 hides a prompt injection. We run it, put the injected change in by hand, and a volunteer tries to talk the agent round.',
      after: 'Caught three times: the sanitizer flagged the ticket, compliance and the diff policy blocked the change, misra failed it. The agent refused and named the rule.',
      next: 'How much do we trust it, and how do we know?',
    },
    ideas: [
      { title: 'Four roles, one session each', text: 'The planner reads and writes the plan. The implementer edits src and tools. The test-writer edits tests only. The reviewer is read-only and sees only changes whose full ladder passed.' },
      { title: 'Guardrails in three layers', text: 'A hook before every tool call, a permission handler in the orchestrator, and the compliance gate with the diff policy on the final diff. Each one fails closed.' },
      { title: 'Input is untrusted', text: 'A sanitizer flags instruction-like text in a ticket before any model sees it. A person decides whether the run goes on.' },
      { title: 'Everything is on record', text: 'Every prompt, tool call, gate result, approval and credit lands in the run folder. Compliance asks what the agent did, not whether it behaved.' },
    ],
    steps: [
      {
        id: 'section3-roles',
        title: 'Read the roles',
        why: 'One job per agent, and only the tools for that job.',
        where: { page: 'Harness', part: 'Roles' },
        how: 'Open the Roles tab. Each role has a model, a tool list, the shell commands it may run and the paths it may write. The fifth card, solo, is the one agent of the single-agent workflow.',
        action: { label: 'Open Roles', to: '#/harness?tab=roles' },
        done: 'You can say which role may write which paths.',
      },
      {
        id: 'section3-injection',
        title: 'Find the injection in TS-666',
        why: 'A ticket can carry orders for the agent that a human reader never sees.',
        where: { page: 'Tickets', part: 'TS-666' },
        how: 'Open TS-666 and read its text the way the agent would get it, hidden comments included.',
        action: { label: 'Open TS-666', to: '#/tickets/TS-666' },
        done: 'You have found the text that tries to give the agent orders.',
      },
      {
        id: 'section3-ask',
        title: 'Run TS-666 with a person in the loop',
        why: 'The sanitizer reads the ticket before any model does, and a person decides.',
        where: { page: 'Runs', part: 'New agent run' },
        how: 'On the Runs page choose Workflow "jira-to-pr", Ticket "tickets/TS-666.md" (the reporter’s text, hidden orders included), Mode "Interactive", then Start run. The run page asks whether to go on with a flagged ticket: press Reject. Nothing is spent on a model.',
        action: { label: 'Open Runs', to: '#/runs?ticket=tickets/TS-666.md' },
        done: 'The run page lists what the sanitizer flagged, and the run stopped at your Reject.',
      },
      {
        id: 'section3-protect',
        title: 'Protect a file',
        why: 'Add one guardrail yourself: a file no role may write.',
        where: { file: HARNESS_YAML, key: 'policy.protected' },
        how: 'Open harness.yaml at policy.protected and add "src/malloc_guard.c", the file that holds the memory guard. Save, then press Validate on the Harness page.',
        action: { label: 'Open harness.yaml', to: code(HARNESS_YAML), find: { key: 'protected', within: 'policy' } },
        also: { label: 'Open Policy', to: '#/harness?tab=policy' },
        done: 'Validate passes and the Policy tab lists the path.',
      },
      {
        id: 'section3-workflow',
        title: 'Read the workflow',
        why: 'The process is a file you can read and change, not a prompt.',
        where: { page: 'Harness', part: 'Workflows' },
        how: 'Open the Workflows tab. jira-to-pr is a list of steps: each one names a role, the gates that run after it, where a failure goes back to, and whether a person approves.',
        action: { label: 'Open Workflows', to: '#/harness?tab=workflows' },
        done: 'You can say which steps wait for a person in Interactive mode.',
      },
      {
        id: 'section3-audit',
        title: 'Read the record of a run',
        why: 'What compliance wants: proof of what the agent did.',
        where: { page: 'Runs', part: 'Credits by role, Timeline' },
        how: 'Open a finished run. Under Result, open the tabs Credits by role and Timeline, then read the Log under them. Every step, tool call, gate result and approval is on record, with the credits each role spent.',
        action: { label: 'Open Runs', to: '#/runs' },
        done: 'You can say which role cost the most credits in that run.',
      },
    ],
  },
  {
    id: 'section4a',
    kind: 'block',
    label: 'Section 4a',
    title: 'Human-in-the-loop vs autonomous',
    icon: 'person',
    start: '12:00',
    end: '12:15',
    level: 4,
    story: {
      before: { text: 'The harness gets hard tickets right now. But a person approving every step is slow, and approving none is risky.' },
      added: [
        { name: 'Approval points', why: 'A person approves where a mistake is expensive: a flagged ticket, the plan, the commit.', action: { label: 'Open Workflows', to: '#/harness?tab=workflows' } },
        { name: 'Unattended runs with a cap', why: 'Nobody watches, so every run has a credit cap and stops instead of asking.', action: { label: 'Open Runs', to: '#/runs' } },
      ],
      test: '24 capped runs start before lunch: four variants on three hard tickets, nobody watching.',
      after: 'They run over lunch on their own. Every approval that passed automatically is counted.',
      next: 'Which variant is best? The numbers come after lunch.',
    },
    ideas: [
      { title: 'Four questions decide', text: 'Is it reversible? What is the blast radius? What does a wrong pass cost? Do we have eval evidence?' },
      { title: 'In our workflow', text: 'A flagged ticket, the plan and the commit wait for a person. Implement, tests and review run alone, because the gates check them.' },
      { title: 'Unattended needs caps', text: 'A run that nobody watches has a credit cap, and stops instead of asking.' },
    ],
    steps: [
      {
        id: 'section4a-modes',
        title: 'Compare the two run modes',
        why: 'The same workflow, with and without a person at the approval points.',
        where: { page: 'Runs', part: 'New agent run, Mode' },
        how: 'Open the Runs page and read the two modes. Interactive stops at the steps marked "human" (in jira-to-pr: a flagged ticket, the plan, the commit) and asks on the run page. Autonomous lets them pass and counts them.',
        action: { label: 'Open Runs', to: '#/runs' },
        done: 'You can say which steps of jira-to-pr you would let run without a person, and why.',
      },
      {
        id: 'section4a-predict',
        title: 'Predict the winner',
        why: 'A prediction written down first is what makes the result worth reading.',
        where: { page: 'Your notes' },
        how: 'Four variants run over lunch on three hard tickets: the raw agent, the cheap harness, the full team, and the full team on the small model. Write down which one has the best pass rate for the fewest credits. No console action.',
        done: 'Your prediction is written down.',
      },
    ],
  },
  { id: 'lunch', kind: 'break', label: 'Lunch', start: '12:15', end: '13:00', note: 'The experiments run unattended.' },
  {
    id: 'section4b',
    kind: 'block',
    label: 'Section 4b',
    title: 'Evals and experimentation',
    icon: 'chart',
    start: '13:00',
    end: '13:40',
    level: 4,
    story: {
      before: { text: 'Several harness variants, one good demo each. One pass says little about the next run, or what a pass costs.' },
      added: [
        { name: 'Evals with held-back tests', why: 'The same tickets, every variant, repeated, judged by tests the agents never see.', action: { label: 'Open Evals', to: '#/evals' } },
        { name: 'The leaderboard', why: 'Pass rate, credits per pass and interventions, side by side.', action: { label: 'Open Evals', to: '#/evals' } },
        { name: 'Resume', why: 'An interrupted experiment goes on where it stopped; finished runs are not paid for twice.', action: null },
      ],
      test: 'The lunch experiment: four variants, three hard tickets, two runs each.',
      after: 'In calibration the cheap harness (cheap, every role on the small model) passed about as often as the full one (team), at about an eighth of the cost. Today\'s numbers are on the leaderboard.',
      next: 'Only we can run it.',
    },
    ideas: [
      { title: 'A test checks the code, an eval checks the harness', text: 'The same tickets, several variants, repeated, judged by tests the agents never see.' },
      { title: 'Five things to measure', text: 'Success, quality, time, human interventions, cost. Read the pass rate with its interval, and the credits per pass.' },
      { title: 'The cheapest harness that passes', text: 'On the hard tickets the cheap harness passed about as often as the full one, for five to eight times fewer credits. Spend on structure, not on the biggest model.' },
      { title: 'One run is an anecdote', text: 'A commit without a pass means a gate is missing. A pass without a commit means the harness stopped on budget or escalation.' },
    ],
    steps: [
      {
        id: 'section4b-compare',
        title: 'Compare two runs',
        why: 'The smallest experiment: one change, two runs, side by side.',
        where: { page: 'Evals', part: 'Compare two runs' },
        how: 'Open Evals and pick two finished runs of this console, for example one before and one after a change to the harness. Read them side by side: passed, credits, retries.',
        action: { label: 'Open Evals', to: '#/evals' },
        done: 'You can say what differed between the two runs.',
      },
      {
        id: 'section4b-leaderboard',
        title: 'Read the leaderboard',
        why: 'Many runs per variant: this is the comparison that counts.',
        where: { page: 'Evals', part: 'Leaderboard' },
        how: 'Open Evals: the leaderboard of the experiments run from this checkout, the lunch runs included, fills in as runs finish. Read three columns: the pass rate with its interval, the credits per pass, and the commit rate.',
        action: { label: 'Open Evals', to: '#/evals' },
        done: 'One sentence: which harness would you run every day, and why?',
      },
    ],
  },
  { id: 'break-2', kind: 'break', label: 'Break', start: '13:40', end: '13:50', note: 'The release run starts; at its last step it waits for a person.' },
  {
    id: 'section5',
    kind: 'block',
    label: 'Section 5',
    title: 'Making it a team workflow',
    icon: 'share',
    start: '13:50',
    end: '14:35',
    level: 5,
    story: {
      before: { text: 'It lives on our laptop and starts with a Python command. Nobody else knows what to do when it fails.' },
      added: [
        { name: 'Three ways to start a run', why: 'By hand, by a label or a ticket status, or in CI.', action: null },
        { name: 'An owner', why: 'One named person changes the harness, reads the failures and holds the budget.', action: null },
        { name: 'A usage guide', why: 'Enough for a colleague to run it without asking you.', action: { label: 'Open Guides', to: '#/guides' } },
      ],
      test: 'The handover test: a colleague runs it from the guide alone, on a real ticket. Explained today, not run.',
      after: 'A team can start it, own it and hand it over, without us.',
      next: '',
    },
    ideas: [
      { title: 'Three ways to start a run', text: 'By hand, to try a ticket type. By a label or a ticket status, for a team pilot. In CI, once the numbers hold.' },
      { title: 'Safety travels with the run', text: 'A sanitized ticket, the same gates and budgets, the role policies, a pull request that a person merges, a credit cap. Safety lives in the harness, not in whoever starts the run.' },
      { title: 'Every workflow needs an owner', text: 'One named person, a cap per run and a budget per month, someone who looks when it fails, and a date to decide: stop, extend or roll out.' },
      { title: 'The handover test', text: 'Someone who has never seen the workflow runs it from the guide alone. Every question they have to ask is a gap in the guide.' },
    ],
    steps: [
      {
        id: 'section5-guide',
        title: 'Start a usage guide',
        why: 'A workflow only you can run is not a team workflow yet.',
        where: { page: 'Guides' },
        how: 'Open Guides and press "Start from the template". It saves the parts to fill in as docs/usage-guide.md: what the workflow is for, how to start it, what it costs, and what to do when it fails.',
        action: { label: 'Open Guides', to: '#/guides' },
        done: 'The guide is started and shows on the Guides page.',
      },
      {
        id: 'section5-handover',
        title: 'Apply the handover test',
        why: 'The honest check of a guide is a reader who was not there.',
        where: { page: 'Your notes' },
        how: 'Could a colleague who was not in the room start a run and read its result with the guide alone? Write down the first thing they would get stuck on. No console action.',
        done: 'One gap is written down.',
      },
    ],
  },
  {
    id: 'wrapup',
    kind: 'block',
    label: 'Wrap-up',
    title: 'The Harness Blueprint',
    icon: 'flag',
    start: '14:35',
    end: '15:00',
    level: null,
    story: {
      before: { text: 'This morning: fourteen broken tickets, and a raw agent that said "completed" when it was not.' },
      added: [
        { name: 'All five levels', why: 'L1 task and context, L2 gates and loops, L3 roles and guardrails, L4 approvals and evals, L5 team workflow.', action: { label: 'Open the Blueprint', to: '#/blueprint' } },
      ],
      test: 'The release run on the v1.0 candidate: fifteen gates green (the fourteen plus the diff-size gate of section 2), then a person approves.',
      after: 'The day in numbers: runs, passes, credits, gate catches, retries, approval points.',
      next: '',
    },
    ideas: [
      { title: 'Start small', text: 'One person, one ticket type, the cheapest harness, and an eval set made of your own past tickets.' },
      { title: 'Then a pilot', text: 'One team, a label trigger, two to six weeks, a budget per month. Move on when it beats your measured baseline.' },
      { title: 'Keep the eval set', text: 'Rerun it whenever the harness changes. It is how you know a change helped.' },
    ],
    note: 'Nothing to do in the console: the blank Blueprint is for a task your own team does again and again.',
    steps: [],
  },
];

export const BLOCKS = AGENDA.filter((entry) => entry.kind === 'block');

/** The day's times as the deck's plan has them (workshop/commands.json "agenda": {block id: {start, end}}): the plan is the
 * one source of the clock, so a change there moves the console too. Unknown ids and malformed times are ignored. */
export function setAgendaTimes(agenda) {
  if (!agenda || typeof agenda !== 'object') return;
  const ok = (t) => typeof t === 'string' && /^\d\d:\d\d$/.test(t);
  for (const block of BLOCKS) {
    const t = agenda[block.id];
    if (t && ok(t.start) && ok(t.end)) Object.assign(block, { start: t.start, end: t.end });
  }
}

/** The block with this id, or null (a break is not a block). */
export const blockById = (id) => BLOCKS.find((block) => block.id === id) ?? null;

// --- time --------------------------------------------------------------------------------------------------------------

/** Minutes since midnight of "HH:MM". */
export function toMinutes(hhmm) {
  const [hours, minutes] = String(hhmm).split(':').map(Number);
  return hours * 60 + minutes;
}

/** Whether `now` (a Date, local time) is on the workshop day. */
export const onDay = (now) => now.getFullYear() === DAY.year && now.getMonth() + 1 === DAY.month && now.getDate() === DAY.date;

/** The agenda entry (block or break) going on at `now`, or null outside the day's hours. */
export function slotAt(now) {
  if (!onDay(now)) return null;
  const at = now.getHours() * 60 + now.getMinutes();
  return AGENDA.find((entry) => toMinutes(entry.start) <= at && at < toMinutes(entry.end)) ?? null;
}

/** "10:05–10:50". */
export const timeText = (entry) => `${entry.start}–${entry.end}`;

/** "Section 2 · Verification and iteration loops". */
export const blockName = (block) => `${block.label} · ${block.title}`;

// --- progress ----------------------------------------------------------------------------------------------------------

/** The items of a block: a step on its own, or the steps that share a `pick` (done when one of them is). */
export function items(block) {
  const list = [];
  for (const step of block.steps) {
    const group = step.pick ? list.find((item) => item.pick === step.pick) : null;
    if (group) group.steps.push(step);
    else list.push({ pick: step.pick ?? null, steps: [step] });
  }
  return list;
}

const itemDone = (item, checked) => item.steps.some((step) => checked.has(step.id));

/** `{done, total, complete}` of a block, counted in items. A block without steps is complete. */
export function blockProgress(block, checked = new Set()) {
  const list = items(block);
  const done = list.filter((item) => itemDone(item, checked)).length;
  return { done, total: list.length, complete: done === list.length };
}

/** A block's progress in words: "2 of 3 done", "Done", or "No steps". */
export function progressText({ done, total, complete }) {
  if (total === 0) return 'No steps';
  return complete ? 'Done' : `${done} of ${total} done`;
}

/**
 * The first item of `block` that is not done: `{step, number, total, options}` (`step` is the item's first step,
 * `number` its place counted from 1, `options` how many steps it offers to pick from), or null when all are done.
 */
export function nextStep(block, checked = new Set()) {
  const list = items(block);
  const index = list.findIndex((item) => !itemDone(item, checked));
  if (index < 0) return null;
  return { step: list[index].steps[0], number: index + 1, total: list.length, options: list[index].steps.length };
}

/**
 * The block to work on now, `{block, why}`: the pinned one (`why` 'pinned'); on the day, the one the clock is in
 * ('clock'), or during a break the one after it ('break'); outside the day's hours, the first block that is not done
 * ('progress'), the last block once all are.
 */
export function currentBlock(now, { checked = new Set(), pinned = null } = {}) {
  const pin = pinned ? blockById(pinned) : null;
  if (pin) return { block: pin, why: 'pinned' };
  const slot = slotAt(now);
  if (slot?.kind === 'block') return { block: slot, why: 'clock' };
  if (slot) return { block: BLOCKS.find((block) => toMinutes(block.start) >= toMinutes(slot.end)) ?? BLOCKS.at(-1), why: 'break' };
  return { block: BLOCKS.find((block) => !blockProgress(block, checked).complete) ?? BLOCKS.at(-1), why: 'progress' };
}

// --- the unit's names ------------------------------------------------------------------------------------------------

/** `text` with `{u}` as the unit's folder name (u04 for unit-04) and `{U}` as its variant (U04); "u<n>" without a unit. */
export function fill(text, unitId) {
  const digits = /^unit-(\d+)$/.exec(String(unitId ?? ''))?.[1];
  return String(text)
    .replaceAll('{u}', digits ? `u${digits}` : 'u<n>')
    .replaceAll('{U}', digits ? `U${digits}` : 'U<n>');
}

// --- progress in the browser -------------------------------------------------------------------------------------------

/** `{checked, hidden, pinned}`: steps ticked, blocks whose step strip is hidden, the pinned block (null: the clock). */
export const emptyProgress = () => ({ checked: new Set(), hidden: new Set(), pinned: null });

/** The localStorage key of a unit's progress. */
export const progressKey = (unitId) => `harness-console.guide.${unitId}`;

const texts = (value) => new Set(Array.isArray(value) ? value.filter((one) => typeof one === 'string') : []);

/** Progress from its stored text; anything that is not what `writeProgress` writes counts as nothing. */
export function parseProgress(text) {
  let data;
  try {
    data = JSON.parse(text);
  } catch {
    return emptyProgress();
  }
  if (!data || typeof data !== 'object' || Array.isArray(data)) return emptyProgress();
  return { checked: texts(data.checked), hidden: texts(data.hidden), pinned: typeof data.pinned === 'string' ? data.pinned : null };
}

// `storage` is for the tests; the page passes nothing and gets localStorage, which can throw on mere access.
function storageOf(storage) {
  try {
    return storage === undefined ? (globalThis.localStorage ?? null) : storage;
  } catch {
    return null;
  }
}

/** A unit's progress; empty when none is kept or the browser refuses storage. */
export function readProgress(unitId, storage) {
  try {
    const text = storageOf(storage)?.getItem(progressKey(unitId));
    return typeof text === 'string' ? parseProgress(text) : emptyProgress();
  } catch {
    return emptyProgress();
  }
}

/** Keep a unit's progress; false when the browser refuses (the page goes on, it only forgets). */
export function writeProgress(unitId, progress, storage) {
  try {
    const store = storageOf(storage);
    if (!store) return false;
    store.setItem(progressKey(unitId), JSON.stringify({ checked: [...progress.checked], hidden: [...progress.hidden], pinned: progress.pinned ?? null }));
    return true;
  } catch {
    return false;
  }
}
