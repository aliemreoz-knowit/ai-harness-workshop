// The app shell: login, the top bar, the left nav, the step strip, the hash router and the "CLI equivalent" drawer.
//
// The nav has groups (Today and Blueprint; Your harness; Team; Admin for the facilitator) and the run queue at its foot; under 900 px
// it is a sheet behind the menu button. The top bar shows the unit, the block of the day with its time, the credits
// ("38 of 60 credits" with a thin bar; in personal mode "38 of 60 credits today") and "Reconnecting…". The step strip under it (every page but Today, units only)
// shows the next step of the current block from the guide (guide.js): a link to where it is done, "Done", and "×" to
// hide it for this block; its text opens Today. The default page is Today for a unit, Admin for the facilitator.
//
// A view is a module in views/<name>.js with `mount(el, ctx)` and `unmount()`; it is loaded the first time its route
// is opened. A route whose module is not there yet shows "coming soon". `ctx` is what a view gets:
//   me          {subject, unit: {id, name, level, spent, budget, left, locked} | null}, kept current; personal
//               mode adds personal: true and folder (where the console keeps the unit's files: the CLI drawer's cd)
//   api         api.js (get, post, stream)
//   route       {name, args, params}: `#/runs/42` is {name: 'runs', args: ['42'], params: {}}; the query of
//               `#/code?path=a.c&line=3` is params {path: 'a.c', line: '3'} (see route.js)
//   toast(msg, kind)   kind is 'info' (default), 'ok' or 'error'
//   cli(text)   sets the command shown in the CLI drawer ("" for none)
//   refreshMe() reloads `me` (credits) and redraws the top bar
//   guideChanged() the guide's progress changed (Today ticked a step, pinned a block): the top bar and strip follow
//   onGlobal(fn, onOpen) `fn({type, data})` runs for every event of the one global stream (`run`, with the public
//               fields of a run), and `onOpen()` (optional) each time it connects again (events may have been missed);
//               returns the function that stops both. The shell owns the stream, so a view must not open its own
//
// The foot of the nav shows the run queue: "Agent runs 2 of 3 · Gate runs 0 of 2 · your run: #2 in queue", from
// GET /api/runs/queue, read again after the global stream says a run changed (and whenever it reconnects); the top bar
// says "Reconnecting…" while the global stream has been away for a few seconds. The stream is closed once the tab has been
// hidden for 30 s (api.js): a browser has few connections to the console for all its tabs.

import * as api from './api.js';
import { inFolder } from './cli.js';
import { DAY, blockName, currentBlock, fill, nextStep, readProgress, slotAt, timeText, writeProgress } from './guide.js';
import { ICONS, actionLink, h, icon } from './guideui.js';
import { NAV_GROUPS, navGroups, parseHash } from './route.js';
import { isFinal, queueSummary } from './runmodel.js';

const VIEWS = NAV_GROUPS.flatMap((group) => group.views); // the router's pages; personal mode takes the shared ones out (see `route`)
const CLOCK_MS = 30000; // how often the top bar's block and the step strip look at the clock
const MAX_TOASTS = 4;
const SESSION_ENDED = 'Your session ended. Log in again. Unsaved edits in this tab are kept: log in with the same code, then save.';

const root = document.getElementById('app');
const toasts = document.getElementById('toasts');

const ctx = { me: null, api, route: { name: '', args: [], params: {} }, toast, cli, refreshMe, onGlobal, guideChanged };
let shell = null; // elements of the shell; null while the login page is up
let mounted = null; // the view module on show (the login page counts)
let routing = 0; // counts route changes: a slow one that was overtaken stops
let cliText = '';
let cliOpen = false;
let projector = false; // the page is the projector board: no login, no shell
let queue = null; // the last GET /api/runs/queue
let queueTimer = null;
let stopGlobal = null; // closes the global stream; null while there is none
let globalDown = false; // the global stream has been away for a while: "Reconnecting…"
let clockTimer = null; // moves the top bar's block and the step strip on with the clock
const globalListeners = new Set(); // {fn, onOpen} of the views

// --- toasts ----------------------------------------------------------------------------------------------------------------

