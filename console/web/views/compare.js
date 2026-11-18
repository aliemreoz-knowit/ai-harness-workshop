// Compare two runs: the second part of the Evals page in personal mode (`#/evals`, under the leaderboard of the
// checkout's experiments: evals.js mounts it with `{embedded: true}` into a section of its page; without it, it is a
// page of its own). A participant has no hidden tests and no ticket branches, so there is no eval batch to join; they
// pick two of the unit's finished runs and read them side by side (evalmodel.js `compareRuns`: result, gates, credits,
// time, retries, harness commit). The official comparison is the stage leaderboard. Everything from the server goes on
// the page as text.

import { get } from '../api.js';
import { compareRuns, finishedRuns } from '../evalmodel.js';
import { h } from '../guideui.js';

const OFFICIAL = 'Two runs are two anecdotes. The comparison that counts is the leaderboard: many runs per variant, judged by tests this checkout does not have.';
const SUB = 'Pick two of your finished runs and read them side by side, for example before and after a change to your harness.';
let ui = null;

const label = (run) => `${run.id} · ${[run.workflow || 'workflow', run.ticket].filter(Boolean).join(' · ')}${run.fake ? ' · fake model' : ''}`;

export async function mount(el, ctx, { embedded = false } = {}) {
  const view = (ui = { ctx, el });
  if (!document.getElementById('evals-css')) {
    const link = h('link');
    link.id = 'evals-css';
    link.rel = 'stylesheet';
    link.href = '/web/views/evals.css';
    document.head.append(link);
  }
  const title = h(embedded ? 'h2' : 'h1', '', 'Compare two runs');
  title.id = 'cmp-title';
  const head = embedded
    ? [h('div', 'section-head', title), h('p', 'section-sub', SUB)]
    : [h('header', 'page-head', title, h('p', 'page-sub', SUB))];
  const status = h('p', 'cmp-status');
  status.setAttribute('aria-live', 'polite');
  const out = h('div', 'cmp-out');
  const official = h('p', 'muted cmp-official', embedded ? OFFICIAL.replace('is the leaderboard:', 'is the leaderboard above:') : OFFICIAL);
  const page = h('div', embedded ? 'cmp' : 'page evals', ...head, h('div', 'cmp-pickers'), status, out, official);
  el.replaceChildren(page);
  let runs = [];
  try {
    const listed = await get('/api/runs');
    runs = finishedRuns(listed?.runs);
  } catch (err) {
    if (ui !== view) return;
    status.textContent = `Your runs could not be loaded: ${err.message}`;
    return;
  }
  if (ui !== view) return;
  if (runs.length < 2) {
    status.textContent = runs.length ? 'You have one finished run so far. Make another one to compare it with.' : 'You have no finished runs yet. Start two on the Runs page, then compare them here.';
    return;
  }
  const pick = (text, id, at) => {
    const select = h('select', '', ...runs.map((run, n) => {
      const option = h('option', '', label(run));
      option.value = run.id;
      option.selected = n === at;
      return option;
    }));
    select.id = id;
    const wrap = h('div', 'cmp-pick');
    const name = h('label', 'cmp-pick-label', text);
    name.htmlFor = id;
    wrap.append(name, select);
    return { wrap, select };
  };
  const a = pick('First run', 'cmp-a', 1); // newest first: the older run is the "before"
  const b = pick('Second run', 'cmp-b', 0);
  page.querySelector('.cmp-pickers').append(a.wrap, b.wrap);
  let asked = 0;
  const show = async () => {
    const mine = ++asked;
    status.textContent = '';
    try {
      const [x, y] = await Promise.all([get(`/api/runs/${encodeURIComponent(a.select.value)}`), get(`/api/runs/${encodeURIComponent(b.select.value)}`)]);
      if (ui !== view || mine !== asked) return;
      out.replaceChildren(table(compareRuns(x, y), a.select.value, b.select.value));
    } catch (err) {
      if (ui !== view || mine !== asked) return;
      out.replaceChildren();
      status.textContent = `The runs could not be loaded: ${err.message}`;
    }
  };
  a.select.addEventListener('change', show);
  b.select.addEventListener('change', show);
  await show();
}

export function unmount() {
  ui = null;
}

function table(rows, idA, idB) {
  const cell = (row, side) => {
    const better = row.better === side;
    const td = h('td', better ? 'cmp-better' : '', row[side]);
    if (better) td.append(h('span', 'badge ok cmp-mark', 'better'));
    return td;
  };
  const caption = h('caption', 'sr-only', 'The two runs side by side');
  const head = h('tr', '', h('th', '', 'Measure'), h('th', '', `First: ${idA}`), h('th', '', `Second: ${idB}`));
  head.children[0].scope = head.children[1].scope = head.children[2].scope = 'col';
  const body = rows.map((row) => {
    const name = h('th', '', row.label);
    name.scope = 'row';
    return h('tr', '', name, cell(row, 'a'), cell(row, 'b'));
  });
  return h('div', 'table-box', h('table', 'table', caption, h('thead', '', head), h('tbody', '', ...body)));
}
