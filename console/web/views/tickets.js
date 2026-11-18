// Tickets view. `#/tickets` is the list of the Jira tickets of the workshop project, each with whether the unit has
// written its agent-ready version. `#/tickets/<KEY>` is one ticket: its Jira text (read-only) next to an editor with
// the agent-ready version, which is saved as tickets/<KEY>.md in the unit's worktree through the ordinary file
// (personal mode: the tickets are the repo's files, "From the repo"; tickets/<KEY>.md is then the source, so the
// agent-ready version is tickets/<KEY>.agent.md, the `path` and `suffix` the server names)
// endpoint, and beside the editor the sections of an agent-ready ticket as a checklist (ticked when the draft has that
// `## ` heading). "Run this ticket" saves it first when needed, then opens the Runs view with the file.
//
// Everything that comes from Jira is shown with textContent (a ticket is untrusted text: it is never HTML); the
// ticket's markdown is formatted by markdown.js, which builds the nodes the same way, or shown as it is ("Markdown"). Edits that
// are not saved stay when the person visits another view and comes back (`drafts` lives as long as the page). The look
// is views/tickets.css.

import { get, post } from '../api.js';
import { codeCommand } from '../cli.js';
import { MONO_FONT, languageFor, loadMonaco } from '../monaco.js';
import { NO_ACCEPTANCE_TESTS, acceptanceTests, sectionChecklist } from '../ticketcheck.js';
import { renderMarkdown } from '../markdown.js';

// --- state that outlives a visit ---------------------------------------------------------------------------------

/** Drafts by ticket key: {key, path, jira, model, text, sha, exists, saved, viewState}.
 *  `sha` is the version of the file the text was read from ("" while there is no file); `saved` is "unchanged". */
const drafts = new Map();
let draftsUnit = null; // whose drafts these are
let lastKey = null;
let jiraShown = 'page'; // the ticket text: 'page' (formatted) or 'source' (its markdown)
let monaco = null; // the Monaco namespace once loaded, else null (plain text box)
let ui = null; // the mounted view, null when none
let personal = false; // personal mode: the tickets are files of the repo, not Jira's (set by mount)
let suffix = '.md'; // the end of the agent-ready file's name (the list answers it in personal mode)

window.addEventListener('beforeunload', (event) => {
  if ([...drafts.values()].some(isDirty)) event.preventDefault(); // the browser asks before the edits are lost
});

// a check mark for a ticked section: fixed markup, never data
const TICK =
  '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="2" stroke-linecap="round" ' +
  'stroke-linejoin="round" aria-hidden="true" focusable="false"><path d="M3.5 8.5l3 3 6-7"/></svg>';

const LIST_TEMPLATE = `
<div class="tk-page">
  <header class="tk-head">
    <div class="tk-head-text">
      <h1>Tickets</h1>
      <p class="page-sub" data-ref="sub"></p>
    </div>
    <button type="button" class="btn quiet" data-ref="reload">Reload</button>
  </header>
  <p class="tk-message" data-ref="listNote" role="status" hidden></p>
  <ul class="tk-list" data-ref="list" aria-label="Tickets"></ul>
</div>`;

