"""Save-time safety rules: what a unit may not put into the harness config (spec §6, rulings R12, R13, R15, R16).

The agent's shell and the harness run on the facilitator's laptop, so an edit that lets either run code of
the participant's choosing is refused when it is saved. The teaching knobs (models within the allowed list,
retry budgets, forbidden calls, prompts, workflows, removing a hook) stay free. ``check`` returns the reasons
as a list (empty: the save may go ahead); it is meant to be bound to the golden config and the allowed models
and handed to ``console.files.write`` as its ``check``.

The golden config is the harness config at ``start_ref`` in ``<data>/source``, read with ``git show`` (the
clone's own ``--git-dir``, never the working tree, never a folder git would look for). Rules:

* ``harness/harness.yaml``: ``runtime``, ``runtimes``, ``paths``, ``ticket.jira`` and four ``copilot`` keys
  equal the golden values; every role ``model`` (and ``escalation_model``, when set) is in the allowed list;
  role ``shell`` entries and ``tools`` come from the golden roles; ``policy.protected`` and ``shell_deny``
  keep every golden entry, ``protected_except``, ``git_allow`` and ``url_allow`` add none; ``mcp_read_only``
  stays true.
* ``harness/gates.yaml``: ``runner`` equals the golden one, a ``kind: python`` gate runs a golden function
  (the harness imports and calls it on the host), a gate's ``env`` names only variables some golden gate sets
  (today ``TS_FUZZ``) and has plain values: the harness puts a gate's ``env`` into the environment of
  ``scripts/check.sh``, which runs on the host and reads ``TS_IN_CONTAINER``, ``TS_IMAGE``, ``TS_ALLOW_PROTECTED``
  and more of these (R19).
* ``.github/hooks/*.json``: every ``bash`` / ``powershell`` command is a golden one, and the file only uses
  the golden top-level keys, event names, entry keys and entry types (``cwd``, ``env``, ``command``, an
  ``http`` type would all run or reach something the golden commands do not).
* Front matter of the Markdown files under ``.github/agents/`` and ``.github/skills/`` only uses
  ``name``, ``description``, ``tools`` and ``model``; ``model`` is in the allowed list.
* Any YAML or JSON file under ``harness/`` or ``.github/`` parses, and none of these files starts with a
  byte order mark (the editor view strips one and the save puts the file's own back, so a second one in the
  text would hide the first key from the harness's reader while this check still saw it).

Values are read the way the harness reads them, but never expanded: a list entry must be a plain value
(a YAML alias can share a nested list with itself, and ``str()`` of that is exponentially large), and every
value shown in a message is cut to 80 characters. Paths are matched with ``/`` separators and ignoring case.
"""

from __future__ import annotations

import json
import re
import subprocess
from dataclasses import dataclass
from pathlib import Path, PurePosixPath
from typing import Any, Iterable, Mapping, Sequence

from console import files, harnesslink, procs

__all__ = ["Golden", "check", "load_golden"]

_HARNESS_YAML = "harness/harness.yaml"
_GATES_YAML = "harness/gates.yaml"
_HOOKS_DIR = ".github/hooks"
_FRONT_MATTER_DIRS = (".github/agents/", ".github/skills/")
_FRONT_MATTER_KEYS = frozenset({"name", "description", "tools", "model"})
_HOOK_COMMAND_KEYS = frozenset({"bash", "powershell"})
_PLAIN = (str, int, float, bool, type(None))  # what a list entry may be; anything else is not expanded
_BOM = "\ufeff"
_MISSING = object()
_MAX_MESSAGE = 300
_MAX_SHOWN = 80

_FIXED_TOP = ("runtime", "runtimes", "paths")
_FIXED_COPILOT = ("cli_path", "env_passthrough", "expected_cli_version", "sdk_version")
# policy lists that may only grow (golden entries stay); the others may only shrink (no entry beyond golden)
_ADD_ONLY = ("protected", "shell_deny")
_POLICY_LISTS = ("protected", "protected_except", "shell_deny", "git_allow", "url_allow")


