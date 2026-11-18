// What the Evals view works out before it draws anything: a card and the form that edits it, the leaderboard rows and
// the progress sentence. Pure: no DOM, no imports (a test runs it with node).

export const ROLES = ['planner', 'implementer', 'test_writer', 'reviewer'];

/**
 * The settings of a card that are a single choice, in the order the form shows them. Roles and models are one choice
 * per role (`useRole`, `useModel`), and the cap is no choice: it is always sent.
 */
export const SETTINGS = ['retry_budget', 'custom_instructions', 'prompt_template', 'autonomy', 'gates'];

const TICKET = /^[A-Z][A-Z0-9]*-[0-9]+$/;

/**
 * The form for a stored card (or none). `use[setting]`, `useRole[role]` and `useModel[role]` say whether the card sets
 * that one; every value starts from the card, else from `defaults` (the reference variant cheap), so a person who ticks a
 * setting sees a sensible value. The implementer role is never a choice: it is always on.
 */
export function formFromCard(card, defaults) {
  const given = card ?? {};
  const base = defaults ?? {};
  const group = (source, name) => (source[name] && typeof source[name] === 'object' ? source[name] : {});
  const pick = (key) => (Object.hasOwn(given, key) ? given[key] : base[key]);
  const value = (name, role, fallback) => {
    for (const source of [group(given, name), group(base, name)]) if (Object.hasOwn(source, role)) return source[role];
    return fallback;
  };
  return {
    use: Object.fromEntries(SETTINGS.map((key) => [key, Object.hasOwn(given, key)])),
    useRole: Object.fromEntries(ROLES.map((role) => [role, role !== 'implementer' && Object.hasOwn(group(given, 'roles'), role)])),
    useModel: Object.fromEntries(ROLES.map((role) => [role, Object.hasOwn(group(given, 'models'), role)])),
    roles: Object.fromEntries(ROLES.map((role) => [role, role === 'implementer' ? true : Boolean(value('roles', role, false))])),
    models: Object.fromEntries(ROLES.map((role) => [role, value('models', role, '')])),
    retry_budget: pick('retry_budget') ?? 3,
    custom_instructions: pick('custom_instructions') ?? true,
    prompt_template: pick('prompt_template') ?? 'v1',
    autonomy: pick('autonomy') ?? 'human',
    gates: pick('gates') ?? true,
    max_ai_credits: pick('max_ai_credits') ?? 12,
  };
}

/** The card a form stands for: only the ticked roles, models and settings, and the cap. The implementer role is never sent. */
export function cardFromForm(form) {
  const card = {};
  const roles = ROLES.filter((role) => role !== 'implementer' && form.useRole[role]);
  const models = ROLES.filter((role) => form.useModel[role]);
  if (roles.length > 0) card.roles = Object.fromEntries(roles.map((role) => [role, Boolean(form.roles[role])]));
  if (models.length > 0) card.models = Object.fromEntries(models.map((role) => [role, form.models[role]]));
  for (const key of SETTINGS) if (form.use[key]) card[key] = form[key];
  card.max_ai_credits = form.max_ai_credits;
  return card;
}

/** One line for the admin: which settings a card names. */
export function summarizeCard(card) {
  const parts = [];
  if (card.roles) parts.push(`roles ${Object.entries(card.roles).map(([role, on]) => `${on ? '+' : '-'}${role}`).join(' ')}`);
  if (card.models) parts.push(`models ${Object.entries(card.models).map(([role, model]) => `${role}=${model}`).join(' ')}`);
  for (const [key, label] of [['retry_budget', 'retries'], ['custom_instructions', 'instructions'], ['prompt_template', 'template'], ['autonomy', 'autonomy'], ['gates', 'gates']]) {
    if (Object.hasOwn(card, key)) parts.push(`${label} ${card[key]}`);
  }
  parts.push(`cap ${card.max_ai_credits ?? 'run cap'}`);
  return parts.join(', ');
}

/** The variant a unit's card is run as: `unit-01` is `U01`. */
export const variantOf = (unitId) => `U${String(unitId).replace(/^unit-/, '')}`;

// --- the card as exactly one change (handbook block 4a) -----------------------------------------------------------------
//
// The Evals page offers a card as one change ("exactly one change, otherwise the leaderboard cannot tell what helped"):
// a kind of change, its value and the cap. `role` switches one role on or off, `model` sets one role's model.

/** The kinds of change a card can make, in the order the form offers them. */
export const CHANGES = ['prompt_template', 'retry_budget', 'role', 'autonomy', 'custom_instructions', 'gates', 'model'];

/** The roles a card may switch on or off: the implementer always runs. */
export const SWITCHABLE = ROLES.filter((role) => role !== 'implementer');

const named = (card, group) => (card?.[group] && typeof card[group] === 'object' ? Object.keys(card[group]) : []);

