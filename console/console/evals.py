"""Evals: the variant card of a unit, the variants file and one eval batch: launch, progress, stop.

At lunch every unit submits a *variant card*: the settings it wants its own harness evaluated with. The facilitator
then launches one batch, ``python -m harness eval`` over the hidden-test tickets, that runs every unit's harness (the
files of its worktree, through ``overlay_from``) and the reference variant ``cheap`` (the harness's own cheap variant). The
leaderboard (Task 17's ``routes_board``) shows pass rate and cost per pass. Everything below is plain files under
``<data>/evals``::

    cards/unit-NN.json          the card of a unit (atomic replace; the file is the card itself)
    <experiment>.variants.yaml  the variants file of a batch (U01 ... and cheap), written at launch
    <experiment>.meta.json      ``{experiment, launched_at, hashes}`` written at launch: ``hashes`` is the
                                ``structure.config_hash`` of each unit's harness files then, by variant (``{"U01": ...}``).
                                The public results compare it with the hash now: a unit that changed its harness
                                since has a stale row on the leaderboard (``routes_board``)
    <experiment>.log            what the batch printed
    batch.json                  the batch last launched: experiment, pid and start time, plan, ``stopped`` once the admin
                                stopped it (so a restarted console still knows it runs, and does not start a second one)
    results/<experiment>.jsonl  one line per finished run (written by the harness)
    results/results.json        the leaderboard file (written by the harness after every run; the public
                                ``GET /api/eval/results.json`` serves it, scrubbed, from ``routes_board``)
    archive/<UTC timestamp>/    ``results/`` and ``batch.json`` of the batches before ``archive_results`` moved them

A card holds only variant settings (``CARD_KEYS``). With ``overlay_from`` the harness applies just the settings a
variant names itself, on top of the unit's own harness files, so a card that leaves a setting out means "my harness
files decide". The cap ``max_ai_credits`` is the exception: it is always written (the card's, else ``cfg.run_cap``) and
never above ``cfg.run_cap``. The credits of a batch are the facilitator's experiment budget: nothing here touches the
units' ``spent``.

``launch`` starts the batch the way ``procs`` starts every process: allowlisted environment (the ``.env`` tokens are not in
it), its own process group, cwd ``cfg.harness_repo``. One batch at a time: ``BatchRunning`` while one runs, also after
the console was restarted (``batch.json`` keeps the pid and its start time). The batch's progress is the number of lines
in ``results/<experiment>.jsonl`` against the runs planned. ``stop`` kills the batch's process tree (``procs.kill_tree``
with the recorded start time, so a pid that was reused is left alone); the runs finished so far stay in the results.
After a restart a batch counts as running only if its start time was recorded and that very process is still there: a
bare pid is never trusted or killed (as ``runs`` does). ``plan`` is what a launch would run now, for the cost estimate. The hidden tests folder is only ever passed to the
harness: no answer names it.
"""

from __future__ import annotations

import contextlib
import copy
import json
import logging
import math
import os
import re
import stat
import subprocess
import threading
import time
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Callable

from console import procs, structure
from console.atomic import atomic_write
from console.config import ConsoleConfig
from console.units import NEEDS_RESET, Unit, UnitStore

__all__ = [
    "CARD_KEYS", "EXPERIMENT", "META_SUFFIX", "BatchRunning", "CardInvalid", "EvalError", "Evals", "NothingToArchive",
    "NotRunning", "StartFailed", "StopFailed", "dump_yaml", "unit_of", "validate_card", "variant_name",
]  # fmt: skip

log = logging.getLogger(__name__)

ROLES = ("planner", "implementer", "test_writer", "reviewer")  # harness/api.py ROLES
AUTONOMY = ("human", "auto")  # harness/api.py AUTONOMY
# The settings of a variant a unit may choose (harness/api.py SCALAR_KNOBS, ROLE_KNOBS, CAP_KNOB; a test keeps them in step).
# Not here: timeout_s and extra (facilitator knobs), overlay_from (the console sets it), label and the rest.
CARD_KEYS = ("roles", "models", "retry_budget", "custom_instructions", "prompt_template", "autonomy", "gates", "max_ai_credits")
REFERENCE = "cheap"  # the variant of the harness's variants.yaml that every unit is compared with

