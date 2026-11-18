// Board view: the harness's mission board and eval leaderboard (their own pages, in frames), the run queue and every
// unit's credits. All of it is public data.
//
// `#/board?projector=1` is the projector: app.js shows it with no login and no shell (`ctx.me` is null then), and this
// view leaves out its heading. It is read from the back of a room: big type, ink on the surface, the queue and every
// unit's credits as large rows (board.css). The queue is the public GET /api/runs/queue, read when the view opens and
// whenever its stream connects again, kept current by the public `run` events (queue.js): in the shell from the shell's
// global stream (`ctx.onGlobal`: a tab holds one stream), on the projector from a public stream of its own that never
// gives up. Credits come from GET /api/units, every UNITS_EVERY_MS and when a run ends.
//
// The two harness pages run in sandboxed frames (scripts only, no origin of their own: the console's cookie and API are
// not theirs), and the mission page polls its picture every 2 s. The markup below is fixed: names and numbers from the
// server go in as text.

import { applyQueue, applyRun, isOver, queueView } from '../queue.js';

const MISSION = '/board?data=/api/board/&rate=2000';
const LEADERBOARD = '/leaderboard?src=/api/eval/results.json';
const UNITS_EVERY_MS = 5000;

const TEMPLATE = `
<div class="board">
  <header class="board-head" data-ref="head">
    <div class="board-title">
      <h1>Board</h1>
      <p class="page-sub">What the projector shows: the mission board, the eval leaderboard, the run queue and every unit’s credits.</p>
    </div>
    <a class="btn" href="#/board?projector=1" target="_blank" rel="noopener">Open the projector view</a>
  </header>
  <section class="board-box board-queue" aria-labelledby="board-queue-title">
    <h2 id="board-queue-title">Run queue</h2>
    <div data-ref="queue"></div>
  </section>
  <section class="board-box board-credits" aria-labelledby="board-credits-title">
    <h2 id="board-credits-title">Credits per unit</h2>
    <div data-ref="credits"></div>
  </section>
  <section class="board-frame board-mission">
    <iframe title="Mission board" sandbox="allow-scripts" src="${MISSION}"></iframe>
  </section>
  <section class="board-frame board-leader">
    <iframe title="Eval leaderboard" sandbox="allow-scripts" src="${LEADERBOARD}"></iframe>
  </section>
</div>`;

let ui = null; // the mounted view: {api, refs, runs, units, loaded, stale, loading, events, queueing, timer, closeStream}

// --- small helpers ---------------------------------------------------------------------------------------------

function node(tag, className, text) {
  const el = document.createElement(tag);
  if (className) el.className = className;
  if (text !== undefined) el.textContent = text;
  return el;
}

const credits = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

/** The name a unit is shown by: its own name, else "Unit 04". */
function unitName(view, id) {
  const unit = view.units.find((u) => u.id === id);
  return unit?.name || String(id).replace(/^unit-/, 'Unit ');
}

// --- the run queue -----------------------------------------------------------------------------------------------

const BAD = new Set(['failed', 'error', 'refused', 'escalated']);

/** The word for where a run is, and its tone (board.css `.q-state.<tone>`). */
function stateOf(run, kind) {
  if (kind === 'running') return run.status === 'waiting' ? ['waiting for an answer', 'busy'] : ['running', 'busy'];
  if (kind === 'queued') return ['waiting', ''];
  return [run.status, run.status === 'done' ? 'ok' : BAD.has(run.status) ? 'bad' : run.status === 'stopped' ? 'warn' : ''];
}

function runItem(view, run, kind) {
  const item = node('li', `q-item${kind === 'finished' ? ' over' : ''}`);
  const [word, tone] = stateOf(run, kind);
  item.append(
    node('span', 'q-place', kind === 'queued' ? `${run.position ?? '?'}.` : ''),
    node('span', 'q-unit', unitName(view, run.unit)),
    node('span', 'q-kind', run.kind),
    node('span', `q-state${tone ? ` ${tone}` : ''}`, word),
  );
  if (run.kind === 'workflow' && run.credits > 0) item.append(node('span', 'q-spent', `${credits(run.credits)} credits`));
  return item;
}

function group(view, title, runs, kind) {
  const box = document.createDocumentFragment();
  if (!runs.length) return box;
  const list = node('ul', 'board-list');
  list.append(...runs.map((run) => runItem(view, run, kind)));
  box.append(node('h3', '', title), list);
  return box;
}

function paintQueue(view) {
  const { running, queued, finished } = queueView(view.runs);
  const box = view.refs.queue;
  box.replaceChildren();
  if (!running.length && !queued.length && !finished.length) box.append(node('p', 'board-empty', 'No runs yet. Runs show here as units start them.'));
  box.append(group(view, 'Running', running, 'running'), group(view, 'Waiting', queued, 'queued'), group(view, 'Finished', finished, 'finished'));
  if (finished.length) box.append(node('p', 'board-foot', 'Finished: the runs seen since this page was opened.'));
}

