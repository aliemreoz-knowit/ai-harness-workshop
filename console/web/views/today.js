// Today (`#/today`): the guided page, on one screen. At the top the title, "Your setup" and the level track (L0 to L5,
// filled up to the section on show; with several units, a marker at the unit's own level). Under it the day as a row of
// tabs, one per section (the breaks between them), and under the tabs the section on show: what the stage shows,
// what the section teaches, and its steps as a checklist. A step shows its title and why; the next step that is not
// done is open with where, how, a link there and "Done when", and any other opens with a click.
//
// Which section is on show comes from the clock on the day (web/guide.js); a click on a tab pins one by hand, and
// "Follow the clock" lets go again. Ticks, the pin and the step strip's "hide for this section" are kept in the browser
// per unit (guide.js). Everything is put on the page as text.
//
// Personal mode (`me.personal`): "Your setup" next to the title, what the harness doctor says of this laptop (GET
// /api/setup: the depth, "Try (fake model)" ..., and the gate ladder). The doctor runs in the background: the line says
// "Checking your setup…" and the page asks again every few seconds, without holding anything else; "Check again" (POST
// /api/setup) runs it now.
//
// Personal mode, the day's commands (GET /api/workshop/commands: <checkout>/workshop/commands.json; no part when there
// is none): the section's commands on the stage, in order, with Run (POST /api/workshop/commands/<id>/run; never for an
// interactive one, which goes in a terminal) and Copy. A command that ran has its own state chip and output pane (the
// command line, then its output; open while it runs and after it ends, collapsible), and Stop while it runs. Several may
// run at once (a long experiment in the background); the card's head says how many run, whatever section is on show.
// The page asks for the jobs every 500 ms while one runs (GET /api/workshop/jobs, then /api/workshop/jobs/<id> for the
// output of an open pane), every few seconds otherwise.
//
// On the day nothing runs live on the stage: the presenter shows a command, then its output as it was recorded
// beforehand. A command with `recorded` has "Show recorded result" under it: it reads the file (GET
// /api/workshop/commands/<id>/recorded, at every opening) and shows it in the same output block as a run, with "Recorded
// <when> · took <how long> · <n> credits" above it. Run stays, for whoever wants to run it themselves.

import { get, post } from '../api.js';
import { recordedCut, recordedLine, setupSummary } from '../runmodel.js';
import { parseRunLog } from '../runlog.js';
import { renderRunLog } from '../runlogview.js';
import {
  AGENDA,
  DAY,
  LEVELS,
  blockProgress,
  currentBlock,
  fill,
  items,
  nextStep,
  progressText,
  readProgress,
  setAgendaTimes,
  timeText,
  writeProgress,
} from '../guide.js';
import { actionLink, h, icon, whereNode } from '../guideui.js';

const WHY = { clock: 'now', break: 'after the break', pinned: 'pinned', progress: 'up next' };
const SETUP_POLL_MS = 3000; // how often the page asks again while the doctor checks the setup
const JOBS_POLL_MS = 500; // how often the page asks for the commands' jobs while one runs
const JOBS_IDLE_MS = 4000; // and while none runs (one started in another tab shows up)
const STATE_TEXT = { running: 'running', done: 'done', stopped: 'stopped' }; // failed: "exit N"
let rawLog = false; // a terminal that shows a harness run or an experiment: as printed (Raw) instead of Readable
const shownStep = new Map(); // section id -> the step the person opened ('' = all closed); unset = the next one not done
let ui = null; // the mounted view, null when none

export function mount(el, ctx) {
  const unitKey = ctx.me.unit?.id ?? ctx.me.subject;
  const view = (ui = { ctx, el, unitKey, unitId: ctx.me.unit?.id ?? null, shown: '', timer: null, setup: null, cmds: null });
  el.scrollTop = 0;
  if (ctx.me.personal) {
    view.setup = setupSection(view);
    view.cmds = commandsState();
  }
  paint(view);
  if (view.setup) readSetup(view, get);
  if (view.cmds) readCommands(view);
  // the clock moves the day on: draw again when the section on now changes (not on every tick: focus and scroll stay)
  view.timer = setInterval(() => {
    if (ui === view && stateOf(view) !== view.shown) paint(view);
  }, 30000);
}

export function unmount() {
  clearInterval(ui?.timer);
  clearTimeout(ui?.setup?.timer);
  clearTimeout(ui?.cmds?.timer);
  ui = null;
}

const progressOf = (view) => readProgress(view.unitKey);

function stateOf(view) {
  const { block, why } = currentBlock(new Date(), progressOf(view));
  return `${block.id}:${why}`;
}

/** Keep `progress`, draw the page again (the focus goes back where it was) and tell the shell. */
function change(view, progress, focusId = document.activeElement?.id) {
  writeProgress(view.unitKey, progress);
  paint(view, focusId);
  view.ctx.guideChanged?.();
}

