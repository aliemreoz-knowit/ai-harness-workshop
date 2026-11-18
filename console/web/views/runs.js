// Runs view. `#/runs` starts runs and lists them: "Check my code" (the fast gates: no agent, free), the New run form
// (workflow, ticket, harness, mode, and what a run may cost) and the unit's runs (status, what started it, when,
// credits). `#/runs/<id>` is one run: its header (status, workflow and ticket, credits, time, Stop), the steps of its
// workflow, the approval card, the loop warning, the live log, the gate matrix and, once there is something to show,
// the result (Keep and Discard, then the tabs plan, review, diff, credits by role and timeline). Once the run is over
// its result and gates come before the log.
// `#/runs?ticket=tickets/PROJ-5.md` opens the list with that ticket picked (the Tickets view links there);
// `#/runs?overlay_from=unit-04` with that unit's harness picked (the Guides view links there): a handover run takes
// that unit's agents, skills, instructions, hooks, workflows and prompts for this run only.
// Personal mode (`me.personal`): the form also has "Model": Your Copilot or Fake model (no credits, `fake: true`), the
// default following GET /api/setup until the person picks one; the ticket list has the agent-ready files only
// (`tickets/*.agent.md`). A fake run is labelled "Fake model" in the list and on its page and costs nothing. A run whose
// gates ran without Docker shows the harness's "lite ladder: ... not run here" line under its gates.
//
// The step strip is the run's workflow as the unit's harness has it now (GET /api/structure), with each step's state from
// the run's progress; when the run does not fit it (the workflow changed since, a handover run, another unit's run) it
// is the steps the run went through. The "Timeline" tab draws the run by role (flow.js) from the log lines the page has
// received: a lane per role, a bar per step, marks for gates, approvals and retries, and the credits. A run that started
// with a weakened harness (`weakened` on its record) says so under the header, in yellow: "weakened - on purpose?" and
// what was weakened.
//
// A run page opens only its own run stream (the app shell owns the one global stream) and closes it when the person
// leaves (and once the tab has been hidden for 30 s: api.js). The stream starts with the run's record as it is now, so
// a page that was away (a reload, a lost connection, a hidden tab) shows the real state again, and replays the log from
// the id the browser had: a repeat of a line is never drawn. "Reconnecting…" shows while the stream has been away a few
// seconds. Stop and Discard ask first, in a dialog of the page; a discarded run that has a commit can still be kept
// ("Keep anyway"). A call that got no answer may still have been done: the page reads the run (or the list) again.
// Every string that comes from the server is put on the page as text. The look is views/runs.css.

import { get, post, stream } from '../api.js';
import { runCommand } from '../cli.js';
import { parseDiff } from '../diff.js';
import { costChart, journey, keepLine, legend, loadStyles, sparkline, timeline } from '../flow.js';
import { createLog } from '../log.js';
import { drawFlow, flowModel } from '../runflow.js';
import { diffView, languageFor, loadMonaco } from '../monaco.js';
import {
  MIN_CREDITS,
  answerGiven,
  approvalChoices,
  askedAt,
  capQuestion,
  costLine,
  creditsText,
  formatCredits,
  gateCell,
  gateMatrix,
  gateVerdict,
  isFinal,
  isOpen,
  modelDefault,
  modelLabel,
  openRunState,
  overlayLabel,
  runCost,
  runDuration,
  runOrigin,
  runTitle,
  skippedNote,
  statusChip,
  ticketChoices,
  workflowStrip,
} from '../runmodel.js';
import { cleanFlags, WEAK_TITLE } from '../weakmodel.js';

const FALLBACK_WORKFLOWS = ['jira-to-pr', 'single-agent', 'review-only', 'test-only']; // when the harness structure cannot be read
const REFRESH_ON = new Set(['step', 'gate', 'cost', 'review', 'retry', 'approval', 'escalation', 'end']); // lines that change the strip, matrix or credits
const CAPPED = "credits capped: the run's files could not be trusted";
const GATES_HINT = 'Builds and tests your saved code with the fast gates of gates.yaml. No agent, no credits: run it as often as you like.';
const SHEET = '/web/views/runs.css';

const MODES = [
  ['interactive', 'Interactive', 'Stops at the steps marked “human” and asks you: you approve or reject them on the run page.'],
  ['autonomous', 'Autonomous', 'Does not ask you: the steps marked “human” go through on their own.'],
];

// Personal mode: whose model a run uses. The default follows GET /api/setup (modelDefault) until the person picks one.
const MODELS = [
  ['copilot', 'Your Copilot', 'Your own Copilot login does the work. The run counts against today’s credits.'],
  ['fake', 'Fake model (no credits)', 'No model: a scripted stand-in plays the agents and changes no code, so a run stops at the first step that must change it (implement, in jira-to-pr). You see your harness’s steps, gates and approvals.'],
];
const FAKE_PRICE = 'Free: the fake model uses no model and no credits.';
const SETUP_POLL_MS = 3000; // how often the page asks again while the doctor checks the setup

const RESULT_TABS = [
  ['plan', 'Plan'],
  ['review', 'Review'],
  ['diff', 'Diff'],
  ['credits', 'Credits by role'],
  ['timeline', 'Timeline'],
];

/** The badge of a status tone (statusChip, gateVerdict): the word is always there, the colour adds to it. */
const BADGE = { ok: 'pass', bad: 'fail', warn: 'warn', busy: 'running', muted: '' };

// --- state that outlives a visit ---------------------------------------------------------------------------------

let lastTab = null; // the result tab the person last chose; until then a done run opens on its diff, any other on its timeline
const lastChoice = { workflow: '', mode: 'interactive', model: null }; // what the form had when the person last started a run (model: picked by hand)
let monaco = null; // the Monaco namespace once loaded, else null (plain text box)
let ui = null; // the mounted view, null when none
let sheet = null; // views/runs.css once asked for: a promise that settles when it has loaded (or failed)

// --- small DOM helpers (text only: nothing from the server is parsed as HTML) -----------------------------------

function h(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

function button(label, className, onClick) {
  const node = h('button', `btn ${className}`.trim(), label);
  node.type = 'button';
  node.addEventListener('click', onClick);
  return node;
}

const cls = (...names) => names.filter(Boolean).join(' ');
const link = (href, text, className = '') => Object.assign(h('a', className), { href, textContent: text });
const runHref = (id) => `#/runs/${encodeURIComponent(id)}`;
const withId = (node, id) => Object.assign(node, { id });
const statusBadge = (rec) => {
  const { text, tone } = statusChip(rec);
  return h('span', cls('badge', BADGE[tone]), text);
};

function when(ts) {
  const at = new Date(typeof ts === 'string' ? ts : NaN);
  if (Number.isNaN(at.getTime())) return '';
  const time = at.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });
  return at.toDateString() === new Date().toDateString() ? time : `${at.toLocaleDateString([], { month: 'short', day: 'numeric' })} ${time}`;
}

/** Run `fn` once, `ms` after the last call (a burst of events becomes one request). */
function debounced(fn, ms) {
  let timer = null;
  const call = () => {
    clearTimeout(timer);
    timer = setTimeout(fn, ms);
  };
  call.cancel = () => clearTimeout(timer);
  return call;
}

function failed(view, err, what) {
  if (ui !== view || err.status === 401) return; // gone, or the login page is on its way
  view.ctx.toast(`${what}: ${err.message}`, 'error');
}

