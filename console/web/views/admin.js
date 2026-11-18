// Admin view (the facilitator only): the room. First the units in one table: name, level, credits with +10 and +30, what
// each is running (with Stop), its lock, a catch-up reset, for a unit that waits for a reset a restore to its own branch,
// and a new code. Under a line over the room ("3 units · 1 with a run · …"). Then every run, the eval batch (its state,
// archiving its results; launching and stopping it are on the Evals page), warming the caches and the doctor. Under
// 720 px each unit is a card of its own (admin.css), so nothing scrolls sideways.
//
// What changes something at once (rename, level, credits, stop a run, warm the caches) just does it. What cannot be
// undone asks first, in a dialog of its own (`ask`, not window.confirm): a reset, a restore, a new code (it also logs everyone out)
// and archiving the eval results, and so does stopping a run (the table moves every few seconds: a click must not land on
// another run). The table is built once and updated in place, so a field being edited, or a catch-up
// ref being picked, is not lost when the numbers refresh. The numbers come from GET /api/admin/units and GET /api/runs
// every REFRESH_MS and again a moment after any `run` event of the global stream; the words come from adminmodel.js.

import { get, holdLogout, post } from '../api.js';
import { archiveBlocked, archiveText, credits, doctorTally, roomText, runRows, unitRows, warmText } from '../adminmodel.js';
import { progress } from '../evalmodel.js';

const REFRESH_MS = 4000;
const AFTER_EVENT_MS = 300;
const DOCTOR_POLL_MS = 2000;
const RUNS_SHOWN = 50;
const BUDGET_STEPS = [10, 30];
const MAYBE_APPLIED = 'The console did not answer; it may have applied the change. The list now shows the current state.';
const CODE_LOST = 'The code was changed but the answer was lost: make a new code again.';
const UNIT_COLUMNS = ['Unit', 'Level', 'Credits', 'Running', 'Lock', 'Catch-up', 'Code'];
const RUN_TONE = { failed: 'bad', refused: 'bad', error: 'bad', escalated: 'bad', running: 'busy', waiting: 'busy', stopped: 'warn', done: 'ok' };
const CHECK_BADGE = { OK: 'pass', WARN: 'warn', FAIL: 'fail' };

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
  if (document.getElementById('admin-css')) return;
  document.head.append(h('link', { id: 'admin-css', rel: 'stylesheet', href: '/web/views/admin.css' }));
}

const clock = (iso) => {
  const time = new Date(iso);
  return Number.isNaN(time.getTime()) ? String(iso ?? '') : time.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit' });
};

// --- mount and unmount -----------------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  loadStyles();
  const view = (ui = {
    ctx,
    units: [], // the last answer of GET /api/admin/units
    unitRefs: [], // the refs a reset may use
    maxLevel: 5,
    runs: [],
    status: null, // the eval batch
    doctor: null, // the doctor's last state
    rows: new Map(), // unit id -> {tr, ...controls}
    loading: false,
    again: false, // a refresh was asked while one was under way, or while the polls were paused
    paused: 0, // calls under way that end every session (a new code): no poll runs meanwhile
    failing: false, // the last refresh failed (a person is told once, not at every round)
    timer: null,
    later: null,
    doctorTimer: null,
    closeStream: null,
    refs: {},
  });
  buildPage(view, el);
  ctx.cli('python -m console doctor');
  await refresh(view);
  if (ui !== view) return;
  view.timer = setInterval(() => refresh(view), REFRESH_MS);
  view.closeStream = ctx.onGlobal(() => { // the shell's stream: a tab holds one
    clearTimeout(view.later);
    view.later = setTimeout(() => refresh(view), AFTER_EVENT_MS); // a run changed: its unit's lock and credits did too
  });
  loadDoctor(view, false);
}

export function unmount() {
  const view = ui;
  if (!view) return;
  ui = null; // every call still waiting for the server sees that the view is gone
  clearInterval(view.timer);
  clearTimeout(view.later);
  clearTimeout(view.doctorTimer);
  view.closeStream?.();
  if (view.refs.dialog.open) view.refs.dialog.close();
  view.ctx.cli('');
}

// --- the page --------------------------------------------------------------------------------------------------------------