// --- the page -------------------------------------------------------------------------------------------------------

function paint(view, focusId = null) {
  const progress = progressOf(view);
  const { block, why } = currentBlock(new Date(), progress);
  view.shown = `${block.id}:${why}`;
  const page = h(
    'div',
    'today',
    h(
      'div',
      'today-top',
      h(
        'header',
        'page-head today-head',
        h('h1', '', 'Today'),
        h('p', 'page-sub', h('span', 'today-date', `${DAY.label}, 09:00–15:00. `), 'Section by section: what was wrong, what the harness gained and why, how it was tested, and what that showed.'),
      ),
      view.setup?.node ?? null, // the same element on every draw: its state stays
    ),
    view.ctx.me.personal ? null : levelTrack(block.level, view.ctx.me.unit?.level ?? null), // personal: the rail carries the levels
    h('div', 'today-main', dayRail(view, block, progress), currentCard(view, block, why, progress)),
  );
  view.el.replaceChildren(page);
  if (focusId) document.getElementById(focusId)?.focus();
}

// --- your setup (personal mode) -----------------------------------------------------------------------------------------

function setupSection(view) {
  const title = h('h2', 'setup-title', 'Your setup');
  title.id = 'today-setup';
  const text = h('p', 'setup-text', 'Checking your setup…');
  text.setAttribute('role', 'status');
  const reason = h('p', 'setup-reason');
  reason.hidden = true;
  const again = h('button', 'btn small quiet', 'Check again');
  again.type = 'button';
  again.disabled = true;
  again.addEventListener('click', () => readSetup(view, (path) => post(path)));
  const node = h('section', 'setup-line', h('span', 'setup-icon', icon('laptop')), h('div', 'setup-body', h('div', 'setup-head', title, again), text, reason));
  node.setAttribute('aria-labelledby', title.id);
  return { node, text, reason, again, timer: null };
}

/** Ask the console for the setup (`ask`: GET, or POST for "Check again"); while the doctor checks, ask again soon. */
async function readSetup(view, ask) {
  const box = view.setup;
  clearTimeout(box.timer);
  let setup;
  try {
    setup = await ask('/api/setup');
  } catch (err) {
    if (ui !== view || err.status === 401) return;
    setup = { state: 'error', depth: '', ladder: '', reason: `The console did not answer: ${err.message}` };
  }
  if (ui !== view) return;
  const shown = setupSummary(setup);
  const fine = setup?.state === 'ready' && Boolean(setup.depth);
  box.text.textContent = shown.text;
  box.reason.textContent = shown.reason;
  box.reason.hidden = !shown.reason || fine; // when all is well the line says it; the sentence is its tooltip
  box.node.title = fine ? shown.reason : '';
  box.again.disabled = shown.busy;
  if (shown.busy) box.timer = setTimeout(() => readSetup(view, get), SETUP_POLL_MS);
}

// --- the level track: the one bold element of the page -----------------------------------------------------------------

function levelTrack(blockLevel, unitLevel) {
  const reached = blockLevel ?? LEVELS.length - 1; // the wrap-up has no level of its own: the whole track
  const list = h('ol', 'levels');
  list.setAttribute('aria-label', 'Harness levels');
  for (const { level, name, adds } of LEVELS) {
    const item = h('li', `lv${level <= reached ? ' reached' : ''}${level === blockLevel ? ' now' : ''}`);
    item.title = `L${level} ${name}: ${adds}`;
    const head = h('span', 'lv-head', h('span', 'lv-n', `L${level}`), h('span', 'lv-name', name));
    const states = [level === blockLevel ? 'this section' : '', level === unitLevel ? 'your unit' : ''].filter(Boolean);
    item.append(h('span', 'lv-bar'), head);
    if (states.length) item.append(h('span', 'sr-only', `(${states.join(', ')})`));
    if (level === unitLevel) {
      const mark = h('span', 'lv-unit', 'Your unit');
      mark.setAttribute('aria-hidden', 'true');
      item.append(mark);
    }
    list.append(item);
  }
  const box = h('section', 'level-track', list);
  if (unitLevel !== null) {
    const current = LEVELS.find((one) => one.level === blockLevel);
    const caption = h('p', 'lv-caption');
    if (current) caption.append(h('strong', '', `This section builds L${current.level}, ${current.name}.`), ` ${current.adds}`);
    else caption.append(h('strong', '', 'The wrap-up looks back at every level.'));
    caption.append(` Your unit is at L${unitLevel}.`);
    box.append(caption);
  }
  box.setAttribute('aria-label', 'Harness levels');
  return box;
}

// --- the day, as a row of tabs --------------------------------------------------------------------------------------------------

/** The icon of a section in its tile. */
const tile = (block, className = 'sec-tile') => h('span', className, icon(block.icon ?? 'doc'));