function toast(message, kind = 'info') {
  const el = document.createElement('div');
  el.className = `toast ${kind}`;
  el.setAttribute('role', kind === 'error' ? 'alert' : 'status');
  el.textContent = message;
  el.addEventListener('click', () => el.remove());
  toasts.append(el);
  while (toasts.children.length > MAX_TOASTS) toasts.firstElementChild.remove();
  setTimeout(() => el.remove(), kind === 'error' ? 10000 : 4000);
}

// --- start and login -------------------------------------------------------------------------------------------------------

api.onUnauthorized(() => {
  if (ctx.me) showLogin(SESSION_ENDED);
});
window.addEventListener('hashchange', () => {
  if (Boolean(projectorRoute()) !== projector) location.reload(); // the page changes kind: start over
  else if (ctx.me) route();
});
document.addEventListener('visibilitychange', () => {
  if (!ctx.me || document.hidden) return;
  refreshMe();
  refreshQueue();
});

async function boot() {
  const here = projectorRoute();
  if (here) return showProjector(here);
  let who;
  try {
    who = await api.get('/api/me');
  } catch (err) {
    root.innerHTML = '<p class="plain"></p><p class="plain"><button type="button" class="btn">Try again</button></p>';
    root.firstElementChild.textContent = `Cannot reach the console: ${err.message}`;
    root.querySelector('button').addEventListener('click', boot);
    return;
  }
  if (who.subject) start(who);
  else showLogin();
}

function start(who) {
  closeGlobal(); // a tab that was logged in as another unit
  ctx.me = who;
  buildShell();
  openGlobal();
  if (!location.hash || location.hash === '#') history.replaceState(null, '', `#/${home()}`);
  route();
}

/** The page a person lands on: Today for a unit, Admin for the facilitator. */
const home = () => (ctx.me?.subject === 'admin' ? 'admin' : 'today');

// The projector board, `#/board?projector=1`, is the one page that needs no login and has no shell: it shows only public
// data (the harness's two board pages, the run queue, every unit's credits). It starts before /api/me is asked.
function projectorRoute() {
  const here = parseHash(location.hash, 'today');
  return here?.name === 'board' && here.params.projector === '1' ? here : null;
}

async function showProjector(here) {
  projector = true;
  ctx.route = here;
  document.title = 'Board - Harness Console';
  root.replaceChildren();
  try {
    const view = await import('./views/board.js');
    mounted = view;
    await view.mount(root, ctx);
  } catch (err) {
    console.error(err);
    root.innerHTML = '<p class="plain"></p>';
    root.firstElementChild.textContent = `The board could not start: ${err.message}`;
  }
}

async function showLogin(note) {
  leaveView();
  closeGlobal();
  clearInterval(clockTimer);
  ctx.me = null;
  shell = null;
  routing += 1;
  document.title = 'Harness Console';
  root.replaceChildren();
  const host = document.createElement('div');
  root.append(host);
  const id = routing;
  const login = await import('./views/login.js');
  if (id !== routing) return;
  mounted = login;
  login.mount(host, { api, toast, note, onLogin: start });
}

async function logout() {
  try {
    await api.post('/api/logout');
  } catch (err) {
    toast(`Could not log out: ${err.message}`, 'error');
    return;
  }
  showLogin();
}

async function refreshMe() {
  let who;
  try {
    who = await api.get('/api/me');
  } catch {
    return; // the credits stay as they were; the next call tells the person if the console is gone
  }
  if (!ctx.me) return; // logged out meanwhile
  if (!who.subject) {
    showLogin(SESSION_ENDED);
  } else if (who.subject !== ctx.me.subject || who.unit?.id !== ctx.me.unit?.id) {
    // The cookie is shared by every tab of this browser: someone logged in as another unit in another tab, and from
    // now on this tab's requests act on that unit. Show that unit, not the one this page was started for.
    leaveView();
    toast(`This browser is now logged in as ${who.unit?.id ?? who.subject}.`);
    start(who);
  } else {
    ctx.me = who;
    paintTopBar();
  }
}

// --- the shell ---------------------------------------------------------------------------------------------------------------