/** Adds views/runs.css to the page once; resolves when it has loaded, so the page is never drawn without its look. */
function loadSheet() {
  sheet ??= new Promise((resolve) => {
    if (document.getElementById('runs-css')) {
      resolve();
      return;
    }
    const node = Object.assign(document.createElement('link'), { id: 'runs-css', rel: 'stylesheet', href: SHEET });
    node.addEventListener('load', resolve, { once: true });
    node.addEventListener('error', resolve, { once: true }); // unstyled is better than not at all
    setTimeout(resolve, 3000);
    document.head.append(node);
  });
  return sheet;
}

// --- mount -------------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  const view = (ui = { ctx, el, disposers: [], id: ctx.route.args[0] ?? null });
  el.scrollTop = 0;
  await loadSheet();
  if (ui !== view) return;
  if (view.id === null) await mountList(view);
  else await mountRun(view);
}

export function unmount() {
  const view = ui;
  ui = null; // calls still on their way see that the view is gone
  for (const dispose of (view?.disposers ?? []).splice(0).reverse()) dispose();
}

// --- the list and the forms --------------------------------------------------------------------------------------

async function mountList(view) {
  const { ctx, el } = view;
  const admin = ctx.me.subject === 'admin';
  const wanted = Object.hasOwn(ctx.route.params, 'ticket') ? ctx.route.params.ticket : '';
  const wantedHarness = Object.hasOwn(ctx.route.params, 'overlay_from') ? ctx.route.params.overlay_from : '';
  view.runs = [];
  view.rows = [];

  const list = h('ul', 'runs-list');
  list.setAttribute('aria-labelledby', 'runs-list-title');
  const head = h('div', 'runs-head', h('span', '', 'Status'), h('span', '', 'Run'), h('span', '', 'When'), h('span', '', 'Credits'));
  head.setAttribute('aria-hidden', 'true');
  const empty = h('div', 'runs-empty', 'Loading the runs…');
  empty.setAttribute('role', 'status');
  list.hidden = head.hidden = true;
  const start = admin ? null : buildStart(view, wanted, wantedHarness);
  el.replaceChildren(
    h(
      'div',
      'runs-page',
      h(
        'header',
        'page-head',
        h('h1', '', 'Runs'),
        h('p', 'page-sub', admin ? "Every unit's runs, newest first: open one to watch it or to answer for its unit. Runs are started from a unit (log in with its code)." : 'Check your code with the gates for free, or let the agents of your harness work on one ticket and follow every step: the plan, each attempt, the gates, the review, your approvals and what it cost. The commands on Today are the stage demos; experiments started there (many runs at once) are on Evals.'),
      ),
      ...(start?.parts ?? []),
      h('section', 'runs-listing', withId(h('h2', '', admin ? 'All runs' : 'Your runs'), 'runs-list-title'), head, list, empty),
    ),
  );

  const paintList = () => {
    view.rows = [];
    list.replaceChildren(...view.runs.map((rec) => runRow(view, rec, admin)));
    list.hidden = head.hidden = view.runs.length === 0;
    empty.hidden = view.runs.length > 0;
    empty.replaceChildren(
      ...(admin
        ? ['No runs yet. Units start runs from their own Runs page.']
        : [h('strong', '', 'No runs yet.'), ' Start one above, or check your code with the gates for free.']),
    );
    start?.update();
  };
  const refresh = async () => {
    try {
      view.runs = (await get('/api/runs')).runs;
    } catch (err) {
      failed(view, err, 'Could not load the runs');
      if (ui === view && !view.runs.length) empty.textContent = `Could not load the runs: ${err.message}. The page tries again on its own every 20 seconds.`;
      return;
    }
    if (ui === view) paintList();
  };
  const soon = debounced(refresh, 300);
  view.refresh = soon;

  const stopGlobal = ctx.onGlobal(
    ({ data }) => {
      if (admin || data?.unit === ctx.me.unit?.id) soon();
    },
    soon, // the stream came back (a hidden tab, a lost connection): events may have been missed
  );
  const interval = setInterval(() => {
    for (const { rec, clock } of view.rows) if (isOpen(rec.status)) clock.textContent = runDuration(rec, Date.now());
  }, 1000);
  const safety = setInterval(refresh, 20000); // in case the live stream was away
  view.disposers.push(stopGlobal, () => clearInterval(interval), () => clearInterval(safety), soon.cancel);

  await Promise.all([refresh(), start?.load()]);
}

function runRow(view, rec, admin) {
  const row = h('a', 'runs-row');
  row.href = runHref(rec.id);
  const clock = h('span', 'runs-dur', runDuration(rec, Date.now()));
  const origin = [admin ? rec.unit : null, runOrigin(rec)].filter(Boolean).join(' · ');
  row.append(
    h('span', 'runs-status', statusBadge(rec)),
    h(
      'span',
      'runs-what',
      h('span', 'runs-title', runTitle(rec)),
      h('span', 'runs-origin', origin, modelLabel(rec) ? h('span', 'badge', modelLabel(rec)) : null, rec.accounting === 'capped' ? h('span', 'badge warn', 'credits capped') : null),
    ),
    h('span', 'runs-when', h('span', 'runs-time', when(rec.queued_at)), clock),
    h('span', 'runs-credits', runCost(rec)),
  );
  view.rows.push({ rec, clock });
  return h('li', '', row);
}