MAX_RETRY_BUDGET = 10
MAX_TICKETS = 40
MAX_PARALLEL = 8
MAX_LABEL = 60
MAX_CARD_BYTES = 64 * 1024  # a card file or the batch record
MAX_FILE_BYTES = 8 * 1024 * 1024  # one experiment's .jsonl
STOP_WAIT_S = 10  # how long a stopped batch gets to be gone
# Credits a run may end above its cap: the harness's credit guard stops a run up to one model call over it, 0.04 to 0.4
# credits (harness/eval/variants.yaml, harness/eval/README.md); the estimate counts a little more than the dearest call.
CAP_OVERSHOOT = 0.5

META_SUFFIX = ".meta.json"  # <experiment>.meta.json: the config hashes of a batch (see the module doc)

_UNIT_ID = re.compile(r"unit-[0-9]{2}")
_VARIANT = re.compile(r"U([0-9]{2})")
TICKET = re.compile(r"[A-Z][A-Z0-9]*-[0-9]+")  # the harness's own rule (harness/eval/tickets.py)
EXPERIMENT = re.compile(r"[A-Za-z0-9][A-Za-z0-9_-]{0,47}")  # also a file name, a folder name and part of git branch names
_TEMPLATE = re.compile(r"[A-Za-z0-9][A-Za-z0-9_.-]{0,31}")

_IDLE: dict[str, Any] = {
    "running": False, "experiment": None, "done": 0, "total": 0, "credits": 0.0, "exit": None, "stopped": False,
}  # fmt: skip
_HEADER = "# One eval batch, written by the Harness Console at launch. Change nothing here: the next batch writes its own file.\n"


class EvalError(ValueError):
    """The request cannot be done as asked; the message is for the person who asked (never a host path)."""


class CardInvalid(EvalError):
    """A card that breaks the rules: ``errors`` are plain sentences, one per problem."""

    def __init__(self, errors: list[str]) -> None:
        super().__init__("; ".join(errors))
        self.errors = list(errors)


class BatchRunning(RuntimeError):
    """An eval batch is running already."""


class StartFailed(RuntimeError):
    """The batch process could not be started (the details are in the log)."""


class NotRunning(RuntimeError):
    """There is no eval batch to stop."""


class StopFailed(RuntimeError):
    """The batch process could not be stopped (the details are in the log)."""


class NothingToArchive(RuntimeError):
    """There are no results to move to the archive."""


# --- the card ----------------------------------------------------------------------------------------------------


def _shown(value: object) -> str:
    """A value of a card for an error sentence: short, one line."""
    text = value if isinstance(value, str) else repr(value)
    return re.sub(r"[^\x20-\x7e]", "?", text)[:40]


def validate_card(card: object, cfg: ConsoleConfig) -> list[str]:
    """What is wrong with ``card``, one sentence each (empty when it is fine).

    Only ``CARD_KEYS`` are known. Models must be in ``cfg.allowed_models``; the implementer cannot be switched off (the
    harness refuses such a variant); ``max_ai_credits`` is above 0 and at most ``cfg.run_cap``.
    """
    if not isinstance(card, dict):
        return ["a card is an object of settings"]
    errors = [f'"{_shown(key)}" is not a setting of a variant card' for key in sorted(card, key=str) if key not in CARD_KEYS]

    if "roles" in card:
        roles = card["roles"]
        if not isinstance(roles, dict):
            errors.append('roles must be an object like {"planner": true}')
        else:
            for role, on in roles.items():
                if role not in ROLES:
                    errors.append(f'roles: "{_shown(role)}" is not a role (use {", ".join(ROLES)})')
                elif not isinstance(on, bool):
                    errors.append(f"roles.{role} must be true or false")
                elif role == "implementer" and not on:
                    errors.append("roles.implementer: the implementer cannot be switched off")
    if "models" in card:
        models = card["models"]
        if not isinstance(models, dict):
            errors.append('models must be an object like {"planner": "claude-haiku-5.5"}')
        else:
            for role, model in models.items():
                if role not in ROLES:
                    errors.append(f'models: "{_shown(role)}" is not a role (use {", ".join(ROLES)})')
                elif not isinstance(model, str) or model not in cfg.allowed_models:
                    errors.append(f"models.{role}: {_shown(model)} is not allowed (use {', '.join(cfg.allowed_models)})")
    if "retry_budget" in card:
        value = card["retry_budget"]
        if isinstance(value, bool) or not isinstance(value, int) or not 0 <= value <= MAX_RETRY_BUDGET:
            errors.append(f"retry_budget must be a whole number from 0 to {MAX_RETRY_BUDGET}")
    for key in ("custom_instructions", "gates"):
        if key in card and not isinstance(card[key], bool):
            errors.append(f"{key} must be true or false")
    if "autonomy" in card and not (isinstance(card["autonomy"], str) and card["autonomy"] in AUTONOMY):
        errors.append(f"autonomy must be {' or '.join(AUTONOMY)}")
    if "prompt_template" in card:
        template = card["prompt_template"]
        if not (isinstance(template, str) and _TEMPLATE.fullmatch(template)):
            errors.append("prompt_template must be 1 to 32 letters, digits, dots, dashes or underscores")
    if "max_ai_credits" in card:
        cap = card["max_ai_credits"]
        if isinstance(cap, bool) or not isinstance(cap, (int, float)) or not 0 < cap <= cfg.run_cap:
            errors.append(f"max_ai_credits must be a number above 0 and at most {cfg.run_cap:g} (the cap of one run)")
    return errors


