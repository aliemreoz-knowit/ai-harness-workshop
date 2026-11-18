// Evals view (`#/evals`). A unit fills in its variant card once, for the lunch batch (handbook block 4a): its name and
// what it is compared with, exactly one change, a hypothesis and what a run may cost, then "Send my card". Under it the
// leaderboard: the batch's progress, every variant's row, the unit's row next to cheap with its hypothesis, and a link to
// the full leaderboard page. The facilitator gets the batch instead of a card (launch with the cost estimate, stop),
// every unit's card, then the same leaderboard.
//
// The card is one change and the cap (evalmodel.js `cardFromChange`); a setting it does not name stays as the unit's
// harness files have it. The card's API has no place for the hypothesis: it is kept in this browser, per unit, and shown
// with the unit's row in block 4b. The leaderboard is the public /api/eval/results.json, which the board routes serve;
// it is polled. Everything from the server goes on the page as text.
//
// Personal mode (one participant's laptop: no cards, no batch) shows two parts. The leaderboard of the experiments run
// from this checkout (the presenter's `python -m harness experiment start ...`), read-only: the same table and the same
// link to the full page, from the same /api/eval/results.json, which personal mode makes of the result files of the
// checkout and of the console's copy (routes_board.py), `?experiment=` for one experiment. It is polled, so rows come in
// as runs finish. One experiment is on show: the one the address names (`#/evals?experiment=lunch-20261120`, empty for
// all of them), else the newest lunch experiment (section 4b reads that one), else the newest; picking another keeps it
// in the address. Under it "Compare two runs" (compare.js): two of the participant's own runs side by side.

import { get, post } from '../api.js';
import * as compare from './compare.js';
import {
  CHANGES,
  DECIDERS,
  ROLES,
  SWITCHABLE,
  STALE_HINT,
  STALE_TEXT,
  boardRows,
  cardFromChange,
  cellText,
  changesOf,
  compareText,
  credits,
  estimate,
  formFromCard,
  launchCommand,
  oneChange,
  otherModel,
  parseTickets,
  progress,
  readHypothesis,
  summarizeCard,
  variantOf,
  writeHypothesis,
} from '../evalmodel.js';

const POLL_RUNNING_MS = 3000;
const POLL_IDLE_MS = 15000;
const POLL_PERSONAL_MS = 5000; // personal mode: nothing says when a run of the stage's experiments finishes
const ALL = ''; // personal mode: the experiment choice that means every experiment
const ROLE_LABELS = { planner: 'Planner', implementer: 'Implementer', test_writer: 'Test writer', reviewer: 'Reviewer' };
// the cards say small / large model, not a model id: the room reads them on the shared screen
const modelLabel = (id) => (/haiku|mini|small/i.test(id || '') ? 'small model' : /sonnet|opus|large/i.test(id || '') ? 'large model' : id);
const AUTONOMY_TEXT = { human: 'Keep approval steps (the eval passes and counts them)', auto: 'No approval steps' };

/** Every kind of change: its title, what it does and what cheap has (`v`: cheap's form). */
const KINDS = {
  prompt_template: {
    title: 'Prompt template',
    hint: () => "The orchestrator's prompt template: the one you edited in block 1, harness/prompts/implement.md, which you changed in place.",
    v11: (v) => v.prompt_template,
  },
  retry_budget: {
    title: 'Retry budget',
    hint: () => 'Loops after a failed gate or a rejected review before the run escalates.',
    v11: (v) => String(v.retry_budget),
  },
  role: {
    title: 'A role on or off',
    hint: () => 'The implementer always runs.',
    v11: (v) => SWITCHABLE.map((role) => `${ROLE_LABELS[role].toLowerCase()} ${v.roles[role] ? 'on' : 'off'}`).join(', '),
  },
  autonomy: {
    title: 'Autonomy',
    hint: () => 'What the eval does with a workflow step that waits for a person.',
    v11: (v) => (v.autonomy === 'auto' ? 'no approval steps' : 'keeps approval steps'),
  },
  custom_instructions: {
    title: 'Instructions',
    hint: () => 'AGENTS.md and the instruction files the agents read.',
    v11: (v) => (v.custom_instructions ? 'loaded' : 'not loaded'),
  },
  gates: {
    title: 'Gates in the loop',
    hint: () => 'The gate ladder runs inside the loop and gives the agent its feedback.',
    v11: (v) => (v.gates ? 'on' : 'off'),
  },
  model: {
    title: "A role's model",
    hint: () => 'The rule of the day is the small model only: ask the facilitator before you pick another model.',
    v11: (v) => {
      const models = [...new Set(ROLES.map((role) => v.models[role]).filter(Boolean))];
      return models.length === 1 ? `all ${modelLabel(models[0])}` : ROLES.map((role) => `${ROLE_LABELS[role].toLowerCase()} ${modelLabel(v.models[role]) || '-'}`).join(', ');
    },
  },
};

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

function fail(ctx, err, what) {
  if (err.status === 401) return; // the login page is on its way
  ctx.toast(`${what}: ${err.message}`, 'error');
}

function loadStyles() {
  if (document.getElementById('evals-css')) return;
  document.head.append(h('link', { id: 'evals-css', rel: 'stylesheet', href: '/web/views/evals.css' }));
}