function buildPage(view, el) {
  const r = view.refs;
  r.warm = h('button', { type: 'button', class: 'btn', text: 'Warm caches' });
  r.warm.addEventListener('click', () => warm(view));
  r.room = h('span', { class: 'adm-room', 'aria-live': 'polite' });
  r.units = h('tbody');
  r.unitsNote = h('p', { class: 'empty-line', text: 'Loading the units…' });
  r.runs = h('tbody');
  r.runsBox = h('div', { class: 'table-box', hidden: true });
  r.runsNote = h('p', { class: 'empty-line' });
  r.evalLine = h('p', { class: 'adm-line' });
  r.evalMeter = h('progress', { class: 'adm-progress', max: '1', value: '0', 'aria-label': 'Eval batch progress', hidden: true });
  r.archive = h('button', { type: 'button', class: 'btn', text: 'Archive results' });
  r.archive.addEventListener('click', () => archive(view));
  r.archiveHint = h('p', { class: 'hint adm-hint' });
  r.doctorLine = h('p', { class: 'adm-line', 'aria-live': 'polite' });
  r.doctorRows = h('ul', { class: 'adm-checks' });
  r.doctorRun = h('button', { type: 'button', class: 'btn', text: 'Run the checks again' });
  r.doctorRun.addEventListener('click', () => loadDoctor(view, true));
  r.dialog = h('dialog', { class: 'adm-dialog', 'aria-labelledby': 'adm-dialog-title' });

  const head = (id, title, ...more) => h('div', { class: 'section-head' }, h('h2', { id, text: title }), ...more);
  const thead = (titles) => h('thead', {}, h('tr', {}, titles.map((text) => h('th', { scope: 'col', text }))));
  r.runsBox.append(h('table', { class: 'table adm-runs', 'aria-label': 'Runs' }, thead(['Run', 'Unit', 'Kind', 'Status', 'Credits', 'Queued', '']), r.runs));

  el.replaceChildren(
    h(
      'div',
      { class: 'page admin' },
      h('header', { class: 'page-head' }, h('h1', { text: 'Admin' }), h('p', { class: 'page-sub', text: 'The room: every unit, what it is running and its credits. Then the runs, the eval batch, the caches and the doctor.' })),
      h(
        'section',
        { class: 'section', 'aria-labelledby': 'adm-units' },
        head('adm-units', 'Units', r.room),
        h('div', { class: 'table-box adm-units-box' }, h('table', { class: 'table adm-units', 'aria-label': 'Units' }, thead(UNIT_COLUMNS), r.units)),
        r.unitsNote,
        h('p', { class: 'hint adm-hint', text: 'Credits: used of the budget. A reset puts the unit on the catch-up ref and keeps its old work on a backup branch; a restore puts a unit that waits for a reset back on its own branch; a new code logs every unit out.' }),
      ),
      h('section', { class: 'section', 'aria-labelledby': 'adm-runs' }, head('adm-runs', 'Runs'), r.runsBox, r.runsNote),
      h(
        'section',
        { class: 'section', 'aria-labelledby': 'adm-evals' },
        head('adm-evals', 'Eval batch'),
        r.evalLine,
        r.evalMeter,
        h('p', { class: 'hint adm-hint' }, 'Launching and stopping a batch, and its cost estimate, are on the ', h('a', { href: '#/evals', text: 'Evals page' }), '.'),
        h('div', { class: 'actions adm-actions' }, r.archive),
        r.archiveHint,
      ),
      h(
        'section',
        { class: 'section', 'aria-labelledby': 'adm-warm' },
        head('adm-warm', 'Warm caches'),
        h('p', { class: 'section-sub', text: 'Queues one gates run for every unit that is free, so the first real runs of the day start warm. Units that are busy are left out.' }),
        h('div', { class: 'actions' }, r.warm),
      ),
      h('section', { class: 'section', 'aria-labelledby': 'adm-doctor' }, head('adm-doctor', 'Doctor'), r.doctorLine, r.doctorRows, h('div', { class: 'actions adm-actions' }, r.doctorRun)),
      r.dialog,
    ),
  );
  paintEval(view);
  paintDoctor(view);
}

// --- the numbers, polled -----------------------------------------------------------------------------------------------------