/** The changes a card makes, in the form's order: `[{kind}]`, `[{kind: 'role', role}]`, ... (the cap is no change). */
export function changesOf(card) {
  const found = [];
  for (const kind of CHANGES) {
    if (kind === 'role') found.push(...named(card, 'roles').filter((role) => SWITCHABLE.includes(role)).map((role) => ({ kind, role })));
    else if (kind === 'model') found.push(...named(card, 'models').map((role) => ({ kind, role })));
    else if (card && Object.hasOwn(card, kind)) found.push({ kind });
  }
  return found;
}

/**
 * The one-change form for a stored card (or none). `change` is the card's first change (null when it makes none) and
 * `extra` the number of changes after it. A value the card does not set starts turned away from cheap (`defaults`), so
 * that picking a kind is already a change: the unit's own template `u<NN>`, a role or a setting the other way round,
 * the first of `models` that cheap does not give the role.
 */
export function oneChange(card, defaults, unitId, models) {
  const form = formFromCard(card, defaults);
  const v11 = formFromCard(null, defaults);
  const found = changesOf(card);
  const roleOf = (kind) => found.find((change) => change.kind === kind)?.role;
  const role = roleOf('role') ?? SWITCHABLE[0];
  const modelRole = roleOf('model') ?? ROLES[0];
  const turned = (key) => (form.use[key] ? form[key] : !v11[key]);
  return {
    change: found[0]?.kind ?? null,
    extra: Math.max(0, found.length - 1),
    prompt_template: form.use.prompt_template || !unitId ? form.prompt_template : `u${String(unitId).replace(/^unit-/, '')}`,
    retry_budget: form.retry_budget,
    role,
    roleOn: form.useRole[role] ? form.roles[role] : !v11.roles[role],
    autonomy: form.use.autonomy ? form.autonomy : v11.autonomy === 'auto' ? 'human' : 'auto',
    custom_instructions: turned('custom_instructions'),
    gates: turned('gates'),
    modelRole,
    model: form.useModel[modelRole] ? form.models[modelRole] : otherModel(v11, modelRole, models),
    cap: form.max_ai_credits,
  };
}

/** The first of `models` that cheap (`v11`, a form of the defaults) does not give `role`; cheap's own when there is none. */
export function otherModel(v11, role, models) {
  return (models ?? []).find((model) => model !== v11.models[role]) ?? v11.models[role] ?? '';
}

/** The card a one-change form sends: its one change, if one is chosen, and the cap. */
export function cardFromChange(form) {
  const card = {};
  if (form.change === 'role') card.roles = { [form.role]: Boolean(form.roleOn) };
  else if (form.change === 'model') card.models = { [form.modelRole]: form.model };
  else if (CHANGES.includes(form.change)) card[form.change] = form[form.change];
  card.max_ai_credits = form.cap;
  return card;
}

// --- the hypothesis: what the unit expects, and the leaderboard column that decides it ------------------------------------
//
// The card's API has no place for it: the page keeps it in the browser, per unit, for block 4b. `storage` is
// localStorage (a test passes its own); a browser that refuses storage reads an empty hypothesis and writes nothing.

/** The leaderboard columns that can decide a hypothesis: `[key of a boardRows row, label]`. */
export const DECIDERS = [
  ['passRate', 'Pass rate'],
  ['costPerPass', 'Cost per pass'],
  ['commitRate', 'Commit rate'],
  ['creditsMean', 'Credits per run'],
  ['retries', 'Retries'],
];
const MAX_HYPOTHESIS = 600;
const hypothesisKey = (unitId) => `harness-console.hypothesis.${unitId}`;

/** `{text, column}` of a unit's hypothesis ('' for none). */
export function readHypothesis(unitId, storage) {
  try {
    const kept = JSON.parse(storage?.getItem(hypothesisKey(unitId)) ?? 'null');
    return {
      text: typeof kept?.text === 'string' ? kept.text.slice(0, MAX_HYPOTHESIS) : '',
      column: DECIDERS.some(([key]) => key === kept?.column) ? kept.column : '',
    };
  } catch {
    return { text: '', column: '' };
  }
}

/** Keep a unit's hypothesis; false when the browser would not. */
export function writeHypothesis(unitId, { text, column }, storage) {
  try {
    storage.setItem(hypothesisKey(unitId), JSON.stringify({ text: String(text ?? '').slice(0, MAX_HYPOTHESIS), column: column ?? '' }));
    return true;
  } catch {
    return false;
  }
}

/** A leaderboard cell as the table shows it; `key` is one of the DECIDERS. */
export function cellText(row, key) {
  switch (key) {
    case 'passRate':
      return `${percent(row.passRate)} (${row.passes}/${row.runs})`;
    case 'costPerPass':
      return row.costPerPass == null ? 'no pass yet' : `${credits(row.costPerPass)} credits`;
    case 'commitRate':
      return percent(row.commitRate);
    case 'creditsMean':
      return row.creditsMean == null ? '-' : `${credits(row.creditsMean)} credits`;
    case 'retries':
      return credits(row.retries);
    default:
      return '';
  }
}