/** localStorage, or null where the browser refuses it. */
function storage() {
  try {
    return window.localStorage;
  } catch {
    return null;
  }
}

const clock = (time) => time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit' });

// --- mount and unmount -----------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  if (ctx.me.personal === true) return mountPersonal(el, ctx); // no cards, no batch: the checkout's experiments
  loadStyles();
  const admin = ctx.me.subject === 'admin';
  const unitId = ctx.me.unit?.id ?? null;
  const view = (ui = {
    ctx,
    admin,
    unitId,
    variant: unitId ? variantOf(unitId) : null,
    timer: null,
    status: null, // the last batch status the server gave
    board: null, // the last leaderboard file
    rows: [], // the leaderboard as table rows
    settled: '', // what the board on show was drawn for: the batch and its finished runs
    cards: null, // the admin's view of the submitted cards: {unit: card}
    plan: null, // what a launch would run now (the admin's estimate)
    failing: false, // the last poll failed (a person is told once, not at every poll)
    stored: null, // the unit's card on file
    form: null, // the unit's card being filled in (evalmodel.js oneChange)
    v11: null, // cheap's values, as a form
    hypothesis: { text: '', column: '' },
    sentAt: null,
    refs: {},
  });

  const r = view.refs;
  r.line = h('p', { class: 'ev-line', 'aria-live': 'polite' });
  r.meter = h('progress', { class: 'ev-progress', max: '1', value: '0', 'aria-label': 'Eval batch progress', hidden: true });
  const page = h(
    'div',
    { class: 'page evals' },
    h(
      'header',
      { class: 'page-head' },
      h('h1', { text: 'Evals' }),
      h('p', {
        class: 'page-sub',
        text: admin
          ? 'The lunch batch: launch it on the hidden tickets, read every unit’s card, follow the leaderboard.'
          : 'Your variant for the lunch batch: one change to your harness, measured on hidden tickets next to cheap. Send it by 12:08.',
      }),
    ),
  );
  if (admin) {
    r.batch = h('section', { class: 'section', 'aria-labelledby': 'ev-batch-title' });
    r.cards = h('section', { class: 'section', 'aria-labelledby': 'ev-cards-title' });
    page.append(r.batch, r.cards);
    buildBatch(view);
  } else {
    r.card = h('section', { class: 'section', 'aria-labelledby': 'ev-card-title' }, cardHead(), h('p', { class: 'empty-line', text: 'Loading your card…' }));
    page.append(r.card);
  }
  r.board = h('section', { class: 'section', 'aria-labelledby': 'ev-board-title' });
  page.append(r.board);
  buildBoard(view);
  el.scrollTop = 0;
  el.replaceChildren(page);
  paintBatch(view);
  if (!admin) await showCard(view);
  if (ui === view) await refresh(view);
}

export function unmount() {
  const view = ui;
  if (!view) return;
  ui = null; // every call still waiting for the server sees that the view is gone
  clearTimeout(view.timer);
  if (view.personal) {
    document.removeEventListener('visibilitychange', view.onVisible);
    compare.unmount();
  }
  view.ctx.cli('');
}

// --- personal mode: the checkout's leaderboard and "Compare two runs" -------------------------------------------------

async function mountPersonal(el, ctx) {
  loadStyles();
  const asked = typeof ctx.route.params.experiment === 'string' ? ctx.route.params.experiment.slice(0, 200) : null;
  const view = (ui = {
    ctx,
    personal: true,
    admin: false,
    unitId: null, // no "you" row: the stage's experiments are not this unit's variant
    timer: null,
    board: null,
    rows: [],
    drawn: '', // the answer the table was drawn from
    experiment: asked, // the experiment on show ('' for all); null until the first answer picks one
    picked: asked !== null, // a person (or the address) chose it: a new experiment does not move it
    experiments: [], // experiment_runs of the last answer, newest first
    failing: false,
    refs: {},
  });
  const r = view.refs;
  r.board = h('section', { class: 'section', 'aria-labelledby': 'ev-board-title' });
  r.compare = h('section', { class: 'section', 'aria-labelledby': 'cmp-title' });
  const page = h(
    'div',
    { class: 'page evals' },
    h(
      'header',
      { class: 'page-head' },
      h('h1', { text: 'Evals' }),
      h('p', { class: 'page-sub', text: 'The leaderboard of the experiments run from this checkout, and two of your own runs side by side.' }),
    ),
    r.board,
    r.compare,
  );
  buildPersonalBoard(view);
  el.scrollTop = 0;
  el.replaceChildren(page);
  view.onVisible = () => {
    if (!document.hidden && ui === view) refreshPersonal(view);
  };
  document.addEventListener('visibilitychange', view.onVisible);
  await Promise.all([refreshPersonal(view), compare.mount(r.compare, ctx, { embedded: true })]);
}