# --- YAML --------------------------------------------------------------------------------------------------------

_PLAIN = re.compile(r"[A-Za-z][A-Za-z0-9_./-]*")  # a string that is safe unquoted, if it is not a word YAML reads as a value
_WORDS = frozenset({"null", "true", "false", "yes", "no", "on", "off", "y", "n"})
_ESCAPED = {"\\": "\\\\", "\n": "\\n", "\r": "\\r", "\t": "\\t", '"': "\\u0022"}
# A quote is written as ", never \": the harness's small YAML reader looks for the end of a quoted text without
# knowing backslashes, and ``- "say \"hi\": x"`` would read as a mapping there.


def dump_yaml(obj: Any) -> str:
    """``obj`` (dicts with text keys, lists, text, numbers, booleans, ``None``) as block-style YAML.

    ``harnesslink.yaml_load`` reads it back to an equal value, with the harness's own reader and with PyYAML: a text is
    plain only when it is made of letters, digits and ``_ . / -`` after a first letter and is no word YAML takes for a
    value, every other text is double quoted with its special characters escaped; an empty dict or list is ``{}`` or ``[]``.
    ``TypeError`` for anything else, ``ValueError`` for a number that is not finite or text YAML cannot hold.
    """
    lines: list[str] = []
    if _filled(obj):
        _block(obj, 0, lines)
    else:
        lines.append(_value(obj))
    return "\n".join(lines) + "\n"


def _filled(value: object) -> bool:
    return isinstance(value, (dict, list, tuple)) and len(value) > 0


def _block(value: Any, indent: int, lines: list[str]) -> None:
    pad = " " * indent
    if isinstance(value, dict):
        for key, item in value.items():
            if not isinstance(key, str):
                raise TypeError(f"YAML keys must be text, got {type(key).__name__}")
            if _filled(item):
                lines.append(f"{pad}{_text(key)}:")
                _block(item, indent + 2, lines)
            else:
                lines.append(f"{pad}{_text(key)}: {_value(item)}")
    else:
        for item in value:
            if _filled(item):
                lines.append(f"{pad}-")
                _block(item, indent + 2, lines)
            else:
                lines.append(f"{pad}- {_value(item)}")


def _value(value: Any) -> str:
    if value is None:
        return "null"
    if isinstance(value, bool):
        return "true" if value else "false"
    if isinstance(value, int):
        return str(value)
    if isinstance(value, float):
        return _number(value)
    if isinstance(value, str):
        return _text(value)
    if isinstance(value, dict) and not value:
        return "{}"
    if isinstance(value, (list, tuple)) and not value:
        return "[]"
    raise TypeError(f"cannot write a {type(value).__name__} as YAML")


def _number(value: float) -> str:
    if not math.isfinite(value):
        raise ValueError("YAML here holds finite numbers only")
    text = repr(value)
    if "e" in text:  # both readers need a dot in a float: 1e-05 is text to them, 1.0e-05 a number
        mantissa, _, exponent = text.partition("e")
        text = f"{mantissa if '.' in mantissa else mantissa + '.0'}e{exponent}"
    return text


def _text(text: str) -> str:
    if _PLAIN.fullmatch(text) and text.lower() not in _WORDS:
        return text
    out = []
    for char in text:
        code = ord(char)
        if char in _ESCAPED:
            out.append(_ESCAPED[char])
        elif 0xD800 <= code <= 0xDFFF:
            raise ValueError("text with a lone surrogate cannot be written as YAML")
        elif code < 0x20 or 0x7F <= code <= 0x9F or char in "  ﻿￾￿":
            out.append(f"\\u{code:04x}")  # control characters, and the ones YAML reads as line breaks or marks
        else:
            out.append(char)
    return '"' + "".join(out) + '"'