const TICKET_TEMPLATE = `
<div class="tk-ticket">
  <header class="tk-ticket-head">
    <a class="tk-back" href="#/tickets">← All tickets</a>
    <h1 class="tk-title" data-ref="title"></h1>
    <p class="tk-meta" data-ref="meta"></p>
  </header>
  <div class="code-bar">
    <span class="code-path" data-ref="path"></span>
    <span class="code-state" data-ref="state" aria-live="polite"></span>
    <button type="button" class="btn" data-ref="save" disabled>Save</button>
    <button type="button" class="btn primary" data-ref="run" disabled>Run this ticket</button>
  </div>
  <p class="note" data-ref="editorNote" hidden></p>
  <div class="tk-split" data-ref="split" hidden>
    <section class="tk-pane tk-jira" aria-label="Ticket text">
      <div class="caption tk-jira-bar">
        <span data-ref="sourceCaption"></span>
        <span class="view-switch" role="group" aria-label="Show the ticket">
          <button type="button" class="view-switch-btn" data-ref="showPage" aria-pressed="true">Formatted</button>
          <button type="button" class="view-switch-btn" data-ref="showSource" aria-pressed="false">Markdown</button>
        </span>
      </div>
      <div class="jira-text" data-ref="jira" tabindex="0"></div>
    </section>
    <section class="tk-pane tk-mine" aria-label="Your version">
      <div class="caption tk-jira-bar">
        <span>Your version for the agent</span>
        <span class="view-switch" role="group" aria-label="Show your version">
          <button type="button" class="view-switch-btn" data-ref="mineEdit" aria-pressed="true">Edit</button>
          <button type="button" class="view-switch-btn" data-ref="minePreview" aria-pressed="false">Preview</button>
        </span>
      </div>
      <div class="ticket-editor" data-ref="editorBox"><div class="editor-host" data-ref="host"></div></div>
      <div class="jira-text tk-preview" data-ref="preview" tabindex="0" hidden></div>
    </section>
    <aside class="tk-sections" aria-labelledby="tk-sections-title">
      <div class="tk-sections-head">
        <h2 id="tk-sections-title">Agent-ready sections</h2>
        <span class="tk-sections-count" data-ref="count"></span>
      </div>
      <p class="tk-sections-hint">Each one is a <code>##</code> heading in your version; a tick shows it is there.</p>
      <ul class="tk-checks" data-ref="checks"></ul>
      <p class="tk-warning" data-ref="warning" role="status" hidden></p>
    </aside>
  </div>
  <p class="empty" data-ref="empty">Opening the ticket…</p>
  <div class="errors" data-ref="errors" role="alert" hidden>
    <p>Not saved:</p>
    <ul></ul>
  </div>
</div>`;

// --- drafts ----------------------------------------------------------------------------------------------------------

// What "unchanged" means: Monaco's version id (undo back to the start is clean again), or the text itself.
const snapshot = (draft) => (draft.model ? draft.model.getAlternativeVersionId() : draft.text);
const valueOf = (draft) => (draft.model ? draft.model.getValue(monaco.editor.EndOfLinePreference.LF) : draft.text);
const isDirty = (draft) => snapshot(draft) !== draft.saved;
const needsSave = (draft) => !draft.exists || isDirty(draft); // a template nobody has saved is not on disk yet

function makeDraft(key, text, sha, exists, path = `tickets/${key}.md`) {
  const draft = { key, path, jira: '', model: null, text, sha, exists, saved: null, viewState: null };
  if (monaco) draft.model = monaco.editor.createModel(text, languageFor(path));
  draft.saved = snapshot(draft);
  return draft;
}

function setValue(draft, text) {
  if (draft.model) draft.model.setValue(text);
  else draft.text = text;
}

function disposeDraft(draft) {
  drafts.delete(draft.key);
  draft.model?.dispose();
}

// --- small DOM helpers (text only) -----------------------------------------------------------------------------------

