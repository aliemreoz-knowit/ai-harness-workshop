// What the Runs view shows and how it words it: status chips, the gate matrix, log line looks, approval buttons,
// credits and durations, the queue widget and the log's line buffer. Pure: no DOM, no imports.

export const FINAL = ['done', 'failed', 'escalated', 'stopped', 'refused', 'error'];
export const ACTIVE = ['running', 'waiting'];
/** What a unit needs left to start a workflow run (the server checks it too). */
export const MIN_CREDITS = 1;

/** A run that is over (its result may be kept or discarded). */
export const isFinal = (status) => FINAL.includes(status);

/** A run that is queued or going: a unit has at most one of these. */
export const isOpen = (status) => status === 'queued' || ACTIVE.includes(status);

const parseTime = (ts) => {
  if (typeof ts !== 'string') return null;
  const ms = Date.parse(ts.replace(/(\.\d{3})\d+/, '$1')); // the server writes microseconds; only milliseconds are standard
  return Number.isNaN(ms) ? null : ms;
};

// --- status ----------------------------------------------------------------------------------------------------------

const CHIPS = {
  queued: ['queued', 'muted'],
  running: ['running', 'busy'],
  waiting: ['waiting', 'busy'],
  done: ['done', 'ok'],
  failed: ['failed', 'bad'],
  escalated: ['escalated', 'warn'],
  stopped: ['stopped', 'muted'],
  refused: ['refused', 'bad'],
  error: ['error', 'bad'],
};

/** `{text, tone}` of a run record's status chip. tone: ok | bad | warn | busy | muted. */
export function statusChip(rec) {
  const status = String(rec?.status ?? '');
  if (status === 'queued') return { text: Number.isInteger(rec.position) ? `queued #${rec.position}` : 'starting', tone: 'muted' };
  if (status === 'waiting') return { text: rec.pending?.kind === 'escalation' ? 'needs a decision' : 'needs approval', tone: 'busy' };
  const [name, tone] = Object.hasOwn(CHIPS, status) ? CHIPS[status] : [status || 'unknown', 'muted'];
  let text = name;
  if (isFinal(status) && status !== 'refused') {
    if (rec.kept === true) text += ', kept';
    else if (rec.kept === false) text += ', discarded';
  }
  return { text, tone };
}

const STEP_TONES = { done: 'ok', running: 'busy', failed: 'bad', fail: 'bad', rejected: 'bad', escalated: 'warn', aborted: 'warn' };

/** The steps of a run in the order they started: `[{id, status, tone, current}]` from `progress.steps`. */
export function stepStrip(progress) {
  const steps = progress?.steps;
  if (!steps || typeof steps !== 'object') return [];
  return Object.entries(steps).map(([id, status]) => ({
    id,
    status: String(status),
    tone: Object.hasOwn(STEP_TONES, status) ? STEP_TONES[status] : 'muted',
    current: progress.current === id,
  }));
}

/**
 * The step strip of a run against its workflow: `workflow` is the workflow's `steps` as GET /api/structure has them
 * (`[{id, role, approval}]`). When every step the run has started is in it, in its order, the strip is the whole
 * workflow: a step the run has not reached has the status `''`. Otherwise (the workflow changed since, or there is
 * none to go by) it is the run's own steps, as `stepStrip` has them. `[{id, status, tone, current, human, role}]`:
 * `human` is a step that asks a person (`approval: human`). `over`: the run has ended, so a step still `running` was
 * never finished (it was stopped, or the run broke off): `not finished`.
 */
