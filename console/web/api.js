// The browser's side of the console API: JSON over fetch, and the live event stream.
// Every call is same-origin, so the session cookie goes along and the server's Origin check passes.
//
// A browser has six connections to one server for all of its tabs, and an event stream holds one for as long as it is
// open. So a tab has one global stream (the app shell's) and at most one run stream, both closed once the tab has been
// hidden for HIDDEN_CLOSE_MS; and a call that waits longer than TIMEOUT_MS (all six taken, say) gives up and says so.

/** How long a call may wait for its answer (`get` and `post` take a longer `timeoutMs` for a call that may be slow). */
export const TIMEOUT_MS = 30000;
/** What a call that got no answer in time says. */
export const NO_ANSWER = 'The console did not answer. Close other console tabs and try again.';
/** How long a stream may be away before the page says "Reconnecting…" (`onDown`). */
export const DOWN_MS = 3000;
/**
 * How long a tab is hidden before its streams close. Not at once: the server frees a stream's slot (8 per address) only
 * when a write to it fails, 15-30 s after the browser closed it, so closing at every tab switch would use them all up.
 */
export const HIDDEN_CLOSE_MS = 30000;

/**
 * A failed call: `status` is the HTTP status (0 when the server could not be reached), `body` the JSON answer.
 * `timeout` is true for a call that got no answer in time: the server may still have done what it asked.
 */
export class ApiError extends Error {
  constructor(status, body, timeout = false) {
    super((body && body.error) || (status ? `HTTP ${status}` : 'cannot reach the console'));
    this.name = 'ApiError';
    this.status = status;
    this.body = body;
    this.timeout = timeout;
  }
}

let whenLoggedOut = null;
let holds = 0; // calls under way that end every session (a new unit code); a 401 meanwhile is judged after them
let heldBack = false; // a 401 came while one of them was under way
let judging = null; // the question to /api/me about a 401 that is under way

/**
 * `fn` runs when the session is gone: a call was answered with 401 and `/api/me` says nobody is logged in (a 401 can be a
 * race: the call was sent with a cookie that a new session key ended a moment before the new cookie arrived).
 * The app shows the login page.
 */
export function onUnauthorized(fn) {
  whenLoggedOut = fn;
}

/**
 * Run `work`, a call that ends every session and answers with a new cookie for this browser (a new unit code), and
 * return what it returns. A 401 that other calls get meanwhile (the 4 s poll, a refresh after a run event) is not judged
 * until `work` is over and its answer has set the cookie: then `/api/me` tells whether the session is gone.
 */
export async function holdLogout(work) {
  holds += 1;
  try {
    return await work();
  } finally {
    holds -= 1;
    if (holds === 0 && heldBack) {
      heldBack = false;
      judgeSession();
    }
  }
}

// A 401: the session is gone only if /api/me says so. Many 401s of one moment make one question. Resolves true when
// the session is gone (`onUnauthorized` has been told).
function judgeSession() {
  if (!whenLoggedOut) return Promise.resolve(false);
  judging ??= (async () => {
    try {
      if ((await request('GET', '/api/me'))?.subject) return false;
      whenLoggedOut();
      return true;
    } catch {
      return false; // the console cannot be reached: the next call says so
    } finally {
      judging = null;
    }
  })();
  return judging;
}

// A call that changes something and got no answer at all: the server may have refused it before reading its body (no
// session) and hung up, which the browser reports as a lost connection, not as the 401. So /api/me is asked first.
function lostSession(method, path) {
  if (method === 'GET' || path === '/api/login') return Promise.resolve(false);
  if (holds > 0) {
    heldBack = true;
    return Promise.resolve(false);
  }
  return judgeSession();
}

async function request(method, path, body, timeoutMs = TIMEOUT_MS) {
  const abort = new AbortController();
  const init = { method, credentials: 'same-origin', headers: { Accept: 'application/json' }, signal: abort.signal };
  if (body !== undefined) {
    init.headers['Content-Type'] = 'application/json';
    init.body = JSON.stringify(body);
  }
  const timer = setTimeout(() => abort.abort(), timeoutMs);
  let response;
  let text;
  try {
    response = await fetch(path, init);
    text = await response.text();
  } catch {
    if (abort.signal.aborted) throw new ApiError(0, { error: NO_ANSWER }, true);
    if (await lostSession(method, path)) throw new ApiError(401, { error: 'login required' });
    throw new ApiError(0, { error: 'cannot reach the console' });
  } finally {
    clearTimeout(timer);
  }
  let data = null;
  if (text) {
    try {
      data = JSON.parse(text);
    } catch {
      data = { error: text.slice(0, 200) };
    }
  }
  if (!response.ok) {
    // a wrong code at the login form is also a 401, but there is no session to lose there
    if (response.status === 401 && path !== '/api/login' && path !== '/api/me') {
      if (holds > 0) heldBack = true;
      else judgeSession();
    }
    throw new ApiError(response.status, data);
  }
  return data;
}

function withQuery(path, params) {
  return params ? `${path}?${new URLSearchParams(params)}` : path;
}

/** GET `path` (with `params` as the query string); the parsed JSON, or an `ApiError`. `timeoutMs` as for `post`. */
export const get = (path, params, { timeoutMs = TIMEOUT_MS } = {}) => request('GET', withQuery(path, params), undefined, timeoutMs);