/** "Check my code", the New run form and the note about the unit's open run: `{parts, load, update}`. */
function buildStart(view, wanted, wantedHarness) {
  const { ctx } = view;
  const personal = Boolean(ctx.me.personal); // the participant's own laptop: their Copilot or the fake model
  const workflow = withId(h('select'), 'run-workflow');
  const ticket = withId(h('select'), 'run-ticket');
  const harness = withId(h('select'), 'run-harness'); // whose harness structure the run takes: '' is the unit's own
  const workflows = new Map(); // name -> the workflow as GET /api/structure has it
  let cap = null; // what one run may spend (the console's run cap), once read
  let busy = false;
  let setup = null; // GET /api/setup (personal mode), once read
  let setupTimer = null;
  view.disposers.push(() => clearTimeout(setupTimer));

  const opened = h('div', 'runs-open');
  opened.setAttribute('role', 'status');
  opened.hidden = true;

  const gates = button('Check my code', 'primary', () => submit({ kind: 'gates' }));
  gates.setAttribute('aria-describedby', 'runs-check-text');
  const check = h('section', 'runs-check', gates, withId(h('p', 'runs-check-text', h('strong', '', 'Free. '), GATES_HINT), 'runs-check-text'));
  check.setAttribute('aria-label', 'Check my code');

  const modes = MODES.map(([value, label, text]) => {
    const input = Object.assign(h('input'), { type: 'radio', name: 'run-mode', value, checked: value === lastChoice.mode });
    const said = h('span', 'runs-choice-text', text);
    input.addEventListener('change', update);
    return { value, text, input, said, node: h('label', 'runs-choice', input, h('span', 'runs-choice-body', h('span', 'runs-choice-name', label), said)) };
  });
  const modeValue = () => modes.find((one) => one.input.checked)?.value ?? 'interactive';
  const models = (personal ? MODELS : []).map(([value, label, text]) => {
    const input = Object.assign(h('input'), { type: 'radio', name: 'run-model', value, checked: value === (lastChoice.model ?? 'fake') });
    input.addEventListener('change', () => {
      lastChoice.model = value; // picked by hand: the setup no longer chooses
      update();
    });
    return { value, input, node: h('label', 'runs-choice', input, h('span', 'runs-choice-body', h('span', 'runs-choice-name', label), h('span', 'runs-choice-text', text))) };
  });
  const fakeChosen = () => personal && (models.find((one) => one.input.checked)?.value ?? 'fake') === 'fake';
  const modelNote = h('p', 'runs-model-note');
  const about = h('p', 'runs-about');
  const handover = h('p', 'runs-handover');
  const price = h('p', 'runs-price-line');
  const note = h('p', 'runs-note');
  note.setAttribute('role', 'status');
  const start = button('Start run', '', () => // secondary: Check my code is the page's one accent (and the free one)
    submit({
      kind: 'workflow',
      workflow: workflow.value,
      ticket: ticket.value,
      mode: modeValue(),
      ...(harness.value ? { overlay_from: harness.value } : {}),
      ...(fakeChosen() ? { fake: true } : {}),
    }),
  );
  const field = (control, label) => h('div', 'runs-field', Object.assign(h('label', 'runs-label', label), { htmlFor: control.id }), control);
  const form = h(
    'section',
    'runs-new',
    withId(h('h2', '', 'New agent run'), 'runs-new-title'),
    h('div', 'runs-fields', field(workflow, 'Workflow'), field(ticket, 'Ticket'), field(harness, 'Harness')),
    about,
    handover,
    h('fieldset', 'runs-mode', h('legend', '', 'Mode'), h('div', 'runs-choices', ...modes.map((one) => one.node))),
    personal ? h('fieldset', 'runs-mode', h('legend', '', 'Model'), h('div', 'runs-choices', ...models.map((one) => one.node)), modelNote) : null,
    h('div', 'runs-price', h('span', 'runs-price-label', 'Cost'), price),
    h('div', 'runs-start', start, note),
  );
  form.setAttribute('aria-labelledby', 'runs-new-title');

  const options = (select, values, chosen, labelFor = (value) => value) => {
    select.replaceChildren(...values.map((value) => Object.assign(h('option'), { value, textContent: labelFor(value) })));
    if (chosen && values.includes(chosen)) select.value = chosen;
  };

  function update() {
    const open = view.runs.find((rec) => isOpen(rec.status));
    const unit = ctx.me.unit;
    const fake = fakeChosen();
    const enough = fake || !(unit?.left < MIN_CREDITS); // read now: the credits change while the page is open (the fake model needs none)
    const noTicket = !ticket.value;

    opened.hidden = !open;
    opened.className = cls('runs-open', open?.status === 'waiting' && 'waiting');
    opened.replaceChildren(
      ...(open
        ? [h('span', 'runs-open-text', 'Your run ', link(runHref(open.id), runTitle(open)), ` is ${openRunState(open)}. One run at a time.`), link(runHref(open.id), 'Open the run', 'btn small')]
        : []),
    );

    const reasons = [];
    if (open) reasons.push(h('span', '', 'Your run must end first (see above), or stop it on its page.'));
    else if (!enough) reasons.push(h('span', '', `Not enough credits left for an agent run (it needs ${MIN_CREDITS}).${personal ? ' The fake model needs none.' : ''} Check my code stays free.`));
    else if (noTicket && personal) reasons.push(h('span', '', 'Write the agent-ready version on the ', link('#/tickets', 'Tickets page'), ' first.'));
    else if (noTicket) reasons.push(h('span', '', 'There is no ticket to run yet: write one in the ', link('#/tickets', 'Tickets view'), '.'));
    else if (personal && ticket.value && !ticket.value.endsWith('.agent.md')) reasons.push(h('span', '', 'This is the reporter’s text as it came in, not an agent-ready version. The sanitizer reads it before any model does.'));
    note.replaceChildren(...reasons);
    modelNote.textContent = !personal ? ''
      : setup?.depth ? `Your setup: ${setup.depth}.${setup.state === 'checking' ? ' Checking again…' : ''}`
      : setup && setup.state !== 'checking' ? 'Your setup could not be checked: see Today.'
      : 'Checking your setup…';

    const chosen = harness.value ? null : workflows.get(workflow.value); // a handover run takes the other unit's workflows
    about.textContent = chosen?.description ?? '';
    about.hidden = !about.textContent;
    const asks = chosen ? askedAt(chosen.steps) : null;
    for (const one of modes) {
      let text = one.text;
      if (one.value === 'interactive' && asks) text += asks.length ? ` In ${workflow.value}: ${asks.join(', ')}.` : ` ${workflow.value} has no such step.`;
      one.said.textContent = text;
    }
    handover.textContent = harness.value ? `This run takes the agents, skills, instructions, hooks, workflows and prompts of ${harness.value} as that unit has saved them, for this run only. Your own files are not changed, and the result is not put on it.` : '';
    handover.hidden = !harness.value;
    price.textContent = fake ? FAKE_PRICE : unit ? costLine(unit.left, unit.budget, cap) : '';

    start.disabled = busy || Boolean(open) || !enough || noTicket || !workflow.value;
    gates.disabled = busy || Boolean(open);
    ctx.cli(runCommand({ kind: 'workflow', workflow: workflow.value, ticket: ticket.value, mode: modeValue(), overlay_from: harness.value, fake }));
  }

  /** Personal mode: what GET /api/setup says; the model follows it until the person picks one, and while the doctor
   * still checks the page asks again. */
  async function readSetup() {
    clearTimeout(setupTimer);
    try {
      setup = await get('/api/setup');
    } catch (err) {
      if (ui !== view || err.status === 401) return;
      setup = { state: 'error', depth: '', ladder: '', reason: err.message };
    }
    if (ui !== view) return;
    if (lastChoice.model === null) {
      const chosen = modelDefault(setup);
      for (const one of models) one.input.checked = one.value === chosen;
    }
    update();
    if (setup.state === 'checking') setupTimer = setTimeout(readSetup, SETUP_POLL_MS);
  }

  async function load() {
    const [structure, tickets, units, card] = await Promise.allSettled([
      get('/api/structure'),
      get('/api/files/tree', { path: 'tickets' }),
      get('/api/units'),
      get('/api/eval/card'), // the one place the browser can read the cap of one run
    ]);
    if (ui !== view) return;
    const known = structure.status === 'fulfilled' ? structure.value.structure.workflows.filter((w) => !w.error) : [];
    for (const one of known) workflows.set(one.name, one);
    const names = known.length ? known.map((one) => one.name) : FALLBACK_WORKFLOWS;
    options(workflow, names, lastChoice.workflow || (names.includes('jira-to-pr') ? 'jira-to-pr' : names[0]));

    const found = tickets.status === 'fulfilled' ? tickets.value.entries.filter((entry) => !entry.dir && entry.name.endsWith('.md')).map((entry) => entry.path) : [];
    let files = ticketChoices(found, personal); // personal: the agent-ready versions first, then the reporters' texts
    if (tickets.status === 'rejected' && tickets.reason.status !== 404) failed(view, tickets.reason, 'Could not list your tickets');
    if (wanted && !files.includes(wanted) && ticketChoices([wanted], personal).length) files = [wanted, ...files]; // a link from another view names it even if the list does not
    options(ticket, files, files.includes(wanted) ? wanted : files[0]);

    let others = units.status === 'fulfilled' ? units.value.units.map((unit) => unit.id).filter((id) => id !== ctx.me.unit?.id) : [];
    if (wantedHarness && !others.includes(wantedHarness) && wantedHarness !== ctx.me.unit?.id) others = [wantedHarness, ...others]; // a link names it even if the list does not
    options(harness, ['', ...others], wantedHarness, (id) => (id ? `Harness of ${id} (handover)` : 'Your own harness'));

    const runCap = card.status === 'fulfilled' ? card.value?.options?.run_cap : null;
    cap = Number.isFinite(runCap) && runCap > 0 ? runCap : null;
    update();
    if (personal) await readSetup();
  }

  async function submit(body) {
    if (busy) return;
    busy = true;
    update();
    try {
      const rec = await post('/api/runs', body);
      if (ui !== view) return;
      if (body.kind === 'workflow') Object.assign(lastChoice, { workflow: body.workflow, mode: body.mode });
      if (rec.status === 'refused') {
        ctx.toast(`The run was refused: ${rec.error || 'no reason given'}`, 'error');
        view.runs = [rec, ...view.runs.filter((other) => other.id !== rec.id)];
      } else {
        location.hash = runHref(rec.id);
      }
    } catch (err) {
      failed(view, err, 'Could not start the run');
      if (err.timeout && ui === view) view.refresh?.(); // no answer: the run may have started all the same
    } finally {
      busy = false;
      if (ui === view) update();
    }
  }

  for (const control of [workflow, ticket, harness]) control.addEventListener('change', update);
  return { parts: [opened, check, form], load, update };
}

