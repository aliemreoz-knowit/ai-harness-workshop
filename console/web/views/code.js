// Code view: the unit's file tree (what can be edited and what is read-only), an editor (Monaco, or a plain text box
// when Monaco is not installed), save with conflict handling, what the unit has changed since it started, and "Check my
// code (gates)", the free gates run the Runs view starts too.
//
// Open files keep their unsaved edits when the person visits another view and comes back (`docs` lives as long
// as the page). Saving is one commit on the server; a stale `base_sha` comes back as 409 with the current file. The bar
// over the editor says where a file stands in plain words: "Saved", "Not saved", "Changed on the server".
// `#/code?path=<file>&line=<n>` opens that file with line n selected (the Harness view's "Edit" links). The look is
// views/code.css. A .md file has Edit / Preview in the bar: Preview shows it as a page (markdown.js), the choice stays
// for the next .md file; a link to a line opens the editor.

import { get, post } from '../api.js';
import { codeCommand } from '../cli.js';
import { MONO_FONT, diffView, languageFor, loadMonaco } from '../monaco.js';
import { parseDiff } from '../diff.js';
import { renderMarkdown } from '../markdown.js';

// --- state that outlives a visit ---------------------------------------------------------------------------------

/** Open files by path. doc = {path, sha, editable, reason, binary, language, model, text, saved, conflict, viewState}. */
const docs = new Map();
const expanded = new Set(['src']); // folders shown open in the tree
let docsUnit = null; // whose files `docs` holds
let lastPath = null;
let monaco = null; // the Monaco namespace once loaded, else null (plain text box)
let ui = null; // the mounted view, null when none
let mdPreview = false; // a .md file: shown as a page (Preview) instead of in the editor

window.addEventListener('beforeunload', (event) => {
  if ([...docs.values()].some(isDirty)) event.preventDefault(); // the browser asks before the edits are lost
});

const CHECK_LABEL = 'Check my code';
const CHECK_HINT = 'Builds and tests your saved code with the fast gates. No agent, no credits.';

// a padlock, 12 px: fixed markup, never data
const LOCK =
  '<svg viewBox="0 0 16 16" width="12" height="12" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" ' +
  'stroke-linejoin="round" aria-hidden="true" focusable="false"><rect x="3.5" y="7" width="9" height="6.5" rx="1"/><path d="M5.5 7V5a2.5 2.5 0 0 1 5 0v2"/></svg>';

const TEMPLATE = `
<div class="code">
  <header class="code-head">
    <div class="code-head-text">
      <h1>Code</h1>
      <p class="page-sub">The files of your working copy. Each save is a commit; runs and gates use what is saved.</p>
    </div>
    <div class="code-head-actions">
      <button type="button" class="btn" data-ref="changes">Changes since start</button>
      <button type="button" class="btn" data-ref="check"></button>
    </div>
  </header>
  <div class="code-split">
    <aside class="code-side" aria-label="Files">
      <div class="tree" data-ref="tree"></div>
      <p class="tree-key"><span class="lock" data-ref="keyLock"></span> Read-only: you can open it, not change it.</p>
    </aside>
    <section class="code-main" aria-label="Editor">
      <div class="code-bar">
        <span class="code-path" data-ref="path"></span>
        <span class="code-state" data-ref="state" aria-live="polite"></span>
        <span class="view-switch" role="group" aria-label="Show the file" data-ref="show" hidden>
          <button type="button" class="view-switch-btn" data-ref="showEdit" aria-pressed="true">Edit</button>
          <button type="button" class="view-switch-btn" data-ref="showPreview" aria-pressed="false">Preview</button>
        </span>
        <button type="button" class="btn primary" data-ref="save" disabled>Save</button>
      </div>
      <p class="note" data-ref="note" hidden></p>
      <div class="code-body">
        <div class="editor-host" data-ref="host" hidden></div>
        <div class="md-preview" data-ref="preview" tabindex="0" hidden></div>
        <p class="empty" data-ref="empty">Open a file on the left. To change your harness, start in <a href="#/harness">Harness</a>: its Edit links open the right line here.</p>
        <div class="overlay" data-ref="overlay" hidden></div>
      </div>
      <div class="errors" data-ref="errors" role="alert" hidden>
        <p>Not saved:</p>
        <ul></ul>
      </div>
    </section>
  </div>
</div>`;

