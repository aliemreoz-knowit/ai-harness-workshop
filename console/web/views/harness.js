// Harness view: the unit's harness as structure, the free-play home. Roles, skills, hooks, gates, workflows (with the
// points where a person approves), policy, instructions and the loop settings, one tab each, from GET /api/structure.
// `#/harness?tab=<id>` opens that tab (the guide on the Today page links there).
//
// Every card names the file it comes from: "Edit" opens that file in the Code view at the line of the card's key,
// and "Changed" says the file differs from where the unit started. "Validate" runs the save rules over all harness
// files and the harness's own check (POST /api/validate). Everything the server sends is put on the page as text.
//
// A guardrail the unit gave up (`weakened` in the answer: a gate taken out of the ladder, a protected path unprotected, a
// reviewer that is gone ...) is flagged "weakened — on purpose?": in a note above the tabs with all of them, on the tab
// and on the card it is about. The flags ask a question; nothing here stops a save. The look is views/harness.css.

import { get, post } from '../api.js';
import { LADDER, gateLadder, profileUse } from '../harnessmodel.js';
import { lineOf } from '../lines.js';
import { codeLink } from '../route.js';
import { cleanFlags, countsByPlace, flagsAt, WEAK_TITLE, weakSummary } from '../weakmodel.js';

const HARNESS = 'harness/harness.yaml';
const GATES = 'harness/gates.yaml';
const CHECK_PROFILE = 'fast'; // the profile "Check my code" runs (console/runs.py: harness gates --profile fast)

const CLI_TEXT = ['python -m harness workflows', 'copilot --agent implementer', '.github/config-tests/check_config.sh'].join('\n');

const TABS = [
  { id: 'roles', label: 'Roles' },
  { id: 'skills', label: 'Skills' },
  { id: 'hooks', label: 'Hooks' },
  { id: 'gates', label: 'Gates' },
  { id: 'workflows', label: 'Workflows' },
  { id: 'policy', label: 'Policy' },
  { id: 'instructions', label: 'Instructions' },
  { id: 'loop', label: 'Loop' },
];

// One line under the tabs: what the tab shows, and where it comes from.
const INTRO = {
  roles: 'Each agent role: its model, its tools, the files it may write and the shell commands it may run. From harness.yaml › roles.',
  skills: 'Instructions an agent loads when a task needs them: one folder with a SKILL.md in .github/skills.',
  hooks: 'Commands that run before or after an agent uses a tool, from .github/hooks, with the cases they are tested on.',
  gates: 'The checks a change must pass. Each column is a profile: the gates it runs, numbered in the order they run.',
  workflows: 'The steps of each workflow, in order. A marked step waits until a person approves.',
  policy: 'What no agent may do, whatever its role. From harness.yaml › policy.',
  instructions: 'The files the agents read as context before they start.',
  loop: 'How often a failure goes back to the agent before a person is asked. From harness.yaml › loop.',
};

// What each setting of the loop does, in the words of harness.yaml.
const LOOP_FIELDS = [
  ['retry_budget', 'Retries per step', 'Gate-triggered implementer retries before a person is asked.'],
  ['review_rounds', 'Review rounds', 'Reviewer rejections sent back to the implementer.'],
  ['ladder_rounds', 'Ladder rounds', 'Full-ladder failures sent back to the implementer, before the review.'],
  ['baseline', 'Baseline', 'auto: test gates are judged against the untouched base. off: absolute gates.'],
];

// The parts of the policy, each a card: [key, title, what an empty list means].
const POLICY_LISTS = [
  ['shell_deny', 'Denied in the shell', 'Nothing is denied.'],
  ['git_allow', 'Git commands allowed', 'No git command is allowed.'],
  ['url_allow', 'URLs allowed', 'None: no agent may fetch a URL.'],
];

// The policy card each weakened-guardrail rule of the policy is about (weakmodel.js places them on the Policy tab).
const POLICY_RULES = { 'protected-removed': 'protected', 'shell-deny-removed': 'shell_deny', 'forbidden-calls-removed': 'forbidden_calls' };

const PERSON =
  '<svg viewBox="0 0 16 16" width="14" height="14" aria-hidden="true" focusable="false">' +
  '<circle cx="8" cy="5" r="3" fill="currentColor"/><path d="M2 14.5c0-3.4 2.6-5.2 6-5.2s6 1.8 6 5.2z" fill="currentColor"/></svg>';

