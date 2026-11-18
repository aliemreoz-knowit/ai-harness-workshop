// What the Admin view works out before it draws anything: a row per unit (its lock, its run, whether a reset may be
// asked), the list of runs, the doctor's tally and the sentences the page says about warming, archiving and a batch that
// runs. Pure: no DOM, no imports (a test runs it with node).
//
// A unit is what GET /api/admin/units answers: {id, name, level, budget, spent, left, locked_by, lock}, `lock` being
// free, run or needs-reset. A run is what GET /api/runs lists for the admin (newest first).

const OPEN = new Set(['queued', 'running', 'waiting']);
const plural = (count, word) => `${count} ${word}${count === 1 ? '' : 's'}`;

/** Credits with at most two decimals. */
export const credits = (value) => String(Math.round(value * 100) / 100);

/** Whether a run is queued or going (it can be stopped). */
export const isOpen = (run) => OPEN.has(run.status);

/** What a unit's lock means, in words. */
function stateText(unit) {
  if (unit.lock === 'run') return `held by run ${unit.locked_by}`;
  if (unit.lock === 'needs-reset') return `needs a reset (run ${String(unit.locked_by).replace(/^needs-reset:/, '')})`;
  return 'free';
}

/**
 * One row per unit, in the order given. `run` is the newest queued or going run of the unit (`runs` is newest first), or
 * null. A reset may be asked unless a run holds the unit: a unit that waits for a reset is what a reset is for. Such a
 * unit may also be restored (`canRestore`): put back on its own branch with every save, no backup.
 */
export function unitRows(units, runs) {
  const newest = new Map();
  for (const run of runs ?? []) if (isOpen(run) && !newest.has(run.unit)) newest.set(run.unit, run);
  return units.map((unit) => ({
    id: unit.id,
    name: unit.name,
    level: unit.level,
    budget: unit.budget,
    spent: unit.spent,
    left: unit.left,
    state: unit.lock,
    stateText: stateText(unit),
    canReset: unit.lock !== 'run',
    canRestore: unit.lock === 'needs-reset',
    run: newest.get(unit.id) ?? null,
    credits: `${credits(unit.spent)} / ${credits(unit.budget)} (${credits(unit.left)} left)`,
  }));
}

/** One line over the units (`unitRows`): how many there are, how many have a run queued or going, how many wait for a
 * reset, and the credits the room has used of its budgets ("" when there are no units). */
export function roomText(rows) {
  if (!rows?.length) return '';
  const running = rows.filter((row) => row.run).length;
  const waiting = rows.filter((row) => row.state === 'needs-reset').length;
  const sum = (key) => rows.reduce((total, row) => total + (Number(row[key]) || 0), 0);
  return [
    plural(rows.length, 'unit'),
    running ? `${running} with a run` : 'none with a run',
    waiting ? `${waiting} ${waiting === 1 ? 'waits' : 'wait'} for a reset` : null,
    `${credits(sum('spent'))} of ${credits(sum('budget'))} credits used`,
  ]
    .filter(Boolean)
    .join(' · ');
}

/** The first `limit` runs (newest first) with whether each can be stopped, and how many are left out. */
export function runRows(runs, limit) {
  const list = runs ?? [];
  return { rows: list.slice(0, limit).map((run) => ({ ...run, stoppable: isOpen(run) })), more: Math.max(0, list.length - limit) };
}

/** How many of the doctor's rows are OK, WARN and FAIL, and the sentence for them. */
export function doctorTally(rows) {
  const count = (status) => (rows ?? []).filter((row) => row.status === status).length;
  const [ok, warn, fail] = [count('OK'), count('WARN'), count('FAIL')];
  const warnings = warn > 0 ? `, ${plural(warn, 'warning')}` : '';
  return { ok, warn, fail, text: fail > 0 ? `${plural(fail, 'problem')} to fix${warnings}` : `All checks passed${warnings}` };
}

/** What a warm request did: the units it queued a run for, and those it left out with the reason. */
export function warmText(answer) {
  const queued = answer.runs.length;
  const base = queued > 0 ? `Warming ${plural(queued, 'unit')}.` : 'No unit to warm.';
  if (answer.skipped.length === 0) return base;
  return `${base} Left out: ${answer.skipped.map((one) => `${one.unit} (${one.reason})`).join(', ')}.`;
}

/** What archiving the eval results did. */
export const archiveText = (answer) => `Archived ${plural(answer.files, 'file')} as ${answer.archived}.`;

/** Why the results cannot be archived now (the batch `status` of GET /api/eval/status), or null when they can. */
export function archiveBlocked(status) {
  return status?.running ? 'A batch is running: stop it or wait for it first.' : null;
}
