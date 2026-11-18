// Loads the Monaco editor from web/vendor/monaco (put there by scripts/vendor_monaco.py) with its AMD loader.
// `loadMonaco()` resolves to the `monaco` namespace, or to null when the editor is not installed or fails to start:
// the views then fall back to plain text boxes, so the console still works.

const BASE = '/web/vendor/monaco/vs';
const LANGUAGES = { c: 'c', h: 'c', yaml: 'yaml', yml: 'yaml', md: 'markdown', json: 'json', sh: 'shell', py: 'python', ps1: 'powershell' };

export const MONO_FONT = '"IBM Plex Mono", ui-monospace, "Cascadia Mono", "SF Mono", Menlo, Consolas, "DejaVu Sans Mono", monospace';

/** Monaco language id for a file path, by extension; anything unknown is plain text. */
export function languageFor(path) {
  const name = String(path).split('/').pop();
  const dot = name.lastIndexOf('.');
  const extension = dot > 0 ? name.slice(dot + 1).toLowerCase() : '';
  return Object.hasOwn(LANGUAGES, extension) ? LANGUAGES[extension] : 'plaintext';
}

let loading = null;

export function loadMonaco() {
  loading ??= new Promise((resolve) => {
    const style = document.createElement('link');
    style.rel = 'stylesheet';
    style.href = `${BASE}/editor/editor.main.css`;
    style.dataset.name = 'vs/editor/editor.main'; // the loader's css plugin skips a sheet it finds with this name
    document.head.append(style);

    const script = document.createElement('script');
    script.src = `${BASE}/loader.js`;
    script.onerror = () => resolve(null);
    script.onload = () => {
      window.require.config({ paths: { vs: BASE } });
      window.require(
        ['vs/editor/editor.main'],
        // the editor measures its font once: the self-hosted mono face must be there first, or the cursor drifts
        () => monoReady().then(() => resolve(setUpThemes(window.monaco))),
        () => resolve(null),
      );
    };
    document.head.append(script);
  });
  return loading;
}

/** Resolves once the mono face is loaded, or has failed to load (then the editor measures the fallback). */
function monoReady() {
  const fonts = document.fonts;
  if (!fonts?.load) return Promise.resolve();
  return Promise.all(['400 13px "IBM Plex Mono"', '500 13px "IBM Plex Mono"'].map((face) => fonts.load(face))).catch(() => {});
}

// One theme, navy like the page (docs/console/ui-redesign.md: dark is the only theme), its syntax colours from the Knowit
// palette or tints of it mixed with navy: periwinkle keywords, cream text, peach strings, lavender numbers and keys.
// Every token Monaco's own dark theme colours (vs-dark: blue, teal, green, red...) is given one of these instead.
const RULES = [
  { token: '', foreground: 'fefbe6' }, // cream
  { token: 'comment', foreground: '8a8ab3' }, // lavender 65% on navy (5.8:1)
  { token: 'keyword', foreground: '9795ff' }, // periwinkle
  { token: 'keyword.flow', foreground: '9795ff' },
  { token: 'keyword.json', foreground: '9795ff' },
  { token: 'constant', foreground: '9795ff' },
  { token: 'tag', foreground: '9795ff' },
  { token: 'metatag', foreground: '9795ff' },
  { token: 'type', foreground: 'cfceff' }, // lavender: YAML keys, C types
  { token: 'key', foreground: 'cfceff' },
  { token: 'string.key', foreground: 'cfceff' },
  { token: 'string.key.json', foreground: 'cfceff' },
  { token: 'attribute.name', foreground: 'cfceff' },
  { token: 'string', foreground: 'ffd6b8' }, // peach
  { token: 'string.yaml', foreground: 'ffd6b8' },
  { token: 'string.value.json', foreground: 'ffd6b8' },
  { token: 'attribute.value', foreground: 'ffd6b8' },
  { token: 'regexp', foreground: 'ffd6b8' },
  { token: 'number', foreground: 'cfceff' }, // lavender
  { token: 'number.hex', foreground: 'cfceff' },
  { token: 'variable', foreground: 'fefbe6' },
  { token: 'variable.predefined', foreground: 'cfceff' },
  { token: 'variable.parameter', foreground: 'fefbe6' },
  { token: 'delimiter', foreground: 'aba9a5' }, // cream 66% on navy
  { token: 'annotation', foreground: 'fcb27c' }, // orange
  { token: 'invalid', foreground: 'ff8a7a' }, // the console's fail colour
];

// The editor's own colours: the page's navy, the line it is on a lifted navy, the selection and cursor periwinkle, an
// added or removed line in a diff a tint of the pass green or the fail coral.
const COLOURS = {
  'editor.background': '#0b0b26',
  'editor.foreground': '#fefbe6',
  'editorLineNumber.foreground': '#5f5ea8',
  'editorLineNumber.activeForeground': '#cfceff',
  'editorCursor.foreground': '#9795ff',
  'editor.lineHighlightBackground': '#13133c',
  'editor.lineHighlightBorder': '#13133c',
  'editor.selectionBackground': '#9795ff40',
  'editor.inactiveSelectionBackground': '#9795ff26',
  'editorIndentGuide.background': '#2a2956',
  'editorWhitespace.foreground': '#2a2956',
  'editorGutter.background': '#0b0b26',
  'editorWidget.background': '#19194b',
  'editorWidget.border': '#2a2956',
  'editorSuggestWidget.background': '#19194b',
  'editorHoverWidget.background': '#19194b',
  'diffEditor.insertedTextBackground': '#55d34026',
  'diffEditor.removedTextBackground': '#ff8a7a2e',
  'diffEditor.insertedLineBackground': '#55d34014',
  'diffEditor.removedLineBackground': '#ff8a7a17',
  'scrollbarSlider.background': '#5f5ea866',
  'scrollbarSlider.hoverBackground': '#5f5ea899',
  'focusBorder': '#9795ff',
};

function setUpThemes(monaco) {
  monaco.editor.defineTheme('console', { base: 'vs-dark', inherit: true, rules: RULES, colors: COLOURS });
  monaco.editor.setTheme('console');
  return monaco;
}

const DIFF_OPTIONS = {
  automaticLayout: true,
  readOnly: true,
  originalEditable: false,
  renderSideBySide: true,
  minimap: { enabled: false },
  fontFamily: MONO_FONT,
  fontSize: 13,
  scrollBeyondLastLine: false,
};

/** A read-only side-by-side Monaco diff in `host` for two texts (`monaco` is what `loadMonaco` gave); the function that tears it down. */
export function diffView(monaco, host, original, modified, language) {
  const originalModel = monaco.editor.createModel(original, language);
  const modifiedModel = monaco.editor.createModel(modified, language);
  const editor = monaco.editor.createDiffEditor(host, DIFF_OPTIONS);
  editor.setModel({ original: originalModel, modified: modifiedModel });
  return () => {
    editor.dispose();
    originalModel.dispose();
    modifiedModel.dispose();
  };
}