export function workflowStrip(progress, workflow, over = false) {
  const own = stepStrip(progress).map((step) => (over && step.status === 'running' ? { ...step, status: 'not finished', tone: 'warn', current: false } : step));
  const steps = (Array.isArray(workflow) ? workflow : []).filter((step) => typeof step?.id === 'string' && step.id);
  const ids = steps.map((step) => step.id);
  const at = own.map((step) => ids.indexOf(step.id));
  const fits = steps.length > 0 && at.every((index, n) => index >= 0 && (n === 0 || index > at[n - 1]));
  if (!fits) return own.map((step) => ({ ...step, human: false, role: null }));
  return steps.map((step) => {
    const seen = own.find((one) => one.id === step.id) ?? { id: step.id, status: '', tone: 'muted', current: false };
    return { ...seen, human: step.approval === 'human', role: typeof step.role === 'string' ? step.role : null };
  });
}

/** The ids of the steps of a workflow that ask a person (`approval: human`), in order. */
export function askedAt(workflow) {
  return (Array.isArray(workflow) ? workflow : []).filter((step) => step?.approval === 'human' && typeof step.id === 'string').map((step) => step.id);
}

/** What a run is called: "jira-to-pr · tickets/PROJ-5.md", "Check my code". */
export function runTitle(rec) {
  if (rec?.kind === 'gates') return 'Check my code';
  if (rec?.kind === 'warm') return 'Warm-up (gates)';
  return [rec?.workflow || 'workflow', rec?.ticket].filter(Boolean).join(' · ');
}

/** What started a run, in words: "Check my code", "Agent run, interactive, harness of unit-04". */
export function runOrigin(rec) {
  if (rec?.kind === 'gates') return 'Check my code';
  if (rec?.kind === 'warm') return 'Warm-up, by the facilitator';
  const parts = ['Agent run'];
  if (typeof rec?.mode === 'string' && rec.mode) parts.push(rec.mode);
  if (typeof rec?.overlay_from === 'string' && rec.overlay_from) parts.push(`harness of ${rec.overlay_from}`);
  return parts.join(', ');
}

/**
 * "harness of unit-04 (a1b2c3d)" for a run that took another unit's harness structure (a handover run), with the short
 * id of the commit of that unit's branch it took once the run has it; `''` for any other run.
 */
export function overlayLabel(rec) {
  if (typeof rec?.overlay_from !== 'string' || !rec.overlay_from) return '';
  const commit = rec.overlay_source_commit;
  return `harness of ${rec.overlay_from}${typeof commit === 'string' && /^[0-9a-f]{7,}$/.test(commit) ? ` (${commit.slice(0, 7)})` : ''}`;
}

// --- personal mode: the model of a run, the setup, the tickets --------------------------------------------------------

/** "Fake model" for a run on the harness's scripted fake model (`fake` on its record: no model, no credits), else `''`. */
export function modelLabel(rec) {
  return rec?.fake === true ? 'Fake model' : '';
}

/**
 * The model the Runs page picks for a new run in personal mode, from GET /api/setup: `'copilot'` when the doctor says the
 * participant's own Copilot works ("Full (your Copilot)"), else `'fake'`: "(fake model)", or not known (yet).
 */
export function modelDefault(setup) {
  return typeof setup?.depth === 'string' && setup.depth.endsWith('(your Copilot)') ? 'copilot' : 'fake';
}

/** The tickets a run may take: in personal mode only the agent-ready versions (`tickets/<KEY>.agent.md`). */
export function ticketChoices(paths, personal) {
  const all = (Array.isArray(paths) ? paths : []).filter((path) => typeof path === 'string');
  if (!personal) return all;
  // personal: the agent-ready versions first, then the reporters' own texts (a ticket as it came in, for example
  // TS-666 with its hidden orders, which the sanitizer must catch)
  const ready = all.filter((path) => path.endsWith('.agent.md'));
  return [...ready, ...all.filter((path) => /(^|\/)[A-Z][A-Z0-9]*-\d+\.md$/.test(path))];
}

const LADDER_WORDS = { lite: 'gates without Docker (lite ladder)', full: 'gates in Docker (full ladder)' };
// A lite console says why: it was started with TS_LITE=1, the setup recorded "no Docker", or the image is not there
// (`ladder_why` of GET /api/setup).
const LITE_WHY_WORDS = {
  'ts-lite': 'lite ladder (TS_LITE=1)',
  'no-docker': 'lite ladder (set up without Docker)',
  'no-image': 'lite ladder (no Docker image)',
};

