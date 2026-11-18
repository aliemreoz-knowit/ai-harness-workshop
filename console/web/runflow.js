// The workflow of a run as a flow: one box per step in order, an arrow from each to the next, and under the row the
// ways back that the workflow allows (a failed ladder or a rejected review returns to an earlier step). A box shows the
// step's state, its role, whether it asks a person, and how many attempts it has had; the step the run is at is ringed.
//
// Two halves, like flow.js: `flowModel` works the picture out (no DOM, no imports: run with node in the tests) and
// `drawFlow` builds SVG with `createElementNS`. Every string from the server (a step, a role) goes on the page with
// `textContent`; the look is in style.css (`.runflow`).

const NS = 'http://www.w3.org/2000/svg';
const BOX_W = 152;
const BOX_H = 64;
const GAP = 30;
const PAD = 10;
const LOOP_STEP = 22; // the height of one way back under the row

/**
 * `{nodes, loops}` of a run. `steps` is the strip of the run page (`workflowStrip`: `{id, status, tone, current,
 * human, role}`), `workflow` the steps of the workflow as the structure gives them (`on_fail`, `on_reject`, `kind`),
 * `gates` the gate attempts of the run (`progress.gates`: `{step, attempt}`), `waitingAt` the step that waits for a
 * person (or null).
 * A node: `{id, n, role, tone, word, current, human, attempts, gated}`. A loop: `{from, to, label, used}`, both ends
 * indexes into `nodes`; `used` when the run has been back at `to` (it has had more than one attempt).
 */
export function flowModel(steps, workflow = null, gates = null, waitingAt = null) {
  const list = Array.isArray(steps) ? steps.filter((step) => typeof step?.id === 'string' && step.id) : [];
  const defs = new Map((Array.isArray(workflow) ? workflow : []).filter((step) => typeof step?.id === 'string').map((step) => [step.id, step]));
  const tries = new Map();
  for (const one of Array.isArray(gates) ? gates : []) {
    if (typeof one?.step !== 'string') continue;
    const n = Number.isInteger(one.attempt) ? one.attempt : (tries.get(one.step) ?? 0) + 1;
    tries.set(one.step, Math.max(tries.get(one.step) ?? 0, n));
  }
  const nodes = list.map((step, index) => {
    const def = defs.get(step.id);
    const waiting = waitingAt !== null && step.id === waitingAt;
    return {
      id: step.id,
      n: index + 1,
      role: typeof step.role === 'string' ? step.role : '',
      tone: waiting ? 'busy' : (step.tone ?? 'muted'),
      word: waiting ? 'waiting for you' : (step.status || 'to do'),
      current: Boolean(step.current) || waiting,
      human: Boolean(step.human),
      attempts: tries.get(step.id) ?? 0,
      gated: def?.kind === 'agent_gated',
    };
  });
  const at = new Map(nodes.map((node, index) => [node.id, index]));
  const loops = [];
  nodes.forEach((node, index) => {
    const def = defs.get(node.id);
    for (const [key, label] of [['on_fail', 'gates fail'], ['on_reject', 'rejected']]) {
      const to = at.get(def?.[key]);
      if (to === undefined || to >= index) continue; // "fail", "continue", "escalate" and forward targets are no way back
      loops.push({ from: index, to, label, used: nodes[to].attempts > 1 && node.tone !== 'muted' });
    }
  });
  loops.sort((a, b) => a.from - a.to - (b.from - b.to)); // the shortest way back sits closest to the row
  return { nodes, loops };
}

/** The size of the drawing and where a box starts. */
export function flowSize({ nodes, loops }) {
  const width = PAD * 2 + nodes.length * BOX_W + Math.max(0, nodes.length - 1) * GAP;
  const height = PAD + BOX_H + (loops.length ? 14 + loops.length * LOOP_STEP : 0) + PAD;
  return { width, height, x: (index) => PAD + index * (BOX_W + GAP), y: PAD };
}

const el = (name, attrs = {}, text = null) => {
  const node = document.createElementNS(NS, name);
  for (const [key, value] of Object.entries(attrs)) node.setAttribute(key, String(value));
  if (text !== null) node.textContent = text;
  return node;
};

const MARK = { ok: '✓', bad: '✗', warn: '!', busy: '●' };
const clip = (text, max) => (text.length > max ? `${text.slice(0, max - 1)}…` : text);

