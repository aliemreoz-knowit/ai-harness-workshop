"""Command line: ``python -m console serve | units init | units codes --regenerate unit-NN | warm | doctor``.

``serve --personal`` and ``doctor --personal`` are personal mode: the console of one participant, on their own laptop,
for the checkout of the public repo it sits in (``config.find_personal_root``). See ``_personal_server``.
"""

from __future__ import annotations

import argparse
import contextlib
import dataclasses
import logging
import math
import re
import sys
import time
from http.server import ThreadingHTTPServer
from typing import Mapping, Sequence

from console import __version__, admin, doctor, ladder
from console.config import ConsoleConfig, find_personal_root, load_config
from console.server import App, lan_urls, make_server, rotate_session_key
from console.units import PERSONAL_UNIT, UnitStore

LOGGED_OUT = "Everyone who is logged in is logged out: the session key is new."
_PERSONAL_HELP = "personal mode: this checkout's own console, on 127.0.0.1, one unit, no login"
_STARTING_WAIT_S = 10.0  # how long a second `serve --personal` waits for the one that holds the lock to serve
_STARTING_POLL_S = 0.25


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(prog="python -m console", description="Harness Console (workshop web app).")
    parser.add_argument("--version", action="version", version=f"console {__version__}")
    parser.add_argument("--config", metavar="PATH", help="path to console.yaml (default: the one in the repo)")
    commands = parser.add_subparsers(dest="command", metavar="COMMAND", required=True)

    serve = commands.add_parser("serve", help="start the web server")
    serve.add_argument("--host", help="address to listen on (default: host in console.yaml)")
    serve.add_argument("--port", type=int, help="port to listen on (default: port in console.yaml; 0 picks a free one)")
    serve.add_argument("--personal", action="store_true", help=_PERSONAL_HELP + " (a busy port: a free one instead)")
    serve.add_argument(
        "--budget", type=_budget, metavar="N",
        help="with --personal: the unit's credits per day (default: unit_budget in console.yaml, 60)",
    )  # fmt: skip
    units = commands.add_parser("units", help="manage units")
    unit_commands = units.add_subparsers(dest="units_command", metavar="ACTION", required=True)
    init = unit_commands.add_parser("init", help="create units and print their codes")
    init.add_argument("--count", type=int, metavar="N", help="number of units (default: units in console.yaml)")
    init.add_argument("--force", action="store_true", help="rebuild units that already exist (discards their work)")
    codes = unit_commands.add_parser(
        "codes", help="make a new code for a unit (codes are not stored, so they cannot be shown again)"
    )
    codes.add_argument("--regenerate", metavar="UNIT", help="the unit to give a new code, for example unit-03")
    commands.add_parser("warm", help="warm build caches for every unit")
    doctor_cmd = commands.add_parser("doctor", help="check this machine and print fixes")
    doctor_cmd.add_argument("--personal", action="store_true", help=_PERSONAL_HELP)
    return parser


def _units_init(args: argparse.Namespace) -> int:
    """Build the units and print each id with its code, admin last. The codes are shown only here (and in the cards).

    The codes go to stdout, one ``unit-NN CODE`` line each; what else there is to say goes to stderr. ``unit-cards.txt``
    in the data folder holds the unit codes (not the admin's) with the addresses to open, for printing. ``--force`` also
    replaces the session key, so no session of the old units stays valid.
    """
    try:
        cfg = load_config(args.config)
        reason = doctor.console_may_be_running(cfg) if args.force else None
        if reason:  # the units lock is local to a process: a console would not know its worktrees went away
            print(
                f"units init: a console may be running ({reason}); stop it first (--force rebuilds every unit)",
                file=sys.stderr,
            )
            return 1
        if args.force:
            rotate_session_key(cfg.data_dir)  # first: a key that cannot be replaced stops it before any unit is touched
        codes = UnitStore(cfg).init(cfg.units if args.count is None else args.count, force=args.force)
    except (OSError, RuntimeError, ValueError) as e:  # includes ConfigError, LeakError, git failures
        print(f"units init: {e}", file=sys.stderr)
        return 1
    for subject, code in codes.items():
        print(f"{subject:<8} {code}")
    _write_cards(cfg, {subject: code for subject, code in codes.items() if subject != "admin"}, "units init")
    if args.force:
        print(f"units init: {LOGGED_OUT}", file=sys.stderr)
    return 0