function buildPersonalBoard(view) {
  const r = view.refs;
  r.pick = h('select', { id: 'ev-experiment' });
  r.pick.addEventListener('change', () => {
    view.experiment = r.pick.value;
    view.picked = true;
    // kept in the address (a reload, a link) without a new route: the page stays as it is
    history.replaceState(null, '', `#/evals?experiment=${encodeURIComponent(view.experiment)}`);
    refreshPersonal(view);
  });
  r.line = h('p', { class: 'ev-line', 'aria-live': 'polite' });
  r.boardBody = h('div', {});
  r.board.replaceChildren(
    h(
      'div',
      { class: 'section-head' },
      h('h2', { id: 'ev-board-title', text: 'Leaderboard' }),
      h('span', { class: 'spacer' }),
      h('a', { class: 'btn', href: '/leaderboard', target: '_blank', rel: 'noopener', text: 'Open the full leaderboard' }),
    ),
    h('p', { class: 'section-sub' }, 'Read-only: the experiments started from this checkout with ', h('code', { text: 'python -m harness experiment start' }), '. Rows come in as runs finish.'),
    h('div', { class: 'ev-pick' }, h('label', { for: 'ev-experiment', text: 'Experiment' }), r.pick),
    r.line,
    r.boardBody,
  );
}

/** The experiment to show when nobody picked one: the newest lunch experiment, else the newest (the list is newest first). */
function defaultExperiment(list) {
  return (list.find((one) => /^lunch-/.test(one.experiment)) ?? list[0])?.experiment ?? ALL;
}

async function refreshPersonal(view) {
  clearTimeout(view.timer);
  const ask = (experiment) => get(`/api/eval/results.json${experiment ? `?experiment=${encodeURIComponent(experiment)}` : ''}`);
  try {
    let board = await ask(view.experiment ?? ALL);
    if (ui !== view) return;
    if (!view.picked) {
      const wanted = defaultExperiment(Array.isArray(board.experiment_runs) ? board.experiment_runs : []);
      if (wanted !== (view.experiment ?? ALL)) {
        board = await ask(wanted);
        if (ui !== view) return;
      }
      view.experiment = wanted;
    }
    view.experiments = Array.isArray(board.experiment_runs) ? board.experiment_runs : [];
    view.board = board;
    const drawn = JSON.stringify(board);
    if (drawn !== view.drawn) {
      view.drawn = drawn;
      paintPicker(view);
      paintPersonalLine(view);
      paintBoard(view);
    }
    view.failing = false;
  } catch (err) {
    if (ui !== view) return;
    if (!view.failing) fail(view.ctx, err, 'Could not load the leaderboard');
    view.failing = true;
  }
  // a hidden tab does not poll; it reads again when it is shown (onVisible)
  if (ui === view && !document.hidden) view.timer = setTimeout(() => refreshPersonal(view), POLL_PERSONAL_MS);
}

/** "12:41" of an ISO time, or null. */
const finishedAt = (iso) => {
  const time = iso ? new Date(iso) : null;
  return time && !Number.isNaN(time.getTime()) ? clock(time) : null;
};

const runsText = (n) => `${n} run${n === 1 ? '' : 's'}`;

function paintPicker(view) {
  const { pick } = view.refs;
  const list = view.experiments;
  const options = [h('option', { value: ALL, text: list.length > 1 ? `All ${list.length} experiments together` : 'All experiments' })];
  for (const one of list) options.push(h('option', { value: one.experiment, text: `${one.experiment} (${runsText(one.runs)})` }));
  if (view.experiment && !list.some((one) => one.experiment === view.experiment)) options.push(h('option', { value: view.experiment, text: `${view.experiment} (no runs yet)` }));
  pick.replaceChildren(...options);
  pick.value = view.experiment ?? ALL;
  view.ctx.cli(view.experiment ? `python -m harness experiment leaderboard results/${view.experiment}.jsonl` : 'python -m harness experiment leaderboard');
}

/** "lunch-20261120: 18 runs finished, the last at 12:41, in the console's copy of the checkout." */
function paintPersonalLine(view) {
  const { line } = view.refs;
  const list = view.experiments;
  const where = (one) => (one.where === 'console copy' ? "in the console's copy of the checkout" : 'in the checkout');
  if (!list.length) {
    line.textContent = 'No experiment has been run from this checkout yet.';
    return;
  }
  if (!view.experiment) {
    const total = list.reduce((sum, one) => sum + one.runs, 0);
    const at = finishedAt(list[0].last);
    line.textContent = `${runsText(total)} in ${list.length} experiment${list.length === 1 ? '' : 's'}, counted together. The newest is ${list[0].experiment}${at ? `, last run at ${at}` : ''}.`;
    return;
  }
  const one = list.find((item) => item.experiment === view.experiment);
  if (!one) {
    line.textContent = `${view.experiment} has no finished run yet.`;
    return;
  }
  const at = finishedAt(one.last);
  const failed = one.runs - one.done;
  const errors = failed ? ` ${failed} ended with a setup or runner error and ${failed === 1 ? 'is' : 'are'} not counted.` : '';
  line.textContent = `${one.experiment}: ${runsText(one.runs)} finished${at ? `, the last at ${at}` : ''}, ${where(one)}.${errors}`;
}

// --- the card (a unit) ---------------------------------------------------------------------------------------------------------

function cardHead(state = null) {
  return h('div', { class: 'section-head' }, h('h2', { id: 'ev-card-title', text: 'Your variant card' }), state);
}