function h(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

const refsOf = (el) => Object.fromEntries([...el.querySelectorAll('[data-ref]')].map((node) => [node.dataset.ref, node]));

/** Adds views/tickets.css once; resolves when it has loaded (or failed), so the page does not show unstyled. */
function loadStyles() {
  let link = document.getElementById('tickets-css');
  if (!link) {
    link = Object.assign(document.createElement('link'), { id: 'tickets-css', rel: 'stylesheet', href: '/web/views/tickets.css' });
    link.ready = new Promise((done) => {
      link.addEventListener('load', done, { once: true });
      link.addEventListener('error', done, { once: true });
    });
    document.head.append(link);
  }
  return link.ready ?? Promise.resolve();
}

// --- mount and unmount -----------------------------------------------------------------------------------------------

export async function mount(el, ctx) {
  const unit = ctx.me.unit;
  if (!unit) {
    el.innerHTML = '<p class="empty">Log in with a unit code to write tickets.</p>';
    return;
  }
  personal = Boolean(ctx.me.personal);
  suffix = personal ? '.agent.md' : '.md';
  if (draftsUnit !== unit.id) {
    [...drafts.values()].forEach(disposeDraft);
    draftsUnit = unit.id;
    lastKey = null;
  }
  const key = ctx.route.args[0] ?? null;
  const mine = (ui = {
    ctx,
    el,
    root: null,
    refs: null,
    key, // the ticket on show, null for the list
    editor: null, // Monaco editor
    area: null, // textarea, when there is no Monaco
    current: null, // the draft on show
    busy: false,
    openToken: 0,
    rows: [], // the list as the server sent it
  });
  await loadStyles();
  if (ui !== mine) return;
  if (key) await mountTicket(mine, key);
  else await mountList(mine);
}

export function unmount() {
  const mine = ui;
  if (!mine) return;
  ui = null; // every call still waiting for the server sees that the view is gone
  if (mine.editor) {
    if (mine.current) mine.current.viewState = mine.editor.saveViewState();
    mine.editor.dispose(); // the models stay: they hold the unsaved edits
  }
}

// --- the list ------------------------------------------------------------------------------------------------------

async function mountList(view) {
  view.el.innerHTML = LIST_TEMPLATE;
  view.root = view.el.firstElementChild;
  view.refs = refsOf(view.el);
  view.refs.sub.textContent = personal
    ? 'The tickets from the repo (the tickets folder). Open one to write its agent-ready version: the text your runs work from.'
    : 'The Jira tickets of the workshop. Open one to write its agent-ready version: the text your runs work from.';
  view.refs.reload.addEventListener('click', () => loadList(view));
  view.ctx.cli('');
  await loadList(view);
}

/** The ticket list, and which tickets have a saved agent-ready version (the files in tickets/). */
async function loadList(view) {
  const { listNote, list, reload } = view.refs;
  reload.disabled = true;
  if (!view.rows.length) list.replaceChildren(h('li', 'tk-loading', 'Loading the tickets…'));
  const [tickets, files] = await Promise.allSettled([get('/api/tickets'), get('/api/files/tree', { path: 'tickets' })]);
  if (ui !== view) return;
  reload.disabled = false;
  if (tickets.status === 'rejected') {
    const err = tickets.reason;
    if (err.status === 401) return; // the login page is on its way
    list.replaceChildren();
    listNote.textContent = `Could not load the tickets: ${err.message}. Press Reload to try again${personal ? '' : '; if it keeps failing, tell your facilitator'}.`;
    listNote.hidden = false;
    return;
  }
  view.rows = tickets.value.tickets;
  if (typeof tickets.value.suffix === 'string') suffix = tickets.value.suffix;
  const saved = new Set(files.status === 'fulfilled' ? files.value.entries.filter((entry) => !entry.dir).map((entry) => entry.name) : []);
  listNote.textContent = view.rows.length ? '' : personal
    ? 'There are no ticket files in the tickets folder of your checkout. Update your checkout, then press Reload.'
    : 'There are no tickets in the Jira project yet. Ask your facilitator, then press Reload.';
  listNote.hidden = view.rows.length > 0;
  list.replaceChildren(...view.rows.map((row) => listRow(row, saved.has(`${row.key}${suffix}`))));
}

function listRow(row, saved) {
  const link = h('a', 'tk-row', h('code', 'tk-key', row.key), h('span', 'tk-row-title', row.title), h('span', 'tk-row-meta', `${row.type} · ${row.status}`));
  link.href = `#/tickets/${encodeURIComponent(row.key)}`;
  link.title = `${row.key}: ${row.title}`;
  const draft = drafts.get(row.key);
  let state = null;
  if (draft && isDirty(draft)) state = h('span', 'tk-row-state warn', 'Not saved');
  else if (saved) state = h('span', 'tk-row-state ok', 'Agent-ready version saved');
  link.append(state ?? h('span', 'tk-row-state', ''));
  if (row.key === lastKey) link.classList.add('last');
  return h('li', '', link);
}

// --- one ticket ------------------------------------------------------------------------------------------------------

async function mountTicket(view, key) {
  view.el.innerHTML = TICKET_TEMPLATE;
  view.root = view.el.firstElementChild;
  const refs = (view.refs = refsOf(view.el));
  refs.sourceCaption.textContent = personal ? 'From the repo (read-only)' : 'Jira text (read-only)';
  refs.title.append(h('code', 'tk-title-key', key));
  refs.showPage.addEventListener('click', () => setJiraShown(view, 'page'));
  refs.showSource.addEventListener('click', () => setJiraShown(view, 'source'));
  view.preview = false; // your version: the editor (Edit) or the same formatted page as the ticket (Preview)
  refs.mineEdit.addEventListener('click', () => setPreview(view, false));
  refs.minePreview.addEventListener('click', () => setPreview(view, true));
  refs.save.addEventListener('click', () => save());
  refs.run.addEventListener('click', () => run());
  view.root.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 's') {
      event.preventDefault(); // not the browser's "save page"
      save();
    }
  });
  paintBar(view);

  monaco ??= await loadMonaco();
  if (ui !== view) return;
  createEditor(view);
  if (!monaco) {
    refs.editorNote.textContent = 'The editor is not installed (run scripts/vendor_monaco.py): a plain text box is used.';
    refs.editorNote.hidden = false;
  }
  // the list names the ticket's title, type and status (Jira is asked at most once a minute for it)
  get('/api/tickets')
    .then((answer) => {
      const row = answer.tickets.find((one) => one.key === key);
      if (ui !== view || !row) return;
      refs.title.append(' ', h('span', '', row.title));
      refs.meta.textContent = `${row.type} · ${row.status}`;
    })
    .catch(() => {}); // the ticket itself says what went wrong
  await openTicket(view, key);
}