const TEMPLATE = `
<div class="harness">
  <header class="hx-head">
    <div class="hx-head-text">
      <h1>Harness</h1>
      <p class="page-sub">The rules your agents work under. Edit opens a part in Code; Validate checks the whole harness.</p>
      <p class="hx-meta" data-ref="meta"></p>
    </div>
    <div class="hx-head-actions">
      <button type="button" class="btn quiet" data-ref="refresh">Refresh</button>
      <button type="button" class="btn primary" data-ref="validate">Validate</button>
    </div>
  </header>
  <section class="hx-result" data-ref="result" aria-label="Validation" aria-live="polite" hidden></section>
  <section class="hx-weak" data-ref="weak" aria-label="Weakened guardrails" hidden></section>
  <nav class="tabs hx-tabs" data-ref="tabs" aria-label="Harness sections"></nav>
  <div class="hx-body" data-ref="body"></div>
</div>`;

// --- state that outlives a visit ---------------------------------------------------------------------------------

let lastTab = 'roles';
let ui = null; // the mounted view, null when none

// --- small DOM helpers (text only: nothing from the server is parsed as HTML) -----------------------------------

function h(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

/** Values as one line of code words, "a, b, c"; `none` (a plain sentence) when there are none. */
const words = (list, none) =>
  list.length ? h('ul', 'hx-words', ...list.map((text) => h('li', '', h('code', '', text)))) : h('span', 'hx-none', none);

/** Label and value rows. A value is text or a node; `null` leaves the row out. */
function facts(rows) {
  const list = h('dl', 'hx-facts');
  for (const [label, value] of rows) {
    if (value === null) continue;
    list.append(h('dt', '', label), h('dd', '', value));
  }
  return list;
}

const empty = (text, ...more) => h('p', 'empty hx-empty', text, ...more);

function errorLine(message) {
  const line = h('p', 'hx-error', message);
  line.setAttribute('role', 'alert');
  return line;
}

function personIcon() {
  const icon = h('span', 'hx-person');
  icon.innerHTML = PERSON; // a constant, nothing from the server
  return icon;
}

// --- links into the Code view ------------------------------------------------------------------------------------

/**
 * "Edit" for `path`. A plain click first looks the key up in the file (`lineOf`) so that the Code view opens at that
 * line; without a key, or when the file cannot be read, it opens at the top. The link works as a link either way.
 */
function editLink(view, { label = 'Edit', name, path, key, within }) {
  const link = h('a', 'hx-edit', label);
  link.href = codeLink(path);
  link.title = `Open ${path} in Code`;
  link.setAttribute('aria-label', `${label}: ${name ?? key ?? path}`); // what the card is, for a screen reader
  link.addEventListener('click', async (event) => {
    if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; // new tab, etc.
    event.preventDefault();
    let line = 0;
    if (key) {
      try {
        line = lineOf((await get('/api/files/read', { path })).content ?? '', key, within);
      } catch {
        /* open it at the top */
      }
    }
    if (ui === view) location.hash = codeLink(path, line);
  });
  return link;
}

/** The changed files among `paths` (a path that ends in `/` stands for the files below it). */
function changedIn(view, paths) {
  return (view.changed ?? []).filter((file) => paths.some((path) => (path.endsWith('/') ? file.startsWith(path) : file === path)));
}

function changedBadge(files) {
  const badge = h('span', 'badge', 'Changed');
  badge.title = `Differs from the start: ${files.join(', ')}`;
  return badge;
}

/** The flags of a tab, or of one card of it (see `flagsAt`); none while the structure is not loaded or they could not be worked out. */
const weakAt = (view, place, card) => flagsAt(view.weakened ?? [], place, card);

function weakBadge(flags) {
  const badge = h('span', 'badge weak', WEAK_TITLE);
  badge.title = flags.map((flag) => flag.detail).join('\n');
  return badge;
}

/** What a card says of its flags, one line each (a touch screen has no tooltip); `null` for none. */
const weakList = (flags) => (flags.length ? h('ul', 'hx-weak-list', ...flags.map((flag) => h('li', '', flag.detail))) : null);

/** The note at the top of the Policy tab for a flag of a rule that has no card there (a newer server). */
function tabWeak(flags) {
  return flags.length ? h('div', 'hx-weak-note', h('p', '', weakBadge(flags)), weakList(flags)) : null;
}

/** A card title that is a name from the files (a role, a workflow ...): code, not prose. */
const code = (text) => h('code', '', text);

/** The top line of a card: title, "weakened — on purpose?" and "Changed" when they apply, and the edit link. */
function cardHead(view, title, files, edit, weak = []) {
  const head = h('div', 'hx-card-head', h('h3', 'hx-card-title', title));
  const changed = changedIn(view, files);
  const marks = [weak.length ? weakBadge(weak) : null, changed.length ? changedBadge(changed) : null].filter(Boolean);
  if (marks.length) head.append(h('span', 'hx-marks', ...marks));
  head.append(h('span', 'spacer'));
  if (edit) head.append(editLink(view, edit));
  return head;
}

const card = (...children) => h('article', 'hx-card', ...children);

// --- the tabs ----------------------------------------------------------------------------------------------------

function roles(view, data) {
  if (data.roles.error) return brokenSection(view, data.roles.error, HARNESS);
  if (!data.roles.length) return empty('No roles in harness.yaml. Add one under roles: ', editLink(view, { label: 'Open harness.yaml', path: HARNESS, key: 'roles' }));
  return h('div', 'hx-grid', ...data.roles.map((role) => roleCard(view, role)));
}

// a word on the roles the day names differently: the four of jira-to-pr, and solo, the one agent of the single-agent workflow
const ROLE_NOTE = { solo: 'The one agent of the single-agent workflow (Section 1). The four roles above split its work.' };

function roleCard(view, role) {
  const files = [role.prompt_file, role.agent_file].filter(Boolean); // harness.yaml as a whole: the line at the top says it
  const weak = weakAt(view, 'roles', { role: role.name });
  const model = role.model ? h('code', '', role.model) : h('span', 'hx-none', 'the default model');
  const more = [];
  if (role.prompt_file) more.push(editLink(view, { label: 'Prompt', name: `${role.name} prompt`, path: role.prompt_file }));
  if (role.agent_file) more.push(editLink(view, { label: 'Agent file', name: `${role.name} agent file`, path: role.agent_file }));
  return card(
    cardHead(view, code(role.name), files, { name: `${role.name} in harness.yaml`, path: HARNESS, key: role.name, within: 'roles' }, weak),
    weakList(weak),
    ROLE_NOTE[role.name] ? h('p', 'hx-soft', ROLE_NOTE[role.name]) : null,
    facts([
      ['Model', h('span', '', model, role.escalation_model ? h('span', 'hx-soft', ', then ', h('code', '', role.escalation_model), ' on a retry') : null)],
      ['Tools', words(role.tools, 'No tools.')],
      ['May write', words(role.writes, 'Nothing: read-only.')],
      ['Shell', words(role.shell, 'No shell commands.')],
    ]),
    more.length ? h('p', 'hx-card-foot', 'Also: ', ...more) : null,
  );
}

function skills(view, data) {
  if (!data.skills.length) return empty('No skills yet. A skill is a folder in .github/skills with a SKILL.md file.');
  return h(
    'div',
    'hx-grid',
    ...data.skills.map((skill) =>
      card(
        cardHead(view, code(skill.name), [skill.file.slice(0, skill.file.lastIndexOf('/') + 1)], { name: skill.name, path: skill.file, key: 'name' }),
        skill.error ? errorLine(skill.error) : h('p', 'hx-text', skill.description || 'No description.'),
        h('p', 'hx-path', h('code', '', skill.file)),
      ),
    ),
  );
}

// What a hook's regression case expects, as the card names it (by event); the order the groups are shown in.
const CASE_LABELS = {
  preToolUse: { deny: 'Denied', none: 'Allowed' },
  postToolUse: { flagged: 'Flagged as instructions', context: 'Marked as data', none: 'Left alone' },
};
const CASE_ORDER = ['deny', 'flagged', 'context', 'none'];
const HOOK_CASES = '.github/config-tests/cases.json';

/** The hook's regression cases (cases.json, by event) grouped by what they expect, one column each; null without. */
function hookCases(hook) {
  const cases = Array.isArray(hook.cases) ? hook.cases : [];
  if (!cases.length) return null;
  const groups = new Map();
  for (const one of cases) {
    if (!groups.has(one.expect)) groups.set(one.expect, []);
    groups.get(one.expect).push(one.name);
  }
  const rank = (expect) => (CASE_ORDER.includes(expect) ? CASE_ORDER.indexOf(expect) : CASE_ORDER.length);
  const sorted = [...groups].sort(([a], [b]) => rank(a) - rank(b));
  const label = (expect) => CASE_LABELS[hook.event]?.[expect] ?? (expect === 'none' ? 'No answer' : expect);
  return h(
    'section',
    'hx-cases',
    h('p', 'hx-cases-head', h('strong', '', `Tested on ${cases.length} cases`), ' in ', h('code', '', HOOK_CASES)),
    h(
      'div',
      'hx-cases-cols',
      ...sorted.map(([expect, names]) =>
        h(
          'div',
          `hx-cases-col is-${expect}`,
          h('h4', 'hx-cases-title', label(expect), h('span', 'hx-cases-count', String(names.length))),
          h('ul', 'hx-cases-list', ...names.map((name) => h('li', '', name))),
        ),
      ),
    ),
  );
}

function hooks(view, data) {
  const weak = weakAt(view, 'hooks'); // a hook that is gone has no card of its own: one card says it
  const gone = weak.length ? card(cardHead(view, 'Hooks removed or changed', [], null, weak), weakList(weak)) : null;
  if (!data.hooks.length) return h('div', 'hx-stack', gone, empty('No hooks in .github/hooks.'));
  return h(
    'div',
    'hx-stack', // full width: the cases of a hook are long lists
    gone,
    ...data.hooks.map((hook) => {
      const name = hook.event || hook.file.split('/').pop();
      const edit = { name: `${name} in ${hook.file}`, path: hook.file, key: hook.event || undefined, within: hook.event ? 'hooks' : undefined };
      return card(
        cardHead(view, code(name), [hook.file], edit),
        hook.error
          ? errorLine(hook.error)
          : facts([
              ['Runs on', hook.matcher ? h('span', '', 'tools matching ', h('code', '', hook.matcher)) : 'every tool call'],
              ['Command', h('code', 'hx-command', hook.command)],
              ['Time limit', hook.timeout_s === null || hook.timeout_s === undefined ? null : `${hook.timeout_s} s`],
            ]),
        hook.error ? null : hookCases(hook),
        h('p', 'hx-path', h('code', '', hook.file)),
      );
    }),
  );
}

function gates(view, data) {
  if (data.gates.error) return brokenSection(view, data.gates.error, GATES);
  const { columns, rows } = gateLadder(data.gates);
  if (!rows.length) return empty('No gates in gates.yaml. ', editLink(view, { label: 'Open gates.yaml', path: GATES }));
  const use = profileUse(Array.isArray(data.workflows) ? data.workflows : []);
  const head = h(
    'tr',
    '',
    h('th', '', 'Gate'),
    h('th', 'hx-gate-time', 'Time limit'),
    ...columns.map((name) => {
      const th = h('th', name === LADDER ? 'hx-prof ladder' : 'hx-prof', h('code', '', name));
      if (name === CHECK_PROFILE) th.append(h('span', 'hx-prof-use', 'Check my code'));
      return th;
    }),
    h('th', '', h('span', 'sr-only', 'Edit')),
  );
  const body = rows.map(({ id, gate, cells }) => {
    const what = gate
      ? h('td', 'hx-gate', h('code', 'hx-gate-id', id), h('span', 'hx-gate-name', gate.name), h('span', 'hx-gate-fn', `${gate.kind} · ${gate.fn || 'no function'}${gate.env ? ` · ${Object.entries(gate.env).map(([k, v]) => `${k}=${v}`).join(' ')}` : ''}`))
      : h('td', 'hx-gate', h('code', 'hx-gate-id', id), h('span', 'hx-error', 'not defined in gates.yaml'));
    return h(
      'tr',
      '',
      what,
      h('td', 'hx-gate-time', gate ? `${gate.timeout_s} s` : ''),
      ...columns.map((name) => {
        const at = cells[name];
        const td = h('td', at ? 'hx-cell on' : 'hx-cell', at ? String(at) : '');
        td.title = at ? `${id} runs ${ordinal(at)} in ${name}` : `${name} does not run ${id}`;
        if (!at) td.append(h('span', 'sr-only', 'not run'));
        return td;
      }),
      h('td', 'hx-row-edit', gate ? editLink(view, { name: id, path: GATES, key: id, within: 'gates' }) : null),
    );
  });
  const weak = weakAt(view, 'gates');
  const used = columns.map((name) => {
    const users = [...(name === CHECK_PROFILE ? ['Check my code (Runs, Code)'] : []), ...(use[name] ?? [])];
    return h('li', '', h('code', '', name), h('span', 'hx-soft', users.length ? ` runs in ${users.join(', ')}` : ' is not run by a workflow step'));
  });
  return h(
    'div',
    'hx-stack',
    card(
      cardHead(view, 'Gate ladder', [GATES], { name: 'gates in gates.yaml', path: GATES, key: 'gates' }, weak),
      weakList(weak),
      h('div', 'hx-table-wrap', h('table', 'table hx-ladder', h('thead', '', head), h('tbody', '', ...body))),
    ),
    columns.length
      ? card(
          cardHead(view, 'Where each profile runs', [], { label: 'Edit profiles', name: 'profiles in gates.yaml', path: GATES, key: 'profiles' }),
          h('ul', 'hx-uses', ...used),
        )
      : null,
  );
}

const ordinal = (n) => `${n}${n % 100 >= 11 && n % 100 <= 13 ? 'th' : ['th', 'st', 'nd', 'rd'][n % 10] ?? 'th'}`;

function workflows(view, data) {
  if (!data.workflows.length) return empty('No workflows in harness/workflows.');
  const legend = h(
    'p',
    'hx-legend',
    h('span', 'hx-legend-item', h('span', 'hx-approve', personIcon(), 'A person approves'), ' the run waits here'),
    h('span', 'hx-legend-item', h('span', 'hx-back', '↩ step'), ' goes back to that step'),
  );
  return h('div', 'hx-stack', legend, ...data.workflows.map((workflow) => workflowCard(view, workflow)));
}

function workflowCard(view, workflow) {
  const weak = weakAt(view, 'workflows', { file: workflow.file });
  const ids = new Set(workflow.steps.map((step) => step.id));
  const approvals = workflow.steps.filter((step) => step.approval === 'human').length;
  const count = workflow.error ? null : h('p', 'hx-soft hx-approvals', approvals ? `${approvals} approval point${approvals === 1 ? '' : 's'}` : 'No approval point: it runs to the end on its own');
  const body = workflow.error ? errorLine(workflow.error) : h('ol', 'hx-steps', ...workflow.steps.map((step, index) => stepRow(step, index, ids)));
  return card(
    cardHead(view, code(workflow.name), [workflow.file], { name: workflow.name, path: workflow.file, key: 'steps' }, weak),
    workflow.description ? h('p', 'hx-text', workflow.description) : null,
    count,
    weakList(weak),
    body,
    h('p', 'hx-path', h('code', '', workflow.file)),
  );
}

function stepRow(step, index, ids) {
  const human = step.approval === 'human';
  const what = [step.kind, step.role, step.gates ? `${step.gates} gates` : null].filter(Boolean).join(' · ');
  const limits = [];
  if (step.retry_budget !== undefined) limits.push(`${step.retry_budget} retries`);
  if (step.max_rounds !== undefined) limits.push(`${step.max_rounds} rounds`);
  const flow = [];
  if (step.on_fail !== 'fail') flow.push(loopBack('on fail', step.on_fail, ids));
  if (step.on_reject) flow.push(loopBack('on reject', step.on_reject, ids));
  let approval = null;
  if (human) approval = h('span', 'hx-approve', personIcon(), 'A person approves');
  else if (step.approval !== 'auto') approval = h('span', 'hx-soft', `approval: ${step.approval}`);
  return h(
    'li',
    human ? 'hx-step human' : 'hx-step',
    h('span', 'hx-step-n', String(index + 1)),
    h('span', 'hx-step-main', h('code', 'hx-step-id', step.id), h('span', 'hx-step-kind', what + (limits.length ? ` · ${limits.join(', ')}` : ''))),
    flow.length ? h('span', 'hx-step-flow', ...flow) : null,
    approval ? h('span', 'hx-step-approval', approval) : null,
  );
}

/** "on fail ↩ implement" for a step to go back to, "on fail: escalate" for anything else. */
function loopBack(label, target, ids) {
  return ids.has(target) ? h('span', 'hx-back', `${label} ↩ ${target}`) : h('span', 'hx-soft', `${label}: ${target}`);
}

function policy(view, data) {
  if (data.policy.error) return brokenSection(view, data.policy.error, HARNESS);
  const p = data.policy;
  const edit = (key, name) => ({ name, path: HARNESS, key, within: 'policy' });
  const calls = Object.entries(p.forbidden_calls);
  const flags = weakAt(view, 'policy');
  const flagsOf = (key) => flags.filter((flag) => POLICY_RULES[flag.rule] === key);
  const head = (title, key, name) => cardHead(view, title, [], edit(key, name), flagsOf(key));
  const cards = [
    card(
      head('Protected files', 'protected', 'protected files'),
      weakList(flagsOf('protected')),
      h('p', 'hx-soft', 'No agent may change these.'),
      words(p.protected, 'Nothing is protected.'),
      p.protected_except.length ? h('p', 'hx-except', 'Except: ', words(p.protected_except, '')) : null,
    ),
    card(
      head('Forbidden calls', 'forbidden_calls', 'forbidden calls'),
      weakList(flagsOf('forbidden_calls')),
      h('p', 'hx-soft', 'Functions no change may call, by file.'),
      calls.length ? facts(calls.map(([glob, list]) => [h('code', '', glob), words(list, 'none')])) : h('span', 'hx-none', 'No call is forbidden.'),
    ),
    ...POLICY_LISTS.map(([key, title, none]) => card(head(title, key, title.toLowerCase()), weakList(flagsOf(key)), words(p[key], none))),
    card(
      cardHead(view, 'MCP servers', [], edit('mcp_read_only', 'MCP servers')),
      h('p', 'hx-text', p.mcp_read_only ? 'Read-only: an agent may read through them, not write.' : 'Not read-only: an agent may write through them.'),
    ),
  ];
  const other = flags.filter((flag) => !Object.hasOwn(POLICY_RULES, flag.rule)); // a rule this page has no card for
  return h('div', 'hx-stack', tabWeak(other), h('div', 'hx-grid', ...cards));
}

// What an instruction file is, by its place.
function instructionKind(path) {
  if (path === 'AGENTS.md') return 'Read by every agent, in every session.';
  if (path === '.github/copilot-instructions.md') return 'Copilot’s instructions for the whole repository.';
  return 'Path-scoped: applies to the files its applyTo names.';
}

function instructions(view, data) {
  if (!data.instructions.length) return empty('No instruction files (AGENTS.md, .github/copilot-instructions.md, .github/instructions).');
  return h(
    'ul',
    'hx-rows',
    ...data.instructions.map((path) => {
      const row = h('li', 'hx-row', h('span', 'hx-row-text', h('code', 'hx-row-path', path), h('span', 'hx-soft', instructionKind(path))));
      if (changedIn(view, [path]).length) row.append(changedBadge([path]));
      row.append(editLink(view, { path }));
      return row;
    }),
  );
}

function loop(view, data) {
  if (data.loop.error) return brokenSection(view, data.loop.error, HARNESS);
  const weak = weakAt(view, 'loop');
  return h(
    'div',
    'hx-stack hx-narrow',
    card(
      cardHead(view, 'Loop settings', [], { name: 'loop in harness.yaml', path: HARNESS, key: 'loop' }, weak),
      weakList(weak),
      h(
        'ul',
        'hx-settings',
        ...LOOP_FIELDS.map(([key, label, text]) =>
          h(
            'li',
            'hx-setting',
            h('strong', 'hx-setting-value', String(data.loop[key])),
            h('span', 'hx-setting-text', h('span', 'hx-setting-label', label, ' ', h('code', '', key)), h('span', 'hx-soft', text)),
            editLink(view, { name: key, path: HARNESS, key, within: 'loop' }),
          ),
        ),
      ),
    ),
  );
}

/** A section whose file does not parse: the message, and the way to the file. */
function brokenSection(view, message, path) {
  return card(cardHead(view, code(path), [path], { path }), errorLine(message), h('p', 'hx-soft', 'Open the file, fix it and save: the tab shows again.'));
}

const RENDER = { roles, skills, hooks, gates, workflows, policy, instructions, loop };

function countOf(data, id) {
  const part = data[id];
  if (part?.error) return '!';
  if (Array.isArray(part)) return part.length;
  return id === 'gates' ? Object.keys(part.gates).length : null;
}

// --- the style sheet ---------------------------------------------------------------------------------------------

/** Adds views/harness.css once; resolves when it has loaded (or failed), so the page does not show unstyled. */
function loadStyles() {
  let link = document.getElementById('harness-css');
  if (!link) {
    link = Object.assign(document.createElement('link'), { id: 'harness-css', rel: 'stylesheet', href: '/web/views/harness.css' });
    link.ready = new Promise((done) => {
      link.addEventListener('load', done, { once: true });
      link.addEventListener('error', done, { once: true });
    });
    document.head.append(link);
  }
  return link.ready ?? Promise.resolve();
}

// --- mount, load, paint ------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  if (!ctx.me.unit) {
    el.innerHTML = '<p class="empty">Log in with a unit code to see the harness.</p>';
    return;
  }
  const mine = (ui = { ctx, refs: null, data: null, changed: null, hash: '', weakened: [], tab: lastTab, validating: false });
  await loadStyles();
  if (ui !== mine) return;
  el.innerHTML = TEMPLATE;
  const refs = (mine.refs = Object.fromEntries([...el.querySelectorAll('[data-ref]')].map((node) => [node.dataset.ref, node])));
  const wanted = ctx.route.params?.tab; // `#/harness?tab=policy`: the guide's links open a tab
  if (TABS.some((tab) => tab.id === wanted)) mine.tab = lastTab = wanted;

  for (const tab of TABS) {
    const button = h('button', 'tab', tab.label);
    button.type = 'button';
    button.dataset.tab = tab.id;
    if (tab.id === mine.tab) button.setAttribute('aria-current', 'true');
    button.addEventListener('click', () => {
      mine.tab = lastTab = tab.id;
      paint(mine);
    });
    refs.tabs.append(button);
  }
  refs.refresh.addEventListener('click', () => load(mine));
  refs.validate.addEventListener('click', () => validate(mine));
  ctx.cli(CLI_TEXT);
  await load(mine);
  if (ui === mine) refs.tabs.querySelector('[aria-current="true"]')?.scrollIntoView({ block: 'nearest', inline: 'nearest' });
}

