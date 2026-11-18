// Guides view (`#/guides`): every unit's usage guide, the file `docs/usage-guide.md` of its worktree. The unit's own comes
// first, with "Edit" (the Code view at the file) or, while it has none, "Start from the template". The other units' guides
// follow, read-only, each with "Run with this harness", which opens the Runs form with that unit's harness picked
// (`#/runs?overlay_from=unit-04`). The facilitator reads them all. Personal mode has one unit: only its own guide. A
// guide is put on the page as text: nothing from the server is parsed as HTML.

import { get, post } from '../api.js';
import { codeLink } from '../route.js';

const GUIDE = 'docs/usage-guide.md';
const NOTES = {
  missing: 'No guide yet.',
  'too long': 'This guide is too long to show here.',
  unreadable: 'This guide cannot be read.',
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

function loadStyles() {
  if (document.getElementById('guides-css')) return;
  document.head.append(h('link', { id: 'guides-css', rel: 'stylesheet', href: '/web/views/guides.css' }));
}

function head(admin, personal = false) {
  return h(
    'header',
    { class: 'page-head' },
    h('h1', { text: 'Guides' }),
    h('p', {
      class: 'page-sub',
      text: admin
        ? 'Every unit’s usage guide to its harness, read-only.'
        : personal
          ? 'A usage guide says what a workflow is for, how to start it, what it costs and what to do when it fails: enough for a colleague to run it without asking you.'
          : 'Each unit writes a short usage guide to its harness. Write yours, then follow another unit’s and run your ticket with its harness.',
    }),
  );
}

export async function mount(el, ctx) {
  loadStyles();
  const view = (ui = { ctx, el, admin: ctx.me.subject === 'admin' });
  el.scrollTop = 0;
  el.replaceChildren(h('div', { class: 'page guides' }, head(view.admin, Boolean(view.ctx.me.personal)), h('p', { class: 'empty-line', text: 'Loading the guides…' })));
  let guides;
  try {
    guides = (await get('/api/guides')).guides;
  } catch (err) {
    if (ui !== view || err.status === 401) return; // gone, or the login page is on its way
    el.replaceChildren(
      h('div', { class: 'page guides' }, head(view.admin, Boolean(view.ctx.me.personal)), h('div', { class: 'notice' }, h('p', { text: `The guides could not be loaded: ${err.message}` }), h('p', { text: 'Reload the page to try again.' }))),
    );
    return;
  }
  if (ui === view) paint(view, guides);
}

export function unmount() {
  ui = null; // calls still on their way see that the view is gone
}

function paint(view, guides) {
  const mine = view.ctx.me.unit?.id ?? null;
  const own = guides.find((g) => g.unit === mine) ?? null;
  const others = guides.filter((g) => g.unit !== mine);
  const page = h('div', { class: 'page guides' }, head(view.admin, Boolean(view.ctx.me.personal)));
  if (mine) page.append(ownSection(view, own ?? { unit: mine, name: '', state: 'missing' }));
  if (!view.ctx.me.personal) page.append(othersSection(view, mine, others));
  view.el.replaceChildren(page);
}

// The room's console: the other units' guides, each with "Run with this harness" (personal mode has no such section).
function othersSection(view, mine, others) {
  return h(
    'section',
    { class: 'section', 'aria-labelledby': 'guides-others' },
    h('div', { class: 'section-head' }, h('h2', { id: 'guides-others', text: mine ? 'Other units’ guides' : 'Units’ guides' })),
    mine
      ? h('p', { class: 'section-sub', text: 'Read-only. “Run with this harness” starts a run of your ticket with that unit’s agents, skills, instructions, hooks, workflows and prompts as it saved them. Your unit branch is not changed; the run costs credits like any run.' })
      : null,
    others.length ? h('ul', { class: 'guide-list' }, others.map((guide) => h('li', {}, otherCard(guide, mine !== null)))) : h('p', { class: 'empty-line', text: 'There are no other units yet.' }),
  );
}

const guideText = (guide) =>
  guide.state === 'ok' && typeof guide.text === 'string' ? h('pre', { class: 'guide-text', tabindex: '0', text: guide.text }) : null;

function ownSection(view, guide) {
  const missing = guide.state === 'missing';
  const action = missing
    ? h('button', { type: 'button', class: 'btn primary', text: 'Start from the template' })
    : h('a', { class: 'btn primary', href: codeLink(GUIDE), text: 'Edit' });
  if (missing) action.addEventListener('click', () => startGuide(view, action));
  const body = guideText(guide) ?? h('div', { class: 'guide-none' }, missing
    ? [h('p', { text: 'You have no guide yet.' }), h('p', { class: 'muted', text: 'Start from the template: it has the sections to fill in. Then edit it like any file.' })]
    : h('p', { text: NOTES[guide.state] ?? 'No guide.' }));
  return h(
    'section',
    { class: 'section', 'aria-labelledby': 'guides-own' },
    h('div', { class: 'section-head' }, h('h2', { id: 'guides-own', text: 'Your guide' }), h('span', { class: 'spacer' }), action),
    h('div', { class: 'box guide own' }, h('p', { class: 'guide-path' }, h('code', { text: GUIDE }), missing ? null : h('span', { class: 'muted', text: ' · in the Code view' })), body),
  );
}

const plain = (text) => String(text ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');

function otherCard(guide, canRun) {
  const own = guide.name && plain(guide.name) !== plain(guide.unit); // a name of its own, not "Unit 02" for unit-02
  const label = own ? guide.name : guide.unit.replace(/^unit-/, 'Unit ');
  const title = h('h3', { class: 'guide-name' }, label, own ? h('span', { class: 'guide-unit', text: guide.unit }) : null);
  const run = canRun ? h('a', { class: 'btn', href: `#/runs?overlay_from=${encodeURIComponent(guide.unit)}`, text: 'Run with this harness' }) : null;
  return h(
    'article',
    { class: 'box guide', 'aria-label': `Guide of ${guide.unit}` },
    h('div', { class: 'guide-head' }, title, h('span', { class: 'spacer' }), run),
    guideText(guide) ?? h('p', { class: 'empty-line', text: guide.state === 'missing' ? `${label} has not written its guide yet.` : NOTES[guide.state] ?? 'No guide.' }),
  );
}

async function startGuide(view, button) {
  button.disabled = true;
  try {
    await post('/api/guides/start');
    if (ui !== view) return;
    view.ctx.toast('Your guide is started. Fill it in.', 'ok');
    location.hash = codeLink(GUIDE);
  } catch (err) {
    if (ui !== view || err.status === 401) return;
    const why = err.status === 409 && /running/.test(err.message) ? 'a run is using your files. Start it when the run has ended.' : err.message;
    view.ctx.toast(`Could not start your guide: ${why}`, 'error');
    button.disabled = false;
  }
}