// --- documents -----------------------------------------------------------------------------------------------------

function makeDoc(view) {
  const doc = {
    path: view.path,
    sha: view.sha,
    editable: view.editable,
    reason: view.reason,
    binary: view.binary,
    language: languageFor(view.path),
    model: null,
    text: view.content ?? '',
    saved: null,
    conflict: false, // a save found the file changed on the server, and the person has not settled it yet
    viewState: null,
  };
  if (monaco && !doc.binary) doc.model = monaco.editor.createModel(doc.text, doc.language);
  doc.saved = snapshot(doc);
  return doc;
}

// What "unchanged" means: Monaco's version id (undo back to the start is clean again), or the text itself.
const snapshot = (doc) => (doc.model ? doc.model.getAlternativeVersionId() : doc.text);
const valueOf = (doc) => (doc.model ? doc.model.getValue(monaco.editor.EndOfLinePreference.LF) : doc.text);
const isDirty = (doc) => !doc.binary && snapshot(doc) !== doc.saved;

function setValue(doc, text) {
  if (doc.model) doc.model.setValue(text);
  else doc.text = text;
}

function disposeDoc(doc) {
  docs.delete(doc.path);
  doc.model?.dispose();
}

function takeView(doc, view) {
  doc.sha = view.sha;
  doc.editable = view.editable;
  doc.reason = view.reason;
  doc.conflict = false;
  setValue(doc, view.content ?? '');
  doc.saved = snapshot(doc);
}

// --- the style sheet ---------------------------------------------------------------------------------------------

/** Adds views/code.css once; resolves when it has loaded (or failed), so the page does not show unstyled. */
function loadStyles() {
  let link = document.getElementById('code-css');
  if (!link) {
    link = Object.assign(document.createElement('link'), { id: 'code-css', rel: 'stylesheet', href: '/web/views/code.css' });
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
    el.innerHTML = '<p class="empty">Log in with a unit code to work on files.</p>';
    return;
  }
  if (docsUnit !== unit.id) {
    [...docs.values()].forEach(disposeDoc);
    docsUnit = unit.id;
    lastPath = null;
  }
  const { path: wanted, line: wantedLine } = ctx.route.params ?? {};
  if (wanted) lastPath = wanted; // its folders are opened in the tree

  const mine = (ui = {
    ctx,
    root: null,
    refs: null,
    editor: null, // Monaco editor
    area: null, // textarea, when there is no Monaco
    current: null, // doc on show
    entries: new Map(), // folder path -> entries of the tree
    nodes: new Map(), // file path -> its button in the tree
    busy: false,
    checking: false,
    openToken: 0,
    overlayDispose: null,
  });
  await loadStyles();
  if (ui !== mine) return;
  el.innerHTML = TEMPLATE;
  mine.root = el.firstElementChild;
  const refs = (mine.refs = Object.fromEntries([...el.querySelectorAll('[data-ref]')].map((node) => [node.dataset.ref, node])));
  refs.keyLock.append(lockIcon());
  refs.check.textContent = CHECK_LABEL;
  refs.check.title = CHECK_HINT;

  refs.changes.addEventListener('click', () => showChanges());
  refs.check.addEventListener('click', () => checkCode(mine));
  refs.save.addEventListener('click', () => save());
  refs.showEdit.addEventListener('click', () => setPreview(mine, false));
  refs.showPreview.addEventListener('click', () => setPreview(mine, true));
  mine.root.addEventListener('keydown', (event) => {
    if ((event.ctrlKey || event.metaKey) && !event.altKey && event.key.toLowerCase() === 's') {
      event.preventDefault(); // not the browser's "save page"
      save();
    }
  });
  paintBar(mine);

  monaco ??= await loadMonaco();
  if (ui !== mine) return;
  createEditor(mine);
  if (!monaco) {
    refs.note.textContent = 'The editor is not installed (run scripts/vendor_monaco.py): files open in a plain text box.';
    refs.note.hidden = false;
  }

  if (lastPath) for (const dir of ancestors(lastPath)) expanded.add(dir);
  await loadTree(mine);
  if (ui !== mine) return;
  if (wanted) await openFile(wanted, Number.parseInt(wantedLine, 10) || 0);
  else if (lastPath && docs.has(lastPath)) await openFile(lastPath);
}