async function showCard(view) {
  const box = view.refs.card;
  if (!view.unitId) {
    box.replaceChildren(cardHead(), h('p', { class: 'empty-line', text: 'Log in with a unit code to send a card.' }));
    return;
  }
  let answer;
  try {
    answer = await get('/api/eval/card');
  } catch (err) {
    if (ui !== view || err.status === 401) return;
    box.replaceChildren(cardHead(), h('div', { class: 'notice' }, h('p', { text: `Your card could not be loaded: ${err.message}` }), h('p', { text: 'Reload the page to try again.' })));
    return;
  }
  if (ui !== view) return;
  view.stored = answer.card;
  view.options = answer.options;
  view.v11 = formFromCard(null, answer.defaults);
  view.form = oneChange(answer.card, answer.defaults, view.unitId, answer.options.models);
  view.hypothesis = readHypothesis(view.unitId, storage());
  buildCard(view);
}

function buildCard(view) {
  const { form, options, variant } = view;
  const r = view.refs;
  r.cardState = h('span', { class: 'badge' });
  r.cardNote = h('div', { class: 'notice ev-note', hidden: true });
  r.options = CHANGES.map((kind) => changeOption(view, kind));
  r.errors = h('div', { class: 'ev-errors', role: 'alert', hidden: true });
  r.send = h('button', { type: 'submit', class: 'btn primary', text: 'Send my card' });
  r.sent = h('span', { class: 'ev-sent', 'aria-live': 'polite' });
  r.cost = h('p', { class: 'ev-cost', 'aria-live': 'polite' });

  r.summary = h('div', { class: 'box ev-card ev-summary', hidden: true });

  // 2. the hypothesis, kept in this browser
  const text = h('textarea', { id: 'ev-hypothesis', rows: '3', maxlength: '600', placeholder: `For example: with retry budget 1, ${variant} passes as often as cheap and costs less per pass.` });
  text.value = view.hypothesis.text;
  const column = h(
    'select',
    { id: 'ev-column' },
    h('option', { value: '', text: 'Pick a column' }),
    DECIDERS.map(([key, label]) => h('option', { value: key, text: label, selected: key === view.hypothesis.column })),
  );
  const kept = h('p', { class: 'hint', text: 'Kept in this browser for block 4b, next to your row on the leaderboard. It is not sent with the card.' });
  const keep = () => {
    view.hypothesis = { text: text.value, column: column.value };
    if (!writeHypothesis(view.unitId, view.hypothesis, storage())) kept.textContent = 'This browser does not keep it: write it down.';
    paintMine(view);
  };
  text.addEventListener('input', keep);
  column.addEventListener('change', keep);

  // 3. the cost
  const cap = h('input', { type: 'number', id: 'ev-cap', min: '0.5', max: String(options.run_cap), step: '0.5', value: String(form.cap) });
  cap.addEventListener('input', () => {
    form.cap = Number.isNaN(cap.valueAsNumber) ? null : cap.valueAsNumber;
    paintCost(view);
  });

  const step = (n, title, ...body) => h('fieldset', { class: 'ev-step' }, h('legend', {}, h('span', { class: 'ev-n', text: `${n}` }), title), ...body);
  const card = h(
    'form',
    { class: 'box ev-card', novalidate: true, 'aria-labelledby': 'ev-card-title' },
    facts(view),
    r.cardNote,
    step(1, 'Your one change', h('p', { class: 'hint ev-step-sub', text: 'Exactly one, or the leaderboard cannot tell what helped. Whatever you do not change stays as your harness files have it.' }), h('ul', { class: 'ev-options' }, r.options.map((one) => one.item))),
    step(
      2,
      'Your hypothesis',
      h('div', { class: 'ev-field' }, h('label', { for: 'ev-hypothesis', text: 'What you expect' }), text),
      h('div', { class: 'ev-field ev-field-short' }, h('label', { for: 'ev-column', text: 'The leaderboard column that decides it' }), column),
      kept,
    ),
    step(
      3,
      'Cost',
      h('div', { class: 'ev-cap' }, h('label', { for: 'ev-cap', text: 'Cap per run' }), cap, h('span', { class: 'muted', text: `credits, at most ${credits(options.run_cap)}` })),
      r.cost,
    ),
    r.errors,
    h('div', { class: 'ev-send' }, r.send, r.sent),
  );
  card.addEventListener('submit', (event) => {
    event.preventDefault();
    send(view);
  });
  r.form = card;
  view.editing = changesOf(view.stored).length !== 1; // a card of one change on file shows as its summary
  view.refs.card.replaceChildren(cardHead(r.cardState), r.summary, card);
  paintOptions(view);
  paintCost(view);
  paintCardState(view);
}

/** The card's name and what it is measured against, as the form and the summary show them. */
function facts(view, ...more) {
  return h(
    'dl',
    { class: 'ev-facts' },
    h('dt', { text: 'Name' }),
    h('dd', {}, h('code', { text: view.variant })),
    h('dt', { text: 'Extends' }),
    h('dd', { text: 'Your harness files as you saved them, compared with cheap, the cheap reference harness.' }),
    ...more,
  );
}