function dayRail(view, current, progress) {
  const list = h('ol', 'day-list');
  for (const entry of AGENDA) {
    if (entry.kind === 'break') {
      const row = h('li', 'day-row break', h('span', 'sec-tile small quiet', icon('cup')), h('span', 'day-name', h('span', 'label', entry.label), h('span', 'day-time', timeText(entry))));
      row.title = entry.note;
      list.append(row);
      continue;
    }
    const state = blockProgress(entry, progress.checked);
    const on = entry.id === current.id;
    const toggle = h(
      'button',
      `day-toggle${on ? ' is-current' : ''}`,
      tile(entry, 'sec-tile small'),
      h('span', 'day-name', h('span', 'label', entry.label), h('span', 'day-time', timeText(entry)), h('span', 'day-title', entry.title)),
      Number.isInteger(entry.level) ? h('span', `day-level${entry.level <= (current.level ?? 5) ? ' reached' : ''}`, `L${entry.level}`) : h('span', 'day-level', ''),
      h('span', `day-state${state.complete && state.total ? ' complete' : ''}`, state.total ? (state.complete ? '✓' : `${state.done}/${state.total}`) : ''),
    );
    toggle.type = 'button';
    toggle.classList.add('with-level');
    toggle.id = `row-${entry.id}`;
    toggle.title = `${entry.label} · ${entry.title}: ${progressText(state)}`;
    if (on) toggle.setAttribute('aria-current', 'true');
    toggle.addEventListener('click', () => {
      if (!on) change(view, { ...progress, pinned: entry.id }, `row-${entry.id}`);
    });
    list.append(h('li', '', toggle));
  }
  const title = h('h2', 'rail-title sr-only', 'The day');
  title.id = 'today-day';
  const box = h('nav', 'day day-tabs', title, list);
  box.setAttribute('aria-labelledby', title.id);
  return box;
}

// --- the section on show -------------------------------------------------------------------------------------------------

function currentCard(view, block, why, progress) {
  const title = h('h2', 'block-title', block.title);
  title.id = 'today-current';
  title.tabIndex = -1;
  const state = blockProgress(block, progress.checked);
  const level = LEVELS.find((one) => one.level === block.level);
  const meta = h(
    'p',
    'block-meta',
    h('span', 'block-label', block.label),
    h('span', 'mono', timeText(block)),
    level ? h('span', 'chip', `L${level.level} · ${level.name}`) : null,
    h('span', `chip${why === 'clock' ? ' chip-now' : ''}`, WHY[why]),
  );
  const side = h('div', 'block-side');
  if (block.steps.length) side.append(h('span', 'block-progress', meter(state), progressText(state)));
  if (why === 'pinned') {
    const follow = h('button', 'btn small quiet', 'Follow the clock');
    follow.type = 'button';
    follow.id = 'today-follow';
    follow.title = 'This section is pinned by hand. Let the clock (or the next step that is not done) choose again.';
    follow.addEventListener('click', () => change(view, { ...progress, pinned: null }, 'today-current'));
    side.append(follow);
  }
  if (progress.hidden.has(block.id) && block.steps.length) {
    const show = h('button', 'btn small quiet', 'Show the next step on every page');
    show.type = 'button';
    show.id = 'today-show-strip';
    show.addEventListener('click', () => {
      const hidden = new Set(progress.hidden);
      hidden.delete(block.id);
      change(view, { ...progress, hidden }, 'today-current');
    });
    side.append(show);
  }
  if (view.cmds?.sections) side.prepend(view.cmds.badge); // how many commands run, whatever section is on show
  const card = h('section', 'block-card', h('div', 'block-head', tile(block, 'sec-tile big'), h('div', 'block-head-text', meta, title), side));
  card.setAttribute('aria-labelledby', title.id);
  card.append(...blockBody(view, block, progress, 'In this console'));
  return card;
}

/** How far a section's steps are: a thin bar. */
function meter({ done, total }) {
  const bar = h('span', 'step-meter', h('span', 'step-meter-fill'));
  bar.firstChild.style.width = `${total ? Math.round((done / total) * 100) : 0}%`;
  bar.setAttribute('aria-hidden', 'true');
  return bar;
}

/** The section's story, then the commands of the stage, the steps of this console, and last what the section
 * teaches. */