@dataclass(frozen=True)
class Golden:
    """The reference config the save rules compare against (see ``load_golden``).

    The ``hook_*`` sets are the top-level keys, event names, entry keys and entry types the golden hooks files use;
    ``gate_env_keys`` are the variables any golden gate sets in its ``env``.
    """

    harness: dict
    gates: dict
    hook_commands: frozenset[str]
    python_gate_fns: frozenset[str]
    hook_top_keys: frozenset[str] = frozenset()
    hook_events: frozenset[str] = frozenset()
    hook_entry_keys: frozenset[str] = frozenset()
    hook_types: frozenset[str] = frozenset()
    gate_env_keys: frozenset[str] = frozenset()


# --- golden ------------------------------------------------------------------------------------


def _git(source: Path, *args: str) -> subprocess.CompletedProcess[str]:
    """Git ``args`` on the repo ``source`` (its ``.git``, named), hardened as the console's other git calls: no hook,
    no fsmonitor, the environment of ``procs.git_env`` (no token of the console's), at most ``procs.GIT_TIMEOUT_S``."""
    head = ["git", "-c", f"core.hooksPath={files.empty_hooks_dir()}", "-c", "core.fsmonitor=false",
            f"--git-dir={Path(source) / '.git'}"]
    return procs.run_git(head, args, text=True)


def _show(source: Path, ref: str, path: str) -> str:
    done = _git(source, "show", f"{ref}:{path}")
    if done.returncode != 0:
        raise RuntimeError(f"golden {path} not found at {ref} in {source}")
    return done.stdout.removeprefix(_BOM)


def _hook_files(source: Path, ref: str) -> list[str]:
    """The ``*.json`` files directly in ``.github/hooks/`` at ``ref``."""
    done = _git(source, "ls-tree", "-z", ref, "--", f"{_HOOKS_DIR}/")
    if done.returncode != 0:
        raise RuntimeError(f"cannot list {_HOOKS_DIR} at {ref} in {source}: {done.stderr.strip()}")
    names = []
    for entry in filter(None, done.stdout.split("\0")):
        info, _, name = entry.partition("\t")
        if info.split(" ")[1:2] == ["blob"] and name.casefold().endswith(".json"):
            names.append(name)
    return names


def _commands(node: Any) -> list[Any]:
    """Every value under a ``bash`` or ``powershell`` key, however deep, in document order.

    Iterative: the input is untrusted and may be nested deeper than the interpreter recurses.
    """
    found: list[Any] = []
    stack: list[tuple[bool, Any]] = [(False, node)]  # (is a command value, value)
    while stack:
        is_command, item = stack.pop()
        if is_command:
            found.append(item)
        if isinstance(item, dict):
            stack.extend(
                (isinstance(key, str) and key.casefold() in _HOOK_COMMAND_KEYS, value)
                for key, value in reversed(item.items())
            )
        elif isinstance(item, list):
            stack.extend((False, value) for value in reversed(item))
    return found


def _hook_layout(data: Any) -> tuple[list[Any], list[Any], list[dict]]:
    """``(top-level keys, event names, entries)`` of a hooks file ``{"version": 1, "hooks": {event: [entry, ...]}}``.

    ``hooks`` may also be a plain list of entries and an event's value a single entry: whatever is a mapping
    where an entry belongs is one. A hook nested anywhere else needs a key the golden entries do not have.
    """
    if not isinstance(data, dict):
        return [], [], []
    hooks = data.get("hooks")
    events = list(hooks) if isinstance(hooks, dict) else []
    groups = list(hooks.values()) if isinstance(hooks, dict) else [hooks]
    entries = [
        entry
        for group in groups
        for entry in (group if isinstance(group, list) else [group])
        if isinstance(entry, dict)
    ]
    return list(data), events, entries


def _parse_golden_yaml(path: str, text: str) -> dict:
    try:
        data = harnesslink.yaml_load(text)
    except Exception as err:  # PyYAML and the mini loader raise different types
        raise RuntimeError(f"golden {path} is not valid YAML: {_one_line(err)}") from err
    return data if isinstance(data, dict) else {}


