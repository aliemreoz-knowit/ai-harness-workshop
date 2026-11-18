// What the Harness view makes of the structure (GET /api/structure) before it draws it. Pure: no DOM, no imports.

/** The profile the gate ladder is: a workflow step and the full check run it. */
export const LADDER = 'ladder';

/**
 * The gates with their profiles, for a table. `columns`: the profile names, the ladder first, then the others in the
 * order of the file. `rows`: every gate once, the ladder's in its order first, then those the other profiles name (in
 * column order), then the gates no profile names (in file order); each `{id, gate, cells}` with `gate` the definition
 * (`null` for an id a profile names that is not defined) and `cells` the 1-based place of the gate in each profile, 0
 * where the profile does not run it.
 */
export function gateLadder({ gates = {}, profiles = {} } = {}) {
  const names = Object.keys(profiles);
  const columns = names.includes(LADDER) ? [LADDER, ...names.filter((name) => name !== LADDER)] : names;
  const ids = [];
  for (const id of [...columns.flatMap((name) => profiles[name]), ...Object.keys(gates)]) {
    if (!ids.includes(id)) ids.push(id);
  }
  const rows = ids.map((id) => ({
    id,
    gate: Object.hasOwn(gates, id) ? gates[id] : null,
    cells: Object.fromEntries(columns.map((name) => [name, profiles[name].indexOf(id) + 1])),
  }));
  return { columns, rows };
}

/** Which workflow steps run each gate profile: `{fast: ['jira-to-pr › implement', ...]}`. */
export function profileUse(workflows = []) {
  const use = {};
  for (const workflow of workflows) {
    for (const step of workflow.steps ?? []) {
      if (!step.gates) continue;
      (use[step.gates] ??= []).push(`${workflow.name} › ${step.id}`);
    }
  }
  return use;
}