export function unmount() {
  ui = null; // calls still on their way see that the view is gone
}

async function load(view) {
  const { body, refresh } = view.refs;
  if (!view.data) body.replaceChildren(empty('Loading the harness…'));
  refresh.disabled = true;
  let reply;
  try {
    reply = await get('/api/structure');
  } catch (err) {
    if (ui !== view || err.status === 401) return; // the login page is on its way
    refresh.disabled = false;
    view.data = null;
    body.replaceChildren(errorLine(`Could not load the harness: ${err.message}. Press Refresh to try again.`));
    return;
  }
  if (ui !== view) return;
  refresh.disabled = false;
  view.data = reply.structure;
  view.changed = reply.changed;
  view.hash = reply.hash;
  view.weakened = reply.weakened === null ? null : cleanFlags(reply.weakened); // null: the server could not work them out
  paint(view);
}

function paint(view) {
  const { refs, data } = view;
  const weakCounts = countsByPlace(view.weakened ?? []);
  for (const button of refs.tabs.children) {
    const id = button.dataset.tab;
    const count = data ? countOf(data, id) : null;
    const weak = weakCounts[id];
    const parts = [TABS.find((tab) => tab.id === id).label];
    if (count !== null) parts.push(h('span', 'hx-count', String(count)));
    if (weak) {
      const dot = h('span', 'hx-tab-weak', h('span', 'sr-only', `, ${weak} ${WEAK_TITLE}`));
      dot.title = `${weak} ${WEAK_TITLE}`;
      parts.push(dot);
    }
    button.replaceChildren(...parts);
    if (id === view.tab) button.setAttribute('aria-current', 'true');
    else button.removeAttribute('aria-current');
  }
  const meta = [];
  if (view.hash) meta.push(h('span', '', 'Config ', h('code', '', view.hash.slice(0, 8))));
  if (data && view.changed === null) meta.push('Changes could not be listed');
  else if (data) meta.push(changedLine(view.changed));
  if (data && view.weakened === null) meta.push('Weakened guardrails could not be checked');
  refs.meta.replaceChildren(...meta.flatMap((part, i) => (i ? [h('span', 'hx-sep', ' · '), part] : [part])));
  paintWeak(view);
  if (data) refs.body.replaceChildren(h('p', 'hx-intro', INTRO[view.tab]), RENDER[view.tab](view, data));
}