/**
 * POST `body` as JSON to `path`; the parsed JSON, or an `ApiError`. `timeoutMs` is for the few calls that may take
 * longer than `TIMEOUT_MS` on purpose (a push to GitHub that waits for the one before it).
 */
export const post = (path, body = {}, { timeoutMs = TIMEOUT_MS } = {}) => request('POST', path, body, timeoutMs);

/**
 * Follow a server-sent event stream. `onEvent({type, data, id})` runs for every event whose name is in `events`
 * (`data` is parsed JSON; `id` is the SSE id, "" if none); `onOpen()` runs each time the connection is made, the
 * first time and after every reconnect. Returns `close()`.
 *
 * The browser reconnects by itself after a lost connection (and sends `Last-Event-ID`). When it gives up (the server
 * answered with an error, for example "too many streams") this asks whether the session is still alive: if it is
 * not, it stops and tells `onUnauthorized` (a stream that answered 401 would be retried forever otherwise); if it
 * is, it opens the stream again after 1 s, doubling up to 15 s. A stream opened again by hand has no
 * `Last-Event-ID` of its own, so it asks to go on from the last id it saw (`?last=`, which the server reads when the
 * header is not there).
 *
 * Once the tab has been hidden for `HIDDEN_CLOSE_MS` the stream is closed (switching tabs back and forth keeps it), and a
 * stream made in a hidden tab waits; it opens when the tab is shown (from the last id it saw, and `onOpen` runs). A
 * stream whose session has ended is closed for good, as by `close()`.
 *
 * `options.public`: a stream that needs no session (the projector's): it always tries again, 1 s doubling up to 15 s,
 * and never stops for good. `options.onDown(down)`: `true` when the stream has not been open for `DOWN_MS` (the page
 * shows "Reconnecting…"), `false` once it is open again.
 */
export function stream(path, onEvent, events = ['message'], onOpen = null, { public: always = false, onDown = null } = {}) {
  let source = null; // the EventSource; null while there is none (hidden, or waiting to try again)
  let timer = null;
  let closed = false;
  let paused = false; // closed because the tab is hidden
  let hideTimer = null; // the tab is hidden: it closes when this goes off
  let delay = 1000;
  let lastId = '';
  let down = false; // onDown(true) was said
  let downTimer = null;
  const doc = typeof document === 'undefined' ? null : document;

  const url = () => (lastId === '' ? path : `${path}${path.includes('?') ? '&' : '?'}last=${encodeURIComponent(lastId)}`);

  const watchDown = () => {
    if (!onDown || down || downTimer !== null) return;
    downTimer = setTimeout(() => {
      downTimer = null;
      down = true;
      onDown(true);
    }, DOWN_MS);
  };
  const stopWatching = () => {
    clearTimeout(downTimer);
    downTimer = null;
  };

  const open = () => {
    source = new EventSource(url());
    watchDown();
    source.onopen = () => {
      delay = 1000;
      stopWatching();
      if (down) {
        down = false;
        onDown(false);
      }
      onOpen?.();
    };
    for (const type of events) {
      source.addEventListener(type, (event) => {
        if (event.lastEventId) lastId = event.lastEventId; // an event without an id leaves the last one as it was
        let data = event.data;
        try {
          data = JSON.parse(event.data);
        } catch {
          /* not JSON: pass the text on */
        }
        onEvent({ type, data, id: event.lastEventId });
      });
    }
    const mine = source;
    source.onerror = () => {
      if (closed || source !== mine) return;
      watchDown();
      if (mine.readyState !== EventSource.CLOSED) return; // still CONNECTING: the browser retries
      mine.close();
      source = null;
      timer = setTimeout(retry, delay);
      delay = Math.min(delay * 2, 15000);
    };
  };

  const close = () => {
    closed = true;
    clearTimeout(timer);
    clearTimeout(hideTimer);
    stopWatching();
    doc?.removeEventListener('visibilitychange', onVisibility);
    source?.close();
    source = null;
  };

  const retry = async () => {
    timer = null;
    if (closed || paused) return;
    if (!always && (await sessionEnded())) {
      close();
      return;
    }
    if (!closed && !paused && source === null) open(); // the page may have left, or the tab come and gone, meanwhile
  };

  // The tab has been hidden for HIDDEN_CLOSE_MS: close, and drop a retry that is waiting.
  const pause = () => {
    hideTimer = null;
    if (closed || paused) return;
    paused = true;
    clearTimeout(timer);
    timer = null;
    stopWatching();
    source?.close();
    source = null;
  };

  function onVisibility() {
    if (closed) return;
    if (doc.hidden) {
      if (!paused && hideTimer === null) hideTimer = setTimeout(pause, HIDDEN_CLOSE_MS);
      return;
    }
    clearTimeout(hideTimer); // shown again in time: the stream stays as it is
    hideTimer = null;
    if (paused) {
      paused = false;
      delay = 1000;
      if (source === null) open();
    }
  }

  doc?.addEventListener('visibilitychange', onVisibility);
  if (doc?.hidden) paused = true; // made in a hidden tab: it waits, and holds no connection meanwhile
  else open();
  return close;
}

/** Whether the session is gone (`onUnauthorized` has been told); false when it is alive or the console cannot be asked. */
async function sessionEnded() {
  try {
    if ((await request('GET', '/api/me'))?.subject) return false;
  } catch (err) {
    return err.status === 401; // `request` has told `onUnauthorized`; any other failure: cannot tell, try again later
  }
  whenLoggedOut?.();
  return true;
}