function buildShell() {
  // fixed markup: nothing from the server goes in here (names and numbers are set as text below)
  root.innerHTML = `
    <div class="shell" data-ref="shell">
      <header class="topbar">
        <button type="button" class="btn quiet icon menu-button" data-ref="menu" aria-controls="console-nav" aria-expanded="false" aria-label="Menu"></button>
        <span class="brand"><span class="brand-mark" aria-hidden="true"></span><span class="app-name">Harness Console</span></span>
        <span class="who" data-ref="who"></span>
        <a class="day-slot" data-ref="day" href="#/today">
          <span class="day-name" data-ref="dayName"></span>
          <span class="day-time" data-ref="dayTime"></span>
        </a>
        <span class="spacer"></span>
        <span class="link-down" data-ref="down" role="status" hidden>Reconnecting…</span>
        <div class="credits" data-ref="credits" hidden>
          <span class="credits-text" data-ref="creditsText"></span>
          <div class="credits-bar" role="meter" aria-label="Credits used" aria-valuemin="0"><div></div></div>
        </div>
      </header>
      <div class="strip" data-ref="strip" role="region" aria-label="Next step" hidden></div>
      <div class="scrim" data-ref="scrim"></div>
      <nav class="nav" id="console-nav" data-ref="nav" aria-label="Pages">
        <div class="nav-head">
          <span class="brand"><span class="brand-mark" aria-hidden="true"></span><span class="app-name">Harness Console</span></span>
          <button type="button" class="btn quiet icon nav-close" data-ref="close" aria-label="Close the menu"></button>
        </div>
        <div class="nav-links" data-ref="links"></div>
        <div class="nav-foot">
          <a class="queue" data-ref="queue" href="#/runs" hidden></a>
          <div class="nav-tools">
            <button type="button" class="btn small quiet" data-ref="cliButton" aria-controls="cli-drawer" aria-expanded="false">CLI equivalent</button>
            <span class="spacer"></span>
            <button type="button" class="btn small quiet" data-ref="logout">Log out</button>
          </div>
        </div>
      </nav>
      <main class="main" data-ref="view"></main>
      <aside class="drawer" id="cli-drawer" data-ref="drawer" aria-label="CLI equivalent" hidden>
        <span class="drawer-label">CLI equivalent</span>
        <pre class="drawer-text" data-ref="cliText"></pre>
        <button type="button" class="btn small" data-ref="copy">Copy</button>
      </aside>
    </div>`;
  shell = Object.fromEntries([...root.querySelectorAll('[data-ref]')].map((el) => [el.dataset.ref, el]));
  shell.menu.append(icon('menu'));
  shell.close.append(icon('close'));

  const admin = ctx.me.subject === 'admin';
  for (const group of navGroups(ctx.me.personal === true, admin)) {
    const views = group.views;
    const list = h('ul', `nav-group${group.admin ? ' nav-group-admin' : ''}`);
    for (const view of views) {
      const mark = view.icon ?? view.name;
      const link = h('a', 'nav-link', ICONS[mark] ? icon(mark) : null, view.label);
      link.href = `#/${view.name}`;
      link.dataset.view = view.name;
      list.append(h('li', '', link));
    }
    if (group.title) {
      const title = h('p', 'nav-title', group.title);
      title.id = `nav-${group.title.toLowerCase().replace(/\W+/g, '-')}`;
      list.setAttribute('aria-labelledby', title.id);
      shell.links.append(h('div', 'nav-group-box', title, list));
    } else {
      shell.links.append(list);
    }
  }
  shell.menu.addEventListener('click', () => setSheet(!shell.shell.classList.contains('nav-open')));
  shell.scrim.addEventListener('click', () => setSheet(false));
  shell.close.addEventListener('click', () => {
    setSheet(false);
    shell.menu.focus();
  });
  shell.nav.addEventListener('keydown', (event) => {
    if (event.key === 'Escape' && shell.shell.classList.contains('nav-open')) {
      setSheet(false);
      shell.menu.focus();
    }
  });
  shell.cliButton.addEventListener('click', () => {
    cliOpen = !cliOpen;
    paintCli();
    setSheet(false);
  });
  shell.copy.addEventListener('click', copyCli);
  shell.logout.addEventListener('click', logout);
  paintTopBar();
  paintDay();
  paintCli();
  paintQueue();
  paintDown();
  refreshQueue();
  clearInterval(clockTimer);
  clockTimer = setInterval(() => {
    paintDay();
    paintStrip();
  }, CLOCK_MS);
}

