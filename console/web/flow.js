// The timeline of a run: one lane per role with a bar for every stretch of a step, marks where a gate ran, a person
// was asked or a step was tried again, the credits spent so far and the credits by role.
//
// It is built from what a run has: the readable log lines of its audit
// trail (`{seq, ts, kind, text, step, role, ok}`) for the times and the roles, and `progress` (`events.progress`) for
// the state of a step and the credits by role.
//
// Two halves. Pure functions that work the numbers out (`timeline`, `journeyScale`, `layout`, `costLayout`,
// `sparklinePoints`, `creditSeries`: no DOM, no imports, run with node in the tests) and drawing functions that build
// SVG with `createElementNS`. Every string from the server (a step, a role, a log text) is put on the page with
// `textContent` and never becomes markup or an attribute; the look is in flow.css (classes, and the page's own colours).

const NS = 'http://www.w3.org/2000/svg';
const MINUTE = 60 * 1000;

/** The lane of the steps that have no role (the ticket, a gate ladder, an approval, the commit). */
export const RUN_LANE = '';
const RUN_LABEL = 'run steps';

const PAUSE_MS = 20 * MINUTE; // a wait longer than this (a person who was away) is drawn as a short break
const BREAK_W = 22;
const MIN_SEG_W = 30;
const LANE_H = 30;
const TOP = 8;
const MARK_GAP = 13;
const TICK_GAP = 54; // pixels between two times on the axis
const END_GAP = TICK_GAP + 36; // and before the end of the run, whose time is written to the left of its tick
const TICK_EVERY = 80; // about this many pixels from one time on the axis to the next, where there is room
// the steps of the axis inside a stretch of work: 1, 2, 5, 10, 15, 30 seconds, then minutes, then hours
const TICK_STEPS = [1, 2, 5, 10, 15, 30, 60, 120, 300, 600, 900, 1800, 3600, 7200, 10800, 21600, 43200].map((seconds) => seconds * 1000);

const KINDS = new Set(['step', 'gate', 'approval', 'retry', 'cost', 'end']); // the lines the timeline reads
const MARKS = new Set(['gate', 'approval', 'retry']);

const MAX_LANES = 12; // more roles than this: the first eleven keep a lane, the rest share the twelfth
const OTHER_LANE = '*other*';
/** How many lines of a run the timeline keeps (`keepLine`): a run has a few hundred. */
export const MAX_LINES = 4000;

const finite = (n) => typeof n === 'number' && Number.isFinite(n);
const clamp = (n, low, high) => Math.min(high, Math.max(low, n));

// The server writes microseconds; only milliseconds are standard (the same as runmodel.js).
const parseTime = (ts) => {
  if (typeof ts !== 'string') return null;
  const ms = Date.parse(ts.replace(/(\.\d{3})\d+/, '$1'));
  return Number.isNaN(ms) ? null : ms;
};

/** "45 s", "3 min 12 s", "1 h 02 min": a time since the start of the run. */
export function elapsed(ms) {
  if (!finite(ms) || ms < 0) return '';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  return `${Math.floor(seconds / 3600)} h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')} min`;
}

const credits = (n) => String(Math.round(n * 100) / 100);
const round = (n) => Math.round(n * 10) / 10;

const clip = (text, max) => {
  const one = String(text ?? '');
  return one.length > max ? `${one.slice(0, Math.max(1, max - 1))}…` : one;
};

// --- the model ---------------------------------------------------------------------------------------------------

/**
 * Keeps `line` (a readable log line) in `lines`, a Map of seq to line, for the timeline: only the kinds it reads, only
 * what it reads of them, each seq once. A run that has more than `max` lines keeps the most recent ones: the oldest is
 * dropped (a line older than all that are kept is not kept). Returns `{kept, dropped}`: whether the line is in `lines`
 * now, and how many lines were lost for it (0 or 1), so that a page can say that the timeline starts late.
 */
export function keepLine(lines, line, max = MAX_LINES) {
  if (!KINDS.has(line?.kind) || !Number.isInteger(line.seq) || lines.has(line.seq)) return { kept: false, dropped: 0 };
  let dropped = 0;
  if (lines.size >= max) {
    const oldest = lines.keys().next().value; // a Map goes in the order the lines were added: oldest first
    if (line.seq < oldest) return { kept: false, dropped: 1 };
    lines.delete(oldest);
    dropped = 1;
  }
  const { seq, ts, kind, text, step, role, ok } = line;
  lines.set(seq, { seq, ts, kind, text, step, role, ok });
  return { kept: true, dropped };
}

