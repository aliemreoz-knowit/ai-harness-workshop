// Command lines for the "CLI equivalent" drawer. Pure: no DOM, no imports.

const SAFE = /^[A-Za-z0-9._/-]+$/;

/** `word` as one bash word: as it is when it has only safe characters, else in single quotes (`'` becomes `'\''`). */
export function quote(word) {
  const text = String(word);
  return SAFE.test(text) ? text : `'${text.replaceAll("'", "'\\''")}'`;
}

/** `code <path>`: open a unit file in VS Code. A name that starts with `-` gets `./` so that it is not an option. */
export function codeCommand(path) {
  return `code ${quote(path.startsWith('-') ? `./${path}` : path)}`;
}

/**
 * What a participant types for the same run, as the server words it (`Runner.cli_equivalent`): `gates` is the fast
 * ladder; a workflow run is `run --workflow W --ticket T`, with `--mode autonomous` when it is not interactive and
 * `--fake` on the fake model (personal mode). `''` while a workflow or a ticket is missing, and for a handover run
 * (`overlay_from`: another unit's harness), which has no single command.
 */
export function runCommand({ kind, workflow, ticket, mode, overlay_from: overlayFrom, fake = false }) {
  if (kind === 'gates') return 'python -m harness gates --profile fast';
  if (!workflow || !ticket || overlayFrom) return '';
  const words = ['python', '-m', 'harness', 'run', '--workflow', workflow, '--ticket', ticket];
  if (mode && mode !== 'interactive') words.push('--mode', mode);
  if (fake === true) words.push('--fake');
  return words.map(quote).join(' ');
}

/**
 * The drawer's text in personal mode: `cd "<folder>"` on a line before `command`, as the console saves in its own copy
 * of the checkout (`.console-data/units/unit-01/repo`), not in the checkout. In double quotes (PowerShell, cmd and bash
 * read them alike), in single quotes when the folder has a `"`, `$` or backtick. `command` as it is without a folder,
 * `''` without a command.
 */
export function inFolder(command, folder) {
  if (!command || typeof folder !== 'string' || !folder) return command || '';
  const where = /["$`]/.test(folder) ? `'${folder.replaceAll("'", "'\\''")}'` : `"${folder}"`;
  return `cd ${where}\n${command}`;
}