/**
 * The Today page's "Your setup" from GET /api/setup: `{text, reason, busy}`. `busy` while the doctor runs (the page asks
 * again; a check after the first one keeps showing the last result).
 */
export function setupSummary(setup) {
  const depth = typeof setup?.depth === 'string' ? setup.depth : '';
  const reason = typeof setup?.reason === 'string' ? setup.reason : '';
  const ladder = setup?.ladder === 'lite' && Object.hasOwn(LITE_WHY_WORDS, setup?.ladder_why ?? '') ? LITE_WHY_WORDS[setup.ladder_why]
    : Object.hasOwn(LADDER_WORDS, setup?.ladder ?? '') ? LADDER_WORDS[setup.ladder] : '';
  const line = [depth, ladder].filter(Boolean).join(' · ');
  if (!setup || setup.state === 'checking') return depth ? { text: line, reason: 'Checking again…', busy: true } : { text: 'Checking your setup…', reason: '', busy: true };
  if (setup.state !== 'ready' || !depth) return { text: 'Your setup could not be checked.', reason, busy: false };
  return { text: line, reason, busy: false };
}

/**
 * The line above a command's recorded result on the Today page (workshop/commands.json: `recorded_at`, `recorded_took`,
 * `recorded_credits`): "Recorded 19 Nov 2026 · took 3:41 · 6.75 credits", the parts that are missing left out.
 */
export function recordedLine(command) {
  const text = (value) => (typeof value === 'string' ? value.trim() : '');
  const at = text(command?.recorded_at);
  const took = text(command?.recorded_took);
  const credits = text(command?.recorded_credits);
  return [at ? `Recorded ${at}` : 'Recorded', took ? `took ${took}` : '', credits ? `${credits} ${credits === '1' ? 'credit' : 'credits'}` : '']
    .filter(Boolean)
    .join(' · ');
}

/**
 * The part of that line that says a recorded result was cut (GET /api/workshop/commands/<id>/recorded: `truncated`,
 * `bytes` the file's size, `max_bytes` what is sent of it): "cut to the first 256 KB of 300 KB"; `''` when it was not.
 */
export function recordedCut(result) {
  if (!result?.truncated) return '';
  const kb = (n) => `${Math.ceil(n / 1024).toLocaleString('en-US')} KB`;
  const shown = Number.isFinite(result.max_bytes) ? `cut to the first ${kb(result.max_bytes)}` : 'cut short';
  return Number.isFinite(result.bytes) ? `${shown} of ${kb(result.bytes)}` : shown;
}

/** What a handover run left out of that harness, in one sentence; `''` when it took everything or is no handover run. */
export function skippedNote(rec) {
  const paths = Array.isArray(rec?.overlay_skipped) ? rec.overlay_skipped.filter((path) => typeof path === 'string') : [];
  if (!overlayLabel(rec) || !paths.length) return '';
  return `Not taken from ${rec.overlay_from}: ${paths.join(', ')}. Links, files that are too large or not text, and files that cannot be edited here are left behind.`;
}

// --- gates -----------------------------------------------------------------------------------------------------------

// "compliance:PASS diff-policy:PASS build:PASS unit-tests:FAIL" is what the harness writes; "build PASS, misra FAIL" is also read.
const GATE = /\b([a-z][a-z0-9]*(?:-[a-z0-9]+)*|G\d+|diff-policy)[:\s]+(PASS|FAIL|SKIP|NOT_RUN|ERROR)\b/g;
const GATE_ORDER = ['compliance', 'style', 'comments', 'diff-size', 'diff-policy', 'build', 'misra', 'no-heap', 'unit-tests', 'property-tests', 'sanitizers', 'fuzz', 'loopback', 'budgets', 'coverage']; // the harness ladder, cheap first
const GATE_MAX = 64;