function blockBody(view, block, progress, heading) {
  const story = storyPart(view, block);
  const ideas = block.ideas?.length
    ? h(
        'section',
        'ideas-box ideas-shown',
        h('h3', 'part-title', icon('bulb'), 'What this section teaches'),
        h('ul', 'ideas', ...block.ideas.map((idea) => h('li', 'idea', h('p', 'idea-title', idea.title), h('p', 'idea-text', idea.text)))),
      )
    : null;
  const commands = commandsPart(view, block);
  if (!block.steps.length) {
    return [story, h('div', `block-parts one${commands ? ' with-commands' : ''}`, commands, h('p', 'block-note', block.note ?? '')), ideas].filter(Boolean);
  }
  const open = shownStep.has(block.id) ? shownStep.get(block.id) : (nextStep(block, progress.checked)?.step.id ?? '');
  const list = h('ol', 'checklist');
  for (const item of items(block)) {
    if (!item.pick) {
      list.append(h('li', '', checkItem(view, block, item.steps[0], progress, open)));
      continue;
    }
    const options = h('ol', 'checklist');
    item.steps.forEach((step, index) => options.append(h('li', '', checkItem(view, block, step, progress, open, String.fromCharCode(65 + index)))));
    list.append(h('li', 'check-group', h('p', 'check-group-title', `Pick one of ${item.steps.length}`), options));
  }
  const steps = h('div', 'steps-box', h('h3', 'part-title steps-title', icon('check'), heading), list);
  return [story, h('div', `block-parts${commands ? ' with-commands' : ' one'}`, commands, steps), ideas].filter(Boolean);
}

/** The section told as a change: what was wrong, what the harness gained (each part linked to where it lives in this
 * console) and why, how it was tested, what that showed; under it the problem the next section takes on. */
function storyPart(view, block) {
  const st = block.story;
  if (!st) return null;
  const step = (n, label, cls, ...body) => h('li', `story-step ${cls}`, h('p', 'story-label', h('span', 'story-n', String(n)), label), ...body);
  const added = st.added?.length
    ? h(
        'ul',
        'story-added',
        ...st.added.map((part) =>
          h('li', '', h('p', 'story-part', part.name), h('p', 'story-why', part.why), part.action ? actionLink(part.action, view.unitId, 'btn small quiet story-link') : null),
        ),
      )
    : h('p', 'story-text', st.none ?? '');
  const list = h(
    'ol',
    'story',
    step(1, 'What was wrong', 'story-before', h('p', 'story-text', st.before?.text ?? '')),
    step(2, 'What we added, and why', 'story-add', added),
    step(3, 'How we test it', 'story-test', h('p', 'story-text', st.test ?? '')),
    step(4, 'What it showed', 'story-after', h('p', 'story-text', st.after ?? '')),
  );
  const title = h('h3', 'part-title', icon('flag'), 'The story of this section');
  title.id = `story-${block.id}`;
  const box = h('section', 'story-box', title, list, st.next ? h('p', 'story-next', h('strong', '', 'Next problem: '), st.next) : null);
  box.setAttribute('aria-labelledby', title.id);
  return box;
}

function checkItem(view, block, step, progress, open, letter = '') {
  const done = progress.checked.has(step.id);
  const isOpen = open === step.id;
  const box = h('input', 'check-box');
  box.type = 'checkbox';
  box.id = `step-${step.id}`;
  box.checked = done;
  box.addEventListener('change', () => {
    const checked = new Set(progress.checked);
    if (box.checked) checked.add(step.id);
    else checked.delete(step.id);
    shownStep.delete(block.id); // the next step that is not done opens
    change(view, { ...progress, checked }, box.id);
  });
  const title = h('label', 'check-title', letter ? h('span', 'check-letter', `${letter}. `) : null, fill(step.title, view.unitId));
  title.htmlFor = box.id;
  const more = h('button', 'btn small quiet icon check-more', icon(isOpen ? 'down' : 'right'));
  more.type = 'button';
  more.id = `more-${step.id}`;
  more.setAttribute('aria-expanded', String(isOpen));
  more.setAttribute('aria-controls', `how-${step.id}`);
  more.setAttribute('aria-label', `${isOpen ? 'Hide' : 'Show'} how to do "${fill(step.title, view.unitId)}"`);
  more.addEventListener('click', () => {
    shownStep.set(block.id, isOpen ? '' : step.id);
    paint(view, more.id);
  });
  const actions = h('div', 'check-actions', step.action ? actionLink(step.action, view.unitId, 'btn small') : null);
  if (isOpen && step.also) actions.append(actionLink(step.also, view.unitId, 'btn small quiet'));
  actions.append(more);
  const detail = h(
    'div',
    'check-detail',
    whereNode(step.where, view.unitId),
    h('p', 'check-how', fill(step.how, view.unitId)),
    h('p', 'check-done', h('strong', '', 'Done when: '), fill(step.done, view.unitId)),
  );
  detail.id = `how-${step.id}`;
  detail.hidden = !isOpen;
  return h(
    'div',
    `check-item${done ? ' done' : ''}${isOpen ? ' open' : ''}`,
    box,
    h('div', 'check-body', title, step.why ? h('p', 'check-why', fill(step.why, view.unitId)) : null, detail),
    actions,
  );
}

// --- the day's commands (personal mode) ---------------------------------------------------------------------------------