/** The one change of a stored card in words, with what cheap has: ["Retry budget: 1", "cheap: 3"]. */
function changeWords(view, card) {
  const [change] = changesOf(card);
  if (!change) return ['none', ''];
  const { kind, role } = change;
  const v11 = view.v11;
  const onOff = (on) => (on ? 'on' : 'off');
  switch (kind) {
    case 'role':
      return [`${ROLE_LABELS[role]}: ${onOff(card.roles[role])}`, `cheap: ${onOff(v11.roles[role])}`];
    case 'model':
      return [`${ROLE_LABELS[role]}’s model: ${modelLabel(card.models[role])}`, `cheap: ${modelLabel(v11.models[role]) || '-'}`];
    case 'autonomy':
      return [`Autonomy: ${(AUTONOMY_TEXT[card.autonomy] ?? card.autonomy).toLowerCase()}`, `cheap: ${KINDS.autonomy.v11(v11)}`];
    case 'custom_instructions':
      return [`Instructions: ${card.custom_instructions ? 'loaded' : 'not loaded'}`, `cheap: ${KINDS.custom_instructions.v11(v11)}`];
    case 'gates':
      return [`Gates in the loop: ${onOff(card.gates)}`, `cheap: ${KINDS.gates.v11(v11)}`];
    default:
      return [`${KINDS[kind].title}: ${card[kind]}`, `cheap: ${KINDS[kind].v11(v11)}`];
  }
}

/** The card on file in a few lines, with "Change my card" to open the form again. */
function paintSummary(view) {
  const { summary } = view.refs;
  const card = view.stored;
  const [change, reference] = changeWords(view, card);
  const { text, column } = view.hypothesis;
  const decider = DECIDERS.find(([key]) => key === column)?.[1];
  const edit = h('button', { type: 'button', class: 'btn', text: 'Change my card' });
  edit.addEventListener('click', () => {
    view.editing = true;
    paintCardState(view);
    const { form } = view.refs;
    (form.querySelector('input[name="ev-change"]:checked') ?? form.querySelector('input[name="ev-change"]'))?.focus();
  });
  summary.replaceChildren(
    facts(
      view,
      h('dt', { text: 'Change' }),
      h('dd', {}, change, reference ? h('span', { class: 'muted', text: ` (${reference})` }) : null),
      h('dt', { text: 'Cap per run' }),
      h('dd', { text: `${credits(card.max_ai_credits)} credits` }),
      h('dt', { text: 'Hypothesis' }),
      h('dd', { class: text.trim() ? 'ev-quote' : 'muted' }, text.trim() || 'Not written yet.', decider ? h('span', { class: 'muted', text: ` Decided by ${decider.toLowerCase()}.` }) : null),
    ),
    h('div', { class: 'ev-send' }, edit, h('span', { class: 'ev-sent', text: view.sentAt ? `Sent at ${clock(view.sentAt)}.` : 'Your card is in for the lunch batch.' })),
  );
}

/** One kind of change: a radio with its title and cheap's value; the controls show once it is picked. */
function changeOption(view, kind) {
  const { form } = view;
  const id = `ev-change-${kind}`;
  const radio = h('input', { type: 'radio', name: 'ev-change', value: kind, checked: form.change === kind, 'aria-labelledby': id, 'aria-describedby': `${id}-v11` });
  radio.addEventListener('change', () => {
    if (!radio.checked) return;
    form.change = kind;
    view.refs.errors.hidden = true;
    paintOptions(view);
  });
  const head = h(
    'label',
    { class: 'ev-option-head' },
    radio,
    h('span', { class: 'ev-option-title', id, text: KINDS[kind].title }),
    h('span', { class: 'ev-v11', id: `${id}-v11` }, 'cheap: ', h('span', { class: 'mono', text: KINDS[kind].v11(view.v11) })),
  );
  const body = h('div', { class: 'ev-option-body' }, h('div', { class: 'ev-controls' }, controls(view, kind)), h('p', { class: 'hint', text: KINDS[kind].hint(view.variant.toLowerCase()) }));
  return { kind, item: h('li', { class: 'ev-option' }, head, body), body };
}

function select(label, choices, value, onChange) {
  const box = h('select', { 'aria-label': label }, choices.map(([key, text]) => h('option', { value: key, text, selected: key === value })));
  box.addEventListener('change', () => onChange(box.value, box));
  return box;
}

const ON_OFF = [['on', 'On'], ['off', 'Off']];
const onOff = (on) => (on ? 'on' : 'off');

