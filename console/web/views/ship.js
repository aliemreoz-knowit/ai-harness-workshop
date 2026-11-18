// Ship view (`#/ship`, block 5): a unit pushes a finished run's branch to the facilitator's GitHub repo, opens a draft PR,
// posts the gate ladder as a check run, reports back to the Jira ticket, and sees its PRs with their checks and Actions runs.
//
// Every write needs the facilitator's switch "GitHub writes" (on for the day): while it is off the page says so plainly
// at the top and the ship buttons are off. The admin turns the switch on here and picks a unit to see (and ship) its runs.
// A run can be shipped when it is done and left a branch; the push is `units/<NN>/<branch>` on the repo. Everything the
// server sends is shown with textContent; a link is only ever one to https://github.com/. The PR list is read when the
// view opens, after an action and on "Refresh" (each read costs several GitHub calls, so nothing polls, and the server
// keeps a unit's list for a minute). The CLI drawer shows the command the server says it ran.

import { get, post } from '../api.js';

const GITHUB = 'https://github.com/';
const KEY = /[A-Z][A-Z0-9]+-[0-9]+/; // a Jira key in a ticket path
const WHOLE_KEY = /^[A-Z][A-Z0-9]+-[0-9]+$/;
const SHIP_TIMEOUT_MS = 300000; // a push waits for the ones before it (one at a time, each up to 2 min): not the usual 30 s
const STATUS_TIMEOUT_MS = 120000; // the PR list is several GitHub calls
const OFF_TITLE = 'GitHub writes are off: the facilitator turns them on.';

let ui = null; // the mounted view, null when none

/** An element: `attrs` are class, text and plain attributes (false and null leave one out). */
function h(tag, attrs = {}, ...children) {
  const node = document.createElement(tag);
  for (const [name, value] of Object.entries(attrs)) {
    if (name === 'class') node.className = value;
    else if (name === 'text') node.textContent = value;
    else if (value === true) node.setAttribute(name, '');
    else if (value !== false && value != null) node.setAttribute(name, value);
  }
  node.append(...children.flat().filter((child) => child != null && child !== false));
  return node;
}

/** A link to GitHub (opened in a new tab), or just the text when `url` is not one of GitHub's. */
function githubLink(url, text) {
  if (typeof url !== 'string' || !url.startsWith(GITHUB)) return h('span', { text });
  return h('a', { href: url, target: '_blank', rel: 'noopener noreferrer', text });
}

function loadStyles() {
  if (document.getElementById('ship-css')) return;
  document.head.append(h('link', { id: 'ship-css', rel: 'stylesheet', href: '/web/views/ship.css' }));
}

const nn = (unitId) => unitId.split('-').pop();
const remoteBranch = (run) => `units/${nn(run.unit)}/${run.branch}`;
const ticketKey = (run) => (typeof run.ticket === 'string' ? (run.ticket.match(KEY)?.[0] ?? '') : '');
const credits = (n) => (Number.isInteger(n) ? String(n) : Number(n).toFixed(2));

// --- mount and unmount -------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  loadStyles();
  const admin = ctx.me.subject === 'admin';
  const view = (ui = {
    ctx,
    admin,
    unitId: ctx.me.unit?.id ?? null, // the unit on show (the admin picks one)
    writes: false,
    known: admin, // whether `writes` is what the server said (a unit learns it with the first status)
    runs: [],
    pulls: null,
    problem: null,
    loaded: false,
    results: new Map(), // run id -> what the last action on it said
    refs: {},
  });
  const refs = view.refs;
  refs.switch = h('section', { class: 'section', 'aria-labelledby': 'ship-switch-title', hidden: !admin });
  refs.off = h('div', { class: 'notice ship-off', role: 'status', hidden: true });
  refs.runs = h('section', { class: 'section', 'aria-labelledby': 'ship-runs-title' });
  refs.pulls = h('section', { class: 'section', 'aria-labelledby': 'ship-pulls-title' });
  el.scrollTop = 0;
  el.replaceChildren(
    h(
      'div',
      { class: 'page ship' },
      h(
        'header',
        { class: 'page-head' },
        h('h1', { text: 'Ship' }),
        h('p', { class: 'page-sub', text: 'Send a run that ended done to GitHub as a draft PR, post its gate ladder as a check, and report back to its Jira ticket.' }),
      ),
      refs.switch,
      refs.off,
      refs.runs,
      refs.pulls,
    ),
  );
  if (admin) await buildSwitch(view);
  if (ui !== view) return;
  paint(view); // "Loading…" while GitHub is asked
  await refresh(view);
}