/** "Cost per pass: U02 1.5 credits, cheap no pass yet." for the unit's row and the reference, or null when one is missing. */
export function compareText(rows, column) {
  const label = DECIDERS.find(([key]) => key === column)?.[1];
  const mine = rows.find((row) => row.mine);
  const reference = rows.find((row) => row.reference);
  if (!label || !mine || !reference) return null;
  return `${label}: ${mine.id} ${cellText(mine, column)}, ${reference.id} ${cellText(reference, column)}.`;
}

/** What the leaderboard says of a row whose unit changed its harness files after the batch ran it (`stale` in the results). */
export const STALE_TEXT = 'harness changed since';
export const STALE_HINT = 'This unit changed its harness files after these runs, so the row measures an older harness. The next batch measures the new one.';

/**
 * The leaderboard as table rows, in the order the harness ranks them (pass rate, then cost). `costPerPass` is null until
 * a run passes; `stale` is true when the unit changed its harness after the batch (the console says so, only ever as true).
 */
export function boardRows(board, unitId, reference = 'cheap') {
  const mine = unitId ? variantOf(unitId) : null;
  return (board?.variants ?? []).map((row, index) => ({
    rank: index + 1,
    id: row.variant,
    label: row.label || row.variant,
    mine: row.variant === mine,
    reference: row.variant === reference,
    stale: row.stale === true,
    runs: row.runs ?? 0,
    passes: row.passes ?? 0,
    passRate: row.pass_rate ?? 0,
    commitRate: row.commit_rate ?? 0,
    costPerPass: row.credits_per_pass ?? null,
    creditsMean: row.credits_mean ?? null,
    retries: row.retries_mean ?? null,
  }));
}

export const percent = (share) => (share == null ? '-' : `${Math.round(share * 100)}%`);

/** Credits with at most two decimals ("-" for none). */
export const credits = (value) => (value == null ? '-' : String(Math.round(value * 100) / 100));

/** `{state, share, text}` for the batch status the server answers: state is none, running, done or stopped. */
export function progress(status) {
  if (!status?.experiment) return { state: 'none', share: 0, text: 'No batch has been launched yet.' };
  const { experiment, done, total } = status;
  const share = total > 0 ? Math.min(1, done / total) : 0;
  const runs = `${done} of ${total} runs`;
  const cost = credits(status.credits);
  if (status.running) return { state: 'running', share, text: `Running ${experiment}: ${runs} finished, ${cost} credits so far.` };
  if (done >= total) return { state: 'done', share: 1, text: `${experiment} finished: ${runs}, ${cost} credits.` };
  if (status.stopped) return { state: 'stopped', share, text: `${experiment} was stopped by the facilitator after ${runs}, ${cost} credits.` };
  const code = status.exit ? ` (exit code ${status.exit})` : '';
  return { state: 'stopped', share, text: `${experiment} ended after ${runs}, ${cost} credits${code}. The facilitator can read evals/${experiment}.log in the console data folder.` };
}

/** Ticket keys from what a person typed ("ts-101, TS-102 ts-103"): upper case, once each, in order. */
export function parseTickets(text) {
  const keys = String(text ?? '').split(/[\s,;]+/).map((key) => key.toUpperCase()).filter(Boolean);
  return [...new Set(keys)];
}

/** The command the launch button stands for, as far as it can be written down (the console writes the variants file). */
export function launchCommand({ tickets, parallel, experiment }) {
  const keys = tickets.length > 0 && tickets.every((key) => TICKET.test(key)) ? tickets.join(',') : '<tickets>';
  const name = /^[A-Za-z0-9][A-Za-z0-9_-]*$/.test(experiment ?? '') ? experiment : '<name>';
  const workers = Number.isInteger(parallel) && parallel > 0 ? parallel : 1;
  return `python -m harness eval --variants <data>/evals/${name}.variants.yaml --tickets ${keys} --parallel ${workers} --experiment ${name}`;
}

const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

/**
 * What a batch should stay under, before it is launched: `plan` is what the server says a launch would run (variants, the
 * largest repeats and cap of any of them, and `overshoot`), `tickets` the keys typed so far. A run is held to its cap,
 * but the harness ends it up to one model call above, so each run counts as its cap plus the overshoot. Text, with no
 * number until there is one.
 */
