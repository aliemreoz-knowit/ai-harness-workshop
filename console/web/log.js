// How a run's log is drawn: one row per `Line` of the run (time, icon, colour, text) and the box that holds the rows.
// The box follows the end of the log unless the person scrolled up, and keeps at most `MAX_LINES` rows in the page
// (the oldest go first; a note at the top says how many). What a row looks like is decided in runmodel.js.
//
// Everything is put on the page as text: a line comes from an agent, never from the console.

import { LineBuffer, atBottom, lineLook } from './runmodel.js';

export const MAX_LINES = 5000;
const FLUSH_MS = 40; // lines that arrive together are drawn together

function span(className, text) {
  const node = document.createElement('span');
  node.className = className;
  node.textContent = text;
  return node;
}

/** "10:03:12" in the viewer's time; `''` for a time that is not one. */
function clock(ts) {
  const at = new Date(typeof ts === 'string' ? ts : NaN);
  return Number.isNaN(at.getTime()) ? '' : [at.getHours(), at.getMinutes(), at.getSeconds()].map((n) => String(n).padStart(2, '0')).join(':');
}

/**
 * The element for one log line (`line` is text or the parsed `Line` of a run): its time, an icon, what kind of line it
 * is in a word ("tool", "said", "gates", "error") and its text.
 */
export function renderLine(line) {
  const row = document.createElement('div');
  if (typeof line === 'string') {
    row.className = 'log-line k-other';
    row.append(span('log-time', ''), span('log-icon', '·'), span('log-kind', ''), span('log-text', line));
    return row;
  }
  const look = lineLook(line);
  row.className = `log-line ${look.cls}`;
  const icon = span('log-icon', look.icon);
  icon.setAttribute('aria-hidden', 'true');
  row.append(span('log-time', clock(line.ts)), icon, span('log-kind', look.label), span('log-text', String(line.text ?? '')));
  const where = [line.step, line.role].filter(Boolean).join(' · ');
  if (where) row.title = where;
  return row;
}

/**
 * Turn `host` (an element that scrolls) into a run log. `add(line)` shows a line (a repeat of one shown is ignored);
 * `onFollow(following)` runs when the person scrolls away from the end or back to it. Returns
 * `{add, follow, isFollowing, setPlaceholder, destroy}`: `follow()` jumps to the end and goes on following;
 * `setPlaceholder(text)` is what the box says while it has no lines (CSS shows `data-placeholder` on `.is-empty`).
 */
export function createLog(host, { max = MAX_LINES, onFollow = () => {} } = {}) {
  const buffer = new LineBuffer(max);
  const note = document.createElement('div');
  note.className = 'log-note';
  note.hidden = true;
  host.replaceChildren(note);
  host.classList.add('is-empty');

  let pending = [];
  let timer = null;
  let following = true;

  const onScroll = () => {
    const now = atBottom(host);
    if (now === following) return;
    following = now;
    onFollow(following);
  };
  host.addEventListener('scroll', onScroll, { passive: true });

  function flush() {
    timer = null;
    const batch = pending;
    pending = [];
    const fresh = document.createDocumentFragment();
    for (const line of batch) {
      const { add, drop } = buffer.push(line);
      if (!add) continue;
      for (let left = drop; left > 0; left -= 1) (note.nextElementSibling ?? fresh.firstChild)?.remove(); // the oldest
      fresh.append(renderLine(line));
    }
    if (!fresh.firstChild) return; // nothing but repeats
    host.append(fresh);
    host.classList.toggle('is-empty', buffer.count === 0);
    note.hidden = buffer.hidden === 0;
    note.textContent = `${buffer.hidden} earlier line${buffer.hidden === 1 ? '' : 's'} hidden`;
    if (following) host.scrollTop = host.scrollHeight;
  }

  return {
    add(line) {
      pending.push(line);
      timer ??= setTimeout(flush, FLUSH_MS);
    },
    follow() {
      following = true;
      host.scrollTop = host.scrollHeight;
      onFollow(true);
    },
    isFollowing: () => following,
    setPlaceholder(text) {
      host.dataset.placeholder = text;
    },
    destroy() {
      clearTimeout(timer);
      host.removeEventListener('scroll', onScroll);
    },
  };
}