export function unmount() {
  const view = ui;
  if (!view) return;
  ui = null; // every call still waiting for the server sees that the view is gone
  view.ctx.cli('');
}

function fail(view, err, what) {
  if (err.status === 401) return; // the login page is on its way
  view.ctx.toast(`${what}: ${err.message}`, 'error');
}

// --- the switch and the unit picker (the admin) --------------------------------------------------------------------------

async function buildSwitch(view) {
  const state = h('span', { class: 'badge' });
  const toggle = h('button', { type: 'button', class: 'btn' });
  const picker = h('select', { id: 'ship-unit' });
  view.refs.switchState = state;
  view.refs.toggle = toggle;
  view.refs.switch.replaceChildren(
    h('div', { class: 'section-head' }, h('h2', { id: 'ship-switch-title', text: 'GitHub writes' }), state),
    h('div', { class: 'box ship-switch' },
      h('p', { class: 'ship-switch-text', text: 'While this is off, no unit can push, open a PR, post a check run or comment on Jira. Once on, it stays on for today and turns itself off at the end of the day.' }),
      h('div', { class: 'actions' }, toggle),
      h('div', { class: 'ship-pick' }, h('label', { for: 'ship-unit', text: 'Show the runs of' }), picker),
    ),
  );
  toggle.addEventListener('click', async () => {
    toggle.disabled = true;
    try {
      const answer = await post('/api/admin/ship', { github_writes: !view.writes });
      if (ui !== view) return;
      view.writes = answer.github_writes;
      view.ctx.toast(view.writes ? 'GitHub writes are on for today.' : 'GitHub writes are off.', 'ok');
    } catch (err) {
      if (ui === view) fail(view, err, 'Could not change the switch');
    }
    toggle.disabled = false;
    if (ui === view) paint(view);
  });
  picker.addEventListener('change', () => {
    view.unitId = picker.value || null;
    view.results.clear();
    view.loaded = false;
    paint(view);
    refresh(view);
  });

  try {
    const [{ units }, switchState] = await Promise.all([get('/api/units'), get('/api/admin/ship')]);
    if (ui !== view) return;
    view.writes = switchState.github_writes;
    const label = (unit) => (unit.name && unit.name !== unit.id ? `${unit.name} (${unit.id})` : unit.id);
    picker.replaceChildren(h('option', { value: '', text: 'Pick a unit' }), ...units.map((unit) => h('option', { value: unit.id, text: label(unit) })));
  } catch (err) {
    if (ui === view) fail(view, err, 'Could not load the units');
  }
}

// --- reading ---------------------------------------------------------------------------------------------------------

async function refresh(view) {
  if (!view.unitId) {
    view.runs = [];
    view.pulls = null;
    view.problem = null;
    paint(view);
    return;
  }
  const asked = view.unitId;
  const params = view.admin ? { unit: asked } : undefined;
  try {
    const [runs, status] = await Promise.all([get('/api/runs', params), get('/api/ship/status', params, { timeoutMs: STATUS_TIMEOUT_MS })]);
    if (ui !== view || view.unitId !== asked) return; // gone, or the admin picked another unit meanwhile
    view.runs = runs.runs.filter((run) => run.status === 'done' && run.branch);
    view.pulls = status.pulls;
    view.problem = status.problem;
    view.loaded = true;
    if (!view.admin) {
      view.writes = status.github_writes;
      view.known = true;
    }
  } catch (err) {
    if (ui === view) fail(view, err, 'Could not load what can be shipped');
  }
  if (ui === view) paint(view);
}

// --- painting --------------------------------------------------------------------------------------------------------

function paint(view) {
  const { refs } = view;
  if (view.admin) {
    refs.switchState.textContent = view.writes ? 'On for today' : 'Off';
    refs.switchState.className = view.writes ? 'badge ok' : 'badge';
    refs.toggle.textContent = view.writes ? 'Turn GitHub writes off' : 'Turn GitHub writes on for today';
    refs.toggle.className = view.writes ? 'btn' : 'btn primary';
  }
  refs.off.hidden = view.admin || view.writes || !view.unitId || !view.known; // the admin has the switch above
  refs.off.replaceChildren(
    h('p', {}, h('strong', { text: 'Nothing can be shipped yet: GitHub writes are off.' })),
    h('p', { text: 'The facilitator turns them on in block 5. Until then no run can be pushed, no PR opened, no check posted and no Jira ticket told. Your runs and your pull requests are listed below.' }),
  );
  paintRuns(view);
  paintPulls(view);
}

