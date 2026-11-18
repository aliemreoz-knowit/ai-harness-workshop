"""Ship: a unit's finished run to the facilitator's GitHub repo, and back to its Jira ticket (block 5).

``Ship(cfg, units, runner, secrets, gh=None, jira=None)`` is what the endpoints in ``routes_ship`` work with. A unit
ships its own runs only, and only a ``done`` run that left a branch and a commit (kept or not):

* ``push(unit_id, run_id)``: the run's commit goes to ``https://github.com/<BRIDGE_GITHUB_REPO>.git`` as the branch
  ``units/<NN>/<branch>``: ``{remote_branch, cli}``. Never forced: a branch there with other commits refuses the push.
* ``open_pr(unit_id, run_id)``: a draft PR from that branch against ``main`` (the bridge's ``create_pull``: an open PR
  of the branch gets its body updated instead). The body is the run's summary: workflow, ticket, the last gate ladder,
  the review verdict, the credits, and "Opened from the Harness Console for unit-NN".
* ``post_ladder_check(unit_id, run_id)``: the check run ``harness-ladder`` on the run's commit with its last gate
  ladder (``success``, ``failure``, or ``neutral`` when the ladder did not say). With a token that may not write check
  runs the bridge posts a commit status of that name instead (``fallback``).
* ``report_to_jira(unit_id, run_id, key)``: one comment on ``key`` (a ticket of the project) tagged
  ``console-<unit>`` (the bridge's ``upsert_comment``: the unit's comment is edited in place the next time), with the
  summary, the branch and, when GitHub knows one, the PR.
* ``status(unit_id)``: the unit's PRs (head ``units/<NN>/*`` in the repo itself, no fork) with their check runs and
  commit statuses (``checks``) and their Actions runs (``actions``). Reads only; kept ``STATUS_TTL_S`` seconds per
  unit (a write of the unit's drops it, and a read that a write overtook is not kept), so GitHub is asked once a minute
  however often the page asks.

Every write (push, PR, check run, Jira comment) needs the admin switch "GitHub writes on": off by default, and on only
for the (local) day it was turned on. It is kept in memory and in ``<data>/ship.json`` (``{"github_writes": false}``,
or ``true`` with its ``day``); a file that cannot be read counts as off.

The push never lets git near the token with a config a run could have written. The unit's git config is the clone's
(``<data>/source/.git/config``), which an agent on the host can change (a proxy, ``sslVerify`` off, a
``pushInsteadOf``, a ``remote.<path>.receivepack`` that runs a program); so the push goes in two steps, both run by git
on ``<data>/ship.git``, the console's own bare repo:

1. Without the token: ``ship.git`` fetches the run's commit from the clone as ``refs/ship/<run id>``. The clone's side
   of that is only ``upload-pack``: no ``pushInsteadOf``, ``receivepack``, ``sshCommand`` or ``ext::`` of its config
   comes into it.
2. With the token: ``ship.git`` pushes that commit (by its id, after checking the ref holds it) to GitHub.

``ship.git`` is made on first use without templates; before each step its config is written anew with a fixed text
(``_ship_config``: hooks from the console's empty folder, no gc, no redirect followed; on Windows the system's certificate
store, as Git for Windows has it), and a ``commondir`` (which would hand git another repo's config) is removed. Git reads no system and no
global config there (``GIT_CONFIG_NOSYSTEM``, ``GIT_CONFIG_GLOBAL`` = an empty file of the console's), so nothing of the
user's or the machine's config (an ``insteadOf``, a proxy) applies either. Every git call under the git lock has a
timeout, and a transfer slower than 1 KB/s for 60 s is given up.

The token reaches git in the environment of that second push only (``_ship_env``): as ``GIT_CONFIG_*`` entries that
set an ``AUTHORIZATION`` header for ``https://github.com/`` and no credential helper, on top of ``procs.run_env``. It is
never on a command line, never in a git config file, never logged: git's output is logged with it taken out. The
``refs/ship/<run id>`` ref is removed afterwards. The GitHub API goes through the bridge's ``GitHubClient``
(``make_github``), Jira through ``tickets.make_writer``. Texts that come from a run (the review, the ladder) are cleaned
of host paths (``events.scrub``) before they are cut to length and, in GitHub's markdown, put in code spans, so they
cannot mention anyone or link anywhere.

A failure is a ``ShipError`` with a fixed text and the HTTP status for it: never an exception's text, a URL or a token.
"""