/** The flow of `model` as an `<svg>`; an empty model gives null. */
export function drawFlow(model) {
  if (!model?.nodes?.length) return null;
  const size = flowSize(model);
  const svg = el('svg', { class: 'runflow-svg', viewBox: `0 0 ${size.width} ${size.height}`, width: size.width, height: size.height, role: 'img' });
  svg.setAttribute('aria-label', `The workflow as a flow: ${model.nodes.map((node) => `${node.id} ${node.word}`).join(', ')}`);
  const defs = el('defs');
  for (const [id, cls] of [['runflow-arrow', 'runflow-head'], ['runflow-arrow-used', 'runflow-head used']]) {
    const marker = el('marker', { id, viewBox: '0 0 8 8', refX: 7, refY: 4, markerWidth: 7, markerHeight: 7, orient: 'auto-start-reverse' });
    marker.append(el('path', { d: 'M0 0.5L7 4 0 7.5z', class: cls }));
    defs.append(marker);
  }
  svg.append(defs);

  // the arrows from each step to the next
  model.nodes.slice(1).forEach((node, i) => {
    const x1 = size.x(i) + BOX_W + 3;
    const x2 = size.x(i + 1) - 4;
    const y = size.y + BOX_H / 2;
    const reached = model.nodes[i].tone === 'ok';
    svg.append(el('path', { d: `M${x1} ${y}H${x2}`, class: `runflow-link${reached ? ' reached' : ''}`, 'marker-end': 'url(#runflow-arrow)' }));
  });

  // the ways back, under the row
  model.loops.forEach((loop, level) => {
    const x1 = size.x(loop.from) + BOX_W / 2 + 10;
    const x2 = size.x(loop.to) + BOX_W / 2 - 10;
    const top = size.y + BOX_H + 3;
    const y = size.y + BOX_H + 14 + level * LOOP_STEP;
    const r = 8;
    const d = `M${x1} ${top}V${y - r}a${r} ${r} 0 0 1 -${r} ${r}H${x2 + r}a${r} ${r} 0 0 1 -${r} -${r}V${top + 2}`;
    svg.append(el('path', { d, class: `runflow-back${loop.used ? ' used' : ''}`, 'marker-end': `url(#runflow-arrow${loop.used ? '-used' : ''})` }));
    const label = el('text', { x: (x1 + x2) / 2, y: y + 3.5, class: `runflow-back-label${loop.used ? ' used' : ''}`, 'text-anchor': 'middle' }, loop.label);
    const pad = el('rect', { x: (x1 + x2) / 2 - loop.label.length * 3.4 - 6, y: y - 8, width: loop.label.length * 6.8 + 12, height: 16, class: 'runflow-back-pad' });
    svg.append(pad, label);
  });

  // the boxes
  model.nodes.forEach((node, index) => {
    const x = size.x(index);
    const y = size.y;
    const group = el('g', { class: `runflow-node tone-${node.tone}${node.current ? ' current' : ''}`, transform: `translate(${x} ${y})` });
    if (node.current) group.append(el('rect', { x: -4, y: -4, width: BOX_W + 8, height: BOX_H + 8, rx: 10, class: 'runflow-ring' }));
    group.append(el('rect', { x: 0, y: 0, width: BOX_W, height: BOX_H, rx: 8, class: 'runflow-box' }));
    group.append(el('rect', { x: 0, y: 0, width: 4, height: BOX_H, rx: 2, class: 'runflow-edge' }));
    group.append(el('text', { x: 14, y: 21, class: 'runflow-n' }, String(node.n)));
    group.append(el('text', { x: 28, y: 21, class: 'runflow-name' }, clip(node.id, 15)));
    const meta = node.human ? (node.role ? `${node.role} · asks you` : 'asks you to approve') : node.role;
    if (meta) group.append(el('text', { x: 14, y: 37, class: 'runflow-meta' }, clip(meta, 20)));
    const mark = MARK[node.tone] ? `${MARK[node.tone]} ` : '';
    group.append(el('text', { x: 14, y: 54, class: 'runflow-word' }, `${mark}${clip(node.word, 18)}`));
    if (node.attempts > 1) {
      const text = `×${node.attempts}`;
      group.append(el('rect', { x: BOX_W - 34, y: 8, width: 26, height: 17, rx: 8.5, class: 'runflow-tries-pad' }));
      group.append(el('text', { x: BOX_W - 21, y: 20.5, class: 'runflow-tries', 'text-anchor': 'middle' }, text));
    }
    const title = el('title', {}, `${node.n}. ${node.id}: ${node.word}${node.role ? `, ${node.role}` : ''}${node.attempts > 1 ? `, ${node.attempts} attempts` : ''}${node.gated ? ', the fast gates run after every attempt' : ''}`);
    group.append(title);
    svg.append(group);
  });
  return svg;
}