function paintRuns(view) {
  const box = view.refs.runs;
  const head = h('div', { class: 'section-head' }, h('h2', { id: 'ship-runs-title', text: 'Runs to ship' }));
  if (!view.unitId) {
    box.replaceChildren(head, h('p', { class: 'empty-line', text: view.admin ? 'Pick a unit above to see its runs.' : 'Log in with a unit code to ship a run.' }));
    return;
  }
  if (!view.loaded) {
    box.replaceChildren(head, h('p', { class: 'empty-line', text: 'Loading the runs…' }));
    return;
  }
  if (!view.runs.length) {
    box.replaceChildren(
      head,
      h('p', { class: 'empty-line' }, 'No run to ship yet. A run that ended done and left a branch shows up here: ', view.admin ? 'none of this unit’s has.' : h('a', { href: '#/runs', text: 'start one on the Runs page' }), view.admin ? null : '.'),
    );
    return;
  }
  box.replaceChildren(
    head,
    h('p', { class: 'section-sub', text: 'In order: push the branch, open the draft PR, post the gate ladder as a check, then tell the Jira ticket.' }),
    h('ul', { class: 'ship-runs' }, view.runs.map((run) => runCard(view, run))),
  );
}

function runCard(view, run) {
  const target = remoteBranch(run);
  const pull = (view.pulls ?? []).find((p) => p.branch === target);
  const facts = [run.workflow, ticketKey(run) || run.ticket, `${credits(run.credits)} credits`, run.kept ? 'kept' : 'not kept'].filter(Boolean);
  const head = h(
    'div',
    { class: 'ship-run-head' },
    h('a', { class: 'mono ship-run-id', href: `#/runs/${encodeURIComponent(run.id)}`, text: run.id }),
    h('span', { class: 'muted', text: facts.join(' · ') }),
    pull ? h('span', { class: 'ship-pr' }, githubLink(pull.url, `PR #${pull.number}`)) : null,
  );
  const branch = h('p', { class: 'ship-branch' }, h('code', { text: run.branch }), h('span', { class: 'muted', text: ' → ' }), h('code', { text: target }));

  const off = !view.writes;
  const button = (text, action, primary = false) => {
    const node = h('button', { type: 'button', class: primary ? 'btn primary' : 'btn', text, disabled: off, title: off ? OFF_TITLE : null });
    node.addEventListener('click', () => act(view, run, action, node));
    return node;
  };
  const key = h('input', { type: 'text', class: 'ship-key mono', value: ticketKey(run), placeholder: 'PROJ-12', 'aria-label': `Jira ticket for ${run.id}`, maxlength: '32', spellcheck: 'false', autocomplete: 'off' });
  const report = h('button', { type: 'button', class: 'btn', text: 'Report to Jira', disabled: off, title: off ? OFF_TITLE : null });
  report.addEventListener('click', () => act(view, run, 'jira', report, { key: key.value.trim() }));

  const said = view.results.get(run.id);
  const result = h('p', { class: `ship-result${said?.ok === false ? ' bad' : ''}`, 'aria-live': 'polite' });
  if (said) result.append(said.node);
  return h(
    'li',
    { class: 'box ship-run' },
    head,
    branch,
    h('ol', { class: 'ship-steps' },
      h('li', {}, button('Push branch', 'push', true)),
      h('li', {}, button('Open draft PR', 'pr')),
      h('li', {}, button('Post ladder check', 'check')),
      h('li', { class: 'ship-jira' }, key, report),
    ),
    result,
  );
}

const STATE_BADGE = { open: 'running', merged: 'ok', closed: '' };
const CHECK_TONE = { success: 'ok', failure: 'bad', error: 'bad', timed_out: 'bad', cancelled: 'bad', pending: 'warn', in_progress: 'warn', queued: 'warn' };

function checkList(items) {
  if (!items?.length) return h('span', { class: 'muted', text: 'none' });
  return h(
    'ul',
    { class: 'ship-checks' },
    items.map((item) => {
      const word = item.conclusion ?? item.status ?? 'unknown';
      const name = item.url ? githubLink(item.url, item.name || 'run') : h('span', { text: item.name || 'check' });
      return h('li', {}, name, ' ', h('span', { class: `ship-word ${CHECK_TONE[word] ?? ''}`.trim(), text: word }));
    }),
  );
}