/** Open or close the nav sheet (under 900 px; wider, the nav is always there and this changes nothing on screen). */
function setSheet(open) {
  if (!shell) return;
  shell.shell.classList.toggle('nav-open', open);
  shell.menu.setAttribute('aria-expanded', String(open));
  if (open) shell.links.querySelector('[aria-current="page"], a')?.focus();
}

const credits = (n) => (Number.isInteger(n) ? String(n) : n.toFixed(1));

/** ["Unit 04", the name the unit gave itself or ""]. */
function unitLabel(unit) {
  const number = /^unit-(\d+)$/.exec(unit.id)?.[1];
  const label = number ? `Unit ${number}` : unit.id;
  const plain = (text) => String(text ?? '').toLowerCase().replace(/[^a-z0-9]/g, '');
  const own = unit.name && plain(unit.name) !== plain(unit.id) && plain(unit.name) !== plain(label);
  return [label, own ? unit.name : ''];
}

function paintTopBar() {
  if (!shell) return;
  const { unit, subject } = ctx.me;
  if (unit && ctx.me.personal) {
    shell.who.replaceChildren(); // personal mode: one checkout, nobody to tell apart
    shell.who.title = '';
  } else if (unit) {
    const [label, name] = unitLabel(unit);
    shell.who.replaceChildren(label, name ? h('span', 'who-name', ` · ${name}`) : '');
    shell.who.title = unit.id;
  } else {
    shell.who.textContent = subject === 'admin' ? 'Facilitator' : '';
  }
  shell.credits.hidden = !unit;
  shell.logout.hidden = ctx.me.personal === true; // personal mode has no login to leave
  if (!unit) return;
  const share = unit.budget > 0 ? Math.min(1, unit.spent / unit.budget) : 1;
  const meter = shell.credits.querySelector('.credits-bar');
  meter.setAttribute('aria-valuemax', String(unit.budget));
  meter.setAttribute('aria-valuenow', String(unit.spent));
  meter.firstElementChild.style.width = `${Math.round(share * 100)}%`;
  shell.credits.classList.toggle('low', share >= 0.85 && share < 1);
  shell.credits.classList.toggle('out', share >= 1);
  // personal mode counts the credits per day: "12 of 60 credits today"
  shell.creditsText.textContent = `${credits(unit.spent)} of ${credits(unit.budget)} credits${ctx.me.personal ? ' today' : ''}`;
  shell.credits.title = ctx.me.personal
    ? `Credits used today of your daily budget: ${credits(unit.left)} left`
    : `Credits your unit has used of its budget: ${credits(unit.left)} left`;
}

// --- the guide in the shell: the block of the day in the top bar, the next step in the strip --------------------------------

/** Whose progress the guide keeps: the unit's, or the facilitator's own. */
const guideKey = () => ctx.me.unit?.id ?? ctx.me.subject;

/** The block (or break) of the day in the top bar: from the clock on the day, else the block the guide is at. */
function paintDay() {
  if (!shell) return;
  const now = new Date();
  const slot = slotAt(now);
  const shown = slot ?? currentBlock(now, readProgress(guideKey())).block;
  shell.dayName.textContent = shown.label;
  shell.dayTime.textContent = slot ? timeText(shown) : `Fri ${timeText(shown)}`;
  shell.day.title = slot
    ? `${shown.kind === 'block' ? blockName(shown) : `${shown.label}: ${shown.note}`}. Open Today.`
    : `${blockName(shown)}, ${DAY.label}. Open Today.`;
}

/** Today ticked a step or pinned a block: the top bar and the strip follow. */
function guideChanged() {
  paintDay();
  paintStrip();
}