// --- one run -------------------------------------------------------------------------------------------------------------

/** The run page's elements (fixed structure; every text is set later, as text). */
function buildRun() {
  const r = {};
  const fact = (label) => {
    const value = h('span', 'run-fact-value');
    const node = h('span', 'run-fact', h('span', 'run-fact-label', label), value);
    node.hidden = true;
    return { node, value };
  };
  r.title = h('h1', 'run-title');
  r.id = h('span', 'run-id');
  r.chip = h('span', 'badge run-status');
  r.origin = h('span', 'run-origin');
  r.credits = fact('Credits');
  r.clock = fact('Time');
  r.step = fact('Now at');
  r.down = h('span', 'link-down', 'Reconnecting…');
  r.down.setAttribute('role', 'status');
  r.down.hidden = true;
  r.stop = h('button', 'btn danger', 'Stop run');
  r.stop.type = 'button';
  r.stop.hidden = true;
  r.again = h('button', 'btn primary', 'Check my code again');
  r.again.type = 'button';
  r.again.hidden = true;
  r.badges = h('div', 'run-badges');
  r.error = h('p', 'run-note bad');
  r.error.setAttribute('role', 'alert');
  r.skipped = h('p', 'run-note warn');
  r.weak = h('div', 'run-weak');
  r.weak.setAttribute('role', 'note');
  r.flow = h('div', 'runflow');
  r.flow.hidden = true;
  r.steps = h('ol', 'run-steps');
  r.steps.setAttribute('aria-label', 'Steps of the workflow');
  r.stepsNow = h('p', 'run-steps-now'); // on a phone the strip is its bars, and this line says where the run is
  r.approval = h('div', 'run-approval');
  r.loop = h('div', 'run-loop');
  for (const node of [r.error, r.skipped, r.weak, r.steps, r.stepsNow, r.loop]) node.hidden = true;

  r.logTitle = withId(h('h2', '', 'Live log'), 'run-log-title');
  r.log = h('div', 'log');
  r.log.setAttribute('role', 'log');
  r.log.setAttribute('aria-live', 'off');
  r.log.setAttribute('aria-labelledby', 'run-log-title');
  r.log.tabIndex = 0;
  r.jump = h('button', 'btn small run-jump', 'Jump to latest');
  r.jump.type = 'button';
  r.jump.hidden = true;
  r.logHint = h('span', 'run-section-hint');
  r.logbox = h(
    'section',
    'run-logbox',
    h('div', 'run-section-head', r.logTitle, r.logHint),
    h('div', 'run-logframe', r.log, r.jump),
  );
  r.logbox.setAttribute('aria-labelledby', 'run-log-title');

  r.gates = h('section', 'run-gates');
  r.gates.setAttribute('aria-label', 'Gates');
  r.gates.hidden = true;

  r.decision = h('div', 'run-decision');
  r.tabs = h('div', 'tabs run-tabs');
  r.tabs.setAttribute('role', 'tablist');
  r.tabs.setAttribute('aria-label', 'Result');
  r.body = withId(h('div', 'run-tabbody'), 'run-tabpanel');
  r.body.setAttribute('role', 'tabpanel');
  r.body.tabIndex = 0;
  r.result = h(
    'section',
    'run-result',
    h('div', 'run-section-head', withId(h('h2', '', 'Result'), 'run-result-title')),
    h('div', 'run-resultbox', r.decision, r.tabs, r.body),
  );
  r.result.setAttribute('aria-labelledby', 'run-result-title');
  r.result.hidden = true;

  r.root = h(
    'div',
    'run-page',
    h('div', 'run-top', link('#/runs', '← All runs', 'run-back'), r.id),
    h(
      'header',
      'run-head',
      h('div', 'run-head-main', r.title, h('div', 'run-facts', r.chip, r.origin, r.credits.node, r.clock.node, r.step.node)),
      h('div', 'run-head-side', r.down, r.stop, r.again),
    ),
    r.badges,
    r.error,
    r.skipped,
    r.weak,
    r.flow,
    r.steps,
    r.stepsNow,
    r.approval,
    r.loop,
    r.logbox,
    r.gates,
    r.result,
  );
  return r;
}

async function mountRun(view) {
  const { ctx, el } = view;
  loadStyles();
  const refs = (view.refs = buildRun());
  el.replaceChildren(refs.root);
  Object.assign(view, {
    rec: null,
    workflow: null, // the steps of the run's workflow (GET /api/structure), when the run fits it
    lines: new Map(), // seq -> the lines the timeline reads, in the order they came (flow.js keepLine: the most recent ones)
    lost: 0, // lines the timeline had to let go of
    rev: 0, // changes when `lines` does
    progress: null,
    plan: null,
    review: null,
    diff: null,
    tab: lastTab,
    streamed: false, // the run stream has said where the run is (its first `run` event): the log and timeline are its
    busy: false,
    answered: null, // {id, answer}: the answer given to the question that is still on the record
    asked: null, // the id of the question the card was last scrolled to
    keys: {},
    token: 0,
    stopDiff: () => {},
    loading: false,
    again: false,
  });
  const unitId = () => ctx.me.unit?.id;
  view.canAct = () => ctx.me.subject === 'admin' || view.rec?.unit === unitId();

  view.tabButtons = new Map(
    RESULT_TABS.map(([id, label]) => {
      const tab = button(label, 'tab', () => chooseTab(view, id));
      tab.id = `run-tab-${id}`;
      tab.setAttribute('role', 'tab');
      tab.setAttribute('aria-controls', 'run-tabpanel');
      refs.tabs.append(tab);
      return [id, tab];
    }),
  );
  refs.tabs.addEventListener('keydown', (event) => {
    const move = { ArrowRight: 1, ArrowLeft: -1 }[event.key];
    if (!move) return;
    event.preventDefault();
    const focused = RESULT_TABS.findIndex(([id]) => view.tabButtons.get(id) === document.activeElement);
    const at = focused >= 0 ? focused : RESULT_TABS.findIndex(([id]) => id === view.tab);
    const next = RESULT_TABS[(at + move + RESULT_TABS.length) % RESULT_TABS.length][0];
    chooseTab(view, next);
    view.tabButtons.get(next).focus();
  });

  view.log = createLog(refs.log, { onFollow: (following) => (refs.jump.hidden = following) });
  view.log.setPlaceholder('Loading the run…');
  refs.jump.addEventListener('click', () => view.log.follow());
  refs.stop.addEventListener('click', () => stopRun(view));
  refs.again.addEventListener('click', () => checkAgain(view));
  const clock = setInterval(() => paintClock(view), 1000);
  const live = setInterval(() => {
    if (view.rec && isOpen(view.rec.status) && view.tab === 'timeline') {
      view.rev += 1; // a step that is going grows with the clock, not only when a line comes
      paintResult(view);
    }
  }, 5000);
  view.later = debounced(() => loadDetail(view), 600);
  view.disposers.push(
    () => view.log.destroy(),
    () => clearInterval(clock),
    () => clearInterval(live),
    view.later.cancel,
    () => view.stopDiff(),
    () => view.close?.(),
    () => view.dialog?.open && view.dialog.close(), // its question is answered "no"
  );

  await loadDetail(view, true);
  if (ui !== view || !view.rec) return;
  ctx.cli(view.rec.cli);
  loadWorkflow(view);
  const onDown = (down) => {
    if (ui === view) refs.down.hidden = !down;
  };
  view.close = stream(`/api/runs/${encodeURIComponent(view.id)}/events`, (event) => onEvent(view, event), ['run', 'line', 'loop', 'approval', 'end'], null, { onDown });
}