def _write_cards(
    cfg: ConsoleConfig, codes: Mapping[str, str], who: str, heading: str = "Unit cards", filename: str = admin.CARDS_FILE
) -> None:
    """Write the cards file; say where (stderr), or that it could not be (the codes are already printed)."""
    try:
        path = admin.write_unit_cards(cfg, codes, heading, filename)
    except OSError as e:
        print(f"{who}: {filename} could not be written ({e.strerror or e}); the codes above exist nowhere else",
              file=sys.stderr)
        return
    print(f"{who}: unit cards written to {path} (print them, then delete the file)", file=sys.stderr)


def _units_codes(args: argparse.Namespace) -> int:
    """Give one unit a new code: printed once, and written as a card to ``unit-card-<unit>.txt`` in the data folder.

    ``unit-cards.txt`` (the cards of ``units init``, which a facilitator may not have printed yet) is left alone.

    Codes are stored as hashes, so the existing ones cannot be shown; ``--regenerate`` is the only action. It replaces
    the session key, which logs everyone out, and a running console would not notice (it holds the key in memory): the
    command refuses then, and the Admin page does the same for a console that runs.
    """
    unit = args.regenerate
    if unit is None:
        print(
            "units codes: codes are shown once, when they are made, and are not stored (only a hash is). "
            "For a new code run: python -m console units codes --regenerate unit-NN",
            file=sys.stderr,
        )
        return 2
    if not re.fullmatch(r"unit-[0-9]{2}", unit):
        print(f"units codes: {unit[:40]!r} is not a unit: it looks like unit-03", file=sys.stderr)
        return 1
    try:
        cfg = load_config(args.config)
        reason = doctor.console_may_be_running(cfg)
        if reason:
            print(
                f"units codes: a console may be running ({reason}); use \"New code\" on its Admin page instead, "
                "or stop it first",
                file=sys.stderr,
            )
            return 1
        store = UnitStore(cfg)
        store.get(unit)  # an unknown unit stops here, before the key is replaced
        rotate_session_key(cfg.data_dir)
        code = store.regenerate_code(unit)
    except KeyError:
        print(f"units codes: there is no {unit} in this console", file=sys.stderr)
        return 1
    except (OSError, RuntimeError, ValueError) as e:  # includes ConfigError and "no units yet"
        print(f"units codes: {e}", file=sys.stderr)
        return 1
    print(f"{unit:<8} {code}")
    print(f"units codes: {LOGGED_OUT}", file=sys.stderr)
    _write_cards(cfg, {unit: code}, "units codes", f"new card for {unit}", f"unit-card-{unit}.txt")
    return 0


def _doctor(args: argparse.Namespace) -> int:
    """Print every check with its fix; exit 1 when one failed (a warning does not fail)."""
    try:
        cfg = load_config(args.config, personal_root=find_personal_root() if args.personal else None)
    except (OSError, RuntimeError, ValueError) as e:  # includes ConfigError, no checkout for --personal
        print(f"doctor: {e}", file=sys.stderr)
        return 1
    print("Checking this machine (the harness check starts the Copilot CLI and can take a minute) ...", flush=True)
    checks = doctor.run_checks(cfg)
    print(doctor.format_report(checks))
    return 1 if any(not c.ok for c in checks) else 0


def _serve(args: argparse.Namespace) -> int:
    """Serve until Ctrl+C. The request log goes to stderr; the URLs to open go to stdout.

    The runner starts once the port is bound (a second console on the same port stops before it touches a run) and
    is shut down on the way out; runs still going carry on and the next start picks them up. ``--personal``: the
    start of ``_personal_server``, then the same; what it holds for the life of the process (the folder's lock) is
    let go on the way out, whichever way that is.
    """
    logging.basicConfig(level=logging.INFO, format="%(asctime)s %(message)s", datefmt="%H:%M:%S")
    with contextlib.ExitStack() as held:
        return _serve_held(args, held)


