// Blueprint view (`#/blueprint`): the Harness Blueprint, thirteen boxes in six parts, filled in section by section with
// what was built for tinystream. The console's port of workshop/blueprint.html (which stays as it is, a page that opens
// without a server), with every behaviour of it: the buttons Kick-off, Section 1, 2, 3, 4a, 4b, 5; each press fills the
// boxes of that section and those before it and lights up the boxes that are new since the step before; keys 0 to 6
// pick a step, the arrows (and Page Up/Down, Space) step, f is full screen (of the Blueprint only); the step is kept in
// the address (`#/blueprint?step=3`; a section's key works too: `?step=section4a`), so a reload keeps it.
//
// Without a step in the address the page opens at the section that is current by the clock: on the day, the last
// section that has started (in a break, the one before it; before 09:00 the kick-off), with the times of the deck's plan
// (workshop/commands.json "agenda", read through GET /api/workshop/commands in personal mode; else guide.js's own); on
// any other day, the section Today is at (guide.js `currentBlock`). Until someone picks a step it follows the clock.
// Everything here is fixed text of this file; nothing from the server goes on the page.

import { get } from '../api.js';
import * as guide from '../guide.js'; // setAgendaTimes is newer than the rest: called only when it is there
import { BLOCKS, currentBlock, onDay, readProgress, timeText, toMinutes } from '../guide.js';
import { h } from '../guideui.js';

const CLOCK_MS = 30000; // how often a page nobody has stepped looks at the clock again

// Steps of the day: the key, the label, and which part each step fills. Step 0 is the empty sheet of the kick-off.
const STEPS = [
  { key: '0', label: 'Kick-off', short: 'Start' },
  { key: '1', label: 'Section 1', short: '1' },
  { key: '2', label: 'Section 2', short: '2' },
  { key: '3', label: 'Section 3', short: '3' },
  { key: '4', label: 'Section 4a', short: '4a' },
  { key: '5', label: 'Section 4b', short: '4b' },
  { key: '6', label: 'Section 5', short: '5' },
];
// The step of each section of the day (guide.js ids): the wrap-up shows the whole Blueprint, as Section 5 does.
const STEP_OF = { kickoff: 0, section1: 1, section2: 2, section3: 3, section4a: 4, section4b: 5, section5: 6, wrapup: 6 };

// The six parts and their boxes, as on the printed sheet. Box numbers follow the sheet, not the order of the day.
// The answers are what we wrote for tinystream (the Blueprint slides of the deck); <b>...</b> is the one markup.
const PARTS = [
  { step: 1, sec: 'Section 1', name: 'Inputs', boxes: [
    { n: 1, name: 'Task spec', q: 'What does done look like, and which check proves it?',
      a: ['A ticket with expected and actual in numbers, and the names of the tests that must pass.'] },
    { n: 2, name: 'Context', q: 'What does the agent read every time?',
      a: ['AGENTS.md on every call, C rules per file type, and PROTOCOL.md as the source of truth.'] },
    { n: 3, name: 'Reusable prompts', q: 'Which instructions repeat?',
      a: ['One prompt per role, and templates for plan, implement, review and retry.'] } ] },
  { step: 2, sec: 'Section 2', name: 'Checks', boxes: [
    { n: 6, name: 'Verification gates', q: 'Which checks with no model in them decide?',
      a: ['Fourteen gates, the ones without a build first, among them compliance, style, comments, diff-policy, build, MISRA subset, unit tests, sanitizers and budgets.'] },
    { n: 7, name: 'Iteration loop', q: 'How many tries, and what does the agent get back?',
      a: ['Three retries. At most six feedback items, each with gate, file, line and message. Then it escalates to a person.'] } ] },
  { step: 3, sec: 'Section 3', name: 'Limits', boxes: [
    { n: 4, name: 'Roles', q: 'Who does what, and what may each one touch?',
      a: ['Planner, implementer, test-writer, reviewer, each with its own tools and write scope.'] },
    { n: 5, name: 'Orchestration', q: 'Which steps, in which order?',
      a: ['Plan, implement, tests, gates, review, commit. A reject goes back twice at most.'] },
    { n: 8, name: 'Guardrails', q: 'What must never be touched or run?',
      a: ['Protected paths, forbidden calls, a shell deny list. Ticket text is data.'] },
    { n: 9, name: 'Compliance and audit', q: 'What is on record afterwards?',
      a: ['Every prompt, tool call, gate result and credit, in the run folder.'] } ] },
  { step: 4, sec: 'Section 4a', name: 'Humans', boxes: [
    { n: 10, name: 'Autonomy', q: 'Which step needs a person?',
      a: ['A person approves the plan and the merge. A flagged ticket stops and waits. Everything else runs alone, with a cap.'] } ] },
  { step: 5, sec: 'Section 4b', name: 'Evidence', boxes: [
    { n: 11, name: 'Evals', q: 'What is the eval set?',
      a: ['Three hard tickets, held-back tests, repeated runs, one leaderboard.'] },
    { n: 12, name: 'Cost', q: 'What may a run cost, and how do we compare?',
      a: ['A cap on every run. Variants are compared by credits per pass, not per run.'] } ] },
  { step: 6, sec: 'Section 5', name: 'Team', boxes: [
    { n: 13, name: 'Team workflow', q: 'Who starts it, who owns it, can a colleague run it?',
      a: ['<b>Trigger:</b> a label on an issue starts the run. A Jira status or a CI job can do the same.',
          '<b>Owner:</b> one named person changes the harness, reads the failures and holds the budget.',
          '<b>Guide:</b> one page: what it does, how to run it, what to expect, what to do when it fails. Tested by a handover.'] } ] },
];
const TOTAL = PARTS.reduce((sum, part) => sum + part.boxes.length, 0);