/** The steps of the run's workflow, for the strip: only for a run of the viewer's unit (or the facilitator's view) that took its own harness. */
async function loadWorkflow(view) {
  const { rec, ctx } = view;
  const admin = ctx.me.subject === 'admin';
  if (rec.kind !== 'workflow' || rec.overlay_from || !rec.workflow || (!admin && rec.unit !== ctx.me.unit?.id)) return;
  try {
    const reply = await get('/api/structure', admin ? { unit: rec.unit } : undefined);
    if (ui !== view) return;
    view.workflow = reply.structure.workflows.find((one) => one.name === rec.workflow && !one.error)?.steps ?? null;
    paint(view);
  } catch {
    /* the strip shows the steps the run went through */
  }
}

/** The run's detail (progress, plan, review, diff). `record`: take its record too (the stream's `run` event is newer otherwise). */
async function loadDetail(view, record = false) {
  if (view.loading) {
    view.again = true;
    return;
  }
  view.loading = true;
  try {
    const detail = await get(`/api/runs/${encodeURIComponent(view.id)}`);
    if (ui !== view) return;
    if (record || !view.rec) view.rec = detail.record;
    Object.assign(view, { progress: detail.progress, plan: detail.plan, review: detail.review, diff: detail.diff });
    paint(view);
  } catch (err) {
    if (ui !== view || err.status === 401) return;
    if (err.status === 404) {
      view.el.replaceChildren(
        h('div', 'panel-note', h('h2', '', 'This run does not exist.'), h('p', '', 'It may have been typed wrong, or the console was set up again. ', link('#/runs', 'See all runs'), '.')),
      );
    } else if (!view.rec) {
      view.el.replaceChildren(
        h('div', 'panel-note', h('h2', '', 'Could not load the run.'), h('p', '', `${err.message}. Reload the page to try again, or `, link('#/runs', 'go back to the runs'), '.')),
      );
    } else failed(view, err, 'Could not refresh the run');
  } finally {
    view.loading = false;
    if (view.again && ui === view) {
      view.again = false;
      view.later();
    }
  }
}

function onEvent(view, { type, data }) {
  if (ui !== view) return;
  if (type === 'line') {
    view.log.add(data);
    keepForTimeline(view, data);
    if (REFRESH_ON.has(data.kind)) view.later();
  } else if (type === 'loop') {
    view.log.add({ ...data, kind: 'loop' });
    view.rec = { ...view.rec, loop_flag: data.text }; // the record gets it with the next `run` event; the page need not wait
    paint(view);
  } else if (type === 'approval') {
    view.rec = { ...view.rec, status: 'waiting', pending: data };
    paint(view);
  } else if (type === 'run' || type === 'end') {
    const wasOver = isFinal(view.rec?.status); // a page opened on a finished run has all of it from the first read
    view.rec = data;
    view.streamed = true;
    paint(view);
    if (isFinal(data.status) && !wasOver) {
      view.ctx.refreshMe(); // the credits are charged when the run ends
      view.later();
    }
    if (type === 'end') {
      view.close?.(); // the server ends the stream after `end`: do not let the browser reconnect to a finished run
      if (!wasOver) loadDetail(view, true); // the plan, review and diff are final now
    }
  }
}

/** Keep a line for the timeline (see `keepLine`: a very long run keeps its most recent lines, and the page says so). */
function keepForTimeline(view, line) {
  const { kept, dropped } = keepLine(view.lines, line);
  view.lost += dropped;
  if (kept || dropped) view.rev += 1;
}

async function act(view, action, body, done) {
  if (view.busy) return;
  setBusy(view, true);
  try {
    const rec = await post(`/api/runs/${encodeURIComponent(view.id)}/${action}`, body);
    if (ui !== view) return;
    view.rec = rec;
    done?.(rec);
  } catch (err) {
    failed(view, err, 'Could not do that');
    // the run moved on (409), or the call got no answer and may have been done all the same: look at the run again
    if ((err.status === 409 || err.timeout) && ui === view) loadDetail(view, true);
  } finally {
    if (ui === view) setBusy(view, false);
  }
}

function setBusy(view, busy) {
  view.busy = busy;
  paint(view);
}

/** A small modal question on the run page: resolves true for the confirm button, false for Cancel, Escape or leaving. */
function ask(view, { title, text, confirm, danger = false }) {
  return new Promise((resolve) => {
    view.dialog?.remove();
    const dialog = (view.dialog = h('dialog', 'run-dialog'));
    dialog.setAttribute('aria-labelledby', 'run-dialog-title');
    const no = button('Cancel', '', () => {
      resolve(false);
      dialog.close();
    });
    const yes = button(confirm, danger ? 'danger' : 'primary', () => {
      resolve(true);
      dialog.close();
    });
    dialog.append(withId(h('h2', '', title), 'run-dialog-title'), h('p', '', text), h('div', 'run-dialog-actions', no, yes));
    dialog.addEventListener('close', () => resolve(false), { once: true }); // after a click the promise has its answer already
    view.el.append(dialog);
    dialog.showModal();
    no.focus(); // the safe answer is the one at hand
  });
}

/** Stop the run (the header's Stop, the loop warning's "Stop run"), once the person has said yes. */
async function stopRun(view) {
  const { rec } = view;
  if (!rec || view.busy) return;
  const what = rec.status === 'queued'
    ? 'It has not started: it leaves the queue.'
    : `It ends now and cannot be continued.${rec.kind === 'workflow' ? ' The credits it has used so far stay charged.' : ''}`;
  const sure = await ask(view, { title: `Stop the run ${runTitle(rec)}?`, text: what, confirm: 'Stop the run', danger: true });
  if (sure && ui === view) act(view, 'stop');
}

/** Discard the result of the run, once the person has said yes. */
async function discardRun(view) {
  const { rec } = view;
  if (!rec || view.busy) return;
  const keepable = Boolean(rec.branch && rec.commit);
  const sure = await ask(view, {
    title: `Discard the result of ${runTitle(rec)}?`,
    text: `Your own files are not changed.${keepable ? ' The commit of the run stays on its own branch: you can still keep it afterwards.' : ''}`,
    confirm: 'Discard',
  });
  if (sure && ui === view) act(view, 'discard');
}

