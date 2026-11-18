// The bits of the guide that the Today page and the shell's step strip both draw: a step's action link, where a step
// happens, and the few inline icons. Text goes in as text; the icons are fixed markup.

import { get } from './api.js';
import { fill } from './guide.js';
import { lineOf } from './lines.js';
import { codeLink } from './route.js';

const SVG = (paths) =>
  `<svg viewBox="0 0 16 16" width="16" height="16" fill="none" stroke="currentColor" stroke-width="1.5" stroke-linecap="round" stroke-linejoin="round" aria-hidden="true" focusable="false">${paths}</svg>`;

/** The handful of 16 px stroke icons of the console. */
export const ICONS = {
  menu: SVG('<path d="M2.5 4h11M2.5 8h11M2.5 12h11"/>'),
  close: SVG('<path d="M4 4l8 8M12 4l-8 8"/>'),
  right: SVG('<path d="M6 3.5L10.5 8 6 12.5"/>'),
  down: SVG('<path d="M3.5 6L8 10.5 12.5 6"/>'),
  check: SVG('<circle cx="8" cy="8" r="5.5"/><path d="M5.6 8.2l1.7 1.7 3.2-3.5"/>'),
  // the pages of the nav
  today: SVG('<rect x="2.5" y="3.5" width="11" height="10" rx="1.5"/><path d="M2.5 6.8h11M5.5 2v3M10.5 2v3"/>'),
  harness: SVG('<path d="M8 2l6 3-6 3-6-3 6-3zM2 8l6 3 6-3M2 11l6 3 6-3"/>'),
  code: SVG('<path d="M5.5 4.5L2 8l3.5 3.5M10.5 4.5L14 8l-3.5 3.5M9 3L7 13"/>'),
  tickets: SVG('<path d="M4 2.5h5.5L12 5v8.5H4zM9.5 2.5V5H12M6 8h4M6 10.5h4"/>'),
  runs: SVG('<circle cx="8" cy="8" r="5.5"/><path d="M6.8 5.8l3.4 2.2-3.4 2.2z"/>'),
  evals: SVG('<path d="M2.5 13.5h11M4.5 13.5V9M8 13.5V3.5M11.5 13.5V6.5"/>'),
  guides: SVG('<path d="M8 4.8c0-.7-.6-1.3-1.3-1.3H2.5v8.7h4.2c.7 0 1.3.6 1.3 1.3M8 4.8c0-.7.6-1.3 1.3-1.3h4.2v8.7H9.3c-.7 0-1.3.6-1.3 1.3M8 4.8v8.7"/>'),
  ship: SVG('<path d="M14 2L7 9M14 2l-4.5 12L7 9 2 6.5z"/>'),
  board: SVG('<rect x="2.5" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="9" y="2.5" width="4.5" height="4.5" rx="1"/><rect x="2.5" y="9" width="4.5" height="4.5" rx="1"/><rect x="9" y="9" width="4.5" height="4.5" rx="1"/>'),
  admin: SVG('<circle cx="8" cy="5.2" r="2.4"/><path d="M3.5 13.5c.5-2.4 2.3-3.8 4.5-3.8s4 1.4 4.5 3.8"/>'),
  // the sections of the day
  bolt: SVG('<path d="M9 1.5L3.5 9H8l-1 5.5L12.5 7H8z"/>'),
  doc: SVG('<path d="M4 2.5h5.5L12 5v8.5H4zM9.5 2.5V5H12M6 8h4M6 10.5h2.5"/>'),
  shield: SVG('<path d="M8 1.8l5 1.8v4c0 3-2.1 5.2-5 6.6-2.9-1.4-5-3.6-5-6.6v-4zM5.8 8l1.6 1.6 3-3"/>'),
  lock: SVG('<rect x="3.5" y="7" width="9" height="6.5" rx="1.5"/><path d="M5.5 7V5a2.5 2.5 0 015 0v2"/>'),
  person: SVG('<circle cx="6.5" cy="5.2" r="2.4"/><path d="M2 13.5c.5-2.4 2.3-3.8 4.5-3.8 1.2 0 2.3.4 3.100 1.200M10.5 12l1.500 1.500 2.500-3"/>'),
  chart: SVG('<path d="M2.5 13.5h11M4.5 13.5V9M8 13.5V3.5M11.5 13.5V6.5"/>'),
  share: SVG('<circle cx="4" cy="8" r="1.8"/><circle cx="12" cy="4" r="1.8"/><circle cx="12" cy="12" r="1.8"/><path d="M5.6 7.2l4.800-2.400M5.6 8.8l4.800 2.400"/>'),
  flag: SVG('<path d="M4 14V2.500M4 3h7.500L10 5.500 11.500 8H4"/>'),
  cup: SVG('<path d="M3.5 6h7v3.500a3.500 3.500 0 01-7 0zM10.500 7h1.200a1.600 1.600 0 010 3.200h-1.200M4 14h6"/>'),
  // the parts of a section
  stage: SVG('<rect x="2.5" y="3" width="11" height="7.5" rx="1"/><path d="M8 10.5v3M5.500 13.500h5"/>'),
  bulb: SVG('<path d="M6.200 11.500h3.600M6.800 13.500h2.400M8 2.200a4 4 0 00-2.300 7.300c.3.200.5.600.5 1h3.600c0-.4.200-.8.5-1A4 4 0 008 2.200z"/>'),
  laptop: SVG('<rect x="3" y="3.500" width="10" height="6.500" rx="1"/><path d="M1.500 12.500h13"/>'),
};

/** An element with a class and children (text or nodes; null and false are left out). */
export function h(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

/** An element holding one of ICONS. */
export function icon(name) {
  const box = document.createElement('span');
  box.className = 'icon';
  box.innerHTML = ICONS[name]; // fixed markup of this file, never data
  return box.firstElementChild;
}

/**
 * The link of a guide action ("Open gates.yaml"), with the unit's names filled in. It works as a plain link; when the
 * action has `find`, a plain click first looks the key up in the file so that the Code view opens at its line.
 */
export function actionLink(action, unitId, className = 'btn small') {
  const to = fill(action.to, unitId);
  const link = h('a', className, action.label);
  link.href = to;
  if (action.find && to.startsWith('#/code?')) {
    link.addEventListener('click', async (event) => {
      if (event.button !== 0 || event.metaKey || event.ctrlKey || event.shiftKey || event.altKey) return; // a new tab, etc.
      event.preventDefault();
      const from = location.hash;
      const path = new URLSearchParams(to.slice(to.indexOf('?') + 1)).get('path') ?? '';
      let line = 0;
      try {
        line = lineOf((await get('/api/files/read', { path })).content ?? '', action.find.key, action.find.within);
      } catch {
        /* open it at the top */
      }
      if (location.hash === from) location.hash = codeLink(path, line); // unless the person went elsewhere meanwhile
    });
  }
  return link;
}

/** Where a step happens: `harness/gates.yaml › profiles.fast`, or `Evals › Your variant card`. */
export function whereNode(where, unitId) {
  const box = h('p', 'check-where');
  if (where.file) {
    box.append(h('code', '', fill(where.file, unitId)));
    if (where.key) box.append(h('span', 'check-sep', '›'), h('code', '', where.key));
  } else {
    box.append(where.page);
    if (where.part) box.append(h('span', 'check-sep', '›'), where.part);
  }
  return box;
}