def load_golden(source: Path, ref: str, also: Sequence[str] = ()) -> Golden:
    """The golden config at ``ref`` of the repo ``source``: ``RuntimeError`` when a file is missing or unreadable.

    ``also`` are the catch-up refs: the facilitator's own states of the day, which a unit is reset to. What they
    add to the sets a save is checked against (python gate functions, gate settings, hook commands and layout) is
    allowed too, so a unit on a catch-up ref can save its files; the harness and gates themselves stay those of
    ``ref``. A catch-up ref the repo does not have is left out. Needs ``harnesslink.bootstrap`` first (the YAML is
    parsed by the harness's own loader).
    """
    for name in (ref, *also):
        if not files.is_plain_ref(name):
            raise ValueError(f"{name!r} is not a usable ref")
    harness = _parse_golden_yaml(_HARNESS_YAML, _show(source, ref, _HARNESS_YAML))
    gates = _parse_golden_yaml(_GATES_YAML, _show(source, ref, _GATES_YAML))
    commands: set[str] = set()
    top_keys: set[str] = set()
    events: set[str] = set()
    entry_keys: set[str] = set()
    types: set[str] = set()
    fns: set[str] = set()
    env_keys: set[str] = set()
    present = [ref] + [name for name in also if _git(source, "rev-parse", "--verify", "-q",
                                                     f"refs/heads/{name}^{{commit}}").returncode == 0]
    for at in present:
        for name in _hook_files(source, at):
            try:
                parsed = json.loads(_show(source, at, name))
            except ValueError as err:
                raise RuntimeError(f"golden {name} at {at} is not valid JSON: {_one_line(err)}") from err
            commands.update(c for c in _commands(parsed) if isinstance(c, str))
            top, hook_events, entries = _hook_layout(parsed)
            top_keys.update(top)
            events.update(hook_events)
            for entry in entries:
                entry_keys.update(k for k in entry if isinstance(k, str))
                if isinstance(entry.get("type"), str):
                    types.add(entry["type"])
        at_gates = gates if at == ref else _parse_golden_yaml(_GATES_YAML, _show(source, at, _GATES_YAML))
        fns.update(
            str(g["fn"])
            for g in _mapping(at_gates.get("gates")).values()
            if isinstance(g, Mapping) and "fn" in g and isinstance(g["fn"], _PLAIN) and _is_python_gate(g)
        )
        env_keys.update(
            str(key)
            for g in _mapping(at_gates.get("gates")).values()
            if isinstance(g, Mapping)
            for key in _mapping(g.get("env"))
            if isinstance(key, _PLAIN)
        )
    return Golden(
        harness, gates, frozenset(commands), frozenset(fns),
        frozenset(top_keys), frozenset(events), frozenset(entry_keys), frozenset(types), frozenset(env_keys),
    )


# --- reading values the way the harness does, without expanding them ---------------------------


def _mapping(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _is_python_gate(gate: Mapping) -> bool:
    kind = gate.get("kind", "check")
    return isinstance(kind, str) and kind == "python"


def _strings(items: Iterable[Any]) -> tuple[list[str], bool]:
    """``str`` of each plain item, and whether there were items that are not plain (those are left alone)."""
    items = list(items)
    plain = [str(item) for item in items if isinstance(item, _PLAIN)]
    return plain, len(plain) != len(items)


def _role_entries(role: Mapping, key: str) -> tuple[list[str], bool]:
    """A role's ``shell`` or ``tools`` as ``harness.config`` reads them: nothing is empty, a lone value is one entry."""
    value = role.get(key)
    return _strings([] if value is None else value if isinstance(value, list) else [value])


def _shown(value: Any) -> str:
    """``value`` for a message: cut to 80 characters; a container is named, not printed."""
    if not isinstance(value, _PLAIN):
        return f"<{type(value).__name__}>"
    text = str(value)
    return text if len(text) <= _MAX_SHOWN else text[:_MAX_SHOWN] + "\u2026"


def _shown_list(values: Sequence[str]) -> str:
    return ", ".join(_shown(value) for value in values)


def _one_line(err: BaseException) -> str:
    text = " ".join(str(err).split()) or type(err).__name__
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 3] + "..."


def _unique(items: Sequence[str]) -> list[str]:
    return list(dict.fromkeys(items))


# --- the rules ---------------------------------------------------------------------------------


def _roles(data: Mapping) -> list[tuple[str, Mapping]]:
    return [(_shown(name), role) for name, role in _mapping(data.get("roles")).items() if isinstance(role, Mapping)]


def _golden_union(gold: Mapping, key: str) -> set[str]:
    return {entry for _, role in _roles(gold) for entry in _role_entries(role, key)[0]}