/** "Check my code again" on a finished gates run: a new gates run, and its page. */
async function checkAgain(view) {
  if (view.busy) return;
  setBusy(view, true);
  try {
    const rec = await post('/api/runs', { kind: 'gates' });
    if (ui !== view) return;
    if (rec.status === 'refused') view.ctx.toast(`The run was refused: ${rec.error || 'no reason given'}`, 'error');
    else location.hash = runHref(rec.id);
  } catch (err) {
    failed(view, err, 'Could not start the gates');
  } finally {
    if (ui === view) setBusy(view, false);
  }
}

// --- painting a run ----------------------------------------------------------------------------------------------------

/** Run `build` only when `key` is not the one `name` was last built with (so a Monaco diff or a button is not redone for nothing). */
function keyed(view, name, key, build) {
  if (view.keys[name] === key) return false;
  view.keys[name] = key;
  build();
  return true;
}

function paint(view) {
  if (!view.rec) return;
  arrange(view);
  paintHeader(view);
  paintSteps(view);
  paintApproval(view);
  paintLoop(view);
  paintGates(view);
  paintResult(view);
  const { status, position, kind } = view.rec;
  view.log.setPlaceholder(
    !view.streamed ? 'Loading the log…'
      : status === 'queued'
      ? Number.isInteger(position) ? `Waiting in the queue (place ${position}). The log starts when the run starts.` : 'The run is starting…'
      : kind !== 'workflow' && !isFinal(status) ? 'The gates are building and testing your saved code. Their result shows here when they are done.'
      : isFinal(status) ? 'This run has no log.' : 'Waiting for the first line…',
  );
}

/** While a run goes, its log comes first; once it is over, its result and gates do (the log is still there, below). */
function arrange(view) {
  const { refs } = view;
  const over = isFinal(view.rec.status);
  keyed(view, 'arrange', over, () => {
    if (over) refs.logbox.before(refs.result, refs.gates);
    else refs.logbox.after(refs.gates, refs.result);
    refs.logTitle.textContent = over ? 'Log' : 'Live log';
  });
  const gatesOnly = view.rec.kind !== 'workflow';
  refs.root.classList.toggle('kind-gates', gatesOnly);
  refs.logHint.textContent = gatesOnly ? '' : over ? 'Tool calls, what the agents said, gates and errors.' : 'Tool calls, what the agents say, gates and errors, as they happen.';
}

function setFact(fact, text) {
  fact.value.textContent = text;
  fact.node.hidden = !text;
}

function paintClock(view) {
  const { rec, refs } = view;
  if (rec) setFact(refs.clock, runDuration(rec, Date.now()));
}

function paintHeader(view) {
  const { rec, refs, progress } = view;
  refs.title.textContent = runTitle(rec);
  refs.id.textContent = `Run ${rec.id}`;
  const { text, tone } = statusChip(rec);
  refs.chip.textContent = text;
  refs.chip.className = cls('badge', 'run-status', BADGE[tone]);
  refs.origin.textContent = runOrigin({ ...rec, overlay_from: null }); // the handover has its own badge
  setFact(refs.credits, rec.kind === 'workflow' ? creditsText(rec, progress) : 'free');
  paintClock(view);
  setFact(refs.step, isOpen(rec.status) ? (progress?.current ?? rec.pending?.step ?? '') : '');

  const badges = [];
  if (overlayLabel(rec)) badges.push(Object.assign(h('span', 'badge', overlayLabel(rec)), { title: `A handover run: it took the harness structure of ${rec.overlay_from}.` }));
  if (modelLabel(rec)) badges.push(Object.assign(h('span', 'badge', modelLabel(rec)), { title: 'No model: a scripted stand-in played the agents. No credits were charged.' }));
  if (!view.canAct()) badges.push(h('span', 'badge', `${rec.unit}: you are watching`));
  if (rec.extensions > 0) badges.push(h('span', 'badge warn', `extended x${rec.extensions}`));
  if (rec.accounting === 'capped') badges.push(Object.assign(h('span', 'badge warn', CAPPED), { title: 'The run is charged its full ceiling.' }));
  const weak = cleanFlags(rec.weakened);
  if (weak.length) badges.push(Object.assign(h('span', 'badge weak', WEAK_TITLE), { title: weak.map((flag) => flag.detail).join('\n') }));
  refs.badges.replaceChildren(...badges);
  keyed(view, 'weak', JSON.stringify(weak), () => {
    refs.weak.hidden = weak.length === 0;
    refs.weak.replaceChildren(
      ...(weak.length ? [h('strong', '', 'This run started with a weakened harness — on purpose?'), h('ul', '', ...weak.map((flag) => h('li', '', flag.detail)))] : []),
    );
  });

  refs.stop.hidden = !(view.canAct() && isOpen(rec.status));
  refs.stop.disabled = view.busy;
  refs.again.hidden = !(rec.kind === 'gates' && isFinal(rec.status) && rec.unit === view.ctx.me.unit?.id);
  refs.again.disabled = view.busy;

  refs.error.textContent = rec.error ?? '';
  refs.error.hidden = !rec.error;
  const skipped = skippedNote(rec);
  refs.skipped.textContent = skipped;
  refs.skipped.hidden = !skipped;
}

const STEP_MARK = { ok: '✓ ', bad: '✗ ' };

/** The steps of the run's workflow, where the run is now, and the steps that ask a person (in interactive mode). */
function paintSteps(view) {
  const { rec, refs, progress } = view;
  const steps = rec.kind === 'workflow' ? workflowStrip(progress, view.workflow, isFinal(rec.status)) : [];
  const open = isOpen(rec.status);
  const asks = rec.mode !== 'autonomous';
  const waitingAt = rec.status === 'waiting' ? (rec.pending?.step ?? '') : null;
  const waitWord = view.canAct() ? 'waiting for you' : `waiting for ${rec.unit}`;
  const atStep = (step) => waitingAt !== null && (step.id === waitingAt || (step.current && !steps.some((other) => other.id === waitingAt)));
  const flow = flowModel(steps.map((step) => ({ ...step, human: step.human && asks })), view.workflow, progress?.gates, waitingAt === null ? null : (steps.find(atStep)?.id ?? null));
  keyed(view, 'flow', JSON.stringify(flow), () => {
    const svg = drawFlow(flow);
    refs.flow.hidden = !svg;
    refs.flow.replaceChildren(
      ...(svg
        ? [
            h('p', 'runflow-title', h('strong', '', 'The workflow, step by step'), flow.loops.length ? 'Dashed line: where a failed gate or a reject sends the run back.' : ''),
            h('div', 'runflow-scroll', svg),
          ]
        : []),
    );
  });
  keyed(view, 'steps', JSON.stringify([steps, open, asks, waitingAt, waitWord]), () => {
    refs.steps.hidden = refs.stepsNow.hidden = steps.length === 0;
    refs.steps.style.setProperty('--steps', String(Math.max(1, steps.length)));
    const at = steps.findIndex((step) => atStep(step) || step.current);
    const done = steps.filter((step) => step.tone === 'ok').length;
    refs.stepsNow.textContent = at >= 0
      ? `Step ${at + 1} of ${steps.length}: ${steps[at].id}, ${atStep(steps[at]) ? waitWord : steps[at].status || 'to do'}`
      : `${done} of ${steps.length} steps done`;
    refs.steps.replaceChildren(
      ...steps.map((step, index) => {
        const waiting = atStep(step);
        const tone = waiting ? 'busy' : step.tone; // waiting for a person is periwinkle, like running
        const word = waiting ? waitWord : step.status || (open ? 'to do' : 'not run');
        const meta = [step.role, step.human && asks ? 'asks you' : null].filter(Boolean).join(' · ');
        const item = h(
          'li',
          cls('run-step', `tone-${tone}`, (step.current || waiting) && 'current', !step.status && !waiting && 'todo'),
          h('span', 'run-step-bar'),
          h('span', 'run-step-name', h('span', 'run-step-n', String(index + 1)), step.id),
          h('span', 'run-step-word', `${STEP_MARK[tone] ?? ''}${word}`),
          meta ? h('span', 'run-step-meta', meta) : null,
        );
        if (step.current || waiting) item.setAttribute('aria-current', 'step');
        return item;
      }),
    );
  });
}