/**
 * What the status word of a step's end line says: "implement done" is `done` (ok), "implement sent back to plan" is
 * `sent back` (warn), "x escalated" and "x aborted" are warn, anything else that is not ok is bad.
 */
function ending(text, step, ok) {
  const rest = String(text).slice(step.length + 1);
  const status = /^sent back\b/.test(rest) ? 'sent back' : rest.split(' (')[0].trim() || 'ended';
  const warn = status === 'sent back' || status === 'escalated' || status === 'aborted';
  return { status, tone: ok === true ? 'ok' : warn ? 'warn' : 'bad' };
}

const MARK_TONES = {
  gate: (ok) => (ok === true ? 'ok' : ok === false ? 'bad' : 'muted'),
  approval: (ok) => (ok === true ? 'ok' : ok === false ? 'bad' : 'warn'),
  retry: () => 'warn',
};

/**
 * The cumulative credits over time from the `cost` lines ("implementer (model): 0.45 credits, 12 s"):
 * `[{t, total, role}]`, in the order of the lines.
 */
export function creditSeries(lines) {
  const out = [];
  let total = 0;
  for (const line of Array.isArray(lines) ? lines : []) {
    if (line?.kind !== 'cost') continue;
    const t = parseTime(line.ts);
    const spent = /(?<![\w.+-])(\d+(?:\.\d+)?(?:e[+-]?\d+)?) credits\b/i.exec(String(line.text ?? ''));
    if (t === null || !spent || !finite(Number(spent[1]))) continue;
    total += Number(spent[1]);
    out.push({ t, total, role: typeof line.role === 'string' ? line.role : '' });
  }
  return out;
}

/**
 * The timeline of a run from its readable `lines` (in the order they came) and its `progress`.
 *
 * `now` is the time in milliseconds for a run that is going (a step in progress reaches to it); `null` for one that is
 * over, where a step that never ended stops at the last line and says what `progress.steps` says of it.
 * Returns `{empty, start, end, moments, lanes: [{key, label, spans, marks}], credits}`: a span is `{step, role, start, end,
 * open, status, tone}` (tone: ok, bad, warn, busy or muted), a mark `{kind, t, tone, step, text}`, `moments` the times
 * something happened (the time scale is made from them: a step that waits an hour for a person has nothing in between).
 * Lanes are in the order a role first shows up; a mark is on the lane of its step, else of its own role, else of the
 * run steps. At most twelve lanes: the roles after the eleventh share one, "other roles".
 */
export function timeline(lines, progress, now = null) {
  const rows = (Array.isArray(lines) ? lines : [])
    .filter((line) => line && typeof line === 'object' && KINDS.has(line.kind))
    .map((line) => ({ ...line, t: parseTime(line.ts) }))
    .filter((line) => line.t !== null);
  const spans = [];
  const open = new Map(); // step -> the span that is going
  const roleOf = new Map(); // step -> its role
  const found = [];
  let last = -Infinity;
  for (const row of rows) {
    last = Math.max(last, row.t);
    const step = typeof row.step === 'string' ? row.step : '';
    const text = String(row.text ?? '');
    if (row.kind === 'step') {
      if (step && row.ok == null && text.startsWith(`${step} started`)) {
        const role = typeof row.role === 'string' ? row.role : RUN_LANE;
        if (open.has(step)) Object.assign(open.get(step), { end: row.t, open: false, status: 'ended', tone: 'muted' });
        const span = { step, role, start: row.t, end: row.t, open: true, status: 'running', tone: 'busy' };
        spans.push(span);
        open.set(step, span);
        roleOf.set(step, role);
      } else if (step && row.ok != null && text.startsWith(`${step} `) && open.has(step)) {
        Object.assign(open.get(step), { end: row.t, open: false }, ending(text, step, row.ok));
        open.delete(step);
      } else if (row.ok === false && text.startsWith('run interrupted')) {
        for (const span of open.values()) Object.assign(span, { end: row.t, open: false, status: 'interrupted', tone: 'warn' });
        open.clear();
      }
    } else if (row.kind === 'end') {
      for (const span of open.values()) Object.assign(span, { end: row.t, open: false, status: 'ended', tone: 'muted' });
      open.clear();
    } else if (MARKS.has(row.kind)) {
      found.push({ kind: row.kind, t: row.t, tone: MARK_TONES[row.kind](row.ok), step, text, role: typeof row.role === 'string' ? row.role : RUN_LANE });
    }
  }
  const going = finite(now);
  for (const span of open.values()) {
    if (going) {
      span.end = Math.max(now, last);
    } else {
      const said = progress?.steps?.[span.step]; // "running" is not an end: nothing ended it
      Object.assign(span, { end: last, open: false, status: typeof said === 'string' && said !== 'running' ? said : 'unfinished', tone: 'muted' });
    }
  }

  const lanes = [];
  const lane = (key) => {
    let one = lanes.find((candidate) => candidate.key === key);
    if (!one) lanes.push((one = { key, label: key || RUN_LABEL, spans: [], marks: [] }));
    return one;
  };
  for (const span of spans) lane(span.role).spans.push(span);
  for (const mark of found) lane(roleOf.has(mark.step) ? roleOf.get(mark.step) : mark.role).marks.push({ kind: mark.kind, t: mark.t, tone: mark.tone, step: mark.step, text: mark.text });
  if (lanes.length > MAX_LANES) {
    const rest = lanes.splice(MAX_LANES - 1);
    lanes.push({
      key: OTHER_LANE,
      label: 'other roles',
      spans: rest.flatMap((one) => one.spans).sort((a, b) => a.start - b.start),
      marks: rest.flatMap((one) => one.marks).sort((a, b) => a.t - b.t), // the layout steps marks aside in the order of time
    });
  }
  const moments = [...rows.map((row) => row.t), ...spans.map((span) => span.end)];
  return {
    empty: lanes.length === 0,
    start: moments.length ? Math.min(...moments) : 0,
    end: moments.length ? Math.max(...moments) : 0,
    moments,
    lanes,
    credits: creditSeries(lines),
  };
}

