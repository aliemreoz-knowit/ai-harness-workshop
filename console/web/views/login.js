// Login: one centred card with the box for the unit code. `ctx = {api, toast, note, onLogin(me)}`; `note` is a line to
// show above the form (why the person is here again).

const ALPHABET = 'ACDEFGHJKMNPQRTUVWXY34679'; // the characters a code is made of (console/auth.py)
const LENGTH = 6;

export function mount(el, ctx) {
  el.innerHTML = `
    <div class="login-page">
      <form class="login" novalidate aria-labelledby="login-title">
        <div class="brand-mark" aria-hidden="true"></div>
        <h1 id="login-title">Harness Console</h1>
        <p class="login-lead">Log in with your unit's code.</p>
        <p class="login-note" hidden></p>
        <label for="code">Unit code</label>
        <input id="code" name="code" type="text" maxlength="${LENGTH}" autocomplete="off" autocapitalize="characters"
               spellcheck="false" autofocus required aria-describedby="code-hint">
        <p class="hint" id="code-hint">It is on the unit card your facilitator gave you: ${LENGTH} letters and digits.</p>
        <p class="form-error" role="alert" hidden></p>
        <button type="submit" class="btn primary">Log in</button>
      </form>
    </div>`;
  const form = el.querySelector('form');
  const input = form.elements.code;
  const error = form.querySelector('.form-error');
  const button = form.querySelector('button');
  const note = form.querySelector('.login-note');
  if (ctx.note) {
    note.textContent = ctx.note;
    note.hidden = false;
  }

  const show = (message) => {
    error.textContent = message;
    error.hidden = !message;
  };

  form.addEventListener('submit', async (event) => {
    event.preventDefault();
    const code = input.value.trim().toUpperCase();
    // checked here as well so that a typo does not use up one of the few attempts the server allows
    if (code.length !== LENGTH || [...code].some((ch) => !ALPHABET.includes(ch))) {
      show(`A code has ${LENGTH} characters: letters and digits. Check it and try again.`);
      return;
    }
    show('');
    button.disabled = true;
    let me;
    try {
      me = await ctx.api.post('/api/login', { code });
    } catch (err) {
      show(
        err.status === 401
          ? 'Wrong code. Check it and try again.'
          : err.status === 429
            ? 'Too many attempts. Wait a minute and try again.'
            : err.message,
      );
      button.disabled = false;
      input.select();
      return;
    }
    ctx.onLogin(me);
  });
}

export function unmount() {}
