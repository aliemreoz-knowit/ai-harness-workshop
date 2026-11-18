// The hash router's address format. Pure: no DOM, no imports.
//
//   #/runs/42                        {name: 'runs', args: ['42'], params: {}}
//   #/code?path=harness/harness.yaml&line=12
//                                    {name: 'code', args: [], params: {path: 'harness/harness.yaml', line: '12'}}
//
// The query is cut off before the path is split, so a "/" in a value is not a separator. `params` holds the query's
// values as text (the last of a repeated name wins).

/**
 * `{name, args, params}` of a location hash, `fallback` for an empty name; `null` when a %-escape in the path is
 * broken.
 */
export function parseHash(hash, fallback) {
  const text = String(hash).replace(/^#\/?/, '');
  const cut = text.indexOf('?');
  const path = cut < 0 ? text : text.slice(0, cut);
  const query = cut < 0 ? '' : text.slice(cut + 1);
  const [name = '', ...args] = path.split('/');
  try {
    return {
      name: name || fallback,
      args: args.filter(Boolean).map(decodeURIComponent),
      params: Object.fromEntries(new URLSearchParams(query)),
    };
  } catch {
    return null;
  }
}

/** The hash that opens `path` in the Code view, at `line` (1-based) when there is one. */
export function codeLink(path, line) {
  const link = `#/code?path=${encodeURIComponent(path).replaceAll('%2F', '/')}`;
  return line ? `${link}&line=${line}` : link;
}

// --- the nav -----------------------------------------------------------------------------------------------------------------

/** The nav's groups. `title` is the group's heading (none for Today and Blueprint); `icon` names the icon of guideui.js when it is not the view's name; `adminOnly` views are the facilitator's; `shared` views are not there in personal mode. */
export const NAV_GROUPS = [
  {
    title: '',
    views: [
      { name: 'today', label: 'Today' },
      { name: 'blueprint', label: 'Blueprint', icon: 'doc' }, // the Harness Blueprint, filled in section by section
    ],
  },
  {
    title: 'Your harness',
    views: [
      { name: 'harness', label: 'Harness' },
      { name: 'code', label: 'Code' },
      { name: 'tickets', label: 'Tickets' },
      { name: 'runs', label: 'Runs' },
    ],
  },
  {
    title: 'Team',
    views: [
      { name: 'evals', label: 'Evals' },
      { name: 'guides', label: 'Guides' },
      { name: 'ship', label: 'Ship', shared: true },
      { name: 'board', label: 'Board', shared: true },
    ],
  },
  { title: '', admin: true, views: [{ name: 'admin', label: 'Admin', adminOnly: true, shared: true }] },
];

/** The nav's groups for this person: no admin views unless `admin`, and no shared views (Board, Ship, Admin) in personal mode; empty groups are left out. */
export function navGroups(personal, admin) {
  return NAV_GROUPS.map((group) => ({ ...group, views: group.views.filter((v) => (admin || !v.adminOnly) && !(personal && v.shared)) })).filter((group) => group.views.length);
}