// --- time to x ---------------------------------------------------------------------------------------------------

/**
 * `journeyScale(spans, width)`: time to x over `width` pixels, with every wait longer than 20 minutes drawn as a short
 * break, so that a night waiting for an approval does not squeeze the steps into slivers. `spans` is `[[start, end]]`
 * in milliseconds. Returns `{x(t), segs, breaks}`: `segs` are the stretches of work (`{start, end, x0, w}`), `breaks`
 * the x of the middle of each break.
 */
export function journeyScale(spans, width) {
  const sorted = spans
    .filter(([a, b]) => finite(a) && finite(b))
    .map(([a, b]) => [Math.min(a, b), Math.max(a, b)])
    .sort((p, q) => p[0] - q[0]);
  const segs = [];
  for (const [a, b] of sorted) {
    const last = segs[segs.length - 1];
    if (last && a - last.end <= PAUSE_MS) last.end = Math.max(last.end, b);
    else segs.push({ start: a, end: b });
  }
  if (!segs.length) return { x: () => 0, segs: [], breaks: [] };
  const usable = Math.max(60, width - BREAK_W * (segs.length - 1));
  const weights = segs.map((seg) => Math.max(MINUTE, seg.end - seg.start)); // a short stretch still gets room
  const total = weights.reduce((a, b) => a + b, 0);
  let widths = weights.map((weight) => Math.max(MIN_SEG_W, (usable * weight) / total));
  const sum = widths.reduce((a, b) => a + b, 0);
  widths = widths.map((w) => (w * usable) / sum);
  let at = 0;
  const breaks = [];
  segs.forEach((seg, i) => {
    seg.x0 = at;
    seg.w = widths[i];
    at += widths[i];
    if (i < segs.length - 1) {
      breaks.push(at + BREAK_W / 2);
      at += BREAK_W;
    }
  });
  const x = (t) => {
    const seg = segs.find((s) => t >= s.start && t <= s.end) ?? segs.reduce((best, s) => (Math.abs(s.start - t) < Math.abs(best.start - t) ? s : best), segs[0]);
    const span = seg.end - seg.start;
    return span <= 0 ? seg.x0 + seg.w / 2 : seg.x0 + ((clamp(t, seg.start, seg.end) - seg.start) / span) * seg.w;
  };
  return { x, segs, breaks };
}

/**
 * Where everything of a `timeline` model goes in a drawing `width` pixels wide: `{width, height, labelW, x0, plotW,
 * top, bottom, lanes: [{key, label, y, bars, marks}], ticks, breaks}`. A bar has `x, w, y, h` and its span; a mark has
 * `x` (its time), `cx` (where it is drawn: marks that would touch step aside) and `y`; a tick `{x, label, anchor}`.
 */