/** "Changed since the start: harness.yaml, gates.yaml and 2 more", or "Unchanged since the start". */
function changedLine(files) {
  if (!files.length) return 'Unchanged since the start';
  const names = files.slice(0, 3).map((file) => file.split('/').pop());
  const line = h('span', '', `Changed since the start: ${names.join(', ')}${files.length > 3 ? ` and ${files.length - 3} more` : ''}`);
  line.title = files.join('\n');
  return line;
}

/** The note above the tabs: every flag, with the file to look at. */
function paintWeak(view) {
  const flags = view.weakened ?? [];
  const box = view.refs.weak;
  box.hidden = flags.length === 0;
  box.replaceChildren(
    ...(flags.length
      ? [
          h('p', 'hx-weak-head', h('strong', '', weakSummary(flags)), ' These settings are weaker than the harness you started with. That can be right for your plan: check that each one is on purpose.'),
          h('ul', 'hx-weak-list', ...flags.map((flag) => h('li', '', flag.detail, ' ', editLink(view, { label: 'Open', path: flag.file, name: flag.detail })))),
        ]
      : []),
  );
}

// --- validate ----------------------------------------------------------------------------------------------------------

async function validate(view) {
  if (view.validating) return;
  const { validate: button } = view.refs;
  view.validating = true;
  button.disabled = true;
  button.textContent = 'Validating…';
  openResult(view, 'running', 'Checking', 'The save rules and the harness’s own check run now. This can take up to half a minute.');
  try {
    showResult(view, await post('/api/validate', {}, { timeoutMs: 60000 })); // the harness's own check runs: may take a while
  } catch (err) {
    if (ui !== view || err.status === 401) return;
    if (err.status === 409) {
      openResult(view, 'warn', 'Not checked', err.message === 'unit is running' ? 'A run is using your files. Validate again when it has finished.' : 'A validation is already running. Wait for it, then validate again.');
    } else {
      openResult(view, 'fail', 'Not checked', `Could not validate: ${err.message}. Try again in a moment.`);
    }
  } finally {
    view.validating = false;
    if (ui === view) {
      button.disabled = false;
      button.textContent = 'Validate';
    }
  }
}

