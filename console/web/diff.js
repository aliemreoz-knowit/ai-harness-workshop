// Reads the text of `git diff` into one entry per file, with both sides rebuilt for a side-by-side view.
// Pure: no DOM, no imports. Used by the Code view; the Runs view can show a run's diff the same way.

/**
 * @param {string} text  unified diff as `git diff --no-color` prints it
 * @returns {{path:string, from:string, status:'modified'|'added'|'deleted'|'renamed'|'binary',
 *            added:number, removed:number, original:string, modified:string}[]}
 *
 * `original` and `modified` hold the lines of the hunks only (not the whole file), each hunk led by its
 * `@@ -a,b +c,d @@` line on both sides so that the view shows where it sits (not for added or deleted files).
 */
export function parseDiff(text) {
  const files = [];
  let file = null;
  let inHunk = false;
  let original = [];
  let modified = [];
  const finish = () => {
    if (file) files.push({ ...file, original: original.join('\n'), modified: modified.join('\n') });
    original = [];
    modified = [];
  };

  for (const line of String(text).split('\n')) {
    if (line.startsWith('diff --git ')) {
      finish();
      const path = pathFromHeader(line);
      file = { path, from: path, status: 'modified', added: 0, removed: 0 };
      inHunk = false;
    } else if (!file) {
      continue;
    } else if (line.startsWith('@@ ')) {
      inHunk = true;
      if (file.status !== 'added' && file.status !== 'deleted') {
        original.push(line);
        modified.push(line);
      } // a new or deleted file is one hunk and one empty side: the header would be its only line there
    } else if (!inHunk) {
      readHeaderLine(file, line);
    } else if (line.startsWith('+')) {
      modified.push(line.slice(1));
      file.added += 1;
    } else if (line.startsWith('-')) {
      original.push(line.slice(1));
      file.removed += 1;
    } else if (line.startsWith(' ')) {
      original.push(line.slice(1));
      modified.push(line.slice(1));
    } // "\ No newline at end of file" and the empty piece after the last newline say nothing about content
  }
  finish();
  return files;
}

// `diff --git a/P b/P`: P is half of what is left after the two prefixes (a path with spaces cannot be split otherwise).
function pathFromHeader(line) {
  const rest = line.slice('diff --git '.length);
  const half = (rest.length - 5) / 2;
  if (Number.isInteger(half) && half > 0 && rest.slice(2, 2 + half) === rest.slice(half + 5)) return rest.slice(2, 2 + half);
  const at = rest.lastIndexOf(' b/');
  return at >= 0 ? rest.slice(at + 3) : rest;
}

function readHeaderLine(file, line) {
  if (line.startsWith('new file mode')) file.status = 'added';
  else if (line.startsWith('deleted file mode')) file.status = 'deleted';
  else if (line.startsWith('Binary files ') || line.startsWith('GIT binary patch')) file.status = 'binary';
  else if (line.startsWith('rename from ')) {
    file.from = line.slice('rename from '.length);
    file.status = 'renamed';
  } else if (line.startsWith('rename to ')) file.path = line.slice('rename to '.length);
  else if (line.startsWith('--- a/')) file.from = file.path = stripTab(line.slice('--- a/'.length));
  else if (line.startsWith('+++ b/')) file.path = stripTab(line.slice('+++ b/'.length));
}

// git ends the name of a file whose path has spaces with a tab on the ---/+++ lines
const stripTab = (name) => name.replace(/\t.*$/, '');