from __future__ import annotations

import base64
import contextlib
import copy
import datetime as dt
import json
import logging
import os
import re
import subprocess
import tempfile
import threading
import time
from pathlib import Path, PurePosixPath
from typing import Any, Callable, Iterator

from console import events, procs, tickets
from console.config import ConsoleConfig
from console.runs import RunRecord, Runner
from console.secrets import Secrets
from console.units import UnitStore

__all__ = [
    "BASE", "CHECK_NAME", "PUSH_FAILED", "STATUS_TTL_S", "WRITES_OFF", "Failed", "Invalid", "NotConfigured",
    "NotShippable", "NotYours", "Ship", "ShipError", "WritesOff", "make_github",
]  # fmt: skip

log = logging.getLogger(__name__)

BASE = "main"  # what a PR goes against
CHECK_NAME = "harness-ladder"
WRITES_OFF = "GitHub writes are off — ask the facilitator"
PUSH_FAILED = "the push failed: the facilitator can see why in the console log"
TIMEOUT_S = 20  # one GitHub API call
PUSH_TIMEOUT_S = 120
STATUS_TTL_S = 60  # how long a unit's PR status is kept
MAX_PULLS = 20  # of a unit's PRs, newest first, in a status
MAX_ITEMS = 50  # check runs, statuses, Actions runs of one PR
_GITHUB = "https://github.com/"
_HEADER_KEY = "http.https://github.com/.extraheader"
_REPO = re.compile(r"[A-Za-z0-9_.-]+/[A-Za-z0-9_.-]+")
_SHA = re.compile(r"[0-9a-f]{40}(?:[0-9a-f]{24})?")
_BRANCH = re.compile(r"[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*")
_BRANCH_MAX = 200
_URL_MAX = 500
_TEXT_MAX = 300
_REPLACE_TRIES, _REPLACE_WAIT_S = 10, 0.05  # Windows refuses os.replace while a reader has the target open


class ShipError(RuntimeError):
    """A ship action that cannot be done; ``str(err)`` is a fixed text for the browser, ``status`` its HTTP status."""

    status = 409


class NotYours(ShipError):
    status = 403


class Invalid(ShipError):
    status = 400


class WritesOff(ShipError):
    status = 409


class NotShippable(ShipError):
    status = 409


class NotConfigured(ShipError):
    status = 503


class Failed(ShipError):
    """GitHub, Jira or git did not do it."""

    status = 502


def make_github(repo: str, token: str, transport: Any = None) -> Any:
    """The bridge's ``GitHubClient`` with the reads a status needs: ``pulls()``, ``check_runs(sha)``,
    ``statuses(sha)`` and ``workflow_runs(sha)``. Without a ``transport`` it talks to GitHub. ``ValueError`` for a repo
    that is not ``owner/name``."""
    from github_client import GitHubClient
    from transport import UrllibTransport

    class ConsoleGitHub(GitHubClient):
        def pulls(self) -> list[dict[str, Any]]:
            return self._pages(f"/repos/{self.repo}/pulls", state="all")

        def check_runs(self, sha: str) -> Any:
            body = self._call("GET", f"/repos/{self.repo}/commits/{sha}/check-runs", per_page=100)
            return body.get("check_runs") if isinstance(body, dict) else None

        def statuses(self, sha: str) -> Any:
            body = self._call("GET", f"/repos/{self.repo}/commits/{sha}/status")
            return body.get("statuses") if isinstance(body, dict) else None

        def workflow_runs(self, sha: str) -> Any:
            body = self._call("GET", f"/repos/{self.repo}/actions/runs", head_sha=sha, per_page=100)
            return body.get("workflow_runs") if isinstance(body, dict) else None

    return ConsoleGitHub(repo, token, UrllibTransport(TIMEOUT_S) if transport is None else transport)


# --- the token, for one git call -------------------------------------------------------------------------------------


def _basic(token: str) -> str:
    return base64.b64encode(f"x-access-token:{token}".encode("utf-8")).decode("ascii")


