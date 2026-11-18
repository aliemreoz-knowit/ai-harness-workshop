// What a command printed, read as a run: the Today page's terminal shows a harness run or an experiment in a form
// people can follow (the steps with their gate checks; the runs of an experiment as a table) next to the raw text.
// Pure: no DOM. parseRunLog gives null for any other output (a JSON answer, a diff, a gate ladder on its own), which
// stays raw text.
//
// A harness run prints (harness/orchestrator.py): "[harness] step <name> (<kind>[, <role>])", under a step
// "[harness]   gates (tool|harness): <gate>:<STATUS> ... (<note>)", "[harness]   review round <n>: <verdict> (...)",
// "[harness]   committed <sha> on <branch> (...)", before the agents "[harness]   baseline <gates> -> <rule>", and at
// the end "[harness] run <id>: <status>" and "[harness] credits <n>, interventions <n>, retries <n>, folder ...".
// An experiment prints (harness/eval): "[eval] experiment <name>: <what>", one "[eval]   i/n PASS|FAIL <variant>
// <ticket> r<k>  status=<s> credits=<c> interventions=<i> retries=<r> <secs>s" per run, a summary table, and
// "[eval] results: <file>".

import { parseGates } from './runmodel.js';

const STEP = /^\[harness\] step (\S+) \(([^,()]+)(?:, ([^()]+))?\)\s*$/;
const CHECK = /^\[harness\]\s+gates \((tool|harness)\): (.*?)\s*(?:\(([^()]*)\))?\s*$/;
const BASELINE = /^\[harness\]\s+baseline (\S+:\S+.*?)\s*->\s*(.*)$/;
const REVIEW = /^\[harness\]\s+review round (\d+): (\S+)(?: \(([^()]*)\))?/;
const COMMITTED = /^\[harness\]\s+committed (\S+) on (\S+)(?: \(([^()]*)\))?/;
const END = /^\[harness\] run (\S+): (\S+)\s*$/;
const TOTALS = /^\[harness\] credits ([\d.]+), interventions (\d+), retries (\d+)/;
const RUN = /^\[eval\]\s+(\d+)\/(\d+) (PASS|FAIL) (\S+)\s+(\S+)\s+r(\d+)\s+status=(\S+) credits=([\d.]+) interventions=(\d+) retries=(\d+) ([\d.]+)s\s*$/;
const EXPERIMENT = /^\[eval\] experiment (\S+): (.*)$/;
const ROW = /^(\S+)\s+(\d+)\s+(\d+)\/(\d+)\s+(\d+)%\s+(\d+)-(\d+)%\s+(\d+)\/(\d+)\s+(\d+)%\s+([\d.]+)\s+([\d.]+)\s+([\d.]+)\s+(\d+)\s+(\S+)\s+(\S+)\s+(.*)$/;
const RULE = /^-{10,}\s*$/;

const strip = (line) => line.replace(/\s+$/, '');

/**
 * A harness run or an experiment in `text`, or null. A run: {kind: 'run', items} in the order printed, each one of
 * {t: 'text', v} | {t: 'baseline', gates, rule} (before any step) | {t: 'step', name, kind, role, round, checks: [{who:
 * 'tool'|'harness'|'baseline', gates, note}],
 * notes: [{t: 'review'|'commit'|'text', ...}]} | {t: 'end', run, status} | {t: 'totals', credits, interventions,
 * retries}. An experiment: {kind: 'experiment', name, what, runs: [...], summary: [...], items} with the other lines
 * as text items in their place.
 */
export function parseRunLog(text) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n').map(strip);
  if (lines.some((line) => STEP.test(line))) return parseRun(lines);
  if (lines.some((line) => RUN.test(line) || EXPERIMENT.test(line))) return parseExperiment(lines);
  return null;
}