function createEditor(view) {
  const { host } = view.refs;
  if (monaco) {
    view.editor = monaco.editor.create(host, {
      model: null,
      automaticLayout: true,
      minimap: { enabled: false },
      fontFamily: MONO_FONT,
      fontSize: 13,
      wordWrap: 'on',
      lineNumbers: 'off',
      scrollBeyondLastLine: false,
      ariaLabel: 'Ticket editor',
    });
    view.editor.onDidChangeModelContent(() => edited(view));
  } else {
    view.area = document.createElement('textarea');
    view.area.className = 'plain-editor';
    view.area.spellcheck = false;
    view.area.setAttribute('aria-label', 'Ticket editor');
    view.area.addEventListener('input', () => {
      if (view.current) view.current.text = view.area.value;
      edited(view);
    });
    host.append(view.area);
  }
}

async function openTicket(view, key) {
  const token = ++view.openToken;
  let info;
  let file = null;
  try {
    info = await get(`/api/tickets/${encodeURIComponent(key)}`);
    const known = drafts.get(key);
    if (info.saved && !(known && isDirty(known))) file = await get('/api/files/read', { path: info.path ?? `tickets/${key}.md` });
  } catch (err) {
    if (ui !== view || token !== view.openToken) return;
    if (err.status === 401) return; // the login page is on its way
    view.current = null;
    view.refs.split.hidden = true;
    view.refs.empty.replaceChildren(
      err.status === 404 ? `There is no ticket ${key} ${personal ? 'in the repo' : 'in the Jira project'}. ` : `Could not open ${key}: ${err.message}. `,
      Object.assign(h('a', '', 'Back to all tickets'), { href: '#/tickets' }),
    );
    view.refs.empty.hidden = false;
    paintBar(view);
    return;
  }
  if (ui !== view || token !== view.openToken) return;

  let draft = drafts.get(key);
  if (!draft) {
    draft = makeDraft(key, file ? (file.content ?? '') : info.template, file ? file.sha : '', Boolean(file), info.path);
    drafts.set(key, draft);
  } else if (file && file.sha !== draft.sha) {
    setValue(draft, file.content ?? ''); // changed on disk (a run, say) and nothing of ours to lose
    Object.assign(draft, { sha: file.sha, exists: true, saved: snapshot(draft) });
  } else if (!info.saved && draft.exists && !isDirty(draft)) {
    Object.assign(draft, { sha: '', exists: false }); // the file is gone: the text stays, Save writes it again
  }
  draft.jira = info.markdown;
  showDraft(view, draft);
}