/** `[{id, status}]` of a gates summary text, in the order written. */
export function parseGates(summary) {
  const found = [];
  for (const match of String(summary ?? '').matchAll(GATE)) {
    if (found.length >= GATE_MAX) break;
    found.push({ id: match[1], status: match[2] });
  }
  return found;
}

const rank = (id) => {
  const at = GATE_ORDER.indexOf(id);
  return at < 0 ? GATE_ORDER.length : at;
};

// "lite ladder: misra no-heap sanitizers coverage not run here (needs Docker)": the gates the Docker-free ladder could not run
const LITE = /lite ladder: ((?:(?:[a-z][a-z0-9]*(?:-[a-z0-9]+)*) )*(?:[a-z][a-z0-9]*(?:-[a-z0-9]+)*)) not run here/;

/** The ids of the gates a summary says the Docker-free ladder did not run here (they need Docker). */
export function notRunHere(summary) {
  const match = LITE.exec(String(summary ?? ''));
  return match ? match[1].split(' ') : [];
}

/**
 * The gate matrix of `progress.gates` (`[{step, attempt, summary, ok}]`): `columns` are the gates seen, in ladder order
 * (compliance, style, comments, diff-policy, build, misra ...; a gate the ladder does not know comes last); each of the `rows` is one attempt with its
 * `label` ("implement #2") and `cells` (gate id to PASS | FAIL | SKIP | NOT_RUN | ERROR, or NOT_HERE: a SKIP of a gate
 * the Docker-free ladder did not run, as its summary says).
 */
export function gateMatrix(attempts) {
  const seen = [];
  const rows = [];
  for (const attempt of Array.isArray(attempts) ? attempts : []) {
    if (!attempt || typeof attempt.summary !== 'string') continue;
    const cells = {};
    const here = notRunHere(attempt.summary);
    for (const { id, status } of parseGates(attempt.summary)) {
      cells[id] = status === 'SKIP' && here.includes(id) ? 'NOT_HERE' : status;
      if (!seen.includes(id)) seen.push(id);
    }
    const number = Number.isInteger(attempt.attempt) ? attempt.attempt : null;
    // who ran the gates: the agent itself, checking its own work with its run_gates tool ("tool"), or the harness,
    // checking the step's result ("harness"); each keeps its own count. The baseline runs before any agent.
    const n = number === null ? '' : ` #${number}`;
    const during = attempt.step ? ` (${attempt.step} step)` : '';
    const label = attempt.step === 'baseline' ? 'Gate check before the agent starts (baseline)'
      : attempt.trigger === 'tool' ? `Agent checks its own work${n}${during}`
      : attempt.trigger === 'harness' ? `Harness checks the agent’s work${n}${during}`
      : `${attempt.step || 'gates'}${n}`;
    rows.push({
      label,
      step: attempt.step || null,
      ok: typeof attempt.ok === 'boolean' ? attempt.ok : null,
      cells,
    });
  }
  return { columns: seen.sort((a, b) => rank(a) - rank(b)), rows };
}

const CELLS = {
  PASS: ['✓ pass', 'ok'],
  FAIL: ['✗ fail', 'bad'],
  SKIP: ['skip', 'muted'],
  NOT_RUN: ['not run', 'muted'],
  NOT_HERE: ['not run here', 'muted'],
  ERROR: ['! error', 'bad'],
};

/**
 * What the last attempt of a gate matrix says, in one line: `{text, tone}` ("All 6 gates passed", ok; "unit-tests failed: 5 of
 * 6 gates passed", bad); `null` when no gate has run.
 */