function commandsState() {
  const badge = h('button', 'btn small quiet cmd-badge');
  badge.type = 'button';
  badge.id = 'today-running';
  badge.hidden = true;
  return {
    sections: null, // section id -> its commands; null until read (and when there is no commands file)
    jobs: new Map(), // command id -> its latest job
    outputs: new Map(), // job id -> { state, text }: its output as last read, and the state it had then
    chosen: new Map(), // section id -> the command whose tab the person picked
    errors: new Map(), // command id -> what a Run or Stop got back
    part: null, // { blockId, node, rows: Map command id -> row, selected }: the commands of the section on show
    badge, // "2 commands running", in the card's head
    timer: null,
  };
}

/** Read the day's commands once; nothing on the page when there are none. */
async function readCommands(view) {
  let data;
  try {
    data = await get('/api/workshop/commands');
  } catch {
    return; // no commands part
  }
  if (ui !== view) return;
  const sections = data?.sections && typeof data.sections === 'object' ? data.sections : {};
  if (!Object.values(sections).some((list) => Array.isArray(list) && list.length)) return;
  view.cmds.sections = sections;
  setAgendaTimes(data?.agenda); // the deck's plan is the clock
  view.ctx.guideChanged?.();
  view.cmds.badge.addEventListener('click', () => {
    const running = [...view.cmds.jobs.values()].find((job) => job.state === 'running');
    const section = running ? sectionOf(view, running.command_id) : null;
    const progress = progressOf(view);
    if (section && section !== currentBlock(new Date(), progress).block.id) change(view, { ...progress, pinned: section }, 'today-running');
  });
  paint(view, document.activeElement?.id);
  pollJobs(view);
}

const commandsOf = (view, blockId) => (Array.isArray(view.cmds?.sections?.[blockId]) ? view.cmds.sections[blockId] : []);

function sectionOf(view, commandId) {
  for (const [id, list] of Object.entries(view.cmds.sections ?? {})) {
    if (Array.isArray(list) && list.some((one) => one.id === commandId)) return id;
  }
  return null;
}

/** The section's commands as a part of the card (the same element while the section stays on show), or null: a row
 * of tabs, one per command, and under it the command on show in a terminal window. */
function commandsPart(view, block) {
  const cmds = view.cmds;
  const list = cmds?.sections ? commandsOf(view, block.id) : [];
  if (!list.length) return null;
  if (cmds.part?.blockId === block.id) return cmds.part.node;
  const rows = new Map();
  const tabs = h('div', 'cmd-tabs');
  tabs.setAttribute('role', 'tablist');
  tabs.setAttribute('aria-label', 'Commands on the stage');
  const panels = h('div', 'cmd-panels');
  list.forEach((command, index) => {
    const row = commandRow(view, command, index);
    rows.set(command.id, row);
    tabs.append(row.tab);
    panels.append(row.panel);
    row.tab.addEventListener('click', () => selectCommand(view, command.id));
  });
  tabs.addEventListener('keydown', (event) => {
    if (event.key !== 'ArrowRight' && event.key !== 'ArrowLeft') return;
    const ids = [...rows.keys()];
    const at = ids.indexOf(cmds.part.selected);
    const next = ids[(at + (event.key === 'ArrowRight' ? 1 : ids.length - 1)) % ids.length];
    selectCommand(view, next);
    rows.get(next).tab.focus();
    event.preventDefault();
  });
  const title = h('h3', 'part-title', icon('code'), 'Commands on the stage');
  title.id = 'today-commands';
  const where = h(
    'p',
    'cmds-where',
    'What the presenter runs in this section, in order. Run works in this console’s copy, the same files as Code and Runs; Recorded shows what the command printed when we recorded the day.',
  );
  const node = h('div', 'cmds-box', title, where, tabs, panels);
  node.setAttribute('role', 'region');
  node.setAttribute('aria-labelledby', title.id);
  const running = list.find((command) => cmds.jobs.get(command.id)?.state === 'running');
  cmds.part = { blockId: block.id, node, rows, selected: (cmds.chosen.get(block.id) ?? running?.id ?? list[0].id) };
  if (!rows.has(cmds.part.selected)) cmds.part.selected = list[0].id;
  for (const row of rows.values()) drawRow(view, row);
  selectCommand(view, cmds.part.selected);
  return node;
}

/** Show one command of the section on show: its tab selected, its panel the only one visible. */
function selectCommand(view, id) {
  const part = view.cmds.part;
  if (!part?.rows.has(id)) return;
  part.selected = id;
  view.cmds.chosen.set(part.blockId, id);
  for (const [one, row] of part.rows) {
    const on = one === id;
    row.tab.classList.toggle('is-current', on);
    row.tab.setAttribute('aria-selected', String(on));
    row.tab.tabIndex = on ? 0 : -1;
    row.panel.hidden = !on;
  }
  pollJobs(view); // the command on show gets its output now
}