/** The controls of one kind of change; each writes into the form at once. */
function controls(view, kind) {
  const { form, options, v11 } = view;
  switch (kind) {
    case 'prompt_template': {
      const input = h('input', { type: 'text', class: 'ev-template mono', maxlength: '32', spellcheck: 'false', autocomplete: 'off', value: form.prompt_template, 'aria-label': 'Prompt template' });
      input.addEventListener('input', () => (form.prompt_template = input.value.trim()));
      return [input];
    }
    case 'retry_budget': {
      const input = h('input', { type: 'number', class: 'ev-number', min: '0', max: String(options.max_retry_budget), step: '1', value: String(form.retry_budget), 'aria-label': 'Retry budget' });
      input.addEventListener('input', () => (form.retry_budget = Number.isNaN(input.valueAsNumber) ? null : input.valueAsNumber));
      return [input, h('span', { class: 'muted', text: `0 to ${options.max_retry_budget}` })];
    }
    case 'role': {
      const state = select('On or off', ON_OFF, onOff(form.roleOn), (value) => (form.roleOn = value === 'on'));
      const role = select('Role', SWITCHABLE.map((name) => [name, ROLE_LABELS[name]]), form.role, (value) => {
        form.role = value;
        form.roleOn = !v11.roles[value]; // the other way round from cheap: a change
        state.value = onOff(form.roleOn);
      });
      return [role, state];
    }
    case 'autonomy':
      return [select('Autonomy', ['human', 'auto'].map((value) => [value, AUTONOMY_TEXT[value]]), form.autonomy, (value) => (form.autonomy = value))];
    case 'custom_instructions':
      return [select('Instructions', [['on', 'Loaded'], ['off', 'Not loaded']], onOff(form.custom_instructions), (value) => (form.custom_instructions = value === 'on'))];
    case 'gates':
      return [select('Gates in the loop', ON_OFF, onOff(form.gates), (value) => (form.gates = value === 'on'))];
    case 'model': {
      const choices = (current) => (options.models.includes(current) ? options.models : [...options.models, current]).map((name) => [name, name]);
      const model = select('Model', choices(form.model), form.model, (value) => (form.model = value));
      const role = select('Role', ROLES.map((name) => [name, ROLE_LABELS[name]]), form.modelRole, (value) => {
        form.modelRole = value;
        form.model = otherModel(v11, value, options.models);
        model.value = form.model;
      });
      return [role, model];
    }
    default:
      return [];
  }
}

function paintOptions(view) {
  for (const one of view.refs.options) {
    const chosen = view.form.change === one.kind;
    one.item.classList.toggle('chosen', chosen);
    one.body.hidden = !chosen;
  }
}

function paintCost(view) {
  const { form, v11, variant } = view;
  if (!view.refs.cost || !form) return;
  const reference = view.rows.find((row) => row.reference);
  const cap = form.cap == null ? 'its cap' : `${credits(form.cap)} credits`;
  view.refs.cost.textContent = [
    `One run of ${variant} stops at ${cap}; its last model call can take it a little over. cheap stops at ${credits(v11.max_ai_credits)} credits.`,
    reference?.creditsMean != null ? `So far cheap has used ${credits(reference.creditsMean)} credits a run.` : null,
    'The batch is paid from the facilitator’s budget, not from your unit’s credits.',
  ]
    .filter(Boolean)
    .join(' ');
}

function paintCardState(view) {
  const { cardState, cardNote, sent } = view.refs;
  const stored = view.stored;
  cardState.textContent = stored ? 'Sent' : 'Not sent';
  cardState.className = stored ? 'badge ok' : 'badge';
  const changes = changesOf(stored);
  cardNote.hidden = !stored || changes.length === 1;
  if (stored && changes.length > 1) {
    cardNote.replaceChildren(
      h('p', {}, h('strong', { text: `Your card on file makes ${changes.length} changes: ` }), summarizeCard(stored), '.'),
      h('p', { text: 'Send it again with one change, or the leaderboard cannot tell what helped. The first of them is picked below.' }),
    );
  } else if (stored && changes.length === 0) {
    cardNote.replaceChildren(
      h('p', {}, h('strong', { text: 'Your card on file makes no change.' }), ' The batch would measure your harness files as they are.'),
      h('p', { text: 'Pick one change below and send the card again.' }),
    );
  }
  if (view.sentAt) sent.textContent = `Sent at ${clock(view.sentAt)}. Sending again replaces it.`;
  else sent.textContent = stored ? 'Your card is in. Sending again replaces it.' : 'Send it by 12:08.';
  const summary = !view.editing && stored;
  view.refs.summary.hidden = !summary;
  view.refs.form.hidden = Boolean(summary);
  if (summary) paintSummary(view);
}

function showErrors(view, lines) {
  const { errors } = view.refs;
  errors.replaceChildren(h('p', { text: 'The card was not sent:' }), h('ul', {}, lines.map((line) => h('li', { text: line }))));
  errors.hidden = false;
}

async function send(view) {
  const { form, refs } = view;
  if (!form.change) {
    showErrors(view, ['Pick your one change in step 1.']);
    refs.options[0].item.querySelector('input').focus();
    return;
  }
  refs.send.disabled = true;
  refs.errors.hidden = true;
  const card = cardFromChange(form);
  try {
    await post('/api/eval/cards', card);
  } catch (err) {
    refs.send.disabled = false;
    if (ui !== view) return;
    if (err.status === 422 && Array.isArray(err.body?.errors)) showErrors(view, err.body.errors);
    else fail(view.ctx, err, 'Could not send the card');
    return;
  }
  refs.send.disabled = false;
  if (ui !== view) return;
  view.stored = card;
  view.sentAt = new Date();
  view.editing = false;
  paintCardState(view);
  view.refs.summary.querySelector('button')?.focus(); // the form it was in has gone
  view.ctx.toast('Card sent.', 'ok');
}

// --- the batch and the cards (the facilitator) -----------------------------------------------------------------------------

// The batch section is built once (the launch form must keep what is typed in it); `paintBatch` only updates it.
function buildBatch(view) {
  const r = view.refs;
  r.stop = h('button', { type: 'button', class: 'btn danger', text: 'Stop batch', hidden: true });
  r.stop.addEventListener('click', () => stopBatch(view));
  r.batch.replaceChildren(
    h('div', { class: 'section-head' }, h('h2', { id: 'ev-batch-title', text: 'Eval batch' }), h('span', { class: 'spacer' }), r.stop),
    r.line,
    r.meter,
    launchForm(view),
  );
}

