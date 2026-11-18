// What the views make of the weakened-guardrail flags (`structure.weakening`: `{rule, detail, file}`): the list the
// server sends is checked, and each flag is placed on the Harness tab or card it is about. Pure: no DOM, no imports.

/** The question every flag asks. */
export const WEAK_TITLE = 'weakened — on purpose?';

const MAX_FLAGS = 50;

// The tab of the Harness view each rule is about. A rule not listed here (a newer server) is shown with the others at
// the top of the view, on no card.
const PLACES = {
  'gate-removed': 'gates',
  'gate-timeout': 'gates',
  'gate-env': 'gates',
  'gate-fn': 'gates',
  'protected-removed': 'policy',
  'shell-deny-removed': 'policy',
  'forbidden-calls-removed': 'policy',
  'role-shell': 'roles',
  'role-writes': 'roles',
  'retry-budget': 'loop',
  'review-rounds': 'loop',
  'ladder-rounds': 'loop',
  'hook-removed': 'hooks',
  'hook-timeout': 'hooks',
  'merge-approval-auto': 'workflows',
  'approval-removed': 'workflows',
  'step-gates': 'workflows',
  'step-rounds': 'workflows',
  'step-review-rounds': 'workflows',
  'step-on-fail': 'workflows',
  'workflow-new': 'workflows',
  'reviewer-off': 'workflows',
};

/** The flags of a server answer as plain `{rule, detail, file}` of text, at most 50; anything that is not a flag is dropped. */
export function cleanFlags(list) {
  if (!Array.isArray(list)) return [];
  return list
    .filter((flag) => flag && typeof flag === 'object' && ['rule', 'detail', 'file'].every((key) => typeof flag[key] === 'string'))
    .slice(0, MAX_FLAGS)
    .map((flag) => ({ rule: flag.rule.slice(0, 60), detail: flag.detail.slice(0, 300), file: flag.file.slice(0, 200) }));
}

/** The tab a flag is about (`roles`, `gates`, ...), or `null` for a rule this page does not know. */
export const placeOf = (flag) => (Object.hasOwn(PLACES, flag.rule) ? PLACES[flag.rule] : null);

/**
 * The flags about one tab. A card narrows it: `file` (a workflow card: the flags about that workflow file) or `role` (a
 * role card: a role flag says "<role>: ..." first).
 */
export function flagsAt(flags, place, { file, role } = {}) {
  return flags.filter((flag) => placeOf(flag) === place && (file === undefined || flag.file === file) && (role === undefined || flag.detail.startsWith(`${role}: `)));
}

/** How many flags each tab has: `{roles: 2, loop: 1}`. */
export function countsByPlace(flags) {
  const counts = {};
  for (const flag of flags) {
    const place = placeOf(flag);
    if (place) counts[place] = (counts[place] ?? 0) + 1;
  }
  return counts;
}

/** The line at the top of the Harness view and the run page: "2 guardrails weakened — on purpose?"; `''` for none. */
export function weakSummary(flags) {
  return flags.length ? `${flags.length} guardrail${flags.length === 1 ? '' : 's'} weakened — on purpose?` : '';
}