async function refresh(view) {
  if (view.loading || view.paused > 0) {
    view.again = true; // asked while one is under way, or while the polls are paused: one more round afterwards
    return;
  }
  view.loading = true;
  try {
    const [units, runs, status] = await Promise.all([get('/api/admin/units'), get('/api/runs'), get('/api/eval/status')]);
    if (ui !== view) return;
    view.units = units.units;
    view.unitRefs = units.refs;
    view.maxLevel = units.max_level;
    view.runs = runs.runs;
    view.status = status;
    view.failing = false;
    paintUnits(view);
    paintRuns(view);
    paintEval(view);
  } catch (err) {
    if (ui === view && !view.failing) fail(view.ctx, err, 'Could not load the admin page');
    if (ui === view) view.failing = true;
  } finally {
    view.loading = false;
  }
  if (ui === view && view.again) {
    view.again = false;
    refresh(view);
  }
}

// What a button did: busy while the call is under way, then a toast and a fresh look. A call that got no answer in time
// may have been done all the same: the units are read again before anything is said.
async function act(view, row, work, failed) {
  row.busy = true;
  paintRow(view, row);
  let lost = false;
  try {
    await work();
  } catch (err) {
    if (err.timeout) lost = true;
    else if (ui === view) fail(view.ctx, err, failed);
  }
  row.busy = false;
  if (ui !== view) return;
  if (!lost) {
    await refresh(view);
    return;
  }
  await lookAgain(view);
  if (ui === view) view.ctx.toast(MAYBE_APPLIED, 'error');
}

/** Read the units now, after a read that is under way (its answer may be older than what was just done). */
async function lookAgain(view) {
  while (view.loading && ui === view) await new Promise((resolve) => setTimeout(resolve, 100));
  if (ui === view) await refresh(view);
}

/**
 * After a new-code call that got no answer: whether the code was changed. The call replaces the session key first, so the
 * admin's cookie, which the lost answer would have renewed, gets 401 now (judged after `holdLogout`: the login page).
 */
async function keyChanged() {
  try {
    await get('/api/admin/units');
    return false;
  } catch (err) {
    return err.status === 401;
  }
}

// --- the units -----------------------------------------------------------------------------------------------------------------

/** A cell of the units table; `label` names it on a phone, where each row is a card (admin.css). */
const cell = (label, className, ...children) => h('td', { class: className, 'data-label': label }, ...children);

function buildRow(view, id) {
  const row = { id, busy: false, runKey: null, data: null };
  row.name = h('input', { type: 'text', class: 'adm-name', maxlength: '40', spellcheck: 'false', autocomplete: 'off', 'aria-label': `Name of ${id}` });
  row.level = h('select', { class: 'adm-level', 'aria-label': `Level of ${id}` }, Array.from({ length: view.maxLevel + 1 }, (_, n) => h('option', { value: String(n), text: `L${n}` })));
  row.credits = h('span', { class: 'adm-credits' });
  row.meter = h('div', { class: 'meter adm-meter', role: 'meter', 'aria-label': `Credits used by ${id}`, 'aria-valuemin': '0' }, h('div'));
  row.plus = BUDGET_STEPS.map((step) => {
    const button = h('button', { type: 'button', class: 'btn small', text: `+${step}`, 'aria-label': `Add ${step} credits to ${id}` });
    button.addEventListener('click', () => act(view, row, async () => {
      const { unit } = await post(`/api/admin/units/${id}`, { budget_add: step });
      view.ctx.toast(`${id} has ${credits(unit.budget)} credits now.`, 'ok');
    }, 'Could not add credits'));
    return button;
  });
  row.state = h('span', { class: 'adm-state' });
  row.run = h('div', { class: 'adm-run' });
  row.ref = h('select', { 'aria-label': `Catch-up ref for ${id}` }, view.unitRefs.map((ref) => h('option', { value: ref, text: ref })));
  row.reset = h('button', { type: 'button', class: 'btn small', text: 'Reset', 'aria-label': `Reset ${id}` });
  row.restore = h('button', {
    type: 'button',
    class: 'btn small',
    text: 'Restore',
    title: "Back to its own branch with every save it made (no backup branch)",
    'aria-label': `Restore ${id} to its own branch, keeping the unit's saves`,
    hidden: true,
  });
  row.code = h('button', { type: 'button', class: 'btn small', text: 'New code', 'aria-label': `New code for ${id}` });

  // The value is read before `act` paints the row busy: that paint puts the server's value back into a field that does
  // not have the focus (a select changed without being focused).
  row.name.addEventListener('change', () => {
    const name = row.name.value;
    act(view, row, async () => {
      try {
        await post(`/api/admin/units/${id}`, { name });
      } catch (err) {
        row.name.value = row.data?.name ?? ''; // the name the server has
        throw err;
      }
    }, 'Could not rename the unit');
  });
  row.level.addEventListener('change', () => {
    const level = Number(row.level.value);
    act(view, row, async () => {
      try {
        await post(`/api/admin/units/${id}`, { level });
      } catch (err) {
        row.level.value = String(row.data?.level ?? 0);
        throw err;
      }
    }, 'Could not set the level');
  });
  row.reset.addEventListener('click', () => resetUnit(view, row));
  row.restore.addEventListener('click', () => restoreUnit(view, row));
  row.code.addEventListener('click', () => newCode(view, row));

  row.tr = h(
    'tr',
    { 'data-unit': id },
    h('th', { scope: 'row', class: 'adm-unit-cell' }, h('div', { class: 'adm-unit' }, row.name, h('code', { class: 'adm-id', text: id }))),
    cell('Level', '', row.level),
    cell('Credits', 'adm-money', h('div', {}, h('div', { class: 'adm-money-line' }, row.credits, h('span', { class: 'adm-steps' }, row.plus)), row.meter)),
    cell('Running', 'adm-run-cell', row.run),
    cell('Lock', 'adm-state-cell', row.state),
    cell('Catch-up', '', h('div', { class: 'adm-pair' }, row.ref, row.reset, row.restore)),
    cell('Code', '', row.code),
  );
  return row;
}