function paintApproval(view) {
  const { rec, refs } = view;
  const pending = rec.status === 'waiting' ? rec.pending : null;
  const answered = answerGiven(pending, view.answered); // sent; the harness has not taken it yet
  const key = JSON.stringify([pending?.id, pending?.allowed_options, view.canAct(), view.busy, answered]);
  keyed(view, 'approval', key, () => {
    if (!pending) {
      refs.approval.replaceChildren();
      return;
    }
    const decision = pending.kind === 'escalation';
    const mine = view.canAct();
    const { choices, note } = approvalChoices(pending);
    const actions = h('div', 'actions run-ask-actions');
    if (mine && answered === null) {
      for (const choice of choices) {
        const node = button(choice.label, choice.tone, () => act(view, 'answer', { answer: choice.value }, () => (view.answered = { id: pending.id, answer: choice.value })));
        node.disabled = view.busy;
        actions.append(node);
      }
    }
    const title = decision ? 'The run needs a decision' : mine ? 'The run needs your approval' : 'The run waits for an approval';
    const card = h(
      'section',
      cls('run-ask', decision && 'decision'),
      h('div', 'run-ask-head', withId(h('h2', '', title), 'run-ask-title'), pending.step ? h('span', 'run-ask-step', `at step ${pending.step}`) : null),
      h('p', 'run-ask-question', capQuestion(pending, rec.cap) ?? String(pending.question ?? '')),
      pending.details ? h('pre', 'run-ask-details', String(pending.details)) : null,
      mine ? (actions.childElementCount ? actions : null) : h('p', 'run-ask-wait', `Waiting for ${rec.unit} to answer.`),
      answered === null ? null : h('p', 'run-ask-wait', `You answered "${answered}". Waiting for the run to go on.`),
      mine && answered === null && note ? h('p', 'hint', note) : null,
    );
    card.setAttribute('aria-labelledby', 'run-ask-title');
    if (mine && answered === null) card.setAttribute('role', 'alert');
    refs.approval.replaceChildren(card);
    if (mine && view.asked !== pending.id) {
      view.asked = pending.id; // a new question: bring it into view, once
      requestAnimationFrame(() => card.isConnected && card.scrollIntoView({ block: 'nearest' }));
    }
  });
}

function paintLoop(view) {
  const { rec, refs } = view;
  const flag = isOpen(rec.status) ? rec.loop_flag : null;
  keyed(view, 'loop', JSON.stringify([flag, view.canAct(), view.busy]), () => {
    refs.loop.hidden = !flag;
    if (!flag) {
      refs.loop.replaceChildren();
      return;
    }
    const stop = view.canAct() ? button('Stop run', 'danger small', () => stopRun(view)) : null;
    if (stop) stop.disabled = view.busy;
    refs.loop.setAttribute('role', 'alert');
    refs.loop.replaceChildren(h('p', '', h('strong', '', 'The agent seems to be going in circles. '), flag), stop);
  });
}

function paintGates(view) {
  const { refs, progress, rec } = view;
  const matrix = gateMatrix(progress?.gates);
  const verdict = gateVerdict(matrix);
  const lite = typeof rec.not_run_here === 'string' ? rec.not_run_here : ''; // the Docker-free ladder's line, under the gates
  keyed(view, 'gates', JSON.stringify([matrix, rec.kind, lite]), () => {
    refs.gates.hidden = matrix.rows.length === 0 && !lite;
    if (!matrix.rows.length) {
      refs.gates.replaceChildren(...(lite ? [h('div', 'run-section-head', h('h2', '', 'Gates')), h('p', 'run-lite', lite)] : []));
      return;
    }
    const th = (text, scope) => Object.assign(h('th', '', text), { scope });
    const td = (status) => {
      const cell = gateCell(status);
      return h('td', '', h('span', `run-cell tone-${cell.tone}`, cell.text));
    };
    let table;
    if (matrix.rows.length === 1) {
      // one attempt (a Check my code run, say): a gate per row reads better than a row as wide as the ladder
      const [only] = matrix.rows;
      table = h(
        'table',
        'run-matrix one',
        h('thead', '', h('tr', '', th('Gate', 'col'), th(rec.kind === 'workflow' ? only.label : 'Result', 'col'))),
        h('tbody', '', ...matrix.columns.map((id) => h('tr', '', th(id, 'row'), td(only.cells[id])))),
      );
    } else {
      table = h(
        'table',
        'run-matrix',
        h('thead', '', h('tr', '', th('Attempt', 'col'), ...matrix.columns.map((id) => th(id, 'col')))),
        h('tbody', '', ...matrix.rows.map((row) => h('tr', '', th(row.label, 'row'), ...matrix.columns.map((id) => td(row.cells[id]))))),
      );
    }
    const said = verdict && (rec.kind === 'workflow' ? `Last attempt: ${verdict.text}` : verdict.text);
    refs.gates.replaceChildren(
      h('div', 'run-section-head', h('h2', '', 'Gates'), said ? h('p', `run-verdict tone-${verdict.tone}`, said) : null),
      h('div', 'run-matrix-wrap', table),
      ...(lite ? [h('p', 'run-lite', lite)] : []),
    );
  });
}

// --- the result ------------------------------------------------------------------------------------------------------------

function chooseTab(view, id) {
  view.tab = lastTab = id;
  paintResult(view);
}

function paintResult(view) {
  const { rec, refs } = view;
  const show = rec.kind === 'workflow' && (isFinal(rec.status) || Boolean(view.plan || view.review || view.diff) || view.lines.size > 0); // a gates run has no plan, diff or credits
  refs.result.hidden = !show;
  if (!show) return;

  view.tab ??= rec.status === 'done' ? 'diff' : 'timeline';
  for (const [id, tab] of view.tabButtons) {
    tab.setAttribute('aria-selected', String(id === view.tab));
    tab.tabIndex = id === view.tab ? 0 : -1;
  }
  refs.body.setAttribute('aria-labelledby', `run-tab-${view.tab}`);
  const key = JSON.stringify([view.tab, view.streamed, view.plan?.length, view.review?.length, view.diff?.text.length, view.diff?.source, view.progress?.credits_by_role, rec.credits, rec.cap, rec.accounting, rec.status, view.rev]);
  keyed(view, 'body', key, () => paintTab(view));
  paintActions(view);
}

function textBlock(text, none) {
  return text ? h('pre', 'codeblock run-text', text) : h('p', 'empty', none);
}