function paintPulls(view) {
  const box = view.refs.pulls;
  const again = h('button', { type: 'button', class: 'btn small', text: 'Refresh' });
  again.addEventListener('click', () => refresh(view));
  const head = h(
    'div',
    { class: 'section-head' },
    h('h2', { id: 'ship-pulls-title', text: 'Pull requests' }),
    h('span', { class: 'spacer' }),
    view.unitId ? h('span', { class: 'hint ship-once', text: 'GitHub is asked at most once a minute.' }) : null,
    view.unitId ? again : null,
  );
  if (!view.unitId) {
    box.replaceChildren(head, h('p', { class: 'empty-line', text: view.admin ? 'Pick a unit above to see its pull requests.' : 'Log in with a unit code to see your pull requests.' }));
    return;
  }
  if (!view.loaded) {
    box.replaceChildren(head, h('p', { class: 'empty-line', text: 'Asking GitHub…' }));
    return;
  }
  if (view.problem) {
    box.replaceChildren(head, h('div', { class: 'notice' }, h('p', { text: `GitHub could not be asked: ${view.problem}` }), h('p', { text: 'Try Refresh in a minute; tell the facilitator if it stays.' })));
    return;
  }
  if (!view.pulls?.length) {
    box.replaceChildren(head, h('p', { class: 'empty-line' }, 'No pull request from ', h('code', { text: `units/${nn(view.unitId)}/` }), ' yet. Push a run above, then open its draft PR.'));
    return;
  }
  const rows = view.pulls.map((pull) => {
    const word = pull.draft && pull.state === 'open' ? 'draft' : pull.state;
    return h(
      'tr',
      {},
      h('td', {}, githubLink(pull.url, `#${pull.number}`)),
      h('td', { class: 'ship-title', text: pull.title }),
      h('td', {}, h('span', { class: `badge ${word === 'draft' ? '' : STATE_BADGE[pull.state] ?? ''}`.trim(), text: word })),
      h('td', {}, h('code', { text: pull.branch })),
      h('td', {}, checkList(pull.checks)),
      h('td', {}, checkList(pull.actions)),
    );
  });
  box.replaceChildren(
    head,
    h(
      'div',
      { class: 'table-box' },
      h('table', { class: 'table ship-table' }, h('thead', {}, h('tr', {}, ['PR', 'Title', 'State', 'Branch', 'Checks', 'Actions'].map((name) => h('th', { scope: 'col', text: name })))), h('tbody', {}, rows)),
    ),
  );
}

// --- the actions -----------------------------------------------------------------------------------------------------

const ACTIONS = {
  push: {
    what: 'Could not push',
    said: (answer) => h('span', {}, 'Pushed as ', h('code', { text: answer.remote_branch }), '.'),
  },
  pr: {
    what: 'Could not open the PR',
    said: (answer) => h('span', {}, answer.draft ? 'Draft PR ' : 'PR ', githubLink(answer.url, `#${answer.number ?? '?'}`), ' is open.'),
  },
  check: {
    what: 'Could not post the check',
    said: (answer) => h('span', { text: `Posted ${answer.name}: ${answer.conclusion}${answer.fallback ? ' (as a commit status)' : ''}.` }),
  },
  jira: {
    what: 'Could not report to Jira',
    said: (answer) => h('span', { text: `Reported on ${answer.key}.` }),
  },
};

async function act(view, run, action, button, body = undefined) {
  const { what, said } = ACTIONS[action];
  if (action === 'jira' && !WHOLE_KEY.test(body.key)) {
    view.ctx.toast('Type the Jira key of the ticket, for example PROJ-12.', 'error');
    return;
  }
  button.disabled = true;
  try {
    const answer = await post(`/api/ship/${encodeURIComponent(run.id)}/${action}`, body ?? {}, { timeoutMs: SHIP_TIMEOUT_MS });
    if (ui !== view) return;
    view.results.set(run.id, { ok: true, node: said(answer) });
    view.ctx.cli(typeof answer.cli === 'string' ? answer.cli : ''); // what the server ran, as a plain command
  } catch (err) {
    if (ui !== view) return;
    view.results.set(run.id, { ok: false, node: h('span', { text: `${what}: ${err.message}` }) });
    if (err.status === 409 && err.body?.error?.startsWith('GitHub writes are off')) view.writes = false;
    fail(view, err, what);
  }
  if (ui === view) await refresh(view);
}