async function stopBatch(view) {
  const { stop } = view.refs;
  if (!window.confirm('Stop the eval batch? Runs in progress are lost; the runs already finished stay in the results.')) return;
  stop.disabled = true;
  try {
    await post('/api/admin/eval/stop');
    view.ctx.toast('The eval batch was stopped.', 'ok');
  } catch (err) {
    fail(view.ctx, err, 'Could not stop the batch');
  }
  stop.disabled = false;
  if (ui === view) await refresh(view);
}

function launchForm(view) {
  const r = view.refs;
  const tickets = h('input', { type: 'text', id: 'ev-tickets', class: 'mono', placeholder: 'TS-101, TS-102', autocomplete: 'off', spellcheck: 'false', 'aria-describedby': 'ev-tickets-hint' });
  const parallel = h('input', { type: 'number', id: 'ev-parallel', min: '1', max: '8', step: '1', value: '2' });
  const name = h('input', { type: 'text', id: 'ev-name', placeholder: 'chosen for you', autocomplete: 'off', maxlength: '48' });
  r.tickets = tickets;
  r.go = h('button', { type: 'submit', class: 'btn primary', text: 'Launch batch' });
  r.launchHint = h('p', { class: 'hint' });
  r.estimate = h('p', { class: 'ev-estimate', 'aria-live': 'polite' });
  const field = (id, label, control, hint = null) => h('div', { class: 'ev-field' }, h('label', { for: id, text: label }), control, hint);
  const form = h(
    'form',
    { class: 'box ev-launch', novalidate: true, 'aria-label': 'Launch a batch' },
    field('ev-tickets', 'Tickets', tickets, h('p', { class: 'hint', id: 'ev-tickets-hint', text: 'Hidden-test keys, separated by commas.' })),
    h('div', { class: 'ev-pair' }, field('ev-parallel', 'Runs at the same time', parallel), field('ev-name', 'Experiment name (optional)', name)),
    r.estimate,
    r.launchHint,
    h('div', { class: 'actions' }, r.go),
  );
  const command = () => view.ctx.cli(launchCommand({ tickets: parseTickets(tickets.value), parallel: parallel.valueAsNumber, experiment: name.value.trim() }));
  form.addEventListener('input', () => {
    command();
    paintEstimate(view);
  });
  command();
  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    r.go.disabled = true;
    try {
      const body = { tickets: parseTickets(tickets.value), parallel: parallel.valueAsNumber };
      if (name.value.trim()) body.experiment = name.value.trim();
      const started = await post('/api/admin/eval', body);
      view.ctx.toast(`Batch ${started.experiment} started: ${started.total} runs planned.`, 'ok');
    } catch (err) {
      fail(view.ctx, err, 'Could not launch the batch');
    }
    if (ui === view) await refresh(view);
  });
  return form;
}

function paintBatch(view) {
  const { line, meter } = view.refs;
  const now = progress(view.status);
  line.className = `ev-line ${now.state}`;
  line.textContent = now.text;
  meter.hidden = now.state === 'none';
  meter.value = now.share;
  if (!view.admin) return;
  const { go, launchHint, stop } = view.refs;
  const running = Boolean(view.status?.running);
  const count = view.plan?.units.length ?? 0;
  go.disabled = running;
  stop.hidden = !running;
  launchHint.textContent = running ? 'A batch is running: wait for it to end, or stop it.' : `${count} unit${count === 1 ? '' : 's'} with a card, plus the reference cheap, on every ticket.`;
  paintEstimate(view);
}

function paintEstimate(view) {
  view.refs.estimate.textContent = estimate(view.plan, parseTickets(view.refs.tickets.value));
}

async function loadCards(view) {
  const { cards, plan } = await get('/api/eval/cards');
  if (ui !== view || (JSON.stringify(cards) === JSON.stringify(view.cards) && JSON.stringify(plan) === JSON.stringify(view.plan))) return;
  view.cards = cards;
  view.plan = plan;
  paintCards(view);
  paintBatch(view);
}

function paintCards(view) {
  const entries = Object.entries(view.cards ?? {});
  const skipped = view.plan?.skipped ?? [];
  const head = h(
    'div',
    { class: 'section-head' },
    h('h2', { id: 'ev-cards-title', text: 'Cards' }),
    h('span', { class: 'muted', text: entries.length === 1 ? '1 unit has sent one' : `${entries.length} units have sent one` }),
  );
  if (entries.length === 0) {
    view.refs.cards.replaceChildren(head, h('p', { class: 'empty-line', text: 'No unit has sent a card yet. Units send theirs from this page by 12:08; a unit without a card is not in the batch.' }));
    return;
  }
  const rows = entries.map(([unit, card]) => {
    const many = changesOf(card).length;
    return h(
      'tr',
      {},
      h('th', { scope: 'row' }, h('code', { text: unit }), ' ', h('span', { class: 'ev-id', text: variantOf(unit) })),
      h('td', {}, summarizeCard(card), many === 1 ? null : [' ', h('span', { class: 'badge warn', text: many === 0 ? 'no change' : `${many} changes` })]),
    );
  });
  view.refs.cards.replaceChildren(
    head,
    h('div', { class: 'table-box' }, h('table', { class: 'table ev-cards' }, h('thead', {}, h('tr', {}, h('th', { scope: 'col', text: 'Unit' }), h('th', { scope: 'col', text: 'Card' }))), h('tbody', {}, rows))),
    ...(skipped.length ? [h('p', { class: 'hint' }, `Left out of a batch launched now: ${skipped.map((one) => `${one.unit} (${one.reason})`).join(', ')}.`)] : []),
  );
}