function paintTab(view) {
  const { body } = view.refs;
  const token = ++view.token;
  view.stopDiff();
  view.stopDiff = () => {};
  const open = isOpen(view.rec.status);
  if (view.tab === 'timeline') body.replaceChildren(timelineTab(view));
  else if (view.tab === 'plan') body.replaceChildren(textBlock(view.plan, open ? 'No plan yet: the planner writes plan.md.' : 'No plan was written.'));
  else if (view.tab === 'review') body.replaceChildren(textBlock(view.review, open ? 'No review yet: the reviewer writes review.md.' : 'No review was written.'));
  else if (view.tab === 'credits') body.replaceChildren(creditsTable(view));
  else paintDiff(view, body, token);
}

/** The timeline of the run: its lanes, then the credits so far and by role. */
function timelineTab(view) {
  const { rec, progress } = view;
  const open = isOpen(rec.status);
  const model = timeline([...view.lines.values()], progress, open ? Date.now() : null);
  if (model.empty) return h('p', 'empty', !view.streamed ? 'Loading the steps…' : open ? 'No steps yet.' : 'No steps were recorded.');
  const width = Math.max(280, Math.round((view.refs.body.clientWidth || 640) - 24)); // drawn for the box it is in; it scales if the box changes
  const spark = sparkline(model.credits.map((point) => point.total));
  const spent = creditsText(rec, progress);
  return h(
    'div',
    'flow',
    spent || spark ? h('p', 'flow-credits', spent ? `Credits ${spent}` : null, spark) : null,
    view.lost ? h('p', 'hint', `The timeline starts late: the ${view.lost} earliest lines are not shown (it keeps the most recent ones).`) : null,
    journey(model, { width }),
    legend(),
    costChart(progress?.credits_by_role, rec.cap, { width }),
  );
}

function creditsTable(view) {
  const { rec, progress } = view;
  if (rec.kind !== 'workflow') return h('p', 'empty', 'This run spends no credits.');
  const roles = Object.entries(progress?.credits_by_role ?? {});
  const spent = isFinal(rec.status) ? rec.credits : (progress?.credits ?? rec.credits);
  const rows = roles.map(([role, amount]) => h('tr', '', Object.assign(h('th', '', role), { scope: 'row' }), h('td', 'run-num', formatCredits(amount))));
  return h(
    'div',
    'run-credits-tab',
    roles.length
      ? h('table', 'table run-credits-table', h('thead', '', h('tr', '', h('th', '', 'Role'), h('th', 'run-num', 'Credits'))), h('tbody', '', ...rows))
      : h('p', 'empty', 'No credits recorded yet.'),
    h('p', 'run-credits-total', `${isFinal(rec.status) ? 'Charged' : 'So far'}: ${formatCredits(spent)} of ${formatCredits(rec.cap)} credits for this run.`),
    rec.accounting === 'capped' ? h('p', 'run-note warn', `${CAPPED}. The run is charged its full ceiling.`) : null,
    rec.fake ? h('p', 'hint', 'The fake model uses no model: these numbers are its stand-ins, and nothing is charged.') : null,
  );
}

function plainText(host, label, text) {
  const area = h('textarea', 'run-plain');
  area.readOnly = true;
  area.wrap = 'off';
  area.value = text;
  area.setAttribute('aria-label', label);
  host.replaceChildren(area);
}

async function paintDiff(view, body, token) {
  const { diff, rec } = view;
  if (!diff || !String(diff.text ?? '').trim()) {
    body.replaceChildren(h('p', 'empty', isOpen(rec.status) ? 'No changes yet.' : 'This run left no changes.'));
    return;
  }
  const caption = h(
    'p',
    'run-diffcaption',
    diff.source === 'commit'
      ? rec.overlay_from
        ? `What the run committed on top of the harness of ${rec.overlay_from}: that harness is not in this diff.`
        : 'What the run committed, against where your branch was when it started.'
      : 'What the run left uncommitted. Nothing was committed.',
    diff.truncated ? ' The diff is cut where it was too long to show.' : '',
  );
  const files = parseDiff(diff.text);
  const host = h('div', 'run-diff');
  body.replaceChildren(caption, host);
  monaco ??= await loadMonaco();
  if (ui !== view || token !== view.token) return;
  if (!monaco || !files.length) {
    plainText(host, 'Changes (unified diff)', diff.text);
    return;
  }

  const list = h('ul', 'run-files');
  list.setAttribute('aria-label', 'Changed files');
  const pane = h('div', 'run-diffpane');
  host.replaceChildren(list, pane);
  const buttons = files.map((file, index) => {
    const counts = file.status === 'binary' ? 'binary' : `+${file.added} -${file.removed}`;
    const item = button('', 'run-file', () => select(index));
    item.append(h('span', 'run-file-name', file.path), h('span', 'run-file-counts', counts));
    item.title = file.path;
    list.append(h('li', '', item));
    return item;
  });
  function select(index) {
    const file = files[index];
    view.stopDiff();
    view.stopDiff = () => {};
    buttons.forEach((node, at) => {
      node.classList.toggle('active', at === index);
      node.setAttribute('aria-pressed', String(at === index));
    });
    pane.replaceChildren();
    if (file.status === 'binary' || (file.status === 'renamed' && !file.original && !file.modified)) {
      pane.append(h('p', 'empty', file.status === 'binary' ? 'A binary file changed.' : `Renamed from ${file.from}.`));
      return;
    }
    view.stopDiff = diffView(monaco, pane, file.original, file.modified, languageFor(file.path));
  }
  select(0);
}

/** Keep and Discard, at the top of the result: what the run's change does to the unit branch, and the choice. */
function paintActions(view) {
  const { rec, refs } = view;
  const mine = view.canAct();
  const key = JSON.stringify([rec.status, rec.kept, rec.branch, rec.commit, rec.overlay_from, mine, view.busy]);
  keyed(view, 'actions', key, () => {
    const children = [];
    const say = (...text) => h('p', 'run-decision-text', ...text);
    if (rec.status === 'done' && mine && rec.overlay_from) {
      children.push(say(`This run used the harness of ${rec.overlay_from}. Its change stays on the branch ${rec.branch ?? 'of the run'}; it is not put into your own files.`));
    } else if (rec.status === 'done' && mine) {
      const keepable = Boolean(rec.branch && rec.commit);
      const keep = (label) => {
        const node = button(label, label === 'Keep' ? 'primary' : '', () => act(view, 'keep', undefined, () => view.ctx.toast('Kept. Your files have the change.', 'ok')));
        node.disabled = view.busy || !keepable;
        return node;
      };
      if (rec.kept === true) children.push(say(h('strong', '', 'Kept: '), "your files have this run's change."));
      else if (rec.kept === false) {
        children.push(say(h('strong', '', 'Discarded: '), 'your files were not changed.', keepable ? ' The commit of the run is still there: Keep anyway puts it into your files.' : ''));
        if (keepable) children.push(h('div', 'run-decision-actions', keep('Keep anyway')));
      } else {
        const discard = button('Discard', '', () => discardRun(view));
        discard.disabled = view.busy;
        children.push(
          say(h('strong', '', keepable ? 'Keep this change? ' : 'Nothing to keep. '), keepable ? 'Keep puts it into your files. Discard leaves them as they are. Look at the diff first.' : 'This run made no commit to keep.'),
          h('div', 'run-decision-actions', keep('Keep'), discard),
        );
      }
    } else if (isFinal(rec.status) && rec.kept === false && mine) {
      children.push(say(h('strong', '', 'Discarded.')));
    }
    refs.decision.replaceChildren(...children);
    refs.decision.hidden = children.length === 0;
  });
}