let ui = null; // the mounted view, null when none

function loadStyles() {
  if (document.getElementById('blueprint-css')) return;
  const link = h('link');
  link.id = 'blueprint-css';
  link.rel = 'stylesheet';
  link.href = '/web/views/blueprint.css';
  document.head.append(link);
}

/** An answer as nodes: its text, with `<b>...</b>` as strong text (the only markup the answers have). */
function rich(text) {
  return text.split(/<b>(.*?)<\/b>/).map((part, index) => (index % 2 ? h('b', '', part) : part));
}

/** The step the address names (`?step=3` or `?step=section4a`), or null. */
export function stepFromParam(value) {
  if (typeof value !== 'string') return null;
  if (/^[0-6]$/.test(value)) return Number(value);
  return Object.hasOwn(STEP_OF, value) ? STEP_OF[value] : null;
}

/**
 * The step that is current at `now`, `{step, why}`: on the day, the last section that has started by the clock (none
 * yet: the kick-off); on another day, the section Today is at (`progress`: guide.js's, of this unit).
 */
export function stepAt(now, progress) {
  if (onDay(now)) {
    const at = now.getHours() * 60 + now.getMinutes();
    const started = BLOCKS.filter((block) => toMinutes(block.start) <= at).sort((a, b) => toMinutes(a.start) - toMinutes(b.start)).at(-1);
    if (!started) return { step: 0, why: 'The day has not started: the empty sheet of the kick-off.' };
    const on = at < toMinutes(started.end);
    const step = STEP_OF[started.id] ?? 0;
    return { step, why: `${started.label} ${on ? 'is on' : 'ended last'} by the clock (${timeText(started)}).` };
  }
  const { block } = currentBlock(now, progress);
  return { step: STEP_OF[block.id] ?? 0, why: `${block.label} is where your Today page is.` };
}

// --- mount and unmount -----------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  loadStyles();
  const asked = stepFromParam(ctx.route.params.step);
  const view = (ui = { ctx, current: -1, chosen: asked !== null, timer: null, boxes: [], parts: [], buttons: [], refs: {} });
  const r = view.refs;
  r.status = h('p', 'bp-status');
  r.status.setAttribute('aria-live', 'polite');
  r.why = h('p', 'bp-why');
  r.full = h('button', 'btn small quiet', 'Full screen');
  r.full.type = 'button';
  r.full.title = 'Full screen (key f)';
  r.full.addEventListener('click', () => fullScreen(view));
  const steps = h('div', 'bp-steps');
  steps.setAttribute('role', 'group');
  steps.setAttribute('aria-label', 'Workshop sections');
  STEPS.forEach((step, index) => {
    const button = h('button', 'bp-step', h('span', 'bp-step-n', step.short), step.label);
    button.type = 'button';
    button.title = `${step.label} (key ${step.key})`;
    button.addEventListener('click', () => pick(view, index));
    steps.append(button);
    view.buttons.push(button);
  });
  const grid = h('div', 'bp-parts');
  for (const part of PARTS) {
    const box = h('section', 'bp-part', h('p', 'bp-sec', part.sec), h('h2', 'bp-part-name', part.name));
    box.setAttribute('aria-label', `${part.name} (${part.sec})`);
    for (const item of part.boxes) {
      const one = h(
        'div',
        'bp-box',
        h('div', 'bp-head', h('span', 'bp-n', String(item.n)), h('span', 'bp-name', item.name)),
        h('p', 'bp-q', item.q),
        ...item.a.map((answer) => h('p', 'bp-a', ...rich(answer))),
      );
      box.append(one);
      view.boxes.push({ step: part.step, el: one });
    }
    grid.append(box);
    view.parts.push({ step: part.step, el: box });
  }
  const keys = h('span', 'bp-keys', 'Keys: ', h('kbd', '', '0'), ' start · ', h('kbd', '', '1'), '–', h('kbd', '', '6'), ' sections · ', h('kbd', '', '←'), ' ', h('kbd', '', '→'), ' step · ', h('kbd', '', 'f'), ' full screen');
  const page = h(
    'div',
    'page blueprint',
    h(
      'header',
      'page-head bp-head-row',
      h('div', 'bp-title', h('h1', '', 'Harness Blueprint'), h('p', 'page-sub', 'Thirteen boxes, six parts: what we built for tinystream, filled in section by section.')),
      h('div', 'bp-state', r.status, r.why),
    ),
    h('div', 'bp-bar', steps, h('span', 'spacer'), r.full),
    grid,
    h('footer', 'bp-foot', keys, h('span', '', 'The blank sheet has the same questions. Fill it in for one task of your own team.')),
  );
  r.page = page;
  el.scrollTop = 0;
  el.replaceChildren(page);

  view.onKey = (event) => onKey(view, event);
  document.addEventListener('keydown', view.onKey);
  if (view.chosen) go(view, asked);
  else follow(view, true);
  view.timer = setInterval(() => follow(view, false), CLOCK_MS);

  // the deck's times (personal mode): the clock's section can be another one with them
  try {
    const data = await get('/api/workshop/commands');
    if (ui !== view) return;
    guide.setAgendaTimes?.(data?.agenda);
    follow(view, false);
  } catch {
    /* the room's console has no commands: guide.js's own times */
  }
}