function parseRun(lines) {
  const items = [];
  const rounds = new Map(); // how often a step name has started: the ladder sends a run back to implement
  let step = null;
  for (const line of lines) {
    let m;
    if ((m = STEP.exec(line))) {
      const round = (rounds.get(m[1]) ?? 0) + 1;
      rounds.set(m[1], round);
      step = { t: 'step', name: m[1], kind: m[2].trim(), role: (m[3] ?? '').trim(), round, checks: [], notes: [] };
      items.push(step);
    } else if ((m = CHECK.exec(line)) && step) {
      step.checks.push({ who: m[1], gates: parseGates(m[2]), note: m[3] ?? '' });
    } else if ((m = BASELINE.exec(line))) {
      // the gates on the untouched code: part of the step it ran in (the ticket step), else a row of its own
      if (step) step.checks.push({ who: 'baseline', gates: parseGates(m[1]), note: m[2].trim() });
      else items.push({ t: 'baseline', gates: parseGates(m[1]), rule: m[2].trim() });
    } else if ((m = REVIEW.exec(line)) && step) {
      step.notes.push({ t: 'review', round: Number(m[1]), verdict: m[2], detail: m[3] ?? '' });
    } else if ((m = COMMITTED.exec(line)) && step) {
      step.notes.push({ t: 'commit', sha: m[1], branch: m[2], detail: m[3] ?? '' });
    } else if ((m = END.exec(line))) {
      items.push({ t: 'end', run: m[1], status: m[2] });
      step = null;
    } else if ((m = TOTALS.exec(line))) {
      items.push({ t: 'totals', credits: Number(m[1]), interventions: Number(m[2]), retries: Number(m[3]) });
    } else if (/^\[harness\] baseline:/.test(line)) {
      continue; // "baseline: test gates ... on the untouched base": the baseline row says it
    } else if (/^\[harness\]\s{2,}\S/.test(line) && step) {
      step.notes.push({ t: 'text', v: line.replace(/^\[harness\]\s+/, '') });
    } else if (line.trim() && !/^\[exit \d+\]$/.test(line)) {
      items.push({ t: 'text', v: line.replace(/^\[harness\]\s+/, '') });
    }
  }
  return { kind: 'run', items };
}

function parseExperiment(lines) {
  const out = { kind: 'experiment', name: '', what: '', runs: [], summary: [], items: [] };
  for (const line of lines) {
    let m;
    if ((m = RUN.exec(line))) {
      out.runs.push({
        n: Number(m[1]), of: Number(m[2]), pass: m[3] === 'PASS', variant: m[4], ticket: m[5], rep: Number(m[6]),
        status: m[7], credits: Number(m[8]), interventions: Number(m[9]), retries: Number(m[10]), secs: Number(m[11]),
      });
    } else if ((m = EXPERIMENT.exec(line))) {
      out.name = m[1];
      out.what = m[2];
    } else if ((m = ROW.exec(line))) {
      out.summary.push({
        variant: m[1], runs: Number(m[2]), passed: Number(m[3]), of: Number(m[4]), rate: Number(m[5]),
        low: Number(m[6]), high: Number(m[7]), credits: Number(m[11]), retries: Number(m[13]), secs: Number(m[14]),
        label: m[17].trim(),
      });
    } else if (/^variant\s+runs\s+pass/.test(line) || RULE.test(line) || !line.trim() || /^\[exit \d+\]$/.test(line)) {
      continue;
    } else {
      out.items.push({ t: 'text', v: line.replace(/^\[eval\]\s+/, '') });
    }
  }
  return out;
}

/** "2:35" for 155 seconds, "15:34" for 934; "" for a value that is not a number. */
export function clock(secs) {
  if (!Number.isFinite(secs)) return '';
  const s = Math.round(secs);
  return `${Math.floor(s / 60)}:${String(s % 60).padStart(2, '0')}`;
}

/** What a step is, in words: the role of an agent step, "full gate ladder", "a person approves" ... */
export function stepWho(step) {
  if (step.role) return step.role;
  return { ticket: 'reads the ticket', gates: 'full gate ladder', approval: 'a person approves', commit: 'commits locally' }[step.kind]
    ?? step.kind;
}

/** `{passed, total, failed: [ids]}` of one gate check. */
export function checkCount(gates) {
  const ran = gates.filter((gate) => gate.status !== 'SKIP' && gate.status !== 'NOT_RUN');
  return {
    passed: ran.filter((gate) => gate.status === 'PASS').length,
    total: ran.length,
    failed: ran.filter((gate) => gate.status === 'FAIL' || gate.status === 'ERROR').map((gate) => gate.id),
  };
}