/** The ticket text, formatted (markdown.js, textContent only) or as its markdown source. */
function showJira(view, text) {
  const { refs } = view;
  refs.jira.replaceChildren(jiraShown === 'source' ? h('pre', 'jira-source', text) : renderMarkdown(text));
  refs.showPage.setAttribute('aria-pressed', String(jiraShown === 'page'));
  refs.showSource.setAttribute('aria-pressed', String(jiraShown === 'source'));
}

function setJiraShown(view, how) {
  jiraShown = how;
  if (view.current) showJira(view, view.current.jira);
}

/** Edit (the editor) or Preview (the draft as it stands, formatted like the ticket) for your version. */
function setPreview(view, on) {
  view.preview = on;
  paintMine(view);
  if (!on) focusEditor(view);
}

function paintMine(view) {
  const { refs } = view;
  refs.mineEdit.setAttribute('aria-pressed', String(!view.preview));
  refs.minePreview.setAttribute('aria-pressed', String(view.preview));
  refs.editorBox.hidden = view.preview;
  refs.preview.hidden = !view.preview;
  if (view.preview && view.current) {
    refs.preview.replaceChildren(renderMarkdown(valueOf(view.current)));
    refs.preview.scrollTop = 0;
  }
}

function focusEditor(view) {
  if (view.editor) view.editor.focus();
  else view.area?.focus();
}

function showDraft(view, draft) {
  const { refs } = view;
  view.current = draft;
  lastKey = draft.key;
  refs.errors.hidden = true;
  refs.empty.hidden = true;
  refs.split.hidden = false;
  showJira(view, draft.jira);
  refs.jira.scrollTop = 0;
  if (view.editor) {
    view.editor.setModel(draft.model);
    if (draft.viewState) view.editor.restoreViewState(draft.viewState);
  } else {
    view.area.value = draft.text;
    view.area.setSelectionRange(0, 0); // otherwise the box opens scrolled to the end
    view.area.scrollTop = 0;
  }
  paintMine(view);
  if (!view.preview) focusEditor(view);
  paintBar(view);
  view.ctx.cli(codeCommand(draft.path));
}

// --- the bar over the editor and the checklist beside it -------------------------------------------------------------

function paintBar(view) {
  const draft = view.current;
  const { path, state, save: saveButton, run: runButton } = view.refs;
  path.textContent = draft ? draft.path : `tickets/${view.key}${suffix}`;
  let text = '';
  let tone = '';
  if (view.busy) text = 'Saving…';
  else if (draft && !draft.exists) [text, tone] = ['Not saved yet', 'warn'];
  else if (draft && isDirty(draft)) [text, tone] = ['Not saved', 'warn'];
  else if (draft) [text, tone] = ['Saved', 'ok'];
  state.textContent = text;
  state.className = `code-state${tone ? ` ${tone}` : ''}`;
  saveButton.disabled = !draft || !needsSave(draft) || view.busy;
  runButton.disabled = !draft || view.busy;
  paintChecklist(view);
}