export function unmount() {
  const view = ui;
  if (!view) return;
  ui = null;
  clearInterval(view.timer);
  document.removeEventListener('keydown', view.onKey);
  if (document.fullscreenElement === view.refs.page) document.exitFullscreen().catch(() => {});
}

// --- steps -------------------------------------------------------------------------------------------------------------------

/** Until someone picks a step, the page shows the clock's; `first` draws it without lighting up boxes as new. */
function follow(view, first) {
  if (view.chosen || ui !== view) return;
  const { step, why } = stepAt(new Date(), readProgress(view.ctx.me.unit?.id ?? view.ctx.me.subject));
  view.refs.why.textContent = why;
  if (first || step !== view.current) go(view, step, first);
}

/** A person picked step `index` (a button, a key): kept in the address, and the clock no longer moves it. */
function pick(view, index) {
  view.chosen = true;
  view.refs.why.textContent = '';
  go(view, index);
  history.replaceState(null, '', `#/blueprint?step=${view.current}`); // no new route: the page stays as it is
}

function go(view, index, fresh = false) {
  const i = Math.max(0, Math.min(STEPS.length - 1, index));
  const prev = fresh ? -1 : view.current;
  view.current = i;
  view.buttons.forEach((button, k) => {
    button.classList.toggle('done', k > 0 && k <= i);
    button.classList.toggle('current', k === i);
    button.setAttribute('aria-pressed', k === i ? 'true' : 'false');
  });
  for (const { step, el } of view.boxes) {
    el.classList.toggle('filled', step <= i);
    el.classList.toggle('new', prev !== -1 && step > prev && step <= i);
  }
  for (const { step, el } of view.parts) el.classList.toggle('reached', step <= i);
  const filled = view.boxes.filter((box) => box.step <= i).length;
  const count = h('b', '', `${filled} of ${TOTAL}`);
  if (i === 0) view.refs.status.replaceChildren('Empty sheet: ', count, ' boxes');
  else if (i === STEPS.length - 1) view.refs.status.replaceChildren('The whole harness: ', count, ' boxes');
  else view.refs.status.replaceChildren(`After ${STEPS[i].label}: `, count, ' boxes');
}

// --- keys and full screen ------------------------------------------------------------------------------------------------

const TYPING = 'input, textarea, select, [contenteditable=""], [contenteditable="true"]';
const CONTROL = 'button, a[href], summary';

function onKey(view, event) {
  if (ui !== view || event.defaultPrevented || event.altKey || event.ctrlKey || event.metaKey) return;
  const target = event.target instanceof Element ? event.target : null;
  if (target?.closest(TYPING)) return; // typing goes where it is typed
  const k = STEPS.findIndex((step) => step.key === event.key);
  if (k >= 0) {
    pick(view, k);
    return;
  }
  if (event.key === 'ArrowRight' || event.key === 'PageDown' || (event.key === ' ' && !target?.closest(CONTROL))) {
    event.preventDefault();
    pick(view, view.current + 1);
  } else if (event.key === 'ArrowLeft' || event.key === 'PageUp') {
    event.preventDefault();
    pick(view, view.current - 1);
  } else if (event.key === 'f') {
    fullScreen(view);
  }
}

function fullScreen(view) {
  if (document.fullscreenElement) document.exitFullscreen().catch(() => {});
  else view.refs.page.requestFullscreen?.().catch(() => {});
}

export { PARTS, STEPS, STEP_OF };
