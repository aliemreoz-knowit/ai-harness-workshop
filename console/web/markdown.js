// Markdown as a page, for the tickets and the repo's .md files: the part of Markdown they use (headings, paragraphs
// with hard breaks, lists with continuation lines, fenced code, quotes, rules, pipe tables, a front matter block,
// <details> with its <summary>, `code`, **bold**, *italic*, links). parseMarkdown is pure (the tests run it in node);
// renderMarkdown builds DOM nodes with textContent only, never innerHTML, because a ticket is untrusted text. Other raw
// HTML stays text. A link opens only when it is http(s); any other target (an anchor, a repo path) is shown as text,
// since "#..." is this console's own route. An HTML comment is shown, in its own box: a page that renders the ticket
// hides it, an agent reads it, and that is where TS-666 keeps its orders.

const FENCE = /^(\s*)(`{3,}|~{3,})\s*([\w+#.-]*)\s*$/;
const HEADING = /^\s{0,3}(#{1,6})\s+(.*?)(?:\s+#+)?\s*$/;
const RULE = /^\s{0,3}([-*_])(?:\s*\1){2,}\s*$/;
const QUOTE = /^\s{0,3}>\s?/;
const ITEM = /^(\s*)([-*+]|\d{1,9}[.)])(\s+)(.*)$/;
const COMMENT = /^\s{0,3}<!--/;
const DETAILS = /^\s{0,3}<details(\s[^>]*)?>\s*$/i;
const DELIMITER = /^\s*\|?\s*:?-+:?\s*(\|\s*:?-+:?\s*)*\|?\s*$/;

const indentOf = (line) => line.length - line.trimStart().length;
const blank = (line) => line.trim() === '';

function startsBlock(line) {
  return FENCE.test(line) || HEADING.test(line) || RULE.test(line) || QUOTE.test(line) || COMMENT.test(line)
    || DETAILS.test(line) || (ITEM.test(line) && indentOf(line) <= 3);
}

const tableAt = (lines, i) => i + 1 < lines.length && lines[i].includes('|') && DELIMITER.test(lines[i + 1])
  && lines[i + 1].includes('-') && cells(lines[i]).length === cells(lines[i + 1]).length;

/** The cells of one table row: split on "|" outside `code` and not escaped, the outer pipes dropped. */
function cells(line) {
  let row = line.trim();
  if (row.startsWith('|')) row = row.slice(1);
  if (row.endsWith('|') && !row.endsWith('\\|')) row = row.slice(0, -1);
  const out = [];
  let cell = '';
  let ticks = 0; // the length of the backtick run that opened the `code` we are in, 0 outside
  for (let i = 0; i < row.length; i += 1) {
    const c = row[i];
    if (c === '\\' && row[i + 1] === '|') {
      cell += '|';
      i += 1;
    } else if (c === '`') {
      let n = 1;
      while (row[i + n] === '`') n += 1;
      if (!ticks) ticks = n;
      else if (ticks === n) ticks = 0;
      cell += '`'.repeat(n);
      i += n - 1;
    } else if (c === '|' && !ticks) {
      out.push(cell.trim());
      cell = '';
    } else {
      cell += c;
    }
  }
  out.push(cell.trim());
  return out;
}

/**
 * The blocks of `text`: {type: heading|para|list|code|comment|quote|rule|table|details, ...}; inline parts as
 * parseInline gives. A front matter block (between "---" lines at the very top) is a code block with `front`.
 */
export function parseMarkdown(text) {
  const lines = String(text ?? '').replace(/\r\n?/g, '\n').split('\n');
  if (lines[0]?.trim() === '---') {
    const end = lines.findIndex((line, k) => k > 0 && line.trim() === '---');
    if (end > 0) {
      return [{ type: 'code', lang: 'yaml', front: true, text: lines.slice(1, end).join('\n') }, ...blocks(lines.slice(end + 1))];
    }
  }
  return blocks(lines);
}