export function gateVerdict(matrix) {
  const last = matrix?.rows?.[matrix.rows.length - 1];
  if (!last) return null;
  const ids = (matrix.columns ?? []).filter((id) => Object.hasOwn(last.cells, id));
  const total = ids.length;
  if (!total) return null;
  const passed = ids.filter((id) => last.cells[id] === 'PASS').length;
  const failed = ids.filter((id) => last.cells[id] === 'FAIL' || last.cells[id] === 'ERROR');
  const here = ids.filter((id) => last.cells[id] === 'NOT_HERE').length;
  if (failed.length) return { text: `${failed.join(', ')} failed: ${passed} of ${total} gates passed`, tone: 'bad' };
  if (passed === total) return { text: `All ${total} gates passed`, tone: 'ok' };
  if (passed + here === total) return { text: `${passed} of ${total} gates passed, ${here} not run here (they need Docker)`, tone: 'ok' };
  return { text: `${passed} of ${total} gates passed, ${total - passed} skipped or not run`, tone: 'muted' };
}

/** `{text, tone}` of one cell of the matrix; a gate that was not in that attempt is "-". */
export function gateCell(status) {
  if (status === undefined) return { text: '-', tone: 'muted' };
  const [text, tone] = Object.hasOwn(CELLS, status) ? CELLS[status] : [String(status), 'muted'];
  return { text, tone };
}

// --- the log ---------------------------------------------------------------------------------------------------------

const LOOKS = {
  say: ['●', 'said'],
  tool: ['›', 'tool'],
  result: ['·', 'result'],
  step: ['▸', 'step'],
  gate: ['◇', 'gates'],
  deny: ['⊘', 'denied'],
  retry: ['↻', 'retry'],
  review: ['✎', 'review'],
  approval: ['?', 'approval'],
  escalation: ['!', 'escalation'],
  cost: ['¤', 'credits'],
  end: ['■', 'end'],
  error: ['✗', 'error'],
  loop: ['∞', 'loop'],
};

const MARKED = ['step', 'result', 'gate', 'review', 'end']; // kinds whose pass or fail is also shown as ✓ or ✗

/**
 * How a log line is drawn: `{icon, cls, label}`. `cls` is `k-<kind>` (a kind the console does not know is `k-other`)
 * plus `ok` or `bad` when the line is a pass or a fail; a pass or a fail also gets a check mark or a cross, so that
 * colour is not the only sign.
 */
export function lineLook(line) {
  const kind = Object.hasOwn(LOOKS, line?.kind) ? line.kind : 'other';
  const [icon, label] = kind === 'other' ? ['·', String(line?.kind ?? 'line')] : LOOKS[kind];
  const ok = line?.ok === true ? 'ok' : line?.ok === false ? 'bad' : '';
  const marked = ok && MARKED.includes(kind);
  return { icon: marked ? (ok === 'ok' ? '✓' : '✗') : icon, cls: `k-${kind}${ok ? ` ${ok}` : ''}`, label };
}

/**
 * What a log keeps of the lines it is given: a line whose seq is not above the last one is a repeat (the stream
 * resends from the id the browser had), the oldest lines are dropped beyond `max` and counted in `hidden`.
 * `loop` lines have no seq of their own and are always kept.
 */
export class LineBuffer {
  constructor(max = 5000) {
    this.max = max;
    this.reset();
  }

  reset() {
    this.last = -1;
    this.count = 0;
    this.hidden = 0;
  }

  /** `{add, drop}`: whether to show `line`, and how many of the oldest lines to remove first. */
  push(line) {
    if (line.kind !== 'loop' && Number.isInteger(line.seq)) {
      if (line.seq <= this.last) return { add: false, drop: 0 };
      this.last = line.seq;
    }
    this.count += 1;
    const drop = Math.max(0, this.count - this.max);
    this.count -= drop;
    this.hidden += drop;
    return { add: true, drop };
  }
}

/** Whether a scrollable box is at its end (within `slack` pixels), so that new lines should keep it there. */
export function atBottom(box, slack = 40) {
  return box.scrollHeight - box.scrollTop - box.clientHeight <= slack;
}

// --- approvals -------------------------------------------------------------------------------------------------------

const CHOICES = {
  approve: ['Approve', 'primary'],
  reject: ['Reject (stops the run)', 'danger'],
  continue: ['Continue', 'primary'],
  abort: ['Abort run', 'danger'],
};