function commandRow(view, command, index) {
  const label = command.title || command.slide_title || command.id;
  const dot = h('span', 'cmd-tab-dot');
  const tab = h('button', 'cmd-tab', h('span', 'cmd-tab-num', String(index + 1)), h('span', 'cmd-tab-title', label), dot);
  tab.type = 'button';
  tab.id = `cmd-tab-${command.id}`;
  tab.setAttribute('role', 'tab');
  tab.setAttribute('aria-controls', `cmd-panel-${command.id}`);
  const slide = Number.isInteger(command.slide) ? `slide ${command.slide}` : '';
  tab.title = [command.id, slide, command.at ?? ''].filter(Boolean).join(' · ');

  const chip = h('span', 'chip cmd-state');
  chip.hidden = true;
  const run = h('button', 'btn small primary', 'Run');
  run.type = 'button';
  run.id = `cmd-run-${command.id}`;
  run.addEventListener('click', () => runCommand(view, command));
  const stop = h('button', 'btn small danger', 'Stop');
  stop.type = 'button';
  stop.id = `cmd-stop-${command.id}`;
  stop.hidden = true;
  stop.addEventListener('click', () => stopCommand(view, command));
  const line = h('code', 'cmd-line', command.cmd);
  const copy = h('button', 'btn small', 'Copy');
  copy.type = 'button';
  copy.addEventListener('click', () => copyCommand(command.cmd, line, copy));
  const recorded = command.recorded ? h('button', 'btn small quiet', 'Recorded') : null;
  if (recorded) {
    recorded.type = 'button';
    recorded.id = `cmd-rec-${command.id}`;
    recorded.setAttribute('aria-pressed', 'false');
  }
  const readableBtn = h('button', 'view-switch-btn', 'Readable');
  const rawBtn = h('button', 'view-switch-btn', 'Raw');
  readableBtn.type = rawBtn.type = 'button';
  readableBtn.addEventListener('click', () => setRawLog(view, false));
  rawBtn.addEventListener('click', () => setRawLog(view, true));
  const switcher = h('span', 'view-switch', readableBtn, rawBtn);
  switcher.setAttribute('role', 'group');
  switcher.setAttribute('aria-label', 'Show the output');
  switcher.hidden = true;
  // started on another page (Tickets: "Run this ticket"): no Run here, a second start would be a second run
  let start = run;
  if (command.interactive) start = h('span', 'chip', 'in a terminal');
  else if (command.started_from) {
    const open = h('a', 'btn small', 'Open Runs');
    open.href = '#/runs';
    const from = h('span', 'chip cmd-from', `Started from ${command.started_from}`);
    from.title = 'Start it there; this tab shows what it printed when we recorded the day.';
    start = h('span', 'cmd-from-box', from, open);
  }

  const what = h('p', 'cmd-what', command.what || label);
  const why = command.why ? h('p', 'cmd-why', h('span', 'cmd-why-label', 'Why: '), command.why) : null;
  const where = command.interactive ? 'a workshop terminal (workshop\\shell)' : 'the console’s copy';
  const bar = h(
    'div',
    'term-bar',
    h('span', 'term-dots', h('span', ''), h('span', ''), h('span', '')),
    h('span', 'term-name', where),
    h('div', 'cmd-actions', switcher, chip, start, stop, recorded, copy),
  );
  const meta = h('p', 'term-meta');
  meta.hidden = true;
  const output = h('pre', 'cmd-output');
  output.tabIndex = 0;
  output.setAttribute('aria-label', `Output of ${command.id}`);
  const readable = h('div', 'cmd-readable');
  readable.tabIndex = 0;
  readable.setAttribute('aria-label', `Output of ${command.id}, readable`);
  readable.hidden = true;
  const body = h('div', 'term-body', meta, h('p', 'cmd-prompt', h('span', 'term-sign', '$ '), line), output, readable);
  const terminal = h('div', 'term', bar, body);
  const expect = command.expect ? h('p', 'cmd-expect', h('span', 'cmd-expect-label', 'You should see: '), command.expect) : null;
  const error = h('p', 'cmd-error');
  error.hidden = true;
  error.setAttribute('role', 'status');
  const chat = Array.isArray(command.chat) && command.chat.length ? chatBox(command.chat) : null;
  const panel = h('div', `cmd-panel${command.interactive ? ' interactive' : ''}`, h('div', 'cmd-info', what, why), terminal, chat, expect, error);
  panel.id = `cmd-panel-${command.id}`;
  panel.setAttribute('role', 'tabpanel');
  panel.setAttribute('aria-labelledby', tab.id);
  const row = {
    command, tab, dot, panel, chip, run, stop, recorded, meta, error, output, readable, switcher, readableBtn, rawBtn,
    mode: 'live', round: 0, shownJob: null, shownText: null, text: '',
  };
  if (recorded) recorded.addEventListener('click', () => (row.mode === 'recorded' ? showLive(view, row) : showRecorded(view, row)));
  return row;
}