export function layout(model, width = 640) {
  const labelW = clamp(Math.round(width * 0.27), 72, 120);
  const x0 = labelW + 8;
  const plotW = Math.max(80, width - x0 - 10);
  const scale = journeyScale(model.moments.map((t) => [t, t]), plotW);
  const X = (t) => x0 + scale.x(t);
  const lanes = model.lanes.map((one, index) => {
    const y = TOP + index * LANE_H;
    const bars = one.spans.map((span) => {
      const a = X(span.start);
      return { ...span, x: a, w: Math.max(4, X(span.end) - a), y: y + 4, h: 16 };
    });
    const marks = one.marks.map((mark) => ({ ...mark, x: X(mark.t), cx: X(mark.t), y: y + 12 }));
    const [low, high] = [x0 + 8, x0 + plotW - 8]; // a mark is drawn with its centre inside the plot
    let edge = low - MARK_GAP;
    for (const mark of marks) edge = mark.cx = Math.max(mark.x, edge + MARK_GAP);
    edge = high + MARK_GAP;
    for (const mark of [...marks].reverse()) edge = mark.cx = Math.min(mark.cx, edge - MARK_GAP);
    for (const mark of marks) mark.cx = clamp(mark.cx, low, high); // more marks than room: they overlap, they do not leave
    return { key: one.key, label: one.label, y, bars, marks };
  });
  const bottom = TOP + model.lanes.length * LANE_H;
  const ticks = [];
  let lastX = -Infinity;
  const tick = (x, t, anchor = 'middle') => {
    if (x - lastX < TICK_GAP) return;
    ticks.push({ x, label: elapsed(t - model.start), anchor });
    lastX = x;
  };
  const last = scale.segs[scale.segs.length - 1];
  const endX = last ? x0 + last.x0 + last.w : 0;
  for (const seg of scale.segs) {
    tick(x0 + seg.x0, seg.start);
    // inside a long stretch of work: round times (a multiple of 5 s, 10 min ...) since the start of the run
    const wanted = (seg.end - seg.start) / Math.max(1, Math.floor(seg.w / TICK_EVERY));
    const step = TICK_STEPS.find((candidate) => candidate >= wanted) ?? TICK_STEPS[TICK_STEPS.length - 1];
    for (let at = model.start + Math.ceil((seg.start - model.start + 1) / step) * step; at < seg.end; at += step) {
      if (X(at) <= endX - END_GAP) tick(X(at), at); // the end of the run keeps its place
    }
  }
  if (last) tick(endX, last.end, 'end');
  return { width, height: bottom + 26, labelW, x0, plotW, top: TOP, bottom, lanes, ticks, breaks: scale.breaks.map((b) => x0 + b) };
}

// --- the credits -------------------------------------------------------------------------------------------------

/** The points of a sparkline ("x,y x,y ...") in a box `w` by `h`; `''` for no values. One value is a flat line. */
export function sparklinePoints(values, w = 120, h = 28) {
  const list = (Array.isArray(values) ? values : []).filter(finite);
  if (!list.length) return '';
  const drawn = list.length === 1 ? [list[0], list[0]] : list;
  const max = Math.max(...drawn, 0.01);
  return drawn.map((v, i) => `${round((i / (drawn.length - 1)) * w)},${round(h - 2 - (v / max) * (h - 4))}`).join(' ');
}

/** A round number at or above `value` for the end of an axis: 1, 2, 2.5, 5, 10, 20, 25 ... */
function niceMax(value) {
  if (!(value > 0)) return 1;
  const power = 10 ** Math.floor(Math.log10(value));
  return ([1, 2, 2.5, 5, 10].find((m) => m * power >= value) ?? 10) * power;
}

/**
 * The credits by role as bars, biggest first, against the run's `cap` (`0` for none): `null` when no role spent anything,
 * else `{width, height, labelW, x0, plotW, max, capX, ticks, rows: [{role, amount, x, w, y}]}`.
 */
export function costLayout(byRole, cap = 0, width = 640) {
  const rows = Object.entries(byRole && typeof byRole === 'object' ? byRole : {})
    .filter(([, amount]) => finite(amount) && amount > 0)
    .sort((a, b) => b[1] - a[1]);
  if (!rows.length) return null;
  const labelW = clamp(Math.round(width * 0.27), 72, 120);
  const x0 = labelW + 8;
  const plotW = Math.max(80, width - x0 - 52); // the amount is written after the bar
  const max = niceMax(Math.max(rows[0][1], finite(cap) && cap > 0 ? cap : 0));
  const top = 16;
  return {
    width,
    height: top + rows.length * 22 + 22,
    labelW,
    x0,
    plotW,
    max,
    top,
    capX: finite(cap) && cap > 0 ? x0 + (cap / max) * plotW : null,
    ticks: [0, max / 2, max].map((value) => ({ value, x: x0 + (value / max) * plotW })),
    rows: rows.map(([role, amount], i) => ({ role, amount, x: x0, w: Math.max(1, (amount / max) * plotW), y: top + i * 22 })),
  };
}