export function estimate(plan, tickets) {
  if (!plan) return 'The cost estimate is not available right now.';
  if (plan.units.length === 0) return 'The cost estimate appears once a unit has submitted a card.';
  if (tickets.length === 0) return 'Enter tickets to see the cost estimate.';
  const perRun = plan.cap + (plan.overshoot ?? 0);
  const total = plan.variants * tickets.length * plan.repeats * perRun;
  return `${plural(plan.variants, 'variant')} × ${plural(tickets.length, 'ticket')} × ${plan.repeats} × ≤ ${credits(perRun)} credits = at most ${credits(total)} credits`;
}

// --- personal mode: compare two runs -------------------------------------------------------------------------------------------

const GATE_RESULT = /\b(?:[a-z][a-z0-9]*(?:-[a-z0-9]+)*|G\d+|diff-policy)[:\s]+(PASS|FAIL|SKIP|NOT_RUN|ERROR)\b/g;
const SHA = /^[0-9a-f]{7,}$/;
const pace = (ms) => {
  const s = Math.floor(ms / 1000);
  return s < 60 ? `${s} s` : s < 3600 ? `${Math.floor(s / 60)} min ${s % 60} s` : `${Math.floor(s / 3600)} h ${String(Math.floor((s % 3600) / 60)).padStart(2, '0')} min`;
};
const ms = (ts) => {
  const t = typeof ts === 'string' ? Date.parse(ts.replace(/(\.\d{3})\d+/, '$1')) : NaN; // the server writes microseconds
  return Number.isNaN(t) ? null : t;
};
const NO = '-';

/** Workflow runs that ran to an end (not gates runs, not refused ones, not queued or going), as the compare selects offer them. */
export const finishedRuns = (runs) =>
  (Array.isArray(runs) ? runs : []).filter((r) => r?.kind === 'workflow' && ['done', 'failed', 'escalated', 'stopped', 'error'].includes(r.status));

// the record and progress of a run, whether given as GET /api/runs/<id> ({record, progress}) or as the record alone
const partsOf = (run) => ({ rec: run?.record ?? run ?? {}, prog: run?.progress ?? run?.record?.progress ?? {} });

const gatesOf = (prog) => {
  const last = (Array.isArray(prog?.gates) ? prog.gates : []).filter((g) => typeof g?.summary === 'string').at(-1);
  if (!last) return null;
  const found = [...last.summary.matchAll(GATE_RESULT)].map((m) => m[1]);
  return found.length ? { passed: found.filter((s) => s === 'PASS').length, total: found.length } : null;
};

const pick = (x, y, lowerIsBetter) => {
  if (x === null || y === null || x === y) return null;
  return (x < y) === lowerIsBetter ? 'a' : 'b';
};

/**
 * The table of "Compare two runs": rows `{label, a, b, better: 'a'|'b'|null}` for Result, Gates (passed of total),
 * Credits and Time (lower is better), Retries (only when both runs have it) and Harness (the short commit of the unit
 * branch when each run started). A run is `{record, progress}` as GET /api/runs/<id> has it (or the record alone).
 */
export function compareRuns(a, b) {
  const [x, y] = [partsOf(a), partsOf(b)];
  const rows = [];
  const status = (p) => String(p.rec.status ?? NO);
  const done = (p) => (status(p) === 'done' ? 1 : 0);
  rows.push({ label: 'Result', a: status(x), b: status(y), better: pick(done(x), done(y), false) });
  const [gx, gy] = [gatesOf(x.prog), gatesOf(y.prog)];
  const show = (g) => (g ? `${g.passed} of ${g.total}` : NO);
  rows.push({ label: 'Gates', a: show(gx), b: show(gy), better: gx && gy ? pick(gx.passed / gx.total, gy.passed / gy.total, false) : null });
  const spent = (p) => (p.rec.fake === true ? 0 : Number.isFinite(p.rec.credits) ? p.rec.credits : null);
  const cash = (p) => (spent(p) === null ? NO : `${Math.round(spent(p) * 100) / 100}${p.rec.fake === true ? ' (fake model)' : ''}`);
  rows.push({ label: 'Credits', a: cash(x), b: cash(y), better: pick(spent(x), spent(y), true) });
  const took = (p) => {
    const [from, to] = [ms(p.rec.started_at), ms(p.rec.ended_at)];
    return from === null || to === null || to < from ? null : to - from;
  };
  const time = (p) => (took(p) === null ? NO : pace(took(p)));
  rows.push({ label: 'Time', a: time(x), b: time(y), better: pick(took(x), took(y), true) });
  const [rx, ry] = [x.prog.retries, y.prog.retries];
  if (Number.isInteger(rx) && Number.isInteger(ry)) rows.push({ label: 'Retries', a: String(rx), b: String(ry), better: pick(rx, ry, true) });
  const sha = (p) => (typeof p.rec.unit_head === 'string' && SHA.test(p.rec.unit_head) ? p.rec.unit_head.slice(0, 7) : NO);
  rows.push({ label: 'Harness', a: sha(x), b: sha(y), better: null });
  return rows;
}