export function unmount() {
  const mine = ui;
  if (!mine) return;
  ui = null; // every call still waiting for the server sees that the view is gone
  mine.overlayDispose?.();
  if (mine.editor) {
    if (mine.current) mine.current.viewState = mine.editor.saveViewState();
    mine.editor.dispose(); // the models stay: they hold the unsaved edits
  }
}

function ancestors(path) {
  const parts = path.split('/').slice(0, -1);
  return parts.map((_, i) => parts.slice(0, i + 1).join('/'));
}

function lockIcon() {
  const box = document.createElement('span');
  box.innerHTML = LOCK; // a constant of this file, never data
  return box.firstElementChild;
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
      scrollBeyondLastLine: false,
      renderWhitespace: 'selection',
      readOnly: true,
      ariaLabel: 'File editor',
    });
    view.editor.onDidChangeModelContent(() => edited(view));
  } else {
    view.area = document.createElement('textarea');
    view.area.className = 'plain-editor';
    view.area.spellcheck = false;
    view.area.wrap = 'off';
    view.area.readOnly = true;
    view.area.setAttribute('aria-label', 'File editor');
    view.area.addEventListener('input', () => {
      if (view.current) view.current.text = view.area.value;
      edited(view);
    });
    host.append(view.area);
  }
}

// --- tree ------------------------------------------------------------------------------------------------------------

async function loadTree(view) {
  const dirs = ['', ...expanded];
  const answers = await Promise.allSettled(dirs.map((path) => get('/api/files/tree', { path })));
  if (ui !== view) return;
  answers.forEach((answer, i) => {
    if (answer.status === 'fulfilled') {
      view.entries.set(dirs[i], answer.value.entries);
    } else if (dirs[i] === '') {
      fail(view.ctx, answer.reason, 'Could not load the files');
    } else {
      expanded.delete(dirs[i]); // gone since last time
    }
  });
  renderTree(view);
}

function renderTree(view) {
  const box = view.refs.tree;
  const top = box.scrollTop;
  view.nodes.clear();
  box.replaceChildren(renderList(view, ''));
  box.scrollTop = top;
}

function renderList(view, dir) {
  const list = document.createElement('ul');
  for (const entry of view.entries.get(dir) ?? []) {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'node';
    const mark = document.createElement('span');
    mark.className = 'mark';
    mark.setAttribute('aria-hidden', 'true');
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = entry.name;
    button.append(mark, name);

    if (entry.dir) {
      const open = expanded.has(entry.path);
      mark.textContent = open ? '▾' : '▸';
      button.setAttribute('aria-expanded', String(open));
      button.addEventListener('click', () => toggleDir(view, entry.path));
      item.append(button);
      if (open && view.entries.has(entry.path)) item.append(renderList(view, entry.path));
    } else {
      view.nodes.set(entry.path, button);
      button.classList.toggle('active', view.current?.path === entry.path);
      markDirty(button, docs.get(entry.path));
      button.addEventListener('click', () => openFile(entry.path));
      item.append(button);
    }
    if (!entry.editable) {
      button.classList.add('ro');
      button.title = entry.dir ? 'Read-only: nothing in this folder can be edited here' : readOnlyTitle(entry.reason);
      const lock = document.createElement('span');
      lock.className = 'lock';
      lock.append(lockIcon(), Object.assign(document.createElement('span'), { className: 'sr-only', textContent: ' (read-only)' }));
      button.append(lock);
    }
    list.append(item);
  }
  return list;
}

const readOnlyTitle = (reason) => (reason ? `Read-only: ${reason}` : 'Read-only');

/** The tree's mark of a file with edits that are not saved (a dot, and the words for a screen reader). */
function markDirty(button, doc) {
  const dirty = Boolean(doc && isDirty(doc));
  button.classList.toggle('dirty', dirty);
  let note = button.querySelector('.dirty-note');
  if (dirty && !note) {
    note = Object.assign(document.createElement('span'), { className: 'dirty-note', title: 'Not saved' });
    note.append(Object.assign(document.createElement('span'), { className: 'sr-only', textContent: ' (not saved)' }));
    button.querySelector('.name').after(note);
  } else if (!dirty) {
    note?.remove();
  }
}

