// The run queue of the board, built from the public `run` events of the global stream. Pure: no DOM, no imports.
//
// An event is {id, unit, kind, status, position, credits} (`RunRecord.public` in console/runs.py). `runs` is a Map by
// run id that holds the runs that are not over and the last KEEP_FINISHED that are. The runs queued or going now come
// from the public GET /api/runs/queue (`applyQueue`), which the board reads when it opens and after its stream comes
// back; the finished ones are those the page has heard of.

export const KEEP_FINISHED = 5;

const RUNNING = new Set(['running', 'waiting']);
const isText = (value) => typeof value === 'string' && value !== '';

/** Whether a run has ended (any status but queued, running and waiting). */
export const isOver = (run) => run.status !== 'queued' && !RUNNING.has(run.status);

/** Fold one event into `runs` (what is not a run event is ignored); returns `runs`. */
export function applyRun(runs, event) {
  if (!event || typeof event !== 'object') return runs;
  if (!isText(event.id) || !isText(event.unit) || !isText(event.status)) return runs;
  runs.delete(event.id); // the newest word about a run goes last: the Map keeps the order of insertion
  runs.set(event.id, {
    id: event.id,
    unit: event.unit,
    kind: typeof event.kind === 'string' ? event.kind : '',
    status: event.status,
    position: Number.isInteger(event.position) ? event.position : null,
    credits: Number.isFinite(event.credits) ? event.credits : 0,
  });
  const over = [...runs.values()].filter(isOver);
  for (const run of over.slice(0, Math.max(0, over.length - KEEP_FINISHED))) runs.delete(run.id);
  return runs;
}

/**
 * Fold the answer of GET /api/runs/queue (`{lanes, runs}`: the runs queued or going now) into `runs`; returns `runs`.
 * A run that `runs` has as queued or going and the answer does not list has ended meanwhile (its event was missed):
 * it is dropped, since how it ended is not known. Credits the events said are kept (the queue has none).
 */
export function applyQueue(runs, snapshot) {
  if (!Array.isArray(snapshot?.runs)) return runs;
  const listed = snapshot.runs.filter((run) => run && typeof run === 'object' && isText(run.id));
  const now = new Set(listed.map((run) => run.id));
  for (const run of [...runs.values()]) if (!isOver(run) && !now.has(run.id)) runs.delete(run.id);
  for (const run of listed) applyRun(runs, { ...run, credits: runs.get(run.id)?.credits });
  return runs;
}

const compare = (a, b) => (a < b ? -1 : a > b ? 1 : 0);
const lane = (run) => (run.kind === 'workflow' ? 0 : 1); // workflow runs go in the agent lane, gates and warm runs in the other
const place = (run) => run.position ?? Infinity;

/** `{running, queued, finished}`: going or waiting for an answer, by unit; waiting their turn, by lane and place; over, newest first. */
export function queueView(runs) {
  const all = [...runs.values()];
  return {
    running: all.filter((run) => RUNNING.has(run.status)).sort((a, b) => compare(a.unit, b.unit) || compare(a.id, b.id)),
    queued: all
      .filter((run) => run.status === 'queued')
      .sort((a, b) => lane(a) - lane(b) || place(a) - place(b) || compare(a.id, b.id)),
    finished: all.filter(isOver).reverse(),
  };
}