/** The result box: a state badge with its word, a headline, and what follows. */
function openResult(view, tone, word, headline, ...parts) {
  if (ui !== view) return;
  const box = view.refs.result;
  const close = h('button', 'btn small quiet', 'Close');
  close.type = 'button';
  close.addEventListener('click', () => {
    box.hidden = true;
    box.replaceChildren();
    view.refs.validate.focus();
  });
  box.className = `hx-result ${tone}`;
  box.replaceChildren(
    h('div', 'hx-result-head', h('span', `badge ${tone}`, word), h('strong', 'hx-result-line', headline), h('span', 'spacer'), tone === 'running' ? null : close),
    ...parts,
  );
  box.hidden = false;
}

function showResult(view, { errors, harness }) {
  const ok = errors.length === 0 && harness.ok;
  const state = harness.ok ? 'passed (exit status 0)' : harness.exit === null ? 'did not finish' : `failed (exit status ${harness.exit})`;
  const problems = errors.length + (harness.ok ? 0 : 1);
  const time = new Date().toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  const output = harness.output ? h('details', 'hx-output', h('summary', '', 'Output of the harness check'), h('pre', 'codeblock', harness.output)) : null;
  if (output && !harness.ok) output.open = true;
  openResult(
    view,
    ok ? 'pass' : 'fail',
    ok ? 'pass' : 'fail',
    ok ? `Your harness is valid (checked at ${time}).` : `${problems} problem${problems === 1 ? '' : 's'} to fix (checked at ${time}).`,
    h(
      'ul',
      'hx-checks',
      h(
        'li',
        errors.length ? 'fail' : 'pass',
        h('strong', '', 'Save rules: '),
        errors.length ? `${errors.length} problem${errors.length === 1 ? '' : 's'}` : 'every harness file passes.',
        errors.length ? h('ul', 'hx-problems', ...errors.map((message) => h('li', '', message))) : null,
      ),
      h('li', harness.ok ? 'pass' : 'fail', h('strong', '', 'Harness check: '), h('code', '', 'python -m harness workflows'), ` ${state}.`),
    ),
    output,
  );
}