/**
 * The buttons of an approval card: `{choices: [{value, label, tone}], note}`. Only the options the server says this
 * viewer may answer (`allowed_options`); a unit does not get `continue` at a credit cap and is told who does. `note`
 * also says what a reject does.
 */
export function approvalChoices(pending) {
  const strings = (list) => (Array.isArray(list) ? list.filter((item) => typeof item === 'string') : []);
  const allowed = strings(pending?.allowed_options);
  const withheld = strings(pending?.options).filter((option) => !allowed.includes(option));
  const notes = [];
  const ownCap = Number.isFinite(pending?.extend_credits) || pending?.fake === true; // personal mode: the participant answers the credit cap
  if (allowed.includes('reject')) notes.push('Reject stops the run here. Nothing is committed.');
  if (withheld.includes('continue')) notes.push('Ask the facilitator to extend the run.');
  return {
    choices: allowed.map((value) => {
      const [label, tone] = ownCap && value === 'abort' ? ['Stop', 'danger'] : Object.hasOwn(CHOICES, value) ? CHOICES[value] : [value.charAt(0).toUpperCase() + value.slice(1), ''];
      return { value, label, tone };
    }),
    note: notes.length ? notes.join(' ') : null,
  };
}

/**
 * What a personal-mode credit-cap question says to the participant (the server sets `extend_credits` and `budget` on
 * it, or `fake` for a run on the fake model): `null` for any other question. `cap` is the run's cap.
 */
export function capQuestion(pending, cap) {
  if (pending?.fake === true) return 'This run reached its step limit on the fake model. Continue or Stop.'; // it costs nothing: no credits to name
  if (!Number.isFinite(pending?.extend_credits)) return null;
  const budget = Number.isFinite(pending.budget) ? ` of today's ${formatCredits(pending.budget)}` : '';
  return `This run reached its credit cap (${formatCredits(cap)}). Continue adds up to ${formatCredits(pending.extend_credits)} more credits${budget}.`;
}

/**
 * The answer already given to the question on the record (`given` is `{id, answer}`), or `null`: none was given, the
 * record has no question, or the answer was for an earlier one. While it is not `null` the question is shown without
 * buttons (the harness has not taken the answer yet), so it cannot be answered twice.
 */
export function answerGiven(pending, given) {
  const id = pending?.id;
  return typeof id === 'string' && given?.id === id && typeof given.answer === 'string' ? given.answer : null;
}

// --- credits and time ------------------------------------------------------------------------------------------------

/** A credit amount without trailing zeros: 12, 1.25, 0.5. */
export function formatCredits(n) {
  return Number.isFinite(n) ? String(Math.round(n * 100) / 100) : '0';
}

/**
 * "x of cap" for a workflow run (`''` for a run that spends no credits, "free" for one on the fake model); the live
 * amount comes from `progress`.
 */
export function creditsText(rec, progress) {
  if (!rec || rec.kind !== 'workflow') return '';
  if (rec.fake === true) return 'free'; // what the fake runtime counts is no model's: nothing is charged
  const live = Number.isFinite(progress?.credits) ? progress.credits : rec.credits;
  const text = `${formatCredits(isFinal(rec.status) ? rec.credits : live)} of ${formatCredits(rec.cap)}`;
  return rec.extensions > 0 ? `${text} (extended)` : text;
}

/**
 * What a run costs, for the list: "free" (a gates run, a run on the fake model), "none" (refused), "up to 12 credits"
 * (queued or going: its cap), "4.65 of 12 credits" (over).
 */
