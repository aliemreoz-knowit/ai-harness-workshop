// A run log as the Today page's terminal shows it when "Readable" is on: runlog.js reads the text, this draws it.
// A harness run is its steps in order, each agent step with its role and its gate checks as pass and fail marks; an
// experiment is a table of its runs (the result next to what the agent said) and the summary per variant. Text only
// (textContent), never HTML: the log quotes tickets and model output. The look is style.css (.rl-*).

import { checkCount, clock, stepWho } from './runlog.js';

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children.filter((child) => child !== null && child !== undefined && child !== false));
  return node;
}

const STATUS_TONE = { done: 'ok', completed: 'ok', failed: 'bad', error: 'bad', aborted: 'bad', refused: 'bad', escalated: 'warn', stopped: 'muted' };
const WHO = {
  tool: 'The agent checks its own work',
  harness: 'The harness checks the work',
  baseline: 'Before the agent: the gates on the untouched code',
};

/** One gate as a mark: ✓ or ✗ (or "skip") with its name; colour is never the only sign. */
function gateMark(gate) {
  const pass = gate.status === 'PASS';
  const fail = gate.status === 'FAIL' || gate.status === 'ERROR';
  const mark = el('span', `rl-gate ${pass ? 'is-pass' : fail ? 'is-fail' : 'is-skip'}`, pass ? '✓ ' : fail ? '✗ ' : '– ', gate.id);
  mark.title = `${gate.id}: ${gate.status}`;
  return mark;
}

function gateRow(label, gates, note) {
  const { passed, total, failed } = checkCount(gates);
  const verdict = failed.length
    ? el('span', 'rl-verdict is-fail', `${failed.join(', ')} failed`, el('span', 'rl-count', ` · ${passed} of ${total} passed`))
    : el('span', 'rl-verdict is-pass', total ? `all ${total} passed` : 'no gate ran');
  return el(
    'div',
    'rl-check',
    el('div', 'rl-check-head', el('span', 'rl-check-label', label), verdict),
    el('div', 'rl-gates', ...gates.map(gateMark)),
    note ? el('p', 'rl-note', note) : null,
  );
}

function stepNote(note) {
  if (note.t === 'review') {
    return el('p', 'rl-line', el('strong', '', `Review round ${note.round}: ${note.verdict}`), note.detail ? ` (${note.detail})` : '');
  }
  if (note.t === 'commit') {
    return el('p', 'rl-line', el('strong', '', 'Committed '), el('code', '', note.sha.slice(0, 10)), ' on ', el('code', '', note.branch),
      note.detail ? ` (${note.detail})` : '');
  }
  return el('p', 'rl-line rl-soft', note.v);
}

function drawRun(parsed) {
  const out = [];
  const end = parsed.items.find((item) => item.t === 'end');
  const totals = parsed.items.find((item) => item.t === 'totals');
  if (end || totals) {
    const facts = totals
      ? [`${totals.credits.toFixed(2)} credits`, `${totals.retries} ${totals.retries === 1 ? 'retry' : 'retries'}`,
        `${totals.interventions} ${totals.interventions === 1 ? 'intervention' : 'interventions'}`]
      : [];
    const status = end?.status ?? '';
    out.push(el('div', 'rl-banner',
      status ? el('span', `rl-status is-${STATUS_TONE[status] ?? 'muted'}`, status) : null,
      el('span', 'rl-facts', facts.join(' · ')),
      end ? el('code', 'rl-run-id', end.run) : null));
  }
  const steps = parsed.items.filter((item) => item.t === 'step');
  let number = 0;
  parsed.items.forEach((item) => {
    if (item.t === 'text') {
      out.push(el('p', 'rl-line rl-soft', item.v));
    } else if (item.t === 'baseline') {
      out.push(el('div', 'rl-step rl-baseline',
        el('span', 'rl-num', '·'),
        el('div', 'rl-step-body',
          el('div', 'rl-step-head', el('span', 'rl-step-name', 'Before the agent'), el('span', 'rl-who', 'the gates on the untouched code')),
          gateRow('What is broken now', item.gates, item.rule))));
    } else if (item.t === 'step') {
      number += 1;
      const next = steps[steps.indexOf(item) + 1];
      const last = item.checks[item.checks.length - 1];
      const back = item.kind === 'gates' && last && checkCount(last.gates).failed.length && next && next.round > 1
        ? el('p', 'rl-back', `↩ Back to ${next.name}: the ${stepWho(next)} tries again with the gate output`)
        : null;
      out.push(el('div', `rl-step${item.kind === 'gates' ? ' is-ladder' : ''}`,
        el('span', 'rl-num', String(number)),
        el('div', 'rl-step-body',
          el('div', 'rl-step-head',
            el('span', 'rl-step-name', item.name, item.round > 1 ? el('span', 'rl-round', ` · round ${item.round}`) : null),
            el('span', 'rl-who', stepWho(item))),
          ...item.checks.map((check) => gateRow(WHO[check.who] ?? check.who, check.gates, check.note)),
          ...item.notes.map(stepNote),
          back)));
    }
  });
  return out;
}

function table(head, rows) {
  return el('div', 'rl-table-box', el('table', 'rl-table',
    el('thead', '', el('tr', '', ...head.map((cell) => el('th', '', cell)))),
    el('tbody', '', ...rows.map((cells) => el('tr', '', ...cells.map((cell) => el('td', '', cell)))))));
}

function drawExperiment(parsed) {
  const out = [];
  if (parsed.name) out.push(el('p', 'rl-exp', el('strong', '', `Experiment ${parsed.name}`), parsed.what ? ` · ${parsed.what}` : ''));
  if (parsed.runs.length) {
    out.push(table(
      ['#', 'Held-back tests', 'Variant', 'Ticket', 'Repeat', 'The agent said', 'Credits', 'Retries', 'Time'],
      parsed.runs.map((run) => [
        `${run.n}/${run.of}`,
        el('span', `rl-status is-${run.pass ? 'ok' : 'bad'}`, run.pass ? '✓ PASS' : '✗ FAIL'),
        el('code', '', run.variant),
        run.ticket,
        `r${run.rep}`,
        el('span', `rl-said is-${STATUS_TONE[run.status] ?? 'muted'}`, run.status),
        run.credits.toFixed(2),
        String(run.retries),
        clock(run.secs),
      ]),
    ));
  }
  if (parsed.summary.length) {
    out.push(el('p', 'rl-sub', 'Per variant'));
    out.push(table(
      ['Variant', 'Passed', '95% CI', 'Credits per run', 'Retries per run', 'Time per run'],
      parsed.summary.map((row) => [
        el('span', '', el('code', '', row.variant), el('span', 'rl-label', row.label)),
        el('span', 'rl-pass', el('span', 'rl-bar', el('span', 'rl-bar-fill')), `${row.passed}/${row.of} · ${row.rate}%`),
        `${row.low}–${row.high}%`,
        row.credits.toFixed(2),
        String(row.retries),
        clock(row.secs),
      ]),
    ));
    out[out.length - 1].querySelectorAll('.rl-bar-fill').forEach((fill, k) => {
      fill.style.width = `${Math.max(0, Math.min(100, parsed.summary[k].rate))}%`;
    });
  }
  out.push(...parsed.items.map((item) => el('p', 'rl-line rl-soft', item.v)));
  return out;
}

/** `parsed` (runlog.parseRunLog) as a `div.rl`. */
export function renderRunLog(parsed) {
  return el('div', 'rl', ...(parsed.kind === 'run' ? drawRun(parsed) : drawExperiment(parsed)));
}