def _token_config(token: str) -> dict[str, str]:
    """The token as git config from the environment: an ``AUTHORIZATION`` header for ``https://github.com/`` only, and
    no credential helper (none is asked, none stores anything)."""
    return {
        "GIT_CONFIG_COUNT": "2",
        "GIT_CONFIG_KEY_0": _HEADER_KEY,
        "GIT_CONFIG_VALUE_0": f"AUTHORIZATION: basic {_basic(token)}",
        "GIT_CONFIG_KEY_1": "credential.helper",
        "GIT_CONFIG_VALUE_1": "",
    }


def _hide(text: str, token: str) -> str:
    """``text`` without the token, in either of its spellings (for a log line of git's output)."""
    for secret in (token, _basic(token)):
        text = text.replace(secret, "[token]")
    return text


# --- small helpers ---------------------------------------------------------------------------------------------------


def _today() -> str:
    return dt.date.today().isoformat()


def _write_json(path: Path, data: Any) -> None:
    """Write ``data`` to ``path`` by atomic replace (tried again briefly while a reader has it open on Windows)."""
    path.parent.mkdir(parents=True, exist_ok=True)
    fd, tmp = tempfile.mkstemp(dir=path.parent, prefix=f".{path.name}.", suffix=".tmp")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as f:
            json.dump(data, f, indent=2, allow_nan=False)
            f.write("\n")
            f.flush()
            os.fsync(f.fileno())
        for attempt in range(_REPLACE_TRIES):
            try:
                os.replace(tmp, path)
                break
            except PermissionError:
                if attempt == _REPLACE_TRIES - 1:
                    raise
                time.sleep(_REPLACE_WAIT_S)
    except BaseException:
        with contextlib.suppress(OSError):
            os.unlink(tmp)
        raise


def _fresh_file(path: Path, text: str) -> None:
    """``path`` made anew with ``text``: what was there (a link too) is removed first, never written through."""
    path.unlink(missing_ok=True)
    with open(path, "x", encoding="utf-8", newline="\n") as f:
        f.write(text)


def _config_value(text: str) -> str:
    """``text`` as a quoted git config value."""
    return '"' + text.replace("\\", "\\\\").replace('"', '\\"') + '"'


def _ship_config(no_hooks: Path) -> str:
    """The fixed config of ``ship.git``: bare, hooks from ``no_hooks`` (an empty folder of the console's), no gc after a
    fetch or a push, never a redirect followed (the token would go along to wherever it points); on Windows TLS through
    the system's certificate store (``schannel``), as Git for Windows sets it in the system config that a ship.git call
    does not read."""
    text = (
        "[core]\n\trepositoryformatversion = 0\n\tbare = true\n"
        f"\thooksPath = {_config_value(no_hooks.as_posix())}\n"
        "[gc]\n\tauto = 0\n[receive]\n\tautogc = false\n"
        "[http]\n\tlowSpeedLimit = 1000\n\tlowSpeedTime = 60\n\tfollowRedirects = false\n"
    )
    return text + ("\tsslBackend = schannel\n" if os.name == "nt" else "")  # still in [http], the last section


def _ticket_key(ticket: str | None) -> str | None:
    """``PROJ-5`` for ``tickets/PROJ-5.md`` (or ``tickets/PROJ-5/ticket.md``), as the harness names its runs."""
    if not ticket:
        return None
    path = PurePosixPath(ticket)
    if path.stem.lower() == "ticket" and path.parent.name:
        return path.parent.name
    name = path.name
    return name[: -len(".agent.md")] if name.endswith(".agent.md") else path.stem  # personal: tickets/TS-101.agent.md


def _line(value: Any, limit: int = _TEXT_MAX) -> str:
    """Text as one line of at most ``limit`` characters ("" for anything but text)."""
    text = " ".join(value.split()) if isinstance(value, str) else ""
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _word(value: Any) -> str | None:
    """A short status word such as ``completed`` or ``success``; ``None`` for anything else."""
    return _line(value, 40) or None


def _code(value: Any) -> str:
    """Text as a markdown code span: no mention, link, image or HTML of it is rendered."""
    return "`" + (_line(value).replace("`", "'") or "-") + "`"


def _github_url(value: Any) -> str | None:
    """A link to GitHub's own site, else ``None``."""
    if isinstance(value, str) and value.startswith(_GITHUB) and len(value) <= _URL_MAX and value.isprintable():
        return value if " " not in value else None
    return None


def _items(value: Any) -> list[dict]:
    return [item for item in value[:MAX_ITEMS] if isinstance(item, dict)] if isinstance(value, list) else []