export function runCost(rec) {
  if (rec?.kind === 'gates' || rec?.kind === 'warm') return 'free';
  if (rec?.status === 'refused') return 'none';
  if (rec?.fake === true) return 'free';
  if (!isFinal(rec?.status)) return `up to ${formatCredits(rec?.cap)} credits`;
  const [spent, rest] = creditsText(rec, null).split(/ (?=\()/);
  return `${spent} credits${rest ? ` ${rest}` : ''}`;
}

/**
 * The cost line of the New run form: what one run may spend (`cap`, the console's cap per run; `null` when it could not
 * be read) and what the unit has `left` of its `budget`.
 */
export function costLine(left, budget, cap) {
  const have = `${formatCredits(left)} of ${formatCredits(budget)}`;
  if (!(left >= MIN_CREDITS)) return `You have ${have} credits left: not enough for an agent run. Check my code stays free.`;
  if (!Number.isFinite(cap)) return `A run stops at its credit cap. You have ${have} left.`;
  const most = `A run may spend up to ${formatCredits(cap)} credits`;
  return left < cap ? `${most}; with ${formatCredits(left)} left, this one stops at ${formatCredits(left)}. You have ${have} left.` : `${most}. You have ${have} left.`;
}

/** "45 s", "3 min 12 s", "1 h 02 min"; `''` for a time that is not a number of 0 or more. */
export function formatDuration(ms) {
  if (!Number.isFinite(ms) || ms < 0) return '';
  const seconds = Math.floor(ms / 1000);
  if (seconds < 60) return `${seconds} s`;
  if (seconds < 3600) return `${Math.floor(seconds / 60)} min ${seconds % 60} s`;
  return `${Math.floor(seconds / 3600)} h ${String(Math.floor((seconds % 3600) / 60)).padStart(2, '0')} min`;
}

/** How long the run has gone (to its end, or to `now` in milliseconds); `''` when it has not started. */
export function runDuration(rec, now) {
  const start = parseTime(rec?.started_at);
  if (start === null) return '';
  return formatDuration((parseTime(rec.ended_at) ?? now) - start);
}

// --- the queue widget ------------------------------------------------------------------------------------------------

const LANE_NAMES = { agent: 'agent runs', gates: 'gate runs' };
const capital = (text) => text.charAt(0).toUpperCase() + text.slice(1);

/**
 * The top bar's line for `GET /api/runs/queue` and the unit it is for: "Agent runs 2 of 3 · Gate runs 0 of 2 · your
 * run: #2 in queue". `title` says what the numbers are and how many wait; `mine` is the id of the unit's own run, if it
 * has one.
 */
export function queueSummary(snapshot, unitId) {
  const lanes = snapshot?.lanes;
  if (!lanes || typeof lanes !== 'object') return { text: '', title: '', mine: null };
  const parts = [];
  const waiting = [];
  for (const [lane, state] of Object.entries(lanes)) {
    const [going, limit, queued] = [state?.going, state?.limit, state?.queued].map((n) => (Number.isFinite(n) ? n : 0));
    const name = Object.hasOwn(LANE_NAMES, lane) ? LANE_NAMES[lane] : `${lane} runs`;
    parts.push(`${capital(name)} ${going} of ${limit}`);
    waiting.push(`${name} ${queued}`);
  }
  const run = unitId ? (snapshot.runs ?? []).find((r) => r.unit === unitId) : undefined;
  if (run) {
    if (run.status === 'queued' && Number.isInteger(run.position)) parts.push(`your run: #${run.position} in queue`);
    else if (run.status === 'waiting') parts.push('your run: waiting for you');
    else parts.push(run.status === 'queued' ? 'your run: starting' : 'your run: running');
  }
  const title = parts.length ? `Runs that go at the same time; the rest wait in the queue.\nIn the queue now: ${waiting.join(', ')}.` : '';
  return { text: parts.join(' · '), title, mine: run ? run.id : null };
}

/** What the unit's open run is doing, after "Your run X is": "waiting for your approval", "number 2 in the queue". */
export function openRunState(rec) {
  if (rec?.status === 'waiting') return rec.pending?.kind === 'escalation' ? 'waiting for your decision' : 'waiting for your approval';
  if (rec?.status === 'queued') return Number.isInteger(rec.position) ? `number ${rec.position} in the queue` : 'starting';
  return 'running';
}