// --- the leaderboard, polled ---------------------------------------------------------------------------------------------------

function buildBoard(view) {
  const r = view.refs;
  r.boardBody = h('div', {});
  r.mine = h('div', { class: 'box ev-mine', hidden: true, 'aria-live': 'polite' });
  const head = h(
    'div',
    { class: 'section-head' },
    h('h2', { id: 'ev-board-title', text: 'Leaderboard' }),
    h('span', { class: 'spacer' }),
    h('a', { class: 'btn', href: '/leaderboard', target: '_blank', rel: 'noopener', text: 'Open the full leaderboard' }),
  );
  if (view.admin) r.board.replaceChildren(head, r.boardBody);
  else r.board.replaceChildren(head, r.line, r.meter, r.boardBody, r.mine);
}

async function refresh(view) {
  clearTimeout(view.timer);
  try {
    const status = await get('/api/eval/status');
    if (ui !== view) return;
    view.status = status;
    paintBatch(view);
    if (view.admin) await loadCards(view);
    const key = `${status.experiment}:${status.done}:${status.running}`;
    if (key !== view.settled || !view.board) {
      const board = await get('/api/eval/results.json');
      if (ui !== view) return;
      view.board = board;
      view.settled = key;
      paintBoard(view);
    }
    view.failing = false;
  } catch (err) {
    if (ui !== view) return;
    if (!view.failing) fail(view.ctx, err, 'Could not load the eval results');
    view.failing = true;
  }
  if (ui === view) view.timer = setTimeout(() => refresh(view), view.status?.running ? POLL_RUNNING_MS : POLL_IDLE_MS);
}

function paintBoard(view) {
  const rows = (view.rows = boardRows(view.board, view.unitId));
  const body = view.refs.boardBody;
  paintCost(view);
  paintMine(view);
  if (rows.length === 0) {
    const empty = view.personal
      ? 'No results yet. Rows appear here as the runs of an experiment finish.'
      : view.admin
        ? 'No results yet. Rows appear here as the runs of a batch finish.'
        : 'No results yet. The facilitator runs the batch over lunch; rows appear here as its runs finish.';
    body.replaceChildren(h('p', { class: 'empty-line', text: empty }));
    return;
  }
  const columns = [['#', (row) => String(row.rank), 'num'], ['Variant', null, ''], ['Runs', (row) => String(row.runs), 'num'], ...DECIDERS.map(([key, label]) => [label, (row) => cellText(row, key), ''])];
  const head = h('tr', {}, columns.map(([title, , kind]) => h('th', { scope: 'col', class: kind || null, text: title })));
  const lines = rows.map((row) =>
    h(
      'tr',
      { class: [row.mine ? 'mine' : '', row.reference ? 'reference' : ''].join(' ').trim() || null },
      columns.map(([title, cell, kind]) =>
        title === 'Variant'
          ? h(
              'td',
              { class: 'ev-variant' },
              h('span', { text: row.label }),
              ' ',
              h('span', { class: 'ev-id', text: row.reference ? `${row.id}, reference` : row.mine ? `${row.id}, you` : row.id }),
              row.stale ? [' ', h('span', { class: 'badge warn', title: STALE_HINT, text: STALE_TEXT })] : null,
            )
          : h('td', { class: kind || null, text: cell(row) }),
      ),
    ),
  );
  body.replaceChildren(
    h('div', { class: 'table-box' }, h('table', { class: 'table ev-board' }, h('thead', {}, head), h('tbody', {}, lines))),
    h('p', { class: 'hint ev-board-hint' }, 'Cost per pass is the credits spent by all runs of a variant divided by its passing runs.', rows.some((row) => row.stale) ? ` ${STALE_HINT}` : null),
  );
}

/** The unit's hypothesis under the leaderboard, with its row next to cheap in the column that decides it. */
function paintMine(view) {
  const box = view.refs.mine;
  if (!box || view.admin) return;
  const { text, column } = view.hypothesis;
  box.hidden = !text.trim() && !column;
  if (box.hidden) return;
  const compared = compareText(view.rows, column);
  const label = DECIDERS.find(([key]) => key === column)?.[1];
  box.replaceChildren(
    h('h3', { text: 'Your hypothesis' }),
    text.trim() ? h('p', { class: 'ev-quote', text: text.trim() }) : h('p', { class: 'muted', text: 'Not written yet: step 2 of your card.' }),
    h(
      'p',
      { class: compared ? 'ev-compare' : 'muted' },
      compared ?? (label ? `Decided by ${label.toLowerCase()}: your row shows here once the batch has run ${view.variant}.` : 'Pick the column that decides it in step 2 of your card.'),
    ),
  );
}