def _nn(unit_id: str) -> str:
    return unit_id.rpartition("-")[2]  # unit-01 -> 01


def _remote_branch(rec: RunRecord) -> str:
    """The branch a run is pushed as: ``units/<NN>/<branch>``."""
    return f"units/{_nn(rec.unit)}/{rec.branch}"


def _valid_branch(branch: Any) -> bool:
    return (
        isinstance(branch, str)
        and len(branch) <= _BRANCH_MAX
        and _BRANCH.fullmatch(branch) is not None
        and ".." not in branch
        and not branch.endswith((".", ".lock"))
    )


def _shippable(rec: RunRecord) -> bool:
    return rec.status == "done" and _valid_branch(rec.branch) and isinstance(rec.commit, str) and bool(
        _SHA.fullmatch(rec.commit)
    )


def _failure(service: str, err: BaseException) -> Failed:
    """``Failed`` with a fixed reason: nothing of the exception's text (it may hold a URL or a token)."""
    status = getattr(err, "status", None)  # the bridge's HttpError
    if isinstance(status, int) and not isinstance(status, bool):
        words = f"{service} refused it (HTTP {status})"
    elif isinstance(err, TimeoutError):
        words = f"{service} unreachable: timed out"
    elif isinstance(err, OSError):  # no route, refused, DNS, TLS
        words = f"{service} unreachable: connection failed"
    else:  # an answer the console did not expect: its own code failed on it, so the traceback says where
        log.warning("%s request failed: %s", service, type(err).__name__, exc_info=err)
        return Failed(f"{service} gave an unexpected answer")
    log.warning("%s request failed: %s (%s)", service, type(err).__name__, words)  # never str(err): a URL, a token
    return Failed(words)


# --- ship ------------------------------------------------------------------------------------------------------------