/**
 * Read GET /api/runs/queue into the queue (the runs queued or going now). An event that comes while it is being read
 * may be newer than the answer: then the answer is not used and it is read again.
 */
async function loadQueue(view) {
  if (view.queueing) {
    view.queueAgain = true;
    return;
  }
  view.queueing = true;
  let answer = null;
  try {
    do {
      view.queueAgain = false;
      const seen = view.events;
      try {
        answer = await view.api.get('/api/runs/queue');
      } catch {
        answer = null; // the queue stays as the events made it; the next reconnect reads it again
      }
      if (ui !== view) return;
      if (view.events !== seen) view.queueAgain = true;
      else if (answer) applyQueue(view.runs, answer);
    } while (view.queueAgain);
  } finally {
    view.queueing = false;
  }
  paintQueue(view);
}

// --- credits per unit --------------------------------------------------------------------------------------------

function creditRow(view, unit) {
  const share = unit.budget > 0 ? Math.min(1, unit.spent / unit.budget) : 1;
  const tone = share >= 1 ? ' out' : share >= 0.85 ? ' low' : '';
  const row = node('li', `unit-credit${tone}`);
  const bar = node('div', `meter unit-bar${tone}`);
  bar.setAttribute('role', 'meter');
  bar.setAttribute('aria-label', `Credits used by ${unitName(view, unit.id)}`);
  bar.setAttribute('aria-valuemin', '0');
  bar.setAttribute('aria-valuemax', String(unit.budget));
  bar.setAttribute('aria-valuenow', String(unit.spent));
  const fill = node('div');
  fill.style.width = `${Math.round(share * 100)}%`;
  bar.append(fill);
  row.append(
    node('span', 'unit-name', unitName(view, unit.id)),
    node('span', 'unit-level', `L${unit.level}`),
    bar,
    node('span', 'unit-spent', `${credits(unit.spent)} of ${credits(unit.budget)}`),
    node('span', 'unit-run', unit.locked ? 'busy' : ''), // a run holds the unit, or it waits for the facilitator
  );
  if (unit.locked) row.title = `${unitName(view, unit.id)} is busy: a run holds it, or it waits for the facilitator.`;
  return row;
}

function paintCredits(view) {
  const box = view.refs.credits;
  box.replaceChildren();
  if (!view.units.length) {
    const note = view.stale ? 'Credits are not available right now. The board asks again every few seconds.' : view.loaded ? 'No units yet.' : 'Loading…';
    box.append(node('p', 'board-empty', note));
    return;
  }
  const list = node('ul', 'board-list board-units');
  list.append(...view.units.map((unit) => creditRow(view, unit)));
  box.append(list);
  if (view.stale) box.append(node('p', 'board-foot', 'The console did not answer just now: these are the last numbers.'));
}

async function loadUnits(view) {
  if (view.loading) return;
  view.loading = true;
  let units = null;
  try {
    const answer = await view.api.get('/api/units');
    if (Array.isArray(answer?.units)) units = answer.units;
  } catch {
    /* shown as "not available" below; the next round asks again */
  }
  view.loading = false;
  if (ui !== view) return; // the view was left meanwhile
  if (units) view.units = units;
  view.loaded = view.loaded || Boolean(units);
  view.stale = !units;
  paintCredits(view);
  paintQueue(view); // unit names may have arrived
}

// --- the view ----------------------------------------------------------------------------------------------------

export function mount(el, ctx) {
  unmount();
  el.innerHTML = TEMPLATE; // fixed markup of this file, never data
  const refs = Object.fromEntries([...el.querySelectorAll('[data-ref]')].map((found) => [found.dataset.ref, found]));
  const projecting = ctx.route?.params?.projector === '1';
  el.firstElementChild.classList.toggle('projector', projecting);
  refs.head.hidden = projecting;

  const view = {
    api: ctx.api,
    refs,
    runs: new Map(),
    units: [],
    loaded: false,
    stale: false,
    loading: false,
    events: 0, // run events so far (loadQueue: was one newer than its answer?)
    queueing: false,
    queueAgain: false,
    timer: null,
    closeStream: null,
  };
  ui = view;
  paintQueue(view);
  paintCredits(view);
  loadUnits(view);
  view.timer = setInterval(() => loadUnits(view), UNITS_EVERY_MS);
  const onRun = ({ data }) => {
    view.events += 1;
    applyRun(view.runs, data);
    paintQueue(view);
    if (isOver(view.runs.get(data?.id) ?? { status: 'queued' })) loadUnits(view); // a run that ended has charged its unit
  };
  const reconnected = () => loadQueue(view); // events may have been missed while it was away
  view.closeStream = projecting
    ? ctx.api.stream('/api/stream', onRun, ['run'], reconnected, { public: true })
    : ctx.onGlobal(onRun, reconnected);
  loadQueue(view);
}

export function unmount() {
  if (!ui) return;
  clearInterval(ui.timer);
  ui.closeStream();
  ui = null;
}