function paintUnits(view) {
  const rows = unitRows(view.units, view.runs);
  view.refs.unitsNote.hidden = rows.length > 0;
  view.refs.unitsNote.textContent = 'There are no units yet: run `python -m console units init`.';
  view.refs.room.textContent = roomText(rows);
  const seen = new Set();
  for (const data of rows) {
    seen.add(data.id);
    let row = view.rows.get(data.id);
    if (!row) {
      row = buildRow(view, data.id);
      view.rows.set(data.id, row);
    }
    row.data = data;
    paintRow(view, row);
  }
  for (const [id, row] of view.rows) {
    if (!seen.has(id)) {
      row.tr.remove();
      view.rows.delete(id);
    }
  }
  view.refs.units.replaceChildren(...rows.map((data) => view.rows.get(data.id).tr));
}

/** What a unit's lock says: a word (a badge when it holds the unit), and the whole sentence as its title. */
function lockNodes(data) {
  if (data.state === 'run') return [h('span', { class: 'badge running', text: 'held by a run' })];
  if (data.state === 'needs-reset') {
    const run = /\(run (.+)\)$/.exec(data.stateText)?.[1];
    return [h('span', { class: 'badge warn', text: 'needs a reset' }), run ? h('code', { class: 'adm-lock-run', text: run }) : null];
  }
  return [h('span', { class: 'muted', text: data.stateText })];
}

function paintRow(view, row) {
  const data = row.data;
  if (!data) return;
  if (document.activeElement !== row.name) row.name.value = data.name;
  if (document.activeElement !== row.level) row.level.value = String(data.level);
  const share = data.budget > 0 ? Math.min(1, data.spent / data.budget) : 1;
  row.credits.textContent = `${credits(data.spent)} of ${credits(data.budget)}`;
  row.credits.title = `${credits(data.left)} credits left`;
  row.credits.classList.toggle('out', data.left < 1);
  row.meter.setAttribute('aria-valuemax', String(data.budget));
  row.meter.setAttribute('aria-valuenow', String(data.spent));
  row.meter.className = `meter adm-meter${share >= 1 ? ' out' : share >= 0.85 ? ' low' : ''}`;
  row.meter.firstElementChild.style.width = `${Math.round(share * 100)}%`;
  const lockKey = `${data.state}:${data.stateText}`;
  if (row.state.dataset.key !== lockKey) {
    row.state.dataset.key = lockKey;
    row.state.title = data.stateText;
    row.state.replaceChildren(...lockNodes(data).filter(Boolean));
  }
  row.tr.classList.toggle('busy', row.busy);
  for (const control of [row.name, row.level, ...row.plus, row.code]) control.disabled = row.busy;
  const resettable = data.canReset && !row.busy;
  row.ref.disabled = row.reset.disabled = !resettable;
  row.reset.title = data.canReset ? '' : 'A run holds this unit: stop it first';
  row.restore.hidden = !data.canRestore;
  row.restore.disabled = row.busy;
  const key = data.run ? `${data.run.id}:${data.run.status}:${data.run.position}` : '';
  if (key !== row.runKey) {
    row.runKey = key;
    row.run.replaceChildren(...(data.run ? runCell(view, data.run) : [h('span', { class: 'muted', text: 'nothing' })]));
  }
}