def _is_allowed_model(model: Any, allowed: Sequence[str]) -> bool:
    return isinstance(model, _PLAIN) and str(model) in allowed


def _harness_errors(data: Any, golden: Golden, allowed_models: Sequence[str]) -> list[str]:
    new, gold = _mapping(data), _mapping(golden.harness)
    errors: list[str] = []

    def fixed(key: str, now: Any, then: Any) -> None:
        if now != then:
            errors.append(f"harness.yaml: {key} is fixed (runs on the facilitator's machine)")

    for key in _FIXED_TOP:
        fixed(key, new.get(key, _MISSING), gold.get(key, _MISSING))
    new_copilot, gold_copilot = _mapping(new.get("copilot")), _mapping(gold.get("copilot"))
    for key in _FIXED_COPILOT:
        fixed(f"copilot.{key}", new_copilot.get(key, _MISSING), gold_copilot.get(key, _MISSING))
    fixed(
        "ticket.jira",
        _mapping(new.get("ticket")).get("jira", _MISSING),
        _mapping(gold.get("ticket")).get("jira", _MISSING),
    )

    roles = _roles(new)
    allowed = list(allowed_models)
    for name, role in roles:
        # a role needs a model; only the escalation model may be left out
        for field, optional in (("model", False), ("escalation_model", True)):
            model = role.get(field)
            if (model is None and optional) or _is_allowed_model(model, allowed):
                continue
            errors.append(
                f"harness.yaml: role {name} model {_shown(model)} is not allowed (allowed: {', '.join(allowed)})"
            )

    # R12: what the agent's shell and tool list can reach
    shells, tools = _golden_union(gold, "shell"), _golden_union(gold, "tools")
    for key, golden_entries, word in (("shell", shells, "shell entry"), ("tools", tools, "tool")):
        for name, role in roles:
            entries, odd = _role_entries(role, key)
            if odd:
                errors.append(f"harness.yaml: role {name} {key} entry must be a plain value")
            errors += [
                f"harness.yaml: role {name} {word} not allowed: {_shown(entry)}"
                for entry in _unique(entries)
                if entry not in golden_entries
            ]

    new_policy, gold_policy = _mapping(new.get("policy")), _mapping(gold.get("policy"))
    for key in _POLICY_LISTS:
        value = new_policy.get(key)
        if value is not None and not isinstance(value, list):
            errors.append(f"harness.yaml: policy.{key} must be a list")
            continue
        now, odd = _strings(value or [])
        if odd:
            errors.append(f"harness.yaml: policy.{key} entry must be a plain value")
        golden_value = gold_policy.get(key)
        then, _ = _strings(golden_value if isinstance(golden_value, list) else [])
        if key in _ADD_ONLY:
            removed = _unique([entry for entry in then if entry not in now])
            if removed:
                errors.append(f"harness.yaml: policy.{key} may only add entries (removed: {_shown_list(removed)})")
        else:
            added = _unique([entry for entry in now if entry not in then])
            if added:
                errors.append(f"harness.yaml: policy.{key} may only remove entries (added: {_shown_list(added)})")
    if new_policy.get("mcp_read_only") is not True:
        errors.append("harness.yaml: policy.mcp_read_only must stay true")
    return _unique(errors)


def _gates_errors(data: Any, golden: Golden) -> list[str]:
    new = _mapping(data)
    errors: list[str] = []
    if new.get("runner", _MISSING) != _mapping(golden.gates).get("runner", _MISSING):
        errors.append("gates.yaml: runner is fixed")
    gates = [(_shown(gid), gate) for gid, gate in _mapping(new.get("gates")).items() if isinstance(gate, Mapping)]
    for gid, gate in gates:
        fn = gate.get("fn", _MISSING)
        if fn is _MISSING:
            continue  # the harness refuses a gate without one
        if not isinstance(fn, _PLAIN):
            errors.append(f"gates.yaml: {gid} fn must be a plain value")
        elif _is_python_gate(gate) and str(fn) not in golden.python_gate_fns:
            errors.append(f"gates.yaml: {gid} fn {_shown(fn)} is not a known python gate")
    for gid, gate in gates:
        for key, value in _mapping(gate.get("env")).items():
            if not isinstance(key, _PLAIN):
                errors.append(f"gates.yaml: {gid} env key must be a plain value")
            elif str(key) not in golden.gate_env_keys:
                errors.append(f"gates.yaml: {gid} env {_shown(key)} is not allowed")
            elif not isinstance(value, _PLAIN):
                errors.append(f"gates.yaml: {gid} env {_shown(key)} must be a plain value")
    return _unique(errors)