async function toggleDir(view, path) {
  if (expanded.has(path)) {
    expanded.delete(path);
  } else {
    expanded.add(path);
    if (!view.entries.has(path)) {
      try {
        view.entries.set(path, (await get('/api/files/tree', { path })).entries);
      } catch (err) {
        expanded.delete(path);
        fail(view.ctx, err, 'Could not open the folder');
      }
    }
  }
  if (ui === view) renderTree(view);
}

// --- open a file -------------------------------------------------------------------------------------------------------

async function openFile(path, line = 0) {
  const view = ui;
  const token = ++view.openToken;
  closeOverlay();
  let doc = docs.get(path);
  if (!doc || !isDirty(doc)) {
    let fresh;
    try {
      fresh = await get('/api/files/read', { path });
    } catch (err) {
      fail(view.ctx, err, `Could not open ${path}`);
      return;
    }
    if (ui !== view || token !== view.openToken) return;
    if (doc && doc.binary !== fresh.binary) {
      disposeDoc(doc);
      doc = null;
    }
    if (!doc) {
      doc = makeDoc(fresh);
      docs.set(path, doc);
    } else if (fresh.sha !== doc.sha) {
      takeView(doc, fresh); // changed on disk (a run, say) and nothing of ours to lose
    }
  }
  if (line > 0) mdPreview = false; // a link to a line: the line is in the editor
  showDoc(view, doc);
  if (line > 0 && !doc.binary) selectLine(view, doc, line);
}

// Select line `line` (1-based, kept inside the file) and scroll it into view.
function selectLine(view, doc, line) {
  if (view.editor) {
    const at = Math.min(line, doc.model.getLineCount());
    view.editor.layout(); // the box was hidden until now: measure it, or the line is put at the top (under the sticky lines)
    view.editor.setSelection({ startLineNumber: at, startColumn: 1, endLineNumber: at, endColumn: doc.model.getLineMaxColumn(at) });
    view.editor.revealLineInCenter(at);
    return;
  }
  const area = view.area;
  let start = 0;
  for (let n = 1; n < line && start >= 0; n += 1) {
    const next = doc.text.indexOf('\n', start);
    start = next < 0 ? -1 : next + 1;
  }
  if (start < 0) return; // fewer lines than that
  const end = doc.text.indexOf('\n', start);
  area.setSelectionRange(start, end < 0 ? doc.text.length : end);
  area.scrollTop = Math.max(0, line - 4) * (Number.parseFloat(getComputedStyle(area).lineHeight) || 20);
}

function showDoc(view, doc) {
  const { refs } = view;
  if (view.editor && view.current && view.current !== doc) view.current.viewState = view.editor.saveViewState();
  view.current = doc;
  lastPath = doc.path;
  closeOverlay();
  refs.errors.hidden = true;
  refs.empty.textContent = 'This is a binary file: it cannot be shown here.';
  refs.empty.hidden = !doc.binary;
  const page = paintPreview(view, doc);
  refs.host.hidden = doc.binary || page;
  if (!doc.binary) {
    if (view.editor) {
      view.editor.setModel(doc.model);
      view.editor.updateOptions({
        readOnly: !doc.editable,
        readOnlyMessage: { value: `This file is read-only: ${doc.reason}` },
      });
      if (doc.viewState) view.editor.restoreViewState(doc.viewState);
      if (!page) view.editor.focus();
    } else {
      view.area.value = doc.text;
      view.area.readOnly = !doc.editable;
      view.area.setSelectionRange(0, 0); // otherwise the box opens scrolled to the end
      view.area.scrollTop = 0;
      if (!page) view.area.focus();
    }
  }
  for (const [path, button] of view.nodes) {
    button.classList.toggle('active', path === doc.path);
    markDirty(button, docs.get(path));
  }
  paintBar(view);
  view.ctx.cli(codeCommand(doc.path));
}

const isMarkdown = (doc) => Boolean(doc && !doc.binary && /\.md$/i.test(doc.path));

/** The Edit / Preview switch for a .md file, and the page in Preview; true when the page is on show. */
function paintPreview(view, doc) {
  const { refs } = view;
  const md = isMarkdown(doc);
  const page = md && mdPreview;
  refs.show.hidden = !md;
  refs.showEdit.textContent = doc?.editable ? 'Edit' : 'Text';
  refs.showEdit.setAttribute('aria-pressed', String(!page));
  refs.showPreview.setAttribute('aria-pressed', String(page));
  refs.preview.hidden = !page;
  if (page) {
    refs.preview.replaceChildren(renderMarkdown(valueOf(doc)));
    refs.preview.scrollTop = 0;
  } else {
    refs.preview.replaceChildren();
  }
  return page;
}