function blocks(lines) {
  const out = [];
  let i = 0;
  while (i < lines.length) {
    const line = lines[i];
    if (blank(line)) {
      i += 1;
      continue;
    }
    if (COMMENT.test(line)) {
      const parts = [];
      let rest = line.slice(line.indexOf('<!--') + 4);
      let after = '';
      for (;;) {
        const end = rest.indexOf('-->');
        if (end >= 0) {
          parts.push(rest.slice(0, end));
          after = rest.slice(end + 3);
          i += 1;
          break;
        }
        parts.push(rest);
        i += 1;
        if (i >= lines.length) break;
        rest = lines[i];
      }
      out.push({ type: 'comment', text: parts.join('\n').replace(/^\s*\n/, '').trimEnd() });
      if (after.trim()) lines.splice(i, 0, after); // text after "-->" on the same line is ordinary text
      continue;
    }
    if (DETAILS.test(line)) {
      const inner = [];
      let depth = 1;
      i += 1;
      while (i < lines.length) {
        if (DETAILS.test(lines[i])) depth += 1;
        if (/^\s*<\/details>\s*$/i.test(lines[i])) {
          depth -= 1;
          if (depth === 0) {
            i += 1;
            break;
          }
        }
        inner.push(lines[i]);
        i += 1;
      }
      let summary = 'Details';
      const at = inner.findIndex((one) => !blank(one));
      const head = at >= 0 ? /^\s*<summary>(.*?)<\/summary>\s*$/i.exec(inner[at]) : null;
      if (head) {
        summary = head[1].replace(/<[^>]*>/g, '').trim() || summary;
        inner.splice(at, 1);
      }
      out.push({ type: 'details', summary: parseInline(summary), blocks: blocks(inner) });
      continue;
    }
    if (tableAt(lines, i)) {
      const align = cells(lines[i + 1]).map((cell) => {
        const left = cell.startsWith(':');
        const right = cell.endsWith(':');
        if (left && right) return 'center';
        if (right) return 'right';
        return left ? 'left' : '';
      });
      const head = cells(line).map(parseInline);
      const rows = [];
      i += 2;
      while (i < lines.length && !blank(lines[i]) && lines[i].includes('|')) {
        const row = cells(lines[i]);
        rows.push(align.map((_, k) => parseInline(row[k] ?? '')));
        i += 1;
      }
      out.push({ type: 'table', align, head, rows });
      continue;
    }
    const fence = FENCE.exec(line);
    if (fence) {
      const [, indent, mark, lang] = fence;
      const body = [];
      i += 1;
      while (i < lines.length) {
        const close = /^\s*(`{3,}|~{3,})\s*$/.exec(lines[i]);
        if (close && close[1][0] === mark[0] && close[1].length >= mark.length) {
          i += 1;
          break;
        }
        const cut = Math.min(indent.length, indentOf(lines[i]));
        body.push(lines[i].slice(cut));
        i += 1;
      }
      out.push({ type: 'code', lang, text: body.join('\n') });
      continue;
    }
    const heading = HEADING.exec(line);
    if (heading) {
      out.push({ type: 'heading', level: heading[1].length, inline: parseInline(heading[2]) });
      i += 1;
      continue;
    }
    if (RULE.test(line)) {
      out.push({ type: 'rule' });
      i += 1;
      continue;
    }
    if (QUOTE.test(line)) {
      const inner = [];
      while (i < lines.length && QUOTE.test(lines[i])) inner.push(lines[i++].replace(QUOTE, ''));
      out.push({ type: 'quote', blocks: blocks(inner) });
      continue;
    }
    if (ITEM.test(line) && indentOf(line) <= 3) {
      i = list(lines, i, out);
      continue;
    }
    const para = [line];
    i += 1;
    while (i < lines.length && !blank(lines[i]) && !startsBlock(lines[i]) && !tableAt(lines, i)) para.push(lines[i++]);
    out.push({ type: 'para', inline: paragraph(para) });
  }
  return out;
}

/** One list from lines[i]; pushes it on `out` and returns the index after it. */
function list(lines, i, out) {
  const first = ITEM.exec(lines[i]);
  const base = first[1].length;
  const ordered = /\d/.test(first[2]);
  const node = { type: 'list', ordered, start: ordered ? parseInt(first[2], 10) : 1, items: [] };
  while (i < lines.length) {
    const item = ITEM.exec(lines[i]);
    if (!item || item[1].length !== base || /\d/.test(item[2]) !== ordered) break;
    const content = base + item[2].length + Math.min(item[3].length, 4);
    const keep = Math.min(content, base + 2); // a continuation line needs at least this indent
    const body = [item[4]];
    i += 1;
    let lastBlank = false;
    while (i < lines.length) {
      const line = lines[i];
      if (blank(line)) {
        body.push('');
        lastBlank = true;
        i += 1;
        continue;
      }
      const indent = indentOf(line);
      if (indent >= keep) body.push(line.slice(Math.min(indent, content)));
      else if (!lastBlank && !startsBlock(line)) body.push(line.trimStart()); // a lazy continuation of the text
      else break;
      lastBlank = false;
      i += 1;
    }
    while (body.length && body[body.length - 1] === '') body.pop();
    node.items.push({ blocks: blocks(body) });
  }
  out.push(node);
  return i;
}

const BREAK = '\u0000'; // a hard line break while a paragraph is parsed as one text

/**
 * The lines of one paragraph, parsed as one text (so **bold** may run over a line end): joined by a space, or by a
 * line break where a line ends in two spaces or "\".
 */
function paragraph(lines) {
  const text = lines.map((line, k) => {
    const hard = / {2,}$/.test(line) || /\\$/.test(line);
    let one = line.trim();
    if (/\\$/.test(one)) one = one.slice(0, -1);
    return k < lines.length - 1 ? one + (hard ? BREAK : ' ') : one;
  }).join('');
  return breaks(parseInline(text));
}

/** Text parts with a BREAK in them as text, {t: 'br'}, text; code keeps a space there. */
function breaks(parts) {
  const out = [];
  for (const part of parts) {
    if (part.t === 'text' && part.v.includes(BREAK)) {
      part.v.split(BREAK).forEach((piece, k) => {
        if (k) out.push({ t: 'br' });
        if (piece) out.push({ t: 'text', v: piece });
      });
    } else if (part.t === 'code') {
      out.push({ ...part, v: part.v.split(BREAK).join(' ') });
    } else if (part.c) {
      out.push({ ...part, c: breaks(part.c) });
    } else {
      out.push(part);
    }
  }
  return out;
}

/** Inline parts of one line: {t: text|code, v}, {t: strong|em, c: [...]}, {t: link, href, c: [...]}. */
export function parseInline(s) {
  const out = [];
  let text = '';
  const flush = () => {
    if (text) out.push({ t: 'text', v: text });
    text = '';
  };
  let i = 0;
  while (i < s.length) {
    const c = s[i];
    if (c === '\\' && i + 1 < s.length && /[\\`*_{}[\]()#+\-.!>|~<]/.test(s[i + 1])) {
      text += s[i + 1];
      i += 2;
      continue;
    }
    if (c === '`') {
      let n = 1;
      while (s[i + n] === '`') n += 1;
      const ticks = '`'.repeat(n);
      const end = s.indexOf(ticks, i + n);
      if (end > 0) {
        flush();
        let v = s.slice(i + n, end);
        if (v.length > 2 && v.startsWith(' ') && v.endsWith(' ')) v = v.slice(1, -1);
        out.push({ t: 'code', v });
        i = end + n;
        continue;
      }
      text += ticks;
      i += n;
      continue;
    }
    if (c === '[') {
      const link = /^\[([^\]]+)\]\(([^()\s]+)(?:\s+"[^"]*")?\)/.exec(s.slice(i));
      if (link) {
        flush();
        out.push({ t: 'link', href: link[2], c: parseInline(link[1]) });
        i += link[0].length;
        continue;
      }
    }
    if (c === '*' && s[i + 1] === '*') {
      const end = s.indexOf('**', i + 2);
      if (end > i + 2 && s[i + 2] !== ' ' && s[end - 1] !== ' ') {
        flush();
        out.push({ t: 'strong', c: parseInline(s.slice(i + 2, end)) });
        i = end + 2;
        continue;
      }
    }
    if (c === '*' && s[i + 1] && s[i + 1] !== '*' && s[i + 1] !== ' ') {
      const end = s.indexOf('*', i + 1);
      if (end > i + 1 && s[end - 1] !== ' ' && s[end + 1] !== '*') {
        flush();
        out.push({ t: 'em', c: parseInline(s.slice(i + 1, end)) });
        i = end + 1;
        continue;
      }
    }
    text += c;
    i += 1;
  }
  flush();
  return out;
}

// --- DOM -------------------------------------------------------------------------------------------------------------

function el(tag, className, ...children) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  node.append(...children);
  return node;
}

function inline(parts) {
  return parts.map((part) => {
    if (part.t === 'text') return document.createTextNode(part.v);
    if (part.t === 'br') return document.createElement('br');
    if (part.t === 'code') return el('code', '', part.v);
    if (part.t === 'link') {
      if (!/^https?:\/\//i.test(part.href)) {
        const text = el('span', 'md-link', ...inline(part.c));
        text.title = part.href;
        return text;
      }
      const link = el('a', '', ...inline(part.c));
      Object.assign(link, { href: part.href, target: '_blank', rel: 'noopener noreferrer' });
      return link;
    }
    return el(part.t === 'strong' ? 'strong' : 'em', '', ...inline(part.c));
  });
}

function render(block) {
  switch (block.type) {
    case 'heading':
      return el(`h${Math.min(block.level + 1, 6)}`, `md-h${block.level}`, ...inline(block.inline));
    case 'para':
      return el('p', '', ...inline(block.inline));
    case 'list': {
      const node = el(block.ordered ? 'ol' : 'ul', '', ...block.items.map((item) => el('li', '', ...item.blocks.map(render))));
      if (block.ordered && block.start !== 1) node.start = block.start;
      return node;
    }
    case 'code':
      return el('pre', block.front ? 'md-code md-front' : 'md-code', el('code', '', block.text));
    case 'table': {
      const cell = (tag, parts, k) => {
        const node = el(tag, '', ...inline(parts));
        if (block.align[k]) node.style.textAlign = block.align[k];
        return node;
      };
      return el('div', 'md-table',
        el('table', '',
          el('thead', '', el('tr', '', ...block.head.map((parts, k) => cell('th', parts, k)))),
          el('tbody', '', ...block.rows.map((row) => el('tr', '', ...row.map((parts, k) => cell('td', parts, k)))))));
    }
    case 'details':
      return el('details', 'md-details', el('summary', '', ...inline(block.summary)), ...block.blocks.map(render));
    case 'comment':
      return el('div', 'md-comment',
        el('p', 'md-comment-label', el('strong', '', 'HTML comment'), ' · a rendered page hides it; an agent reads it'),
        el('pre', '', block.text));
    case 'quote':
      return el('blockquote', '', ...block.blocks.map(render));
    default:
      return document.createElement('hr');
  }
}

/** `text` as a `div.md` of DOM nodes (textContent only). */
export function renderMarkdown(text) {
  return el('div', 'md', ...parseMarkdown(text).map(render));
}