def _hook_errors(shown: str, data: Any, golden: Golden) -> list[str]:
    top, events, entries = _hook_layout(data)
    errors = [f"{shown}: hook key not allowed: {_shown(key)}" for key in top if key not in golden.hook_top_keys]
    errors += [
        f"{shown}: hook event not allowed: {_shown(event)}" for event in events if event not in golden.hook_events
    ]
    errors += [
        f"{shown}: hook key not allowed: {_shown(key)}"
        for entry in entries
        for key in entry
        if key not in golden.hook_entry_keys
    ]
    errors += [
        f"{shown}: hook type not allowed: {_shown(entry['type'])}"
        for entry in entries
        if "type" in entry and not (isinstance(entry["type"], str) and entry["type"] in golden.hook_types)
    ]
    errors += [
        f"{shown}: hook command not allowed: {_shown(command)}"
        for command in _commands(data)
        if not (isinstance(command, str) and command in golden.hook_commands)
    ]
    return _unique(errors)


def _front_matter_errors(shown: str, content: str, allowed_models: Sequence[str]) -> list[str]:
    """The YAML between the first two ``---`` lines of a Markdown file; a file without any is not checked."""
    lines = re.split(r"\r?\n", content)
    if lines[0].rstrip() != "---":
        return []
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), None)
    if end is None:
        return [f"{shown}: not valid YAML/JSON: front matter is not closed"]
    try:
        data = harnesslink.yaml_load("\n".join(lines[1:end]))
    except Exception as err:  # PyYAML and the mini loader raise different types
        return [f"{shown}: not valid YAML/JSON: {_one_line(err)}"]
    if data is None:
        return []
    if not isinstance(data, Mapping):
        return [f"{shown}: not valid YAML/JSON: front matter is not a mapping"]
    errors = [
        f"{shown}: front matter key not allowed: {_shown(key)}"
        for key in data
        if not (isinstance(key, str) and key in _FRONT_MATTER_KEYS)
    ]
    if "model" in data and not _is_allowed_model(data["model"], allowed_models):
        errors.append(
            f"{shown}: front matter model {_shown(data['model'])} is not allowed "
            f"(allowed: {', '.join(allowed_models)})"
        )
    return _unique(errors)


def check(rel: str, content: str, golden: Golden, allowed_models: Sequence[str]) -> list[str]:
    """The reasons ``content`` may not be saved as ``rel`` (a path in the unit's files); ``[]`` when it may.

    Anything that is not a YAML or JSON file under ``harness/`` or ``.github/``, or a Markdown file under
    ``.github/agents/`` or ``.github/skills/``, gives ``[]``.
    """
    shown = "/".join(part for part in re.split(r"[\\/]+", rel) if part not in ("", "."))
    path = shown.casefold()
    suffix = PurePosixPath(path).suffix
    structured = suffix in (".yaml", ".yml", ".json") and path.startswith(("harness/", ".github/"))
    markdown = suffix == ".md" and path.startswith(_FRONT_MATTER_DIRS)
    if not structured and not markdown:
        return []
    if content.startswith(_BOM):
        return [f"{shown}: not valid YAML/JSON: byte order mark"]
    if markdown:
        return _front_matter_errors(shown, content, allowed_models)
    try:
        data = json.loads(content) if suffix == ".json" else harnesslink.yaml_load(content)
    except Exception as err:  # PyYAML, the mini loader and json raise different types (and RecursionError)
        return [f"{shown}: not valid YAML/JSON: {_one_line(err)}"]
    if path == _HARNESS_YAML:
        return _harness_errors(data, golden, allowed_models)
    if path == _GATES_YAML:
        return _gates_errors(data, golden)
    if suffix == ".json" and PurePosixPath(path).parent.as_posix() == _HOOKS_DIR:
        return _hook_errors(shown, data, golden)
    return []