function setPreview(view, on) {
  mdPreview = on;
  const doc = view.current;
  if (!doc) return;
  const page = paintPreview(view, doc);
  view.refs.host.hidden = doc.binary || page;
  if (!page) {
    if (view.editor) {
      view.editor.layout();
      view.editor.focus();
    } else {
      view.area?.focus();
    }
  }
}

// --- the bar over the editor ---------------------------------------------------------------------------------------

/** Where the file on show stands, in plain words: `[text, tone]` (tone '' for calm, 'warn' for something to do). */
function stateOf(view, doc) {
  if (!doc) return ['', ''];
  if (doc.binary) return ['Binary file', ''];
  if (!doc.editable) return [`Read-only: ${doc.reason}`, ''];
  if (view.busy && view.saving === doc) return ['Saving…', ''];
  if (doc.conflict) return ['Changed on the server', 'warn'];
  if (isDirty(doc)) return ['Not saved', 'warn'];
  return ['Saved', 'ok'];
}

function paintBar(view) {
  const doc = view.current;
  const { path, state, save: saveButton } = view.refs;
  path.textContent = doc ? doc.path : 'No file open';
  path.title = doc ? doc.path : '';
  const [text, tone] = stateOf(view, doc);
  state.textContent = text;
  state.title = doc?.conflict ? 'Someone or something saved this file after you opened it. Save to compare the two versions.' : text;
  state.className = `code-state${tone ? ` ${tone}` : ''}`;
  saveButton.disabled = !doc || !doc.editable || !(isDirty(doc) || doc.conflict) || view.busy;
}

function edited(view) {
  const doc = view.current;
  if (!doc || ui !== view) return;
  const button = view.nodes.get(doc.path);
  if (button) markDirty(button, doc);
  paintBar(view);
}

function showErrors(view, messages) {
  const box = view.refs.errors;
  box.querySelector('ul').replaceChildren(
    ...messages.map((message) => Object.assign(document.createElement('li'), { textContent: message })),
  );
  box.hidden = messages.length === 0;
}

// --- save ----------------------------------------------------------------------------------------------------------------