function paintChecklist(view) {
  const { checks, count, warning } = view.refs;
  const draft = view.current;
  if (!draft) return;
  const text = valueOf(draft);
  const items = sectionChecklist(text);
  const done = items.filter((item) => item.present).length;
  count.textContent = `${done} of ${items.length}`;
  checks.replaceChildren(
    ...items.map((item) => {
      const tick = h('span', 'tk-tick');
      if (item.present) tick.innerHTML = TICK; // a constant of this file, never data
      return h(
        'li',
        item.present ? 'tk-check done' : 'tk-check',
        tick,
        h('span', 'tk-check-title', item.title),
        item.empty ? h('span', 'tk-check-note', 'empty') : null,
        h('span', 'sr-only', item.present ? (item.empty ? ', heading there, nothing under it yet' : ', there') : ', missing'),
      );
    }),
  );
  warning.textContent = NO_ACCEPTANCE_TESTS;
  warning.hidden = acceptanceTests(text).length > 0;
}

function edited(view) {
  if (!view.current || ui !== view) return;
  paintBar(view);
}

function showErrors(view, messages) {
  const box = view.refs.errors;
  box.querySelector('ul').replaceChildren(
    ...messages.map((message) => Object.assign(document.createElement('li'), { textContent: message })),
  );
  box.hidden = messages.length === 0;
}

// --- save and run --------------------------------------------------------------------------------------------------

/** Saves the draft on show; true when `tickets/<KEY>.md` holds its text afterwards. */
async function save() {
  const view = ui;
  const draft = view?.current;
  if (!view || !draft || view.busy || !needsSave(draft)) return false;
  const content = valueOf(draft);
  const version = snapshot(draft); // edits made while the request is out stay "unsaved"
  view.busy = true;
  showErrors(view, []);
  paintBar(view);
  try {
    let saved;
    try {
      saved = await post('/api/files/write', { path: draft.path, content, base_sha: draft.sha });
    } catch (err) {
      saved = err.timeout ? await landed(draft.path, content) : null;
      if (!saved) throw err;
    }
    Object.assign(draft, { sha: saved.sha, exists: true, saved: version });
    return true;
  } catch (err) {
    if (ui === view) saveFailed(view, draft, err);
    return false;
  } finally {
    view.busy = false;
    if (ui === view && view.current === draft) paintBar(view);
  }
}

/**
 * A save that got no answer in time may have been done all the same: the file as it is now when it holds exactly the
 * text that was sent (then it was saved), else null.
 */
async function landed(path, content) {
  try {
    const now = await get('/api/files/read', { path });
    return now.content === content ? now : null;
  } catch {
    return null;
  }
}

function saveFailed(view, draft, err) {
  if (err.status === 401) return; // the login page is on its way
  const body = err.body ?? {};
  let lines;
  if (err.status === 409 && body.error === 'changed' && body.current) {
    // The file is not the one this text was read from (a run wrote it, or another tab). Save again replaces it.
    draft.sha = body.current.sha;
    lines = [`${draft.path} changed on the server after you opened it (your teammate saved it, or a run you kept changed it). Save again to replace it with your text.`];
  } else if (err.status === 409 && body.error === 'unit is running') {
    lines = ['A run is using your files. Save again when it has finished.'];
  } else if (err.status === 422 && Array.isArray(body.errors)) {
    lines = body.errors;
  } else {
    lines = [`Could not save ${draft.path}: ${err.message}`];
  }
  if (view.current === draft) showErrors(view, lines);
  else view.ctx.toast(`${draft.path} was not saved: ${lines[0]}`, 'error');
}

async function run() {
  const view = ui;
  const draft = view?.current;
  if (!view || !draft || view.busy) return;
  if (needsSave(draft) && !(await save())) return; // the run reads the file, so the file must hold this text
  if (ui !== view) return;
  location.hash = `#/runs?ticket=${draft.path}`;
}