class Ship:
    """Push, PR, ladder check, PR status and Jira report of the units' runs. Safe to share between threads.

    ``gh`` and ``jira`` are the clients (made from ``secrets`` on first use when not given); ``remote`` is where a push
    goes instead of GitHub (tests: a local bare repo); ``today`` gives the local date (``YYYY-MM-DD``) the switch holds
    for; ``clock`` is what the status cache counts its seconds on.
    """

    def __init__(
        self,
        cfg: ConsoleConfig,
        units: UnitStore,
        runner: Runner,
        secrets: Secrets,
        gh: Any = None,
        jira: Any = None,
        *,
        remote: str | None = None,
        today: Callable[[], str] = _today,
        clock: Callable[[], float] = time.monotonic,
    ) -> None:
        self.cfg, self.units, self.runner, self.secrets = cfg, units, runner, secrets
        self._gh, self._jira = gh, jira
        self._remote = remote
        self._today = today
        self._clock = clock
        self._file = cfg.data_dir / "ship.json"
        self._repo = cfg.data_dir / "ship.git"
        self._no_config = cfg.data_dir / "ship-empty.gitconfig"  # the "global" git config of every ship.git call
        self._lock = threading.Lock()  # the switch, the clients, the busy runs, the status cache
        self._git_lock = threading.Lock()  # ship.git: made and its config written by one push at a time
        self._busy: set[str] = set()  # runs being shipped right now
        self._status: dict[str, tuple[float, list[dict[str, Any]]]] = {}  # unit -> (valid until, pulls)
        self._writes: dict[str, int] = {}  # unit -> writes so far: a status read that one overtook is not kept
        self._status_locks: dict[str, threading.Lock] = {}  # one GitHub read per unit at a time
        self._day = self._load()  # the day "GitHub writes on" holds for; None: off

    # -- the switch --

    def _load(self) -> str | None:
        try:
            data = json.loads(self._file.read_text(encoding="utf-8"))
        except FileNotFoundError:
            return None
        except (OSError, ValueError):
            log.warning("%s cannot be read: GitHub writes are off", self._file.name)
            return None
        if not isinstance(data, dict) or data.get("github_writes") is not True or not isinstance(data.get("day"), str):
            return None
        return data["day"]

    def writes_on(self) -> bool:
        with self._lock:
            return self._day is not None and self._day == self._today()

    def switch(self) -> dict[str, Any]:
        """``{github_writes, day}``: whether writes are on now, and for which day (``None`` when off)."""
        with self._lock:
            on = self._day is not None and self._day == self._today()
            return {"github_writes": on, "day": self._day if on else None}

    def set_writes(self, on: bool) -> dict[str, Any]:
        """Turn "GitHub writes on" on (for today) or off; kept in ``ship.json``. The switch afterwards."""
        with self._lock:
            day = self._today() if on else None
            _write_json(self._file, {"github_writes": True, "day": day} if on else {"github_writes": False})
            self._day = day
        log.info("GitHub writes turned %s%s", "on for " if on else "off", day or "")
        return self.switch()

    # -- what may be shipped --

    def _run(self, unit_id: str, run_id: str) -> RunRecord:
        """The run, when ``unit_id`` may write with it now; ``KeyError`` for an unknown run."""
        rec = self.runner.store.get(run_id)
        if rec.unit != unit_id:
            raise NotYours("not your run")
        if not self.writes_on():
            raise WritesOff(WRITES_OFF)
        if not _shippable(rec):
            raise NotShippable("only a done run with a branch can be shipped")
        return rec

    @contextlib.contextmanager
    def _shipping(self, rec: RunRecord) -> Iterator[None]:
        """One ship action per run at a time; afterwards the unit's PR status is read anew."""
        with self._lock:
            if rec.id in self._busy:
                raise NotShippable("this run is being shipped already: try again in a moment")
            self._busy.add(rec.id)
        try:
            yield
        finally:
            with self._lock:
                self._busy.discard(rec.id)
                self._status.pop(rec.unit, None)
                self._writes[rec.unit] = self._writes.get(rec.unit, 0) + 1

    # -- GitHub and Jira --

    def _github_secrets(self) -> tuple[str, str]:
        """``(token, repo)``; ``NotConfigured`` without a token or a repo ``owner/name``."""
        token = self.secrets.get("GH_TOKEN") or ""
        repo = (self.secrets.get("BRIDGE_GITHUB_REPO") or "").strip()
        if not token or not _REPO.fullmatch(repo):
            raise NotConfigured("GitHub is not configured")
        return token, repo

    def _github(self, call: Callable[[Any], Any]) -> Any:
        """``call(client)``; whatever goes wrong in it becomes ``Failed`` with a fixed reason."""
        with self._lock:
            if self._gh is None:
                token, repo = self._github_secrets()
                self._gh = make_github(repo, token)
            client = self._gh
        try:
            return call(client)
        except Exception as err:
            raise _failure("GitHub", err) from None

    def _jira_call(self, call: Callable[[Any], Any]) -> Any:
        with self._lock:
            if self._jira is None:
                try:
                    self._jira = tickets.make_writer(self.secrets)
                except tickets.NotConfigured:
                    raise NotConfigured("Jira is not configured") from None
            client = self._jira
        try:
            return call(client)
        except Exception as err:
            raise _failure("Jira", err) from None

    # -- the summary --

    def _ladder(self, rec: RunRecord) -> dict | None:
        """The run's last gate ladder (``events.progress``): ``{summary, ok, ...}``, or ``None``."""
        progress = self.runner.progress(rec)
        ladders = progress["gates"] if progress else []
        return ladders[-1] if ladders else None

    @staticmethod
    def _verdict(ladder: dict) -> str:
        return {True: "passed", False: "failed"}.get(ladder.get("ok"), "not said")

    def _cleaner(self, rec: RunRecord) -> Callable[[Any], Any]:
        """A text of the run without host paths (``events.scrub`` with the unit's worktree); anything else as it is."""
        root = self.units.get(rec.unit).worktree
        return lambda value: events.scrub(value, root) if isinstance(value, str) else value

    def _facts(self, rec: RunRecord, dress: Callable[[Any], str]) -> list[str]:
        """The summary lines of a run; every value from the run is scrubbed, then ``dress``-ed (cut, put in code)."""
        clean, ladder = self._cleaner(rec), self._ladder(rec)
        gates = f"{dress(clean(ladder['summary']))} ({self._verdict(ladder)})" if ladder else "none"
        review = rec.summary.get("review") if isinstance(rec.summary, dict) else None
        return [
            f"Workflow: {dress(clean(rec.workflow))}",
            f"Ticket: {dress(clean(_ticket_key(rec.ticket)))}",
            f"Gates: {gates}",
            f"Review: {dress(clean(review))}",
            f"Credits: {rec.credits:g}",
            f"Commit: {dress((rec.commit or '')[:12])}",
        ]

    def _pr_body(self, rec: RunRecord) -> str:
        lines = [f"Harness run {_code(rec.id)} of {rec.unit} ({rec.status}).", ""]
        lines += [f"- {fact}" for fact in self._facts(rec, _code)]
        lines += ["", f"Opened from the Harness Console for {rec.unit}."]
        return "\n".join(lines) + "\n"

    # -- the push --

    def _ship_env(self, token: str | None = None) -> dict[str, str]:
        """The environment of a git call on ``ship.git``: ``procs.run_env`` (nothing token-like, no ``GIT_*``), no
        prompt, no system config, an empty file as the global one; with ``token``, the token's config
        (``_token_config``). Replace refs are ignored, as in every console git call."""
        _fresh_file(self._no_config, "")
        env = {
            **procs.run_env(os.environ, {}),
            "GIT_TERMINAL_PROMPT": "0",
            "GIT_NO_REPLACE_OBJECTS": "1",  # a replace ref in the clone never swaps the commit that is shipped
            "GIT_CONFIG_NOSYSTEM": "1",
            "GIT_CONFIG_GLOBAL": str(self._no_config),
        }
        return {**env, **_token_config(token)} if token is not None else env

    def _ship_git(self, *args: str, token: str | None = None) -> subprocess.CompletedProcess[str]:
        """``git args`` on ``ship.git`` (no hooks, no fsmonitor, the environment of ``_ship_env``); never raises for an
        exit code. The token, when given, is in the environment only."""
        return subprocess.run(
            [*self.units.bare_git_argv(self._repo), *args],
            cwd=self.cfg.data_dir,
            env=self._ship_env(token),
            capture_output=True,
            text=True,
            encoding="utf-8",
            errors="replace",
            stdin=subprocess.DEVNULL,
            timeout=PUSH_TIMEOUT_S,
            check=False,
        )

    def _ship_repo(self) -> None:
        """``ship.git`` ready for its next git call: made if missing (no templates: no hooks, nothing else), its config
        the fixed text (written anew, so nothing put there since counts), and no ``commondir``. ``RuntimeError`` if git
        fails."""
        if not (self._repo / "HEAD").is_file():
            self._repo.parent.mkdir(parents=True, exist_ok=True)
            made = self._ship_git("init", "-q", "--bare", "--template=")
            if made.returncode != 0:
                raise RuntimeError(f"git init of ship.git failed: {made.stderr.strip()}")
        (self._repo / "commondir").unlink(missing_ok=True)
        _fresh_file(self._repo / "config", _ship_config(self.cfg.data_dir / "no-hooks"))

    def push(self, unit_id: str, run_id: str) -> dict[str, str]:
        """Push the run's commit as ``units/<NN>/<branch>``: ``{remote_branch, cli}`` (the plain git command)."""
        rec = self._run(unit_id, run_id)
        token, repo = self._github_secrets()
        url = self._remote or f"{_GITHUB}{repo}.git"
        target, staged = _remote_branch(rec), f"refs/ship/{rec.id}"
        clone = self.cfg.data_dir / "source" / ".git"  # the unit worktrees' objects are the clone's (units.py)
        try:  # before the git lock: this call has no timeout of its own
            self.units.git(rec.unit, "cat-file", "-e", f"{rec.commit}^{{commit}}")
        except RuntimeError:
            raise NotShippable("this run's commit is not there any more") from None
        with self._shipping(rec), self._git_lock:
            try:
                self._ship_repo()
                fetched = self._ship_git(
                    "fetch", "-q", "--no-tags", "--no-write-fetch-head", "--", str(clone), f"{rec.commit}:{staged}"
                )
                held = self._ship_git("rev-parse", "--verify", "-q", f"{staged}^{{commit}}")
                if fetched.returncode != 0 or held.returncode != 0 or held.stdout.strip() != rec.commit:
                    log.warning("%s: run %s could not be staged in ship.git: %s", rec.unit, rec.id,
                                fetched.stderr.strip()[-2000:])
                    raise Failed(PUSH_FAILED)
                self._ship_repo()  # once more, right before the token goes in: nothing written meanwhile counts
                refspec = f"{rec.commit}:refs/heads/{target}"
                done = self._ship_git("push", "--no-verify", "--", url, refspec, token=token)
            except ShipError:
                raise
            except subprocess.TimeoutExpired:  # staging or pushing: either way the git lock is let go
                log.warning("%s: the push of run %s as %s timed out", rec.unit, rec.id, target)
                raise Failed("the push timed out") from None
            except (OSError, RuntimeError):  # git could not be started, or ship.git not made: no token in either
                log.warning("%s: the push of run %s failed before git pushed", rec.unit, rec.id, exc_info=True)
                raise Failed(PUSH_FAILED) from None
            finally:
                with contextlib.suppress(OSError, subprocess.SubprocessError):
                    self._ship_git("update-ref", "-d", staged)
        if done.returncode != 0:
            log.warning("%s: the push of run %s as %s failed (exit %s): %s", rec.unit, rec.id, target, done.returncode,
                        _hide(done.stderr.strip(), token)[-2000:])
            raise Failed(PUSH_FAILED)
        log.info("%s pushed run %s as %s", rec.unit, rec.id, target)
        return {"remote_branch": target, "cli": f"git push {_GITHUB}{repo}.git {rec.commit}:refs/heads/{target}"}

    # -- the PR, the check, the Jira comment --

    def open_pr(self, unit_id: str, run_id: str) -> dict[str, Any]:
        """A draft PR of the pushed branch against ``main``: ``{number, url, draft, branch, cli}``."""
        rec = self._run(unit_id, run_id)
        _, repo = self._github_secrets()
        head = _remote_branch(rec)
        key = _ticket_key(rec.ticket)
        title = f"{rec.unit}: {key} ({rec.workflow})" if key else f"{rec.unit}: run {rec.id}"
        with self._shipping(rec):
            body = self._pr_body(rec)
            answer = self._github(lambda gh: gh.create_pull(_line(title, 200), head, BASE, body, draft=True))
        answer = answer if isinstance(answer, dict) else {}
        number = answer.get("number")
        number = number if isinstance(number, int) and not isinstance(number, bool) else None
        log.info("%s opened PR %s for run %s (%s)", rec.unit, number, rec.id, head)
        return {"number": number, "url": _github_url(answer.get("html_url")), "draft": answer.get("draft") is True,
                "branch": head, "cli": f"gh pr create --repo {repo} --draft --base {BASE} --head {head}"}

    def post_ladder_check(self, unit_id: str, run_id: str) -> dict[str, Any]:
        """The check run ``harness-ladder`` on the run's commit: ``{name, conclusion, fallback}``."""
        rec = self._run(unit_id, run_id)
        ladder = self._ladder(rec)
        if ladder is None:
            raise NotShippable("this run has no gate ladder")
        conclusion = {True: "success", False: "failure"}.get(ladder.get("ok"), "neutral")
        ladder_text = self._cleaner(rec)(ladder["summary"])
        title = _line(f"Gate ladder: {ladder_text}", 120)
        summary = (
            f"Last gate ladder of run {_code(rec.id)} of {rec.unit}: {_code(ladder_text)} "
            f"({self._verdict(ladder)}).\n\nPosted from the Harness Console for {rec.unit}.\n"
        )
        with self._shipping(rec):
            answer = self._github(
                lambda gh: gh.check_run(rec.commit, CHECK_NAME, conclusion=conclusion, title=title, summary=summary)
            )
        fallback = isinstance(answer, dict) and answer.get("fallback") == "status"
        log.info("%s posted %s (%s) for run %s", rec.unit, CHECK_NAME, conclusion, rec.id)
        return {"name": CHECK_NAME, "conclusion": conclusion, "fallback": fallback}

    def report_to_jira(self, unit_id: str, run_id: str, key: object) -> dict[str, str]:
        """The unit's comment on ticket ``key`` (tag ``console-<unit>``): ``{key, tag}``."""
        rec = self._run(unit_id, run_id)
        if not isinstance(key, str) or not tickets.project_key(self.cfg, key):
            raise Invalid("not a ticket of the project")
        head = _remote_branch(rec)
        tag = f"console-{rec.unit}"
        with self._shipping(rec):
            lines = [f"Harness run {rec.id} of {rec.unit}: {rec.status}", *self._facts(rec, _line), f"Branch: {head}"]
            pr_url = self._pr_url(head)
            if pr_url:
                lines.append(f"Pull request: {pr_url}")
            lines += ["", f"Reported from the Harness Console for {rec.unit}."]
            text = "\n".join(lines)
            self._jira_call(lambda jira: jira.upsert_comment(key, tag, text))
        log.info("%s reported run %s on %s", rec.unit, rec.id, key)
        return {"key": key, "tag": tag}

    def _pr_url(self, head: str) -> str | None:
        """The open PR of ``head``, when GitHub can say (a report goes without it otherwise)."""
        try:
            found = self._github(lambda gh: gh.open_pull_for_branch(head))
        except ShipError:
            return None
        return _github_url(found.get("html_url")) if isinstance(found, dict) else None

    # -- what GitHub has --

    def status(self, unit_id: str) -> list[dict[str, Any]]:
        """The unit's PRs, newest first (at most ``MAX_PULLS``), each with its ``checks`` and ``actions``; at most
        ``STATUS_TTL_S`` seconds old. A failure is not kept: the next call asks again."""
        self.units.get(unit_id)  # KeyError: no such unit
        with self._lock:
            unit_lock = self._status_locks.setdefault(unit_id, threading.Lock())
        with unit_lock:
            with self._lock:
                kept, writes = self._status.get(unit_id), self._writes.get(unit_id, 0)
            if kept is not None and self._clock() < kept[0]:
                return copy.deepcopy(kept[1])
            pulls = self._read_status(unit_id)
            with self._lock:
                if self._writes.get(unit_id, 0) == writes:  # no write of the unit came in while GitHub was asked
                    self._status[unit_id] = (self._clock() + STATUS_TTL_S, pulls)
            return copy.deepcopy(pulls)

    def _read_status(self, unit_id: str) -> list[dict[str, Any]]:
        _, repo = self._github_secrets()
        prefix = f"units/{_nn(unit_id)}/"
        listed = self._github(lambda gh: gh.pulls())
        pulls = [pull for raw in (listed if isinstance(listed, list) else []) if (pull := _pull(raw, prefix, repo))]
        for pull in pulls[:MAX_PULLS]:
            sha = pull["sha"]
            checks = self._github(lambda gh: gh.check_runs(sha))
            statuses = self._github(lambda gh: gh.statuses(sha))
            actions = self._github(lambda gh: gh.workflow_runs(sha))
            pull["checks"] = [_check(item) for item in _items(checks)] + [_status(item) for item in _items(statuses)]
            pull["actions"] = [_action(item) for item in _items(actions)]
        return pulls[:MAX_PULLS]