/** The step strip: the next step of the current block, on every page but Today, for a unit that has not hidden it. */
function paintStrip() {
  if (!shell) return;
  const strip = shell.strip;
  const unitId = ctx.me.unit?.id;
  const progress = readProgress(guideKey());
  const { block } = currentBlock(new Date(), progress);
  if (!unitId || ctx.route.name === 'today' || progress.hidden.has(block.id)) {
    strip.hidden = true;
    strip.replaceChildren();
    return;
  }
  const next = nextStep(block, progress.checked);
  const text = h('a', 'strip-text');
  text.href = '#/today';
  text.title = 'Open Today';
  const actions = h('div', 'strip-actions');
  if (next) {
    const options = next.options > 1 ? ` (or one of the other ${next.options - 1} on Today)` : '';
    text.append(h('span', 'strip-block', `${block.label}, step ${next.number} of ${next.total}: `), fill(next.step.title, unitId), options);
    const done = h('button', 'btn small', 'Done');
    done.type = 'button';
    done.title = `Done when: ${fill(next.step.done, unitId)}`;
    done.addEventListener('click', () => {
      const checked = new Set(progress.checked);
      checked.add(next.step.id);
      writeProgress(guideKey(), { ...progress, checked });
      guideChanged();
      (shell?.strip.querySelector('.strip-actions .btn') ?? shell?.strip)?.focus?.();
    });
    if (next.step.action) actions.append(actionLink(next.step.action, unitId, 'btn small'));
    actions.append(done); // secondary: the page's own primary is the one accent
  } else {
    text.append(h('span', 'strip-block', `${block.label}: `), block.steps.length ? 'every step is done. Look ahead on Today.' : (block.note ?? ''));
  }
  const hide = h('button', 'btn small quiet icon', icon('close'));
  hide.type = 'button';
  hide.setAttribute('aria-label', `Hide the next step for ${block.label}`);
  hide.title = `Hide for ${block.label}; Today can show it again`;
  hide.addEventListener('click', () => {
    const hidden = new Set(progress.hidden);
    hidden.add(block.id);
    writeProgress(guideKey(), { ...progress, hidden });
    paintStrip();
  });
  actions.append(hide);
  strip.replaceChildren(h('span', 'strip-label', 'Next'), text, actions);
  strip.hidden = false;
}

// --- the global stream and the queue widget ------------------------------------------------------------------------------------

function onGlobal(fn, onOpen = null) {
  const listener = { fn, onOpen };
  globalListeners.add(listener);
  return () => globalListeners.delete(listener);
}

function openGlobal() {
  globalDown = false;
  stopGlobal = api.stream('/api/stream', onGlobalEvent, ['run'], onGlobalOpen, {
    onDown: (down) => {
      globalDown = down;
      paintDown();
    },
  });
}

function closeGlobal() {
  stopGlobal?.();
  stopGlobal = null;
  globalDown = false;
  clearTimeout(queueTimer);
  queue = null;
}

function tell(listeners, call) {
  for (const listener of listeners) {
    try {
      call(listener);
    } catch (err) {
      console.error(err);
    }
  }
}

// A (re)connect may have missed events: the queue is read again, and the views are told.
function onGlobalOpen() {
  refreshQueue();
  tell([...globalListeners], (listener) => listener.onOpen?.());
}

function onGlobalEvent(event) {
  tell([...globalListeners], (listener) => listener.fn(event));
  refreshQueue();
  // the credits of a run are charged when it ends: its unit's top bar follows
  if (ctx.me?.unit && event.data?.unit === ctx.me.unit.id && isFinal(event.data.status)) refreshMe();
}

/** Read the queue soon (a burst of events makes one request). */
function refreshQueue() {
  clearTimeout(queueTimer);
  queueTimer = setTimeout(async () => {
    let reply;
    try {
      reply = await api.get('/api/runs/queue');
    } catch {
      return; // the widget keeps what it had
    }
    if (!ctx.me || !stopGlobal) return; // logged out meanwhile
    queue = reply;
    paintQueue();
  }, 250);
}

function paintQueue() {
  if (!shell) return;
  const { text, title, mine } = queueSummary(queue, ctx.me.unit?.id ?? null);
  if (ctx.me.personal === true && queue?.lanes && typeof queue.lanes === 'object') {
    // one computer, one run of each kind at a time: say whether one is going, not "0 of 1"
    const names = { agent: 'Agent run', gates: 'Gate check' };
    const parts = Object.entries(queue.lanes).map(([lane, state]) => {
      const going = Number(state?.going) > 0;
      const waits = Number(state?.queued) > 0 ? `, ${state.queued} waiting` : '';
      return h('span', going ? 'going' : '', h('i', 'dot'), `${names[lane] ?? lane}: ${going ? 'running' : 'none running'}${waits}`);
    });
    shell.queue.hidden = parts.length === 0;
    shell.queue.replaceChildren(...parts);
    shell.queue.title = 'What is running on this computer right now: one agent run and one gate check can go at a time. Opens Runs.';
    shell.queue.href = mine ? `#/runs/${encodeURIComponent(mine)}` : '#/runs';
    return;
  }
  shell.queue.hidden = !text;
  // one line per part: the foot of the nav is narrow
  shell.queue.replaceChildren(...(text ? text.split(' \u00b7 ') : []).map((part) => h('span', part.startsWith('your run') ? 'mine' : '', part)));
  shell.queue.title = title;
  shell.queue.href = mine ? `#/runs/${encodeURIComponent(mine)}` : '#/runs';
}