/** What to type into a chat command, one message after the other, each with its own Copy. */
function chatBox(messages) {
  return h(
    'div',
    'cmd-chat',
    h('p', 'cmd-chat-label', 'Type into the chat, one message at a time:'),
    ...messages.map((text, index) => {
      const words = h('p', 'cmd-chat-text', text);
      const copy = h('button', 'btn small', 'Copy');
      copy.type = 'button';
      copy.addEventListener('click', () => copyCommand(text, words, copy));
      return h('div', 'cmd-chat-msg', h('span', 'cmd-chat-num', String(index + 1)), words, copy);
    }),
  );
}

/** The terminal shows what the command printed when the day was recorded (read again: it may have been recorded anew). */
async function showRecorded(view, row) {
  const round = ++row.round;
  row.mode = 'recorded';
  row.recorded?.setAttribute('aria-pressed', 'true');
  row.meta.hidden = false;
  row.meta.textContent = `# ${recordedLine(row.command)}`;
  placeholder(row, 'Loading the recorded result…');
  let result;
  try {
    result = await get(`/api/workshop/commands/${encodeURIComponent(row.command.id)}/recorded`);
  } catch (err) {
    if (ui !== view || round !== row.round) return;
    placeholder(row, `Not shown: ${err.message}`);
    return;
  }
  if (ui !== view || round !== row.round || row.mode !== 'recorded') return;
  let text = typeof result?.text === 'string' ? result.text : '';
  const first = `$ ${row.command.cmd}`;
  if (text.startsWith(first)) text = text.slice(first.length).replace(/^\r?\n/, ''); // the prompt line is above it already
  row.meta.textContent = `# ${[recordedLine(row.command), recordedCut(result)].filter(Boolean).join(' · ')}`;
  if (text) fillOutput(row, text);
  else placeholder(row, '(no output)');
  shownBox(row).scrollTop = 0;
}

/** A run log (runlog.js) in the terminal: Readable (drawn) unless Raw is chosen; any other output as printed. */
function fillOutput(row, text) {
  row.text = text;
  const parsed = parseRunLog(text);
  const readable = Boolean(parsed) && !rawLog;
  row.switcher.hidden = !parsed;
  row.readableBtn.setAttribute('aria-pressed', String(!rawLog));
  row.rawBtn.setAttribute('aria-pressed', String(rawLog));
  row.output.hidden = readable;
  row.readable.hidden = !readable;
  row.output.classList.remove('is-empty');
  if (readable) row.readable.replaceChildren(renderRunLog(parsed));
  else row.output.textContent = text;
}

/** A line in place of output ("Press Run ...", "Waiting for output…"). */
function placeholder(row, words) {
  row.text = '';
  row.switcher.hidden = true;
  row.readable.hidden = true;
  row.output.hidden = false;
  row.output.textContent = words;
  row.output.classList.add('is-empty');
}

const shownBox = (row) => (row.readable.hidden ? row.output : row.readable);

/** Readable or Raw, for every terminal of the section on show (the choice stays while the page is open). */
function setRawLog(view, raw) {
  rawLog = raw;
  for (const row of view.cmds.part?.rows.values() ?? []) {
    if (row.text) fillOutput(row, row.text);
  }
}

/** Back to the live output (or the empty prompt when the command has not run). */
function showLive(view, row) {
  row.round += 1;
  row.mode = 'live';
  row.recorded?.setAttribute('aria-pressed', 'false');
  row.meta.hidden = true;
  row.shownJob = null;
  drawRow(view, row);
}

/** Put a command's latest job on its tab and in its terminal: the state, the buttons and the output. */
function drawRow(view, row) {
  const cmds = view.cmds;
  const id = row.command.id;
  const job = cmds.jobs.get(id) ?? null;
  const running = job?.state === 'running';
  row.chip.hidden = !job;
  row.dot.className = `cmd-tab-dot${job ? ` is-${job.state}` : ''}`;
  if (job) {
    row.chip.textContent = job.state === 'failed' ? `exit ${job.exit_code ?? '?'}` : (STATE_TEXT[job.state] ?? job.state);
    row.chip.className = `chip cmd-state is-${job.state}`;
  }
  row.run.disabled = running;
  row.stop.hidden = !running;
  const error = cmds.errors.get(id) ?? '';
  row.error.textContent = error;
  row.error.hidden = !error;
  if (row.mode !== 'live') return;
  if (!job) {
    if (row.shownJob === 'none') return;
    placeholder(row, row.command.interactive
      ? 'This one asks a question: copy it and run it in a workshop terminal. Open one in the repository root with workshop\\shell (Windows), or source workshop/stage-shell.sh in Git Bash, macOS or Linux.'
      : row.command.started_from
        ? `Start it from ${row.command.started_from}, or press Recorded to see what it printed when we recorded the day.`
        : row.command.recorded
          ? 'Press Run to start it here, or Recorded to see what it printed when we recorded the day.'
          : 'Press Run to start it here.');
    row.shownJob = 'none';
    row.shownText = null;
    return;
  }
  const text = cmds.outputs.get(job.id)?.text ?? '';
  if (row.shownJob === job.id && row.shownText === text) return;
  const before = shownBox(row);
  const atEnd = row.shownJob !== job.id || before.scrollTop + before.clientHeight >= before.scrollHeight - 8; // else the person scrolled up
  if (text) fillOutput(row, text);
  else placeholder(row, running ? 'Waiting for output…' : '(no output)');
  const box = shownBox(row);
  if (atEnd) box.scrollTop = box.scrollHeight;
  row.shownJob = job.id;
  row.shownText = text;
}