// --- drawing -----------------------------------------------------------------------------------------------------

/** An SVG element: `attrs` are plain attributes (null and false leave one out), `children` are nodes. */
function el(doc, tag, attrs = {}, ...children) {
  const node = doc.createElementNS(NS, tag);
  for (const [name, value] of Object.entries(attrs)) if (value !== null && value !== undefined && value !== false) node.setAttribute(name, String(value));
  node.append(...children);
  return node;
}

/** An SVG element that holds text: set as text, never as markup. */
function text(doc, tag, attrs, content) {
  const node = el(doc, tag, attrs);
  node.textContent = String(content);
  return node;
}

/** `node` with a `<title>` (what a pointer or a screen reader says of it) as its first child: call it before adding the rest. */
function titled(doc, node, title) {
  node.append(text(doc, 'title', {}, title));
  return node;
}

const MARK_GLYPH = { ok: '✓', bad: '✗' };

function markShape(doc, mark) {
  const { cx, y, kind, tone } = mark;
  if (kind === 'gate') {
    return [el(doc, 'circle', { class: `flow-mark flow-${tone}`, cx, cy: y, r: 6 }), text(doc, 'text', { class: 'flow-glyph', x: cx, y: y + 3, 'text-anchor': 'middle' }, MARK_GLYPH[tone] ?? '·')];
  }
  if (kind === 'approval') return [el(doc, 'rect', { class: `flow-mark flow-${tone}`, x: cx - 4.5, y: y - 4.5, width: 9, height: 9, transform: `rotate(45 ${cx} ${y})` })];
  return [el(doc, 'polygon', { class: `flow-mark flow-${tone}`, points: `${cx},${y - 6} ${cx + 6},${y + 5} ${cx - 6},${y + 5}` })];
}

const MARK_WORDS = { gate: 'gates', approval: 'approval', retry: 'retry' };

/**
 * The timeline as an SVG element (`null` for an empty model): the lanes with their bars and marks, and the time
 * below. `width` is the width it is drawn for; it scales down to the box it is in.
 */
export function journey(model, { width = 640, doc = globalThis.document } = {}) {
  if (!model || model.empty) return null;
  const lay = layout(model, width);
  const steps = model.lanes.reduce((n, one) => n + one.spans.length, 0);
  const marks = model.lanes.reduce((n, one) => n + one.marks.length, 0);
  const svg = el(doc, 'svg', {
    class: 'flow-svg',
    viewBox: `0 0 ${lay.width} ${lay.height}`,
    width: lay.width,
    height: lay.height,
    role: 'img',
    'aria-label': `Timeline of the run: ${steps} ${steps === 1 ? 'step' : 'steps'} in ${lay.lanes.length} ${lay.lanes.length === 1 ? 'lane' : 'lanes'}, ${marks} ${marks === 1 ? 'mark' : 'marks'}`,
  });
  for (const t of lay.ticks) {
    svg.append(el(doc, 'line', { class: 'flow-grid', x1: t.x, x2: t.x, y1: lay.top, y2: lay.bottom }), text(doc, 'text', { class: 'flow-tick', x: t.x, y: lay.bottom + 16, 'text-anchor': t.anchor }, t.label));
  }
  for (const x of lay.breaks) svg.append(el(doc, 'path', { class: 'flow-break', d: `M${x - 4} ${lay.bottom - 4} l4 -6 l4 6 M${x - 4} ${lay.bottom + 3} l4 -6 l4 6` }));
  for (const one of lay.lanes) {
    svg.append(
      text(doc, 'text', { class: 'flow-label', x: 4, y: one.y + 16 }, clip(one.label, Math.floor((lay.labelW - 6) / 6.4))),
      el(doc, 'line', { class: 'flow-grid', x1: lay.x0, x2: lay.x0 + lay.plotW, y1: one.y + 12, y2: one.y + 12 }),
    );
    for (const bar of one.bars) {
      const group = titled(doc, el(doc, 'g', { class: 'flow-bar' }), `${bar.step}: ${bar.status}, ${elapsed(bar.end - bar.start) || '0 s'}`);
      group.append(el(doc, 'rect', { class: `flow-rect flow-${bar.tone}`, x: bar.x, y: bar.y, width: bar.w, height: bar.h, rx: 3 }));
      if (bar.w >= bar.step.length * 6.2 + 8) group.append(text(doc, 'text', { class: bar.tone === 'busy' ? 'flow-step flow-on-light' : 'flow-step', x: bar.x + 4, y: bar.y + 12 }, bar.step));
      svg.append(group);
    }
    for (const mark of one.marks) {
      const group = titled(doc, el(doc, 'g', { class: 'flow-markg' }), `${MARK_WORDS[mark.kind]}${mark.step ? ` (${mark.step})` : ''}: ${mark.text}`);
      group.append(...markShape(doc, mark));
      svg.append(group);
    }
  }
  return svg;
}