function runCell(view, run) {
  const place = run.status === 'queued' && run.position ? ` #${run.position}` : '';
  return [
    h('a', { href: `#/runs/${encodeURIComponent(run.id)}`, text: run.kind, title: run.id }),
    h('span', { class: `adm-status ${RUN_TONE[run.status] ?? ''}`.trim(), text: `${run.status}${place}` }),
    stopButton(view, run),
  ];
}

function stopButton(view, run) {
  const stop = h('button', { type: 'button', class: 'btn small', text: 'Stop', 'aria-label': `Stop run ${run.id}` });
  stop.addEventListener('click', async () => {
    const queued = run.status === 'queued';
    const sure = await ask(view, {
      title: `Stop run ${run.id}?`,
      text: `${run.kind} run of ${run.unit}. ${queued ? 'It has not started: it leaves the queue.' : 'It ends now and cannot be continued; the credits it has used so far stay charged to the unit.'}`,
      confirm: 'Stop the run',
      danger: true,
    });
    if (!sure || ui !== view) return;
    stop.disabled = true;
    try {
      await post(`/api/admin/runs/${encodeURIComponent(run.id)}/stop`);
      if (ui === view) view.ctx.toast(`Run ${run.id} was stopped.`, 'ok');
    } catch (err) {
      if (ui === view) fail(view.ctx, err, 'Could not stop the run');
    }
    if (ui === view) await refresh(view);
  });
  return stop;
}

async function resetUnit(view, row) {
  const id = row.id;
  const ref = row.ref.value;
  const sure = await ask(view, {
    title: `Reset ${id} to ${ref}?`,
    text: `${id} goes back to ${ref}. Its uncommitted edits are lost; what it committed stays on a backup branch.`,
    confirm: 'Reset',
    danger: true,
  });
  if (!sure || ui !== view) return;
  await act(view, row, async () => {
    const answer = await post(`/api/admin/units/${id}/reset`, { ref });
    const kept = (answer.backups ?? []).join(' and ');
    view.ctx.toast(`${id} is back on ${ref}.${kept ? ` Its old work is on ${kept}.` : ''}`, 'ok');
  }, 'Could not reset the unit');
}

async function restoreUnit(view, row) {
  const id = row.id;
  const sure = await ask(view, {
    title: `Restore ${id} to its own branch?`,
    text: `${id} goes back to its last save and is free again. What the stopped run left uncommitted is lost (the run keeps its patch); everything the unit saved stays, and no backup branch is made.`,
    confirm: 'Restore',
    danger: true,
  });
  if (!sure || ui !== view) return;
  await act(view, row, async () => {
    const answer = await post(`/api/admin/units/${id}/restore`);
    if (answer.warning) view.ctx.toast(`${id} is back on its own branch, with its saves. Note: ${answer.warning}.`, 'info');
    else view.ctx.toast(`${id} is back on its own branch, with its saves.`, 'ok');
  }, 'Could not restore the unit');
}

async function newCode(view, row) {
  const id = row.id;
  const sure = await ask(view, {
    title: `New code for ${id}?`,
    text: `The old code of ${id} stops working. Every unit is logged out and has to log in again (you stay logged in). The new code is shown once.`,
    confirm: 'Make a new code',
    danger: true,
  });
  if (!sure || ui !== view) return;
  // The call replaces the session key: the cookie of every call that is still on its way ends with it, and ours is renewed
  // by the answer. So nothing is polled meanwhile, and a 401 that still comes is judged after the answer (`holdLogout`).
  let code = null;
  let lost = null; // the call got no answer: 'changed' (the session key is new, so the code was changed) or 'maybe'
  view.paused += 1;
  try {
    await holdLogout(() =>
      act(view, row, async () => {
        try {
          code = (await post(`/api/admin/units/${id}/code`)).code;
        } catch (err) {
          if (!err.timeout) throw err;
          lost = (await keyChanged()) ? 'changed' : 'maybe'; // looked at before anything is said
        }
      }, 'Could not make a new code'),
    );
  } finally {
    view.paused -= 1;
  }
  if (ui === view && view.paused === 0 && (view.again || lost)) {
    view.again = false;
    await refresh(view);
  }
  if (lost) view.ctx.toast(lost === 'changed' ? CODE_LOST : MAYBE_APPLIED, 'error');
  if (code) await showCode(id, code); // whatever became of the page meanwhile: the code exists nowhere else
}