def _serve_held(args: argparse.Namespace, held: contextlib.ExitStack) -> int:
    try:
        if args.personal:
            started = _personal_server(args, held)
            if started is None:  # a console serves this checkout already, or is starting: that is printed
                return 0
            cfg, app, server = started
        elif args.budget is not None:
            raise ValueError("--budget is for --personal (in the room the Admin page gives a unit credits)")
        else:
            cfg = load_config(args.config)
            app = App(cfg)
            server = make_server(app, args.host, args.port)
    except (OSError, RuntimeError, ValueError) as e:  # includes ConfigError, no units yet, a port in use
        print(f"serve: {e}", file=sys.stderr)
        return 1
    try:
        app.runner.start()
    except (OSError, RuntimeError, ValueError) as e:
        server.server_close()
        print(f"serve: {e}", file=sys.stderr)
        return 1
    host, port = server.server_address[:2]
    try:  # for `units init --force`, which must not rebuild the units under a running console
        doctor.write_serve_record(cfg.data_dir, host, port)
    except OSError as e:
        print(
            f"serve: cannot write {doctor.SERVE_FILE} ({e}); `units init --force` will not know of this console",
            file=sys.stderr,
        )
    if cfg.personal:
        print("Harness Console is running (personal mode: this computer only, no login). Open:", flush=True)
    else:
        print("Harness Console is running. Open one of:", flush=True)
    for url in lan_urls(host, port):
        print(f"  {url}", flush=True)
    print("Press Ctrl+C to stop.", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("Stopping.", flush=True)
    finally:
        server.server_close()
        app.runner.shutdown()
        if app.workshop is not None:  # the day's commands it started end with it
            app.workshop.shutdown()
        doctor.remove_serve_record(cfg.data_dir)
    return 0


def _personal_server(
    args: argparse.Namespace, held: contextlib.ExitStack
) -> tuple[ConsoleConfig, App, ThreadingHTTPServer] | None:
    """Personal mode's start: the config of the checkout this console sits in, its unit, the app, and a server on
    127.0.0.1.

    First of all it takes the data folder's lock (``doctor.ServeLock``) and keeps it in ``held``, for the life of the
    process: two consoles on one checkout would run one unit's runs twice (each runner takes the other's for its own)
    and two first starts would each build the unit over the other. When another start holds the lock, this one waits
    up to ``_STARTING_WAIT_S`` for it to serve, prints its address (or that it is starting) and gives ``None``.

    ``unit-01`` is built on the first start (``UnitStore.init_personal``: no codes, there is no login) and kept from
    then on. ``--budget`` is the credits per day (default: ``unit_budget`` of console.yaml, 60). The port is
    ``--port``, else console.yaml's (8765); when that one cannot be had (another program holds it), a free one, said
    on stdout before the address. Errors are raised for ``_serve`` to print.
    """
    if args.host is not None:
        raise ValueError("--personal listens on 127.0.0.1 only: leave out --host")
    cfg = load_config(args.config, personal_root=find_personal_root())
    if args.budget is not None:
        cfg = dataclasses.replace(cfg, unit_budget=args.budget)
    lock = doctor.ServeLock(cfg.data_dir)
    if not lock.acquire():
        _point_to_the_other_console(cfg)
        return None
    held.callback(lock.release)
    kind, why = ladder.decide(cfg.source_repo)  # once: never an image build behind the participant's back
    cfg = dataclasses.replace(cfg, ladder=kind, ladder_why=why)
    if UnitStore(cfg).init_personal():
        print(f"serve: {PERSONAL_UNIT} built from {cfg.source_repo} ({cfg.start_ref})", file=sys.stderr)
    app = App(cfg)
    port = cfg.port if args.port is None else args.port
    try:
        server = make_server(app, cfg.host, port)
    except OSError as e:
        if port == 0:
            raise
        server = make_server(app, cfg.host, 0)
        print(f"serve: port {port} cannot be used: {e.strerror or e}", file=sys.stderr)  # stderr: the OS's own words
        print(f"Port {port} is busy; using port {server.server_address[1]} instead.", flush=True)
    return cfg, app, server


def _point_to_the_other_console(cfg: ConsoleConfig) -> None:
    """Another ``serve --personal`` holds this checkout's lock: print its address once it serves (``serve.json``), or,
    after ``_STARTING_WAIT_S`` (it may be building the unit), that it is starting."""
    deadline = time.monotonic() + _STARTING_WAIT_S
    while True:
        running = doctor.serving_console(cfg.data_dir)
        if running is not None:
            print("Harness Console is already running for this checkout. Open:", flush=True)
            for url in lan_urls(*running):
                print(f"  {url}", flush=True)
            return
        if time.monotonic() >= deadline:
            break
        time.sleep(_STARTING_POLL_S)
    print("A console is starting already in this folder: open the address it prints.", flush=True)


def _budget(text: str) -> float:
    """``--budget``: credits per day, a finite number of at least 0."""
    try:
        value = float(text)
    except ValueError:
        raise argparse.ArgumentTypeError(f"not a number: {text!r}") from None
    if not math.isfinite(value) or value < 0:
        raise argparse.ArgumentTypeError(f"must be a number of at least 0, got {text!r}")
    return value


def main(argv: Sequence[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    if args.command == "serve":
        return _serve(args)
    if args.command == "units" and args.units_command == "init":
        return _units_init(args)
    if args.command == "units" and args.units_command == "codes":
        return _units_codes(args)
    if args.command == "doctor":
        return _doctor(args)
    name = args.command if args.command != "units" else f"units {args.units_command}"
    print(f"{name}: not implemented yet")
    return 2


if __name__ == "__main__":
    sys.exit(main())
