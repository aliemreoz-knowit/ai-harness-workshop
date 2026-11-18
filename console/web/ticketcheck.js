// What the harness will read from a ticket's "Acceptance tests" section. Pure: no DOM, no imports.
//
// The same rule as the harness (harness/baseline.py, required_tests): the sections are the parts after a `## ` heading;
// a section called "Acceptance tests" or "Required tests" (any case, more than one is fine) holds the `test_*` names a
// change must make pass, and HTML comments do not count.

const SECTIONS = ['acceptance tests', 'required tests'];
const TEST_NAME = /\btest_[A-Za-z0-9_]+\b/g;

/** The `test_*` names under "Acceptance tests" (or "Required tests") in `markdown`, once each, in order. */
export function acceptanceTests(markdown) {
  const names = [];
  let counts = false; // the section being read is one of SECTIONS
  for (const line of String(markdown).split(/\r?\n/)) {
    if (line.startsWith('## ')) {
      counts = SECTIONS.includes(line.slice(3).trim().toLowerCase());
    } else if (counts) {
      names.push(line);
    }
  }
  const text = names.join('\n').replace(/<!--[\s\S]*?-->/g, '');
  return [...new Set(text.match(TEST_NAME) ?? [])];
}

/** Shown when a ticket names no acceptance test: the bridge's note, in the words a person at the editor needs. */
export const NO_ACCEPTANCE_TESTS =
  'No acceptance tests: the harness accepts any progress. Name the tests that must pass, one `test_*` name per line, ' +
  'under "## Acceptance tests". You can still run the ticket.';

/** The sections of an agent-ready ticket, in the handbook's order (block 1, "Rewrite your ticket"). */
export const AGENT_READY_SECTIONS = [
  'Context',
  'Steps to reproduce',
  'Inputs',
  'Expected',
  'Actual',
  'Acceptance criteria',
  'Acceptance tests',
  'Out of scope',
  'Definition of done',
];

// Other headings the harness reads as the same section (see SECTIONS above).
const SAME_AS = { 'required tests': 'acceptance tests' };

/**
 * The checklist of AGENT_READY_SECTIONS for `markdown`: `{title, present, empty}` each, in order. A section is present
 * when a `## ` heading of its name (any case) is there; it is empty while nothing but blank lines and HTML comments
 * stand under it (up to the next `## ` heading).
 */
export function sectionChecklist(markdown) {
  const texts = new Map(); // section name (lower case) -> the lines under its headings
  let current = null;
  for (const line of String(markdown).split(/\r?\n/)) {
    if (line.startsWith('## ')) {
      const name = line.slice(3).trim().toLowerCase();
      current = SAME_AS[name] ?? name;
      if (!texts.has(current)) texts.set(current, []);
    } else if (current) {
      texts.get(current).push(line);
    }
  }
  return AGENT_READY_SECTIONS.map((title) => {
    const lines = texts.get(title.toLowerCase());
    const present = lines !== undefined;
    const empty = present && lines.join('\n').replace(/<!--[\s\S]*?-->/g, '').trim() === '';
    return { title, present, empty };
  });
}