function paintDown() {
  if (shell) shell.down.hidden = !globalDown;
}

function cli(text) {
  cliText = text || '';
  paintCli();
}

// Personal mode: the console saves in its own copy of the checkout (`folder` of /api/me), so the command cds there.
const shownCli = () => inFolder(cliText, ctx.me?.personal === true ? ctx.me.folder : '');

function paintCli() {
  if (!shell) return;
  shell.drawer.hidden = !cliOpen;
  shell.cliButton.setAttribute('aria-expanded', String(cliOpen));
  shell.cliText.textContent = shownCli() || 'There is no command for this view.';
  shell.cliText.classList.toggle('none', !cliText);
  shell.copy.hidden = !cliText;
}

async function copyCli() {
  try {
    await navigator.clipboard.writeText(shownCli());
  } catch {
    // no clipboard API on plain http (a LAN address is not a secure context): select the text and use the old way
    const range = document.createRange();
    range.selectNodeContents(shell.cliText);
    const selection = getSelection();
    selection.removeAllRanges();
    selection.addRange(range);
    if (!document.execCommand('copy')) {
      toast('Select the command and copy it.', 'error');
      return;
    }
  }
  toast('Copied.', 'ok');
}

// --- the router ------------------------------------------------------------------------------------------------------------------

function leaveView() {
  const view = mounted;
  mounted = null;
  if (!view) return;
  try {
    view.unmount();
  } catch (err) {
    console.error(err);
  }
}

async function route() {
  const id = ++routing;
  const { name, args, params } = parseHash(location.hash, home()) ?? { name: '', args: [], params: {} }; // null: a broken %-escape
  const entry = VIEWS.find((v) => v.name === name && (!v.adminOnly || ctx.me.subject === 'admin') && !(ctx.me.personal === true && v.shared));
  if (!entry) { // an unknown view, or a hash that does not parse: the person's first page
    history.replaceState(null, '', `#/${home()}`);
    return route();
  }
  ctx.route = { name, args, params };
  document.title = `${entry.label} - Harness Console`;
  for (const link of shell.links.querySelectorAll('.nav-link')) {
    if (link.dataset.view === name) link.setAttribute('aria-current', 'page');
    else link.removeAttribute('aria-current');
  }
  setSheet(false);
  paintStrip();

  leaveView();
  cli('');
  shell.view.replaceChildren();
  refreshMe();

  let module;
  try {
    module = await import(`./views/${name}.js`);
  } catch (err) {
    if (id === routing) await showMissing(entry, err);
    return;
  }
  if (id !== routing) return;
  mounted = module; // before the mount finishes, so that leaving during a slow mount still calls unmount
  try {
    await module.mount(shell.view, ctx);
  } catch (err) {
    console.error(err);
    if (id === routing) showPanel(`${entry.label} could not start.`, err.message);
  }
}

// A view whose file is not on the server is one that is not built yet; a file that is there but broke is an error.
async function showMissing(entry, err) {
  let missing = false;
  try {
    missing = (await fetch(new URL(`./views/${entry.name}.js`, import.meta.url))).status === 404;
  } catch {
    /* cannot tell: treat as broken */
  }
  if (missing) showPanel(`${entry.label} is coming soon.`, 'This part of the console is not installed yet.');
  else {
    console.error(err);
    showPanel(`${entry.label} could not be loaded.`, err.message);
  }
}

function showPanel(title, detail) {
  const box = document.createElement('div');
  box.className = 'panel-note';
  const heading = document.createElement('h2');
  heading.textContent = title;
  const text = document.createElement('p');
  text.textContent = detail;
  box.append(heading, text);
  shell.view.replaceChildren(box);
}

boot();