// --- the dialog ---------------------------------------------------------------------------------------------------------------

/** A small modal question: resolves true for the confirm button, false for Cancel or Escape. */
function ask(view, { title, text, confirm, danger = false }) {
  return new Promise((resolve) => {
    const { dialog } = view.refs;
    const yes = h('button', { type: 'button', class: `btn ${danger ? 'danger' : 'primary'}`, text: confirm });
    const no = h('button', { type: 'button', class: 'btn', text: 'Cancel' });
    dialog.replaceChildren(h('h2', { id: 'adm-dialog-title', text: title }), h('p', { text }), h('div', { class: 'adm-dialog-actions' }, no, yes));
    dialog.addEventListener('close', () => resolve(false), { once: true }); // Escape; after a click the promise has its answer already
    yes.addEventListener('click', () => {
      resolve(true);
      dialog.close();
    });
    no.addEventListener('click', () => {
      resolve(false);
      dialog.close();
    });
    dialog.showModal();
    no.focus(); // the safe answer is the one at hand
  });
}

/**
 * The new code of a unit, big, with what to do with it, in a dialog of its own on the page (not in the view's: the view may
 * be gone, for a poll that found the session ended). Shown once: nothing else keeps the code.
 */
function showCode(id, code) {
  return new Promise((resolve) => {
    const shown = h('p', { class: 'adm-newcode', text: code, tabindex: '0', 'aria-label': `New code for ${id}` });
    shown.addEventListener('click', () => {
      const range = document.createRange();
      range.selectNodeContents(shown);
      const selection = getSelection();
      selection.removeAllRanges();
      selection.addRange(range);
    });
    const done = h('button', { type: 'button', class: 'btn primary', text: 'Done' });
    const dialog = h(
      'dialog',
      { class: 'adm-dialog', 'aria-labelledby': 'adm-code-title' },
      h('h2', { id: 'adm-code-title', text: `New code for ${id}` }),
      shown,
      h('p', { text: 'Write it down now: it is not stored and will not be shown again. The old code no longer works, and every unit has been logged out.' }),
      h('div', { class: 'adm-dialog-actions' }, done),
    );
    const finish = () => {
      dialog.remove();
      resolve();
    };
    done.addEventListener('click', () => {
      dialog.close();
      finish();
    });
    dialog.addEventListener('close', finish, { once: true }); // Escape
    document.body.append(dialog);
    dialog.showModal();
    done.focus();
  });
}

// --- the runs ------------------------------------------------------------------------------------------------------------------

function paintRuns(view) {
  const { rows, more } = runRows(view.runs, RUNS_SHOWN);
  view.refs.runs.replaceChildren(
    ...rows.map((run) =>
      h(
        'tr',
        {},
        h('th', { scope: 'row' }, h('a', { class: 'mono', href: `#/runs/${encodeURIComponent(run.id)}`, text: run.id })),
        h('td', {}, h('code', { text: run.unit })),
        h('td', { text: run.kind }),
        h('td', { class: `adm-status ${RUN_TONE[run.status] ?? ''}`.trim(), text: run.status, title: run.error || null }),
        h('td', { class: 'num', text: credits(run.credits ?? 0) }),
        h('td', { class: 'mono', text: clock(run.queued_at) }),
        h('td', { class: 'adm-stop' }, run.stoppable ? stopButton(view, run) : null),
      ),
    ),
  );
  view.refs.runsBox.hidden = rows.length === 0;
  view.refs.runsNote.className = rows.length === 0 ? 'empty-line' : 'hint adm-hint';
  view.refs.runsNote.textContent =
    rows.length === 0 ? 'No runs yet. Units start them from their Runs page; Warm caches below queues a gates run for each free unit.' : more > 0 ? `${more} older run${more === 1 ? '' : 's'} not shown.` : '';
  view.refs.runsNote.hidden = !view.refs.runsNote.textContent;
}