function drawCommands(view) {
  const cmds = view.cmds;
  const running = [...cmds.jobs.values()].filter((job) => job.state === 'running');
  cmds.badge.hidden = !running.length;
  cmds.badge.replaceChildren(h('span', 'cmd-dot'), running.length === 1 ? '1 command running' : `${running.length} commands running`);
  cmds.badge.title = running.length ? `Running: ${running.map((job) => job.command_id).join(', ')}. Click to show its section.` : '';
  for (const row of cmds.part?.rows.values() ?? []) drawRow(view, row);
}

/** Ask for the jobs (and the output of every open pane on show that may have changed), draw them, and ask again soon. */
async function pollJobs(view) {
  const cmds = view.cmds;
  clearTimeout(cmds.timer);
  cmds.timer = null;
  const round = (cmds.round = (cmds.round ?? 0) + 1);
  try {
    const { jobs } = await get('/api/workshop/jobs');
    if (ui !== view || round !== cmds.round) return;
    const latest = new Map(); // the list has the running jobs first, then the most recent that ended
    for (const job of jobs ?? []) if (!latest.has(job.command_id)) latest.set(job.command_id, job);
    cmds.jobs = latest;
    const wanted = [...(cmds.part?.rows.keys() ?? [])]
      .map((id) => latest.get(id))
      .filter((job) => job && (job.state === 'running' || cmds.outputs.get(job.id)?.state !== job.state));
    await Promise.all(
      wanted.map(async (job) => {
        try {
          const full = await get(`/api/workshop/jobs/${encodeURIComponent(job.id)}`);
          cmds.outputs.set(full.id, { state: full.state, text: full.output ?? '' });
        } catch {
          /* asked again on the next round */
        }
      }),
    );
  } catch {
    /* the console did not answer: asked again on the next round */
  }
  if (ui !== view || round !== cmds.round) return;
  drawCommands(view);
  const busy = [...cmds.jobs.values()].some((job) => job.state === 'running');
  cmds.timer = setTimeout(() => pollJobs(view), busy ? JOBS_POLL_MS : JOBS_IDLE_MS);
}

async function runCommand(view, command) {
  const cmds = view.cmds;
  const row = cmds.part?.rows.get(command.id);
  if (row) row.run.disabled = true;
  cmds.errors.delete(command.id);
  try {
    const job = await post(`/api/workshop/commands/${encodeURIComponent(command.id)}/run`);
    if (ui !== view) return;
    cmds.jobs.set(command.id, job);
    const row = cmds.part?.rows.get(command.id);
    if (row && row.mode !== 'live') showLive(view, row);
  } catch (err) {
    if (ui !== view) return;
    cmds.errors.set(command.id, `Not run: ${err.message}`);
  }
  drawCommands(view);
  pollJobs(view);
}

async function stopCommand(view, command) {
  const cmds = view.cmds;
  const job = cmds.jobs.get(command.id);
  if (!job) return;
  const row = cmds.part?.rows.get(command.id);
  if (row) row.stop.disabled = true;
  try {
    const stopped = await post(`/api/workshop/jobs/${encodeURIComponent(job.id)}/stop`);
    if (ui !== view) return;
    cmds.jobs.set(command.id, stopped);
    cmds.outputs.set(stopped.id, { state: stopped.state, text: stopped.output ?? '' });
  } catch (err) {
    if (ui !== view) return;
    if (err.status !== 409) cmds.errors.set(command.id, `Not stopped: ${err.message}`); // 409: it had ended already
  }
  if (row) row.stop.disabled = false;
  drawCommands(view);
  pollJobs(view);
}

/** Copy a command; without the clipboard API (plain http on a LAN address) select it, for Ctrl+C. */
async function copyCommand(text, line, button) {
  let said = 'Copied';
  try {
    await navigator.clipboard.writeText(text);
  } catch {
    const range = document.createRange();
    range.selectNodeContents(line);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    if (!document.execCommand('copy')) said = 'Selected';
  }
  button.textContent = said;
  setTimeout(() => {
    button.textContent = 'Copy';
  }, 1500);
}