// Saves `doc` (default: the one on show). The doc is fixed when the save starts: the answer may come after the person
// has opened another file, and then it must not touch what that file shows.
async function save(baseSha, doc = ui?.current) {
  const view = ui;
  if (!view || !doc || !doc.editable || doc.binary || view.busy) return;
  if (baseSha === undefined && !isDirty(doc) && !doc.conflict) return;
  const content = valueOf(doc);
  const version = snapshot(doc); // edits made while the request is out stay "unsaved"
  view.busy = true;
  view.saving = doc;
  if (view.current === doc) showErrors(view, []);
  paintBar(view);
  try {
    let saved;
    try {
      saved = await post('/api/files/write', { path: doc.path, content, base_sha: baseSha ?? doc.sha });
    } catch (err) {
      saved = err.timeout ? await landed(doc.path, content) : null;
      if (!saved) throw err;
    }
    doc.sha = saved.sha;
    doc.saved = version;
    doc.conflict = false;
    if (ui === view && view.current === doc) closeOverlay();
  } catch (err) {
    if (ui === view) saveFailed(view, doc, err);
  } finally {
    view.busy = false;
    view.saving = null;
    if (ui === view) {
      const button = view.nodes.get(doc.path);
      if (button) markDirty(button, doc);
      if (view.current === doc) paintBar(view);
    }
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

function saveFailed(view, doc, err) {
  if (err.status === 401) return; // the login page is on its way
  const body = err.body ?? {};
  const shown = view.current === doc; // otherwise the person has moved on to another file: say it in a toast
  if (err.status === 409 && body.error === 'changed' && body.current) {
    doc.conflict = true;
    if (shown) showConflict(view, doc, body.current);
    else view.ctx.toast(`${doc.path} changed on the server after you opened it. Open it and save again to compare.`, 'error');
    return;
  }
  const lines =
    err.status === 409 && body.error === 'unit is running'
      ? ['A run is using your files. Save again when it has finished.']
      : err.status === 422 && Array.isArray(body.errors)
        ? body.errors
        : null;
  if (lines && shown) showErrors(view, lines);
  else if (lines) view.ctx.toast(`${doc.path} was not saved: ${lines[0]}`, 'error');
  else fail(view.ctx, err, `Could not save ${doc.path}`);
}

// --- check my code: the free gates run, started as the Runs view starts it ----------------------------------------------

async function checkCode(view) {
  if (view.checking) return;
  const { check } = view.refs;
  const unsaved = [...docs.values()].filter(isDirty).map((doc) => doc.path);
  view.checking = true;
  check.disabled = true;
  check.textContent = 'Starting…';
  try {
    const rec = await post('/api/runs', { kind: 'gates' });
    if (ui !== view) return;
    if (rec.status === 'refused') {
      view.ctx.toast(`Check my code did not start: ${rec.error || 'no reason given'}`, 'error');
      return;
    }
    if (unsaved.length) {
      view.ctx.toast(`The gates check what is saved: your edits to ${unsaved.length === 1 ? unsaved[0] : `${unsaved.length} files`} are not part of this run.`);
    }
    location.hash = `#/runs/${encodeURIComponent(rec.id)}`;
  } catch (err) {
    if (ui === view) fail(view.ctx, err, 'Could not start Check my code');
  } finally {
    view.checking = false;
    if (ui === view) {
      check.disabled = false;
      check.textContent = CHECK_LABEL;
    }
  }
}

// --- the overlay: a conflict, or the changes since start ------------------------------------------------------------------

function closeOverlay() {
  const view = ui;
  if (!view) return;
  view.overlayDispose?.();
  view.overlayDispose = null;
  view.refs.overlay.hidden = true;
  view.refs.overlay.replaceChildren();
}

function openOverlay(view, html) {
  closeOverlay();
  view.refs.overlay.innerHTML = html;
  view.refs.overlay.hidden = false;
  return (ref) => view.refs.overlay.querySelector(`[data-ref="${ref}"]`);
}

function plainBox(host, label, text) {
  const caption = document.createElement('p');
  caption.className = 'caption';
  caption.textContent = label;
  const box = document.createElement('textarea');
  box.className = 'plain-editor';
  box.readOnly = true;
  box.wrap = 'off';
  box.value = text;
  box.setAttribute('aria-label', label);
  host.replaceChildren(caption, box);
  return () => {};
}

function showConflict(view, doc, current) {
  const find = openOverlay(
    view,
    `<div class="overlay-head conflict">
       <div class="overlay-text">
         <p><strong>Changed on the server.</strong> This file changed after you opened it: your teammate saved it, or a
            run you kept changed it.</p>
         <p class="overlay-sub">Left: the file now. Right: your version.</p>
       </div>
       <div class="actions">
         <button type="button" class="btn" data-act="theirs">Take the new version (drop mine)</button>
         <button type="button" class="btn danger" data-act="overwrite">Save mine over it</button>
         <button type="button" class="btn" data-act="cancel">Keep editing</button>
       </div>
     </div>
     <div class="overlay-body" data-ref="body"></div>`,
  );
  const theirs = current.content ?? '';
  view.overlayDispose = monaco
    ? diffView(monaco, find('body'), theirs, valueOf(doc), doc.language)
    : plainBox(find('body'), 'The file now (yours is in the editor)', theirs);

  view.refs.overlay.querySelector('[data-act="theirs"]').addEventListener('click', () => {
    replaceText(view, doc, theirs); // an edit, not a reset: Ctrl+Z brings my version back
    doc.sha = current.sha;
    doc.saved = snapshot(doc);
    doc.conflict = false;
    closeOverlay();
    edited(view); // the replacement was seen as an edit a moment ago: clear the "not saved" marks
    focusEditor(view);
    view.ctx.toast('Loaded the other version. Undo (Ctrl+Z) brings yours back.');
  });
  view.refs.overlay.querySelector('[data-act="overwrite"]').addEventListener('click', () => {
    closeOverlay();
    save(current.sha, doc); // my text on top of what is there now
  });
  view.refs.overlay.querySelector('[data-act="cancel"]').addEventListener('click', () => {
    closeOverlay();
    focusEditor(view);
  });
  paintBar(view);
  // Not on a button that discards something: a person still typing when the panel opens must not trigger it with a key.
  view.refs.overlay.querySelector('[data-act="cancel"]').focus();
}

function focusEditor(view) {
  (view.editor ?? view.area)?.focus();
}

// Replace the whole text of `doc` as one edit that stays on the undo stack (the text box keeps its own history).
function replaceText(view, doc, text) {
  if (doc.model) {
    doc.model.pushStackElement(); // typing before this stays a separate undo step...
    doc.model.pushEditOperations([], [{ range: doc.model.getFullModelRange(), text }], () => null);
    doc.model.pushStackElement(); // ...and so does typing after it
  } else if (view.current === doc) {
    view.area.focus();
    view.area.select();
    if (!document.execCommand('insertText', false, text)) view.area.value = text; // insertText keeps undo
    doc.text = view.area.value;
  } else {
    doc.text = text;
  }
}

async function showChanges() {
  const view = ui;
  let reply;
  try {
    reply = await get('/api/files/diff');
  } catch (err) {
    fail(view.ctx, err, 'Could not load the changes');
    return;
  }
  if (ui !== view) return;
  // what was saved since the start, then what is changed in the folder and not saved (a command on the stage, a patch)
  const files = [...parseDiff(reply.diff), ...parseDiff(reply.unsaved ?? '').map((file) => ({ ...file, unsaved: true }))];
  const find = openOverlay(
    view,
    `<div class="overlay-head">
       <div class="overlay-text"><p><strong>Changes since start.</strong> Everything saved here since <code></code>, and what is changed in the folder but not saved yet (marked “not saved”: a command on the stage, for one).</p></div>
       <div class="actions"><button type="button" class="btn" data-act="back">Back to the editor</button></div>
     </div>
     <div class="overlay-body changes">
       <ul class="changed" data-ref="files"></ul>
       <div class="changed-view" data-ref="body"></div>
     </div>`,
  );
  view.refs.overlay.querySelector('code').textContent = reply.base;
  view.refs.overlay.querySelector('[data-act="back"]').addEventListener('click', () => {
    if (view.current) showDoc(view, view.current);
    else {
      closeOverlay();
      view.ctx.cli('');
    }
  });
  view.ctx.cli(`git diff ${reply.base}...HEAD`);

  const list = find('files');
  const body = find('body');
  let stopDiff = () => {};
  view.overlayDispose = () => stopDiff();

  if (files.length === 0) {
    list.hidden = true;
    body.textContent = 'Nothing changed yet. Every save is a commit and shows up here, and so does a change a command makes in the folder.';
    body.className = 'empty';
    return;
  }
  if (!monaco) {
    list.hidden = true;
    stopDiff = plainBox(body, 'Changes (unified diff)', [reply.diff, reply.unsaved].filter(Boolean).join('\n'));
    return;
  }

  const buttons = files.map((file, index) => {
    const item = document.createElement('li');
    const button = document.createElement('button');
    button.type = 'button';
    button.className = 'node';
    const name = document.createElement('span');
    name.className = 'name';
    name.textContent = file.unsaved ? `${file.path} · not saved` : file.path;
    const counts = document.createElement('span');
    counts.className = 'counts';
    counts.textContent = file.status === 'binary' ? 'binary' : `+${file.added} -${file.removed}`;
    button.append(name, counts);
    button.addEventListener('click', () => select(index));
    item.append(button);
    list.append(item);
    return button;
  });

  function select(index) {
    const file = files[index];
    stopDiff();
    stopDiff = () => {};
    buttons.forEach((button, i) => button.classList.toggle('active', i === index));
    body.replaceChildren();
    body.className = 'changed-view';
    if (file.status === 'binary' || (file.status === 'renamed' && !file.original && !file.modified)) {
      const note = document.createElement('p');
      note.className = 'empty';
      note.textContent = file.status === 'binary' ? 'A binary file changed.' : `Renamed from ${file.from}.`;
      body.append(note);
      return;
    }
    stopDiff = diffView(monaco, body, file.original, file.modified, languageFor(file.path));
  }
  select(0);
}

// --- errors --------------------------------------------------------------------------------------------------------------

function fail(ctx, err, what) {
  if (err.status === 401) return; // the login page is on its way
  ctx.toast(`${what}: ${err.message}`, 'error');
}