/** The key to the marks, as a paragraph (the colours are never the only sign: each mark has its own shape). */
export function legend(doc = globalThis.document) {
  const node = doc.createElement('p');
  node.className = 'flow-legend';
  node.textContent = '✓ gates passed · ✗ gates failed · ◆ approval · ▲ retry · bars: green done, red failed, amber sent back or stopped, dashed going, grey not finished';
  return node;
}

/** A small line of the credits spent so far (`values`: the cumulative amounts); `null` for none. */
export function sparkline(values, { w = 120, h = 28, doc = globalThis.document } = {}) {
  const points = sparklinePoints(values, w, h);
  if (!points) return null;
  return el(doc, 'svg', { class: 'flow-spark', viewBox: `0 0 ${w} ${h}`, width: w, height: h, 'aria-hidden': 'true' }, el(doc, 'polyline', { points }));
}

/** The credits by role as an SVG bar chart against the run's `cap`; `null` when no role spent anything. */
export function costChart(byRole, cap = 0, { width = 640, doc = globalThis.document } = {}) {
  const lay = costLayout(byRole, cap, width);
  if (!lay) return null;
  const total = lay.rows.reduce((sum, row) => sum + row.amount, 0);
  const svg = titled(
    doc,
    el(doc, 'svg', {
      class: 'flow-svg',
      viewBox: `0 0 ${lay.width} ${lay.height}`,
      width: lay.width,
      height: lay.height,
      role: 'img',
      'aria-label': `Credits by role: ${credits(total)} credits in ${lay.rows.length} ${lay.rows.length === 1 ? 'role' : 'roles'}${lay.capX === null ? '' : `, cap ${credits(cap)}`}`,
    }),
    lay.rows.map((row) => `${row.role} ${credits(row.amount)}`).join(', '),
  );
  for (const t of lay.ticks) {
    svg.append(el(doc, 'line', { class: 'flow-grid', x1: t.x, x2: t.x, y1: lay.top - 4, y2: lay.height - 20 }), text(doc, 'text', { class: 'flow-tick', x: t.x, y: lay.height - 6, 'text-anchor': t.value === 0 ? 'start' : t.x > lay.width - 30 ? 'end' : 'middle' }, credits(t.value)));
  }
  for (const row of lay.rows) {
    const group = titled(doc, el(doc, 'g', { class: 'flow-bar' }), `${row.role}: ${credits(row.amount)} credits`);
    group.append(
      text(doc, 'text', { class: 'flow-label', x: 4, y: row.y + 13 }, clip(row.role, Math.floor((lay.labelW - 6) / 6.4))),
      el(doc, 'rect', { class: 'flow-rect flow-credit', x: row.x, y: row.y, width: row.w, height: 16, rx: 3 }),
      text(doc, 'text', { class: 'flow-value', x: row.x + row.w + 4, y: row.y + 13 }, credits(row.amount)),
    );
    svg.append(group);
  }
  if (lay.capX !== null) svg.append(el(doc, 'line', { class: 'flow-cap', x1: lay.capX, x2: lay.capX, y1: lay.top - 8, y2: lay.height - 20 }), text(doc, 'text', { class: 'flow-cap-text', x: lay.capX, y: lay.top - 9, 'text-anchor': lay.capX > lay.width - 50 ? 'end' : 'middle' }, `cap ${credits(cap)}`));
  return svg;
}

/** Adds flow.css (the look of the drawings and of the weakened flags) to the page once. */
export function loadStyles(doc = globalThis.document) {
  if (doc.getElementById('flow-css')) return;
  const link = doc.createElement('link');
  link.id = 'flow-css';
  link.rel = 'stylesheet';
  link.href = '/web/flow.css';
  doc.head.append(link);
}