# --- plain files -------------------------------------------------------------------------------------------------


def _read_capped(path: Path, cap: int) -> bytes | None:
    """The bytes of a plain file (no link, no device) of at most ``cap`` bytes; ``None`` when it is missing or not that."""
    try:
        if path.is_symlink():
            raise OSError("a link")
        with open(path, "rb") as fh:
            if not stat.S_ISREG(os.fstat(fh.fileno()).st_mode):
                raise OSError("not a plain file")
            data = fh.read(cap + 1)
    except FileNotFoundError:
        return None
    except OSError:
        log.warning("%s is not a readable plain file", path.name)
        return None
    if len(data) > cap:
        log.warning("%s is larger than %d bytes: not read", path.name, cap)
        return None
    return data


def _read_json(path: Path, cap: int) -> Any:
    data = _read_capped(path, cap)
    if data is None:
        return None
    try:
        return json.loads(data.decode("utf-8"))
    except (ValueError, RecursionError):
        log.warning("%s is not valid JSON", path.name)
        return None


def _write_atomic(path: Path, text: str) -> None:
    """Replace ``path`` with ``text`` in one step: a reader sees the old file or the new one, never half."""
    atomic_write(path, text)


# --- checks of a launch request ---------------------------------------------------------------------------------


def variant_name(unit_id: str) -> str:
    """``unit-01`` is the variant ``U01``."""
    return "U" + unit_id.removeprefix("unit-")


def unit_of(variant: object) -> str | None:
    """The unit a variant stands for: ``U01`` is ``unit-01``; ``None`` for ``cheap`` and anything else."""
    found = _VARIANT.fullmatch(variant) if isinstance(variant, str) else None
    return f"unit-{found.group(1)}" if found else None


def _label(name: str) -> str:
    return re.sub(r"[\x00-\x1f\x7f-\x9f  ]+", " ", name).strip()[:MAX_LABEL].strip()


def _check_tickets(tickets: object) -> list[str]:
    if not isinstance(tickets, list) or not tickets:
        raise EvalError('give at least one ticket, as a list like ["TS-101"]')
    keys: list[str] = []
    for ticket in tickets:
        if not (isinstance(ticket, str) and TICKET.fullmatch(ticket)):
            raise EvalError("a ticket key looks like TS-101 (capital letters, a dash, digits)")
        if ticket not in keys:
            keys.append(ticket)
    if len(keys) > MAX_TICKETS:
        raise EvalError(f"at most {MAX_TICKETS} tickets in one batch")
    return keys


def _check_parallel(parallel: object) -> int:
    if isinstance(parallel, bool) or not isinstance(parallel, int) or not 1 <= parallel <= MAX_PARALLEL:
        raise EvalError(f"parallel must be a whole number from 1 to {MAX_PARALLEL}")
    return parallel


def _left_out(unit: Unit | None, card: dict, cfg: ConsoleConfig) -> str:
    """Why a unit's card is not in a batch launched now, or ``""``. A unit a run holds is left out: the batch would read
    the run's half-done files in its worktree (and one a run could not put back, until a reset)."""
    if unit is None:
        return "no such unit"
    if validate_card(card, cfg):
        return "its card no longer passes the rules"
    if not unit.worktree.is_dir():
        return "its worktree is gone"
    if unit.locked_by is not None:
        return "it waits for a reset" if unit.locked_by.startswith(NEEDS_RESET) else "a run is going"
    return ""