async function warm(view) {
  const button = view.refs.warm;
  button.disabled = true;
  try {
    view.ctx.toast(warmText(await post('/api/admin/warm')), 'ok');
  } catch (err) {
    fail(view.ctx, err, 'Could not warm the caches');
  }
  button.disabled = false;
  if (ui === view) await refresh(view);
}

// --- the eval batch ------------------------------------------------------------------------------------------------------------

function paintEval(view) {
  const { evalLine, evalMeter, archive, archiveHint } = view.refs;
  const now = progress(view.status);
  evalLine.textContent = now.text;
  evalLine.className = `adm-line ${now.state}`;
  evalMeter.hidden = now.state === 'none';
  evalMeter.value = now.share;
  const blocked = archiveBlocked(view.status);
  archive.disabled = blocked !== null;
  archiveHint.textContent = blocked ?? 'Archiving moves the results so far out of the leaderboard (they are kept in the data folder), so the next batch starts a fresh one.';
}

async function archive(view) {
  const sure = await ask(view, {
    title: 'Archive the eval results?',
    text: 'The leaderboard is emptied: the results so far move to the archive folder in the console data. Do this between a trial batch and the real one.',
    confirm: 'Archive',
    danger: true,
  });
  if (!sure || ui !== view) return;
  view.refs.archive.disabled = true;
  try {
    view.ctx.toast(archiveText(await post('/api/admin/eval/archive')), 'ok');
  } catch (err) {
    fail(view.ctx, err, 'Could not archive the results');
  }
  if (ui === view) await refresh(view);
}

// --- the doctor -----------------------------------------------------------------------------------------------------------------

// `start`: begin a run (the button; also the first look, when none has run yet). The checks take seconds, so the page asks
// again every DOCTOR_POLL_MS while they run.
async function loadDoctor(view, start) {
  clearTimeout(view.doctorTimer);
  if (view.paused > 0 && !start) {
    view.doctorTimer = setTimeout(() => loadDoctor(view, false), DOCTOR_POLL_MS);
    return;
  }
  let state;
  try {
    state = start ? await post('/api/admin/doctor') : await get('/api/admin/doctor');
  } catch (err) {
    if (ui === view) fail(view.ctx, err, 'Could not ask the doctor');
    return;
  }
  if (ui !== view) return;
  if (!start && state.state === 'idle') {
    loadDoctor(view, true); // nobody has looked yet: look now
    return;
  }
  view.doctor = state;
  paintDoctor(view);
  if (state.state === 'running') view.doctorTimer = setTimeout(() => loadDoctor(view, false), DOCTOR_POLL_MS);
}

function paintDoctor(view) {
  const { doctorLine, doctorRows, doctorRun } = view.refs;
  const state = view.doctor;
  doctorRun.disabled = !state || state.state === 'running';
  doctorRows.replaceChildren();
  if (!state) {
    doctorLine.textContent = 'Asking the doctor…';
    doctorLine.className = 'adm-line';
    return;
  }
  const rows = state.checks ?? [];
  const tally = doctorTally(rows);
  if (state.state === 'running') doctorLine.textContent = `Checking this machine… the harness check starts the Copilot CLI and can take a minute.${rows.length > 0 ? ' The list below is from the run before.' : ''}`;
  else if (state.error) doctorLine.textContent = state.error;
  else doctorLine.textContent = `${tally.text} (checked at ${clock(state.finished_at)}).`;
  doctorLine.className = `adm-line ${state.state === 'running' ? 'running' : state.error || tally.fail > 0 ? 'bad' : tally.warn > 0 ? 'warn' : 'ok'}`;
  doctorRows.append(
    ...rows.map((row) =>
      h(
        'li',
        { class: `adm-check ${row.status}` },
        h('span', { class: `badge ${CHECK_BADGE[row.status] ?? ''}`.trim(), text: row.status }),
        h('span', { class: 'adm-check-name', text: row.name }),
        h('span', { class: 'adm-check-detail', text: row.detail }),
        row.fix && row.status !== 'OK' ? h('span', { class: 'adm-fix', text: `Fix: ${row.fix}` }) : null,
      ),
    ),
  );
}