def _pull(raw: Any, prefix: str, repo: str) -> dict[str, Any] | None:
    """A PR of the unit: head ``prefix*`` in ``repo`` itself; ``None`` for any other."""
    if not isinstance(raw, dict):
        return None
    head = raw.get("head") if isinstance(raw.get("head"), dict) else {}
    ref, sha = head.get("ref"), head.get("sha")
    where = head.get("repo") if isinstance(head.get("repo"), dict) else {}
    number = raw.get("number")
    if not (isinstance(ref, str) and ref.startswith(prefix) and len(ref) <= _URL_MAX and ref.isprintable()):
        return None
    if not (isinstance(sha, str) and _SHA.fullmatch(sha)) or not isinstance(number, int) or isinstance(number, bool):
        return None
    if not isinstance(where.get("full_name"), str) or where["full_name"].casefold() != repo.casefold():
        return None
    state = raw.get("state") if raw.get("state") in ("open", "closed") else "unknown"
    if raw.get("merged_at"):
        state = "merged"
    return {"number": number, "title": _line(raw.get("title"), 200), "state": state, "draft": raw.get("draft") is True,
            "url": _github_url(raw.get("html_url")), "branch": ref, "sha": sha}


def _check(item: dict) -> dict[str, Any]:
    return {"name": _line(item.get("name"), 100), "status": _word(item.get("status")),
            "conclusion": _word(item.get("conclusion"))}


def _status(item: dict) -> dict[str, Any]:
    """A commit status (what the bridge posts when its token may not write check runs) as a check."""
    state = _word(item.get("state"))
    pending = state in (None, "pending")
    return {"name": _line(item.get("context"), 100), "status": "pending" if pending else "completed",
            "conclusion": None if pending else state}


def _action(item: dict) -> dict[str, Any]:
    return {"name": _line(item.get("name"), 100), "status": _word(item.get("status")),
            "conclusion": _word(item.get("conclusion")), "url": _github_url(item.get("html_url"))}