def _utc_stamp() -> str:
    """The name of an archive folder: the time now in UTC, as ``20261120T101500Z`` (no colon: Windows file names)."""
    return datetime.now(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def _check_experiment(experiment: object) -> str:
    if experiment is None:
        return datetime.now().strftime("exp-%Y%m%d-%H%M%S")
    if not (isinstance(experiment, str) and EXPERIMENT.fullmatch(experiment)):
        raise EvalError("experiment must be 1 to 48 letters, digits, dashes or underscores, starting with a letter or digit")
    return experiment


# --- the evals of one console ---------------------------------------------------------------------------------------


class Evals:
    """The cards, the variants file and the one eval batch of a console. Safe to share between threads.

    ``spawn`` starts a process like ``procs.spawn(cmd, cwd, env, log)``; a test passes its own.
    """

    def __init__(self, cfg: ConsoleConfig, units: UnitStore, spawn: Callable[..., Any] = procs.spawn) -> None:
        self.cfg = cfg
        self.units = units
        self._spawn = spawn
        self.dir = cfg.data_dir / "evals"
        self._cards = self.dir / "cards"
        self._results = self.dir / "results"
        self._batch_file = self.dir / "batch.json"
        self._lock = threading.Lock()  # a launch, and every look at the batch record
        self._proc: Any = None  # what spawn returned for the batch of this process (None after a restart)
        self._plan_failing = False  # the last plan could not be worked out (and was logged)

    # -- cards --

    def save_card(self, unit_id: str, card: dict) -> dict:
        """Store the card of a unit, replacing the earlier one. ``CardInvalid`` when it breaks the rules,
        ``EvalError`` when there is no such unit."""
        errors = validate_card(card, self.cfg)
        if errors:
            raise CardInvalid(errors)
        self._unit(unit_id)
        _write_atomic(self._cards / f"{unit_id}.json", json.dumps(card, indent=2, sort_keys=True, allow_nan=False) + "\n")
        return copy.deepcopy(card)

    def card(self, unit_id: str) -> dict | None:
        """The stored card of a unit, or ``None`` (none stored, or the file is damaged)."""
        if not _UNIT_ID.fullmatch(unit_id):
            return None
        return self._load_card(self._cards / f"{unit_id}.json")

    def cards(self) -> dict[str, dict]:
        """Every stored card by unit id. A file that is not a card (damaged, too large, not an object) is left out."""
        found: dict[str, dict] = {}
        for path in sorted(self._cards.glob("unit-*.json")):
            if _UNIT_ID.fullmatch(path.stem) and (card := self._load_card(path)) is not None:
                found[path.stem] = card
        return found

    @staticmethod
    def _load_card(path: Path) -> dict | None:
        card = _read_json(path, MAX_CARD_BYTES)
        return card if isinstance(card, dict) else None

    def _unit(self, unit_id: object) -> Unit:
        if isinstance(unit_id, str) and _UNIT_ID.fullmatch(unit_id):
            try:
                return self.units.get(unit_id)
            except KeyError:
                pass
        raise EvalError("no such unit")

    def defaults(self) -> dict:
        """The values a card form starts from: the settings of the reference variant ``cheap``, the cap held to the run cap."""
        _raw, _matrix, reference = self._reference()
        values = {key: copy.deepcopy(reference.settings[key]) for key in CARD_KEYS if key in reference.settings}
        values["max_ai_credits"] = min(values.get("max_ai_credits", self.cfg.run_cap), self.cfg.run_cap)
        return values

    def options(self) -> dict:
        """What a card form may offer."""
        return {
            "models": list(self.cfg.allowed_models),
            "run_cap": self.cfg.run_cap,
            "max_retry_budget": MAX_RETRY_BUDGET,
        }

    def plan(self) -> dict | None:
        """``{variants, units, skipped, repeats, cap, overshoot}`` of the batch a launch would start now.

        ``variants`` counts the units with a valid card and the reference; ``repeats`` and ``cap`` are the largest of
        any variant. A run is held to its cap, but the harness ends it up to one model call above (``CAP_OVERSHOOT`` is
        the room the estimate counts for that), so ``variants x tickets x repeats x (cap + overshoot)`` is what a batch
        should stay under. ``None`` when the plan cannot be worked out (the harness's variants file is unreadable, it has
        no cheap): that is logged once, until it works again, so the page asking every few seconds does not fill the log.
        """
        try:
            doc, used, skipped = self.variants_doc()
        except (RuntimeError, ValueError, OSError, ImportError):
            if not self._plan_failing:
                log.error("the plan of the eval batch could not be worked out", exc_info=True)
            self._plan_failing = True
            return None
        self._plan_failing = False
        entries = list(doc["variants"].values())
        fallback = doc["defaults"].get("repeats", 1)
        return {
            "variants": len(entries),
            "units": used,
            "skipped": skipped,
            "repeats": max(int(entry.get("repeats", fallback)) for entry in entries),
            "cap": float(max(entry["max_ai_credits"] for entry in entries)),
            "overshoot": CAP_OVERSHOOT,
        }

    # -- the variants file --

    @staticmethod
    def _reference() -> tuple[dict, Any, Any]:
        """The harness's own variants file: ``(its document, its matrix, the cheap variant)``."""
        from harness.eval import variants  # the harness is linked in by console.config, so this waits for a call
        from harness.eval.yamlio import load_yaml

        path = variants.find_variants_file(None)
        doc = load_yaml(path)
        matrix = variants.parse_matrix(doc, source=str(path))
        if REFERENCE not in matrix.variants:
            raise RuntimeError(f"the harness variants.yaml has no {REFERENCE}")
        return doc, matrix, matrix.variants[REFERENCE]

    def variants_doc(self) -> tuple[dict, list[str], list[dict[str, str]]]:
        """``(document, units used, units left out as {unit, reason})``: the variants file of a batch if it were
        launched now.

        One variant per unit with a valid card and a worktree: ``U<NN>`` with ``overlay_from`` its worktree and only the
        settings of its card, plus the cap. A unit whose card no longer passes ``validate_card`` (the config changed),
        whose worktree is gone, or that a run holds (``a run is going``: its worktree has the run's files, half done) is
        left out with the reason, not an error. Then ``cheap``, as the harness's variants.yaml has it, without
        overlay and with its cap held to the run cap. ``defaults`` are the harness's own (the overlay variants ignore
        what only comes from there, except the cap).
        """
        raw, matrix, reference = self._reference()
        cap = self.cfg.run_cap
        defaults = copy.deepcopy(raw.get("defaults") or {})
        defaults["max_ai_credits"] = cap
        doc: dict[str, Any] = {
            "version": 1,
            "workflow": reference.workflow,
            "defaults": defaults,
            "gate_weights": dict(matrix.gate_weights),
            "variants": {},
        }
        used: list[str] = []
        skipped: list[dict[str, str]] = []
        for unit_id, card in self.cards().items():
            try:
                unit = self._unit(unit_id)
            except EvalError:
                unit = None
            reason = _left_out(unit, card, self.cfg)
            if reason:
                log.warning("the card of %s is left out of the batch: %s", unit_id, reason)
                skipped.append({"unit": unit_id, "reason": reason})
                continue
            entry: dict[str, Any] = {
                "label": _label(unit.name) or unit.id,
                "tests": "the unit's own harness",
                "overlay_from": str(unit.worktree),
            }
            entry.update({key: copy.deepcopy(card[key]) for key in CARD_KEYS if key in card})
            entry["max_ai_credits"] = card.get("max_ai_credits", cap)
            doc["variants"][variant_name(unit_id)] = entry
            used.append(unit_id)
        doc["variants"][REFERENCE] = {
            "label": f"Reference: {reference.label}",
            "tests": reference.tests,
            **copy.deepcopy(reference.settings),
            "max_ai_credits": min(reference.settings.get("max_ai_credits", cap), cap),
        }
        return doc, used, skipped

    def write_variants(self, path: Path) -> Path:
        """Write the variants file of a batch (``variants_doc``) to ``path``; the harness's ``load_matrix`` reads it."""
        _write_atomic(path, _HEADER + dump_yaml(self.variants_doc()[0]))
        return path

    # -- the batch --

    def launch(self, tickets: list[str], parallel: int, experiment: str | None = None) -> dict:
        """Start the batch: ``{experiment, total, units, skipped}``.

        ``total`` is the number of runs planned (every variant on every ticket, times its repeats). ``EvalError`` for a
        request that is wrong or cannot be met (no card yet, the name of the experiment is used, ...), ``BatchRunning``
        while a batch runs, ``StartFailed`` when the process could not be started.
        """
        keys = _check_tickets(tickets)
        workers = _check_parallel(parallel)
        name = _check_experiment(experiment)
        if not self.cfg.hidden_tests.is_dir():
            raise EvalError("the hidden tests folder is missing on this machine")
        with self._lock:
            batch = self._read_batch()
            if batch is not None and self._running(batch):
                raise BatchRunning("an eval batch is running")
            if (self._results / f"{name}.jsonl").exists():
                raise EvalError("that experiment name is already used")
            doc, used, skipped = self.variants_doc()
            if not used:
                raise EvalError("no unit has submitted a valid variant card yet")
            variants_file = self.dir / f"{name}.variants.yaml"
            _write_atomic(variants_file, _HEADER + dump_yaml(doc))
            total = self._check_file(variants_file) * len(keys)
            self._record_hashes(name, used)  # before the batch starts: no result can be older than its record
            cmd = [
                *self.cfg.harness_argv, "eval",
                "--variants", str(variants_file),
                "--tickets", ",".join(keys),
                "--parallel", str(workers),
                "--experiment", name,
                "--repo", str(self.cfg.harness_repo),
                "--hidden-tests", str(self.cfg.hidden_tests),
                "--results-dir", str(self._results),
                "--board-out", str(self._results / "results.json"),
            ]  # fmt: skip
            env = procs.run_env(os.environ, {"PYTHONUTF8": "1"})
            try:
                proc = self._spawn(cmd, self.cfg.harness_repo, env, self.dir / f"{name}.log")
            except OSError:
                log.error("the eval batch %s could not be started", name, exc_info=True)
                raise StartFailed("the eval batch could not be started") from None
            record = {
                "experiment": name,
                "pid": proc.pid,
                "started": procs.start_time(proc.pid),
                "total": total,
                "launched_at": datetime.now().isoformat(timespec="seconds"),
            }
            try:
                _write_atomic(self._batch_file, json.dumps(record, indent=2) + "\n")
            except OSError:
                log.error("the record of eval batch %s could not be written", name, exc_info=True)
                with contextlib.suppress(OSError, ValueError):
                    procs.kill_tree(proc)  # a batch nobody can see would run unwatched
                raise StartFailed("the eval batch could not be started") from None
            self._proc = proc
        return {"experiment": name, "total": total, "units": used, "skipped": skipped}

    def _record_hashes(self, experiment: str, used: list[str]) -> None:
        """Write ``<experiment>.meta.json``: the config hash of the harness files of each unit in the batch, as they are now.

        A failure is logged and the batch goes ahead without the record: none of its rows is then ever marked stale.
        """
        try:
            hashes = {
                variant_name(unit_id): structure.config_hash(self.units.get(unit_id).worktree, self.cfg.overlay)
                for unit_id in used
            }
            record = {"experiment": experiment, "launched_at": datetime.now().isoformat(timespec="seconds"), "hashes": hashes}
            _write_atomic(self.dir / f"{experiment}{META_SUFFIX}", json.dumps(record, indent=2) + "\n")
        except (OSError, KeyError):
            log.error("the harness hashes of eval batch %s could not be recorded: no row can be marked stale", experiment, exc_info=True)

    @staticmethod
    def _check_file(variants_file: Path) -> int:
        """Load the file as the batch will (so a bad file stops the launch, not the batch); the runs per ticket."""
        from harness.eval.variants import load_matrix

        try:
            matrix = load_matrix(variants_file)
        except (ValueError, OSError):
            log.error("the variants file %s does not load", variants_file.name, exc_info=True)
            raise EvalError("the variants file of this batch does not load (see the console log)") from None
        return sum(variant.repeats for variant in matrix.variants.values())

    def status(self) -> dict:
        """``{running, experiment, done, total, credits, exit, stopped}`` of the batch launched last.

        ``done`` is the number of lines in the experiment's ``.jsonl`` (never more than ``total``), ``credits`` what those
        runs cost, ``exit`` the exit code of the process once it has ended (``None`` while it runs, and after a restart of the
        console, which no longer holds the process), ``stopped`` whether the admin stopped it. Before any batch: ``running``
        false and the rest empty.
        """
        with self._lock:
            batch = self._read_batch()
            if batch is None:
                return dict(_IDLE)
            running = self._running(batch)
            code = None if running or self._proc is None else self._proc.poll()
        done, credits = self._progress(batch["experiment"])
        return {
            "running": running,
            "experiment": batch["experiment"],
            "done": min(done, batch["total"]),
            "total": batch["total"],
            "credits": round(credits, 4),
            "exit": code,
            "stopped": batch.get("stopped") is True,
        }

    def stop(self) -> dict:
        """Stop the batch that runs: kill its process tree, keep the results so far, remember it was stopped.

        Returns ``status()``. ``NotRunning`` when no batch runs (or it ended by itself first); ``StopFailed`` when the
        process could not be killed (or is the console's own). A process that now holds the recorded pid but started at
        another time is not ours: it is not touched, and counts as no batch.

        The lock is held only to read and to write the record, not while the process is killed and waited for (that can
        take seconds, and ``status`` and ``launch`` must not wait for it). So a batch launched meanwhile is possible; its
        record is left as it is.
        """
        with self._lock:
            batch = self._read_batch()
            if batch is None or not self._running(batch):
                raise NotRunning("no eval batch is running")
            proc = self._proc  # the process this console started, when it holds it
        target = proc if isinstance(proc, subprocess.Popen) else batch["pid"]  # a Popen can be reaped
        try:
            procs.kill_tree(target, batch.get("started"))
        except (ValueError, OSError):
            log.error("the eval batch %s could not be stopped", batch["experiment"], exc_info=True)
            raise StopFailed("the eval batch could not be stopped") from None
        deadline = time.monotonic() + STOP_WAIT_S
        while not self._gone(proc, batch) and time.monotonic() < deadline:
            time.sleep(0.05)
        if not self._gone(proc, batch):
            log.error("the eval batch %s is still running after it was killed", batch["experiment"])
            raise StopFailed("the eval batch could not be stopped")
        with self._lock:
            current = self._read_batch()
            if current is not None and current["experiment"] == batch["experiment"]:  # not a batch launched since
                try:
                    _write_atomic(self._batch_file, json.dumps({**current, "stopped": True}, indent=2) + "\n")
                except OSError:
                    log.error("the record of eval batch %s could not be updated", batch["experiment"], exc_info=True)
        return self.status()

    def archive_results(self) -> dict:
        """Move the results so far to ``archive/<UTC timestamp>/``: ``{archived: <folder name>, files: <how many>}``.

        A trial batch then does not mix into the day's leaderboard: ``results/`` is gone (the harness makes it again for
        the next batch) and so is ``batch.json`` (it goes into the archive folder beside the results it describes), so
        ``status`` is empty again. ``BatchRunning`` while a batch runs, ``NothingToArchive`` when ``results/`` is missing
        or empty; an ``OSError`` when the disk refuses (the results are put back when ``batch.json`` cannot follow them, so
        ``status`` never describes a batch whose results are gone). The lock a launch takes is held, so no batch starts between the
        look and the move. Folders are never overwritten: a name that is taken gets ``-2``,
        ``-3``, and so on.
        """
        with self._lock:
            batch = self._read_batch()
            if batch is not None and self._running(batch):
                raise BatchRunning("an eval batch is running")
            try:
                count = sum(1 for _ in self._results.iterdir())
            except FileNotFoundError:
                count = 0
            if count == 0:
                raise NothingToArchive("there are no results to archive")
            archive = self.dir / "archive"
            archive.mkdir(parents=True, exist_ok=True)
            stamp = _utc_stamp()
            target, attempt = archive / stamp, 1
            while target.exists():
                attempt += 1
                target = archive / f"{stamp}-{attempt}"
            os.rename(self._results, target)  # one step on one disk; refuses a folder that is there already
            try:
                os.replace(self._batch_file, target / "batch.json")
            except FileNotFoundError:
                pass
            except OSError:  # a status that says "0 of N" for a batch whose results are gone is worse than no archive
                try:
                    os.rename(target, self._results)
                except OSError:
                    log.error("the results are in archive/%s but batch.json is not: put them back", target.name, exc_info=True)
                raise
        log.info("eval results archived as %s (%d files)", target.name, count)
        return {"archived": target.name, "files": count}

    def _read_batch(self) -> dict | None:
        """The record of the batch launched last, or ``None`` (none, or one that does not look like a record)."""
        record = _read_json(self._batch_file, MAX_CARD_BYTES)
        if not isinstance(record, dict):
            return None
        pid, total = record.get("pid"), record.get("total")
        ok = (
            isinstance(record.get("experiment"), str)
            and EXPERIMENT.fullmatch(record["experiment"])
            and all(isinstance(n, int) and not isinstance(n, bool) for n in (pid, total))
            and total >= 0
            and isinstance(record.get("started"), (int, float, type(None)))
        )
        return record if ok else None

    def _running(self, batch: dict) -> bool:
        if self._proc is not None:
            return self._proc.poll() is None
        # After a restart: is that very process still there? Without a recorded start time a pid says nothing (it may be
        # anyone's by now), so the batch is taken as over: it is never trusted, never killed.
        started = batch.get("started")
        return started is not None and procs.alive(batch["pid"], started)

    @staticmethod
    def _gone(proc: Any, batch: dict) -> bool:
        """Whether the process of ``batch`` has ended: ``proc`` is what the console held of it (``None`` after a restart)."""
        if proc is not None:
            return proc.poll() is not None
        return not procs.alive(batch["pid"], batch.get("started"))

    def _progress(self, experiment: str) -> tuple[int, float]:
        """``(runs finished, their credits)`` from the lines of ``results/<experiment>.jsonl``."""
        data = _read_capped(self._results / f"{experiment}.jsonl", MAX_FILE_BYTES)
        done, credits = 0, 0.0
        for line in (data or b"").decode("utf-8", errors="replace").splitlines():
            if not line.strip():
                continue
            done += 1
            try:
                spent = json.loads(line).get("ai_credits")
            except (ValueError, AttributeError, RecursionError):
                continue
            if isinstance(spent, (int, float)) and not isinstance(spent, bool) and math.isfinite(spent):
                credits += spent
        return done, credits
