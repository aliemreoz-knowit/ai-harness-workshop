// Where a key stands in a YAML or JSON file, for the "Edit" links of the Harness view. Pure: no DOM, no imports.

const indentOf = (line) => line.length - line.trimStart().length;
const isBlank = (line) => line.trim() === '' || line.trimStart().startsWith('#');
const escapeRegExp = (text) => text.replace(/[.*+?^$|()[\]{}\\]/g, '\\$&');

// A line that starts the mapping key: `key:`, `"key":` or `- key:` (a comment never does).
const keyPattern = (key) => new RegExp(`^\\s*(?:-\\s+)?["']?${escapeRegExp(String(key))}["']?\\s*:`, 'i');

/**
 * The 1-based line of mapping key `key` in `text`, 0 when there is none. With `within` (the key of a section) only the
 * lines indented under that key are searched; a key that is not under it gives the line of the section itself, and a
 * section that is not there gives 0.
 */
export function lineOf(text, key, within) {
  const lines = String(text).split(/\r?\n/);
  const wanted = keyPattern(key);
  if (within === undefined) return lines.findIndex((line) => wanted.test(line)) + 1;

  const section = lines.findIndex((line) => keyPattern(within).test(line));
  if (section < 0) return 0;
  const depth = indentOf(lines[section]);
  for (let at = section + 1; at < lines.length; at += 1) {
    if (isBlank(lines[at])) continue;
    if (indentOf(lines[at]) <= depth) break; // the section is over
    if (wanted.test(lines[at])) return at + 1;
  }
  return section + 1;
}
