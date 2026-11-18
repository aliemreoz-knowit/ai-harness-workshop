"""The harness of a unit as structure: roles, skills, hooks, gates, workflows, policy, instructions and loop.

``read(worktree)`` parses the unit's harness files into the model the Harness view shows. ``config_hash`` names the
overlay files as they are now, ``changed`` lists the ones that differ from ``start_ref``, and ``overlay_errors`` runs
the save rules over all of them (the validate endpoint).

Every file is read through ``console.files`` (the path guard, links and size limits are its), and the ``start_ref``
side through ``git`` (``UnitStore.git_for``); nothing opens a path of the worktree itself.

``read`` never raises on a broken file. The sections that come out of one file (``roles``, ``policy`` and ``loop`` out
of ``harness.yaml``; ``gates`` out of ``gates.yaml``) are ``{"error": "<file>: <why>"}`` instead of their items when
that file does not parse, so the Harness view can show the broken file and still draw the rest. The sections made of
several files (``skills``, ``hooks``, ``workflows``) give each broken file one item of the usual shape with empty
fields, its ``file`` and an ``error``; the other files are listed as usual. File refs are paths relative to the
worktree with ``/``. Values the harness reads with a default are shown with that default (``loop``), and workflows
are named by their file, the name ``python -m harness run --workflow`` takes. A hook row has its ``bash`` and
``powershell`` commands and its ``timeout_s`` (``timeoutSec``) as well as ``command`` (the one that is shown), and
``cases``: the regression cases of .github/config-tests/cases.json for its event (``[]`` without that file); a gate
that sets ``env`` has it.

Line endings and a byte order mark do not count: the hash and ``changed`` look at the text as the editor shows it
(``\\n`` line ends, no BOM), so a CRLF checkout of the same config is the same config.

``read_at`` reads the same model from the files at a ref through git (the start harness), ``golden`` does that once
per commit, and ``weakening`` compares a unit's structure with it: the guardrails the unit gave up, as flags
``{rule, detail, file}`` (the Harness view shows them, and a run records them when it starts).
"""

from __future__ import annotations

import hashlib
import json
import math
import re
import threading
from pathlib import Path
from typing import Any, Callable, Mapping, Sequence

from console import files, harnesslink

__all__ = ["changed", "config_hash", "golden", "overlay_errors", "read", "read_at", "weakening"]

_HARNESS = "harness/harness.yaml"
_GATES = "harness/gates.yaml"
_WORKFLOWS = "harness/workflows"
_ROLE_PROMPTS = "harness/prompts/roles"
_AGENTS = ".github/agents"
_SKILLS = ".github/skills"
_HOOKS = ".github/hooks"
_HOOK_CASES = ".github/config-tests/cases.json"  # the hooks' regression cases (run_cases.py)
_CASE_EVENTS = {"pre": "preToolUse", "post": "postToolUse"}  # a case's mode: the event its hook runs on
_INSTRUCTIONS = ".github/instructions"
_COPILOT_INSTRUCTIONS = "copilot-instructions.md"  # in .github
_AGENTS_MD = "AGENTS.md"

_DESCRIPTION_MAX = 200
_MAX_MESSAGE = 300
_PLAIN = (str, int, float, bool, type(None))  # what is shown as its text; a container is only named
_UNREADABLE = b"\xff"  # stands for a file that cannot be read in the hash: never the UTF-8 of any text


class _Broken(Exception):
    """A file cannot be shown. The text is the message of the view: ``<file>: <why>``."""


# --- reading one file ---------------------------------------------------------------------------------------


def _one_line(err: BaseException) -> str:
    text = " ".join(str(err).split()) or type(err).__name__
    return text if len(text) <= _MAX_MESSAGE else text[: _MAX_MESSAGE - 3] + "..."


class _AtRef:
    """The harness files at a ref of the unit's repo, read through git: stands in for a worktree folder in ``_names``
    and ``_text``, so the start harness is read by the code that reads a unit's (and nothing is written to a disk)."""

    def __init__(self, git: Callable[..., str], ref: str) -> None:
        self.git, self.ref = git, ref
        self.paths = _tree_blobs(git, ref, "harness", ".github")

    def names(self, rel: str, dirs: bool) -> list[str]:
        prefix = f"{rel}/" if rel else ""
        found = set()
        for path in self.paths:
            if path.startswith(prefix):
                name, slash, _ = path[len(prefix) :].partition("/")
                if bool(slash) == dirs:
                    found.add(name)
        return sorted(found)

    def text(self, rel: str) -> str:
        if rel not in self.paths:
            raise _Broken(f"{rel}: not found")
        try:
            return _normal(self.git("show", f"{self.ref}:{rel}"))
        except RuntimeError:
            raise _Broken(f"{rel}: cannot be read") from None


def _names(worktree: "Path | _AtRef", rel: str, *, dirs: bool = False) -> list[str]:
    """The names of the files (or with ``dirs`` the folders) in folder ``rel``; none when it is not there."""
    if isinstance(worktree, _AtRef):
        return worktree.names(rel, dirs)
    try:
        entries = files.tree(worktree, rel, ())
    except (files.Forbidden, OSError):
        return []
    return [e["name"] for e in entries if e["dir"] == dirs]


def _text(worktree: "Path | _AtRef", rel: str) -> str:
    """The text of file ``rel`` (``\\n`` line ends); ``_Broken`` when it is missing, binary, too large
    or not readable."""
    if isinstance(worktree, _AtRef):
        return worktree.text(rel)
    try:
        view = files.read(worktree, rel, (), max_bytes=files.MAX_BYTES)
    except FileNotFoundError:
        raise _Broken(f"{rel}: not found") from None
    except files.Invalid:
        raise _Broken(f"{rel}: larger than {files.MAX_BYTES // 1024} KB") from None
    except (files.Forbidden, OSError):
        raise _Broken(f"{rel}: cannot be read") from None
    if view.content is None:
        raise _Broken(f"{rel}: not a text file")
    return view.content


def _yaml_mapping(worktree: "Path | _AtRef", rel: str) -> Mapping:
    text = _text(worktree, rel)
    try:
        data = harnesslink.yaml_load(text)
    except Exception as err:  # PyYAML and the mini loader raise different types (and RecursionError)
        raise _Broken(f"{rel}: {_one_line(err)}") from None
    if not isinstance(data, Mapping):
        raise _Broken(f"{rel}: expected a mapping")
    return data


def _json(rel: str, text: str) -> Any:
    try:
        return json.loads(text)
    except (ValueError, RecursionError) as err:
        raise _Broken(f"{rel}: not valid JSON: {_one_line(err)}") from None


def _front_matter(rel: str, text: str) -> Mapping:
    """The YAML between the first two ``---`` lines of a Markdown file; ``{}`` for a file without any."""
    lines = text.split("\n")
    if lines[0].rstrip() != "---":
        return {}
    end = next((i for i, line in enumerate(lines[1:], 1) if line.rstrip() == "---"), None)
    if end is None:
        raise _Broken(f"{rel}: front matter is not closed")
    try:
        data = harnesslink.yaml_load("\n".join(lines[1:end]))
    except Exception as err:  # as in _yaml_mapping
        raise _Broken(f"{rel}: {_one_line(err)}") from None
    if data is None:
        return {}
    if not isinstance(data, Mapping):
        raise _Broken(f"{rel}: front matter is not a mapping")
    return data


# --- values -------------------------------------------------------------------------------------------------
#
# Values come from files a unit wrote: only plain values are turned into text (a YAML alias can share a nested list
# with itself, and ``str()`` of that is exponentially large); a container is named, never expanded.


def _plain(value: Any) -> str:
    try:
        return str(value) if isinstance(value, _PLAIN) else f"<{type(value).__name__}>"
    except ValueError:  # an integer of thousands of digits
        return f"<{type(value).__name__}>"


def _listed(value: Any) -> list:
    """A list the way the harness reads it: nothing is empty, a lone value is one entry."""
    return [] if value is None else value if isinstance(value, list) else [value]


def _texts(value: Any) -> list[str]:
    return [_plain(item) for item in _listed(value) if item is not None]


def _optional(value: Any) -> str | None:
    return None if value is None else _plain(value)


def _setting(mapping: Mapping, key: str, default: Any) -> Any:
    """The value of ``key`` (text, a flag or a number as it is, anything else as text), or ``default``."""
    value = mapping.get(key)
    if value is None:
        return default
    if isinstance(value, (str, bool)) or (isinstance(value, int) and abs(value) < 2**53):
        return value
    return value if isinstance(value, float) and math.isfinite(value) else _plain(value)


def _sub_mapping(data: Mapping, key: str, rel: str) -> Mapping:
    value = data.get(key)
    if value is None:
        return {}
    if not isinstance(value, Mapping):
        raise _Broken(f"{rel}: {key} must be a mapping")
    return value


# --- the sections ------------------------------------------------------------------------------------------


def _roles(worktree: "Path | _AtRef", data: Mapping) -> list[dict]:
    roles = _sub_mapping(data, "roles", _HARNESS)
    prompts, agents = set(_names(worktree, _ROLE_PROMPTS)), set(_names(worktree, _AGENTS))
    out = []
    for key, role in roles.items():
        name = _plain(key)
        if not isinstance(role, Mapping):
            raise _Broken(f"{_HARNESS}: role {name} must be a mapping")
        out.append(
            {
                "name": name,
                "model": _plain(role.get("model") or ""),
                "escalation_model": _optional(role.get("escalation_model")),
                "tools": _texts(role.get("tools")),
                "writes": _texts(role.get("writes")),
                "shell": _texts(role.get("shell")),
                "prompt_file": f"{_ROLE_PROMPTS}/{name}.md" if f"{name}.md" in prompts else None,
                "agent_file": f"{_AGENTS}/{name}.agent.md" if f"{name}.agent.md" in agents else None,
            }
        )
    return out


def _policy(data: Mapping) -> dict:
    policy = _sub_mapping(data, "policy", _HARNESS)
    forbidden = _sub_mapping(policy, "forbidden_calls", _HARNESS)
    return {
        "protected": _texts(policy.get("protected")),
        "protected_except": _texts(policy.get("protected_except")),
        "shell_deny": _texts(policy.get("shell_deny")),
        "git_allow": _texts(policy.get("git_allow")),
        "url_allow": _texts(policy.get("url_allow")),
        "forbidden_calls": {_plain(glob): _texts(calls) for glob, calls in forbidden.items()},
        "mcp_read_only": policy.get("mcp_read_only") is True,
    }


def _loop(data: Mapping) -> dict:
    loop = _sub_mapping(data, "loop", _HARNESS)
    baseline = loop.get("baseline")
    return {  # the defaults are the harness's own (orchestrator.py, config.py)
        "retry_budget": _setting(loop, "retry_budget", 3),
        "review_rounds": _setting(loop, "review_rounds", 2),
        "ladder_rounds": _setting(loop, "ladder_rounds", 2),
        "baseline": "off" if baseline is False else _plain(baseline or "auto").lower(),
    }


def _gates(data: Mapping) -> dict:
    gates = {}
    for key, gate in _sub_mapping(data, "gates", _GATES).items():
        gid = harnesslink.names().gate(_plain(key))  # old codes (build) read as names
        if not isinstance(gate, Mapping):
            raise _Broken(f"{_GATES}: gate {gid} must be a mapping")
        gates[gid] = {
            "name": _plain(gate.get("name", key)),
            "kind": _plain(gate.get("kind", "check")),
            "fn": _plain(gate.get("fn") or ""),
            "timeout_s": _setting(gate, "timeout_s", 300),
        }
        if isinstance(gate.get("env"), Mapping) and gate["env"]:  # only a gate that sets some (TS_FUZZ of the fuzz smoke)
            gates[gid]["env"] = {_plain(name): _plain(value) for name, value in gate["env"].items()}
    profiles = {
        _plain(name): [harnesslink.names().gate(gid) for gid in _texts(ids)]
        for name, ids in _sub_mapping(data, "profiles", _GATES).items()
    }
    return {"gates": gates, "profiles": profiles}


def _workflow(worktree: "Path | _AtRef", name: str, rel: str) -> dict:
    data = _yaml_mapping(worktree, rel)
    steps = []
    for raw in _listed(data.get("steps")):
        if not isinstance(raw, Mapping) or "id" not in raw or "kind" not in raw:
            raise _Broken(f"{rel}: every step needs 'id' and 'kind'")
        kind = _plain(raw["kind"])
        step = {
            "id": _plain(raw["id"]),
            "kind": kind,
            "role": _optional(raw.get("role")),
            "approval": _plain(raw.get("approval", "human" if kind == "approval" else "auto")),
            "gates": _optional(raw.get("gates")),
            "on_fail": _plain(raw.get("on_fail", "fail")),
            "on_reject": _optional(raw.get("on_reject")),
        }
        for limit in ("retry_budget", "max_rounds"):  # only a step that sets its own has them (else the loop's count)
            if raw.get(limit) is not None:
                step[limit] = _setting(raw, limit, None)
        steps.append(step)
    if not steps:
        raise _Broken(f"{rel}: workflow has no steps")
    return {"name": name, "description": _plain(data.get("description") or ""), "file": rel, "steps": steps}


def _workflows(worktree: "Path | _AtRef") -> list[dict]:
    out = []
    for filename in _names(worktree, _WORKFLOWS):
        if not filename.endswith(".yaml"):  # the harness lists *.yaml
            continue
        name, rel = filename[: -len(".yaml")], f"{_WORKFLOWS}/{filename}"
        try:
            out.append(_workflow(worktree, name, rel))
        except _Broken as err:
            out.append({"name": name, "description": "", "file": rel, "steps": [], "error": str(err)})
    return out


def _hook_rows(worktree: "Path | _AtRef", rel: str) -> list[dict]:
    data = _json(rel, _text(worktree, rel))
    if not isinstance(data, Mapping):
        raise _Broken(f"{rel}: expected a mapping")
    hooks = data.get("hooks")
    if hooks is None:
        return []
    if not isinstance(hooks, Mapping):
        raise _Broken(f"{rel}: hooks must be a mapping of events")
    rows = []
    for event, group in hooks.items():
        for entry in group if isinstance(group, list) else [group]:
            if not isinstance(entry, Mapping):
                raise _Broken(f"{rel}: every hook of {_plain(event)} must be a mapping")
            rows.append(
                {
                    "event": _plain(event),
                    "matcher": _plain(entry.get("matcher") or ""),
                    "command": _plain(entry.get("bash") or entry.get("powershell") or ""),
                    "bash": _plain(entry.get("bash") or ""),
                    "powershell": _plain(entry.get("powershell") or ""),
                    "timeout_s": _setting(entry, "timeoutSec", None),
                    "file": rel,
                }
            )
    return rows


def _hook_cases(worktree: "Path | _AtRef") -> dict[str, list[dict]]:
    """The regression cases of the hooks by event, ``{event: [{"name", "expect"}]}`` in file order; ``{}`` without
    a readable cases file. ``expect`` is the case's: deny, none (no answer), context or flagged."""
    try:
        data = _json(_HOOK_CASES, _text(worktree, _HOOK_CASES))
    except _Broken:
        return {}
    cases = data.get("cases") if isinstance(data, Mapping) else None
    out: dict[str, list[dict]] = {}
    for case in cases if isinstance(cases, list) else []:
        if not isinstance(case, Mapping):
            continue
        event, name = _CASE_EVENTS.get(str(case.get("mode") or "")), _plain(case.get("name") or "")
        if event and name:
            out.setdefault(event, []).append({"name": name, "expect": _plain(case.get("expect") or "")})
    return out


def _hooks(worktree: "Path | _AtRef") -> list[dict]:
    out = []
    for filename in _names(worktree, _HOOKS):
        if not filename.lower().endswith(".json"):
            continue
        rel = f"{_HOOKS}/{filename}"
        try:
            out.extend(_hook_rows(worktree, rel))
        except _Broken as err:
            out.append({"event": "", "matcher": "", "command": "", "file": rel, "error": str(err)})
    cases = _hook_cases(worktree) if out else {}
    for row in out:
        if "error" not in row:
            row["cases"] = cases.get(row["event"], [])
    return out


def _skills(worktree: Path) -> list[dict]:
    out = []
    for folder in _names(worktree, _SKILLS, dirs=True):
        if "SKILL.md" not in _names(worktree, f"{_SKILLS}/{folder}"):
            continue
        rel = f"{_SKILLS}/{folder}/SKILL.md"
        try:
            meta = _front_matter(rel, _text(worktree, rel))
        except _Broken as err:
            out.append({"name": folder, "file": rel, "description": "", "error": str(err)})
            continue
        description = " ".join(_plain(meta.get("description") or "").split())[:_DESCRIPTION_MAX]
        out.append({"name": _plain(meta.get("name") or folder), "file": rel, "description": description})
    return out


def _instructions(worktree: Path) -> list[str]:
    found = [_AGENTS_MD] if _AGENTS_MD in _names(worktree, "") else []
    if _COPILOT_INSTRUCTIONS in _names(worktree, ".github"):
        found.append(f".github/{_COPILOT_INSTRUCTIONS}")
    return found + _matching(worktree, [f"{_INSTRUCTIONS}/**"], _INSTRUCTIONS)


def _load(worktree: "Path | _AtRef", rel: str) -> tuple[Mapping, str]:
    """``(data, "")`` of a YAML file, or ``({}, "<file>: <why>")`` when it cannot be shown."""
    try:
        return _yaml_mapping(worktree, rel), ""
    except _Broken as err:
        return {}, str(err)


def _section(source: tuple[Mapping, str], build: Callable[[Mapping], Any]) -> Any:
    """What ``build`` makes of the data of a file, or ``{"error": ...}`` when the file (or its shape) is broken."""
    data, error = source
    try:
        if error:
            raise _Broken(error)
        return build(data)
    except _Broken as err:
        return {"error": str(err)}


def _core(source: "Path | _AtRef") -> dict:
    """The sections that come out of ``harness.yaml``, ``gates.yaml``, the workflows and the hooks."""
    harness, gates = _load(source, _HARNESS), _load(source, _GATES)
    return {
        "roles": _section(harness, lambda data: _roles(source, data)),
        "hooks": _hooks(source),
        "gates": _section(gates, _gates),
        "workflows": _workflows(source),
        "policy": _section(harness, _policy),
        "loop": _section(harness, _loop),
    }


def read(worktree: Path) -> dict:
    """The structure of the harness in ``worktree``: ``roles``, ``skills``, ``hooks``, ``gates``, ``workflows``,
    ``policy``, ``instructions`` and ``loop`` (see the module docstring for the shapes and for broken files)."""
    worktree = Path(worktree)
    return {**_core(worktree), "skills": _skills(worktree), "instructions": _instructions(worktree)}


def read_at(git: Callable[..., str], ref: str) -> dict:
    """The structure of the harness as it is at ``ref``, read through ``git`` (``UnitStore.git_for``): ``read``'s shape,
    with no ``skills`` and ``instructions`` (nothing that ``weakening`` looks at). ``ValueError`` for a ref that is not
    a plain ref name, ``RuntimeError`` when git cannot list it."""
    if not files.is_plain_ref(ref):
        raise ValueError(f"{ref!r} is not a usable ref")
    return {**_core(_AtRef(git, ref)), "skills": [], "instructions": []}


_golden: dict[str, dict] = {}  # commit -> the structure at it
_golden_guard = threading.Lock()
_GOLDEN_KEPT = 4  # commits remembered: the start ref is one; a moved ref adds one


def golden(git: Callable[..., str], ref: str) -> dict:
    """``read_at`` of ``ref``, read once per commit it names (so a ref that moves is read again). What is read is that
    commit, by its id, whatever the ref does meanwhile: the key and the content cannot differ. The same dict goes to
    every caller: do not change it. ``ValueError`` for a ref that is not a plain ref name, ``RuntimeError`` from git."""
    if not files.is_plain_ref(ref):
        raise ValueError(f"{ref!r} is not a usable ref")
    commit = git("rev-parse", "--verify", "-q", f"{ref}^{{commit}}").strip()
    with _golden_guard:
        found = _golden.get(commit)
    if found is None:
        found = read_at(git, commit)  # the commit, not the ref: a ref that moves now is not what the key says
        with _golden_guard:
            if len(_golden) >= _GOLDEN_KEPT:
                _golden.pop(next(iter(_golden)))
            found = _golden.setdefault(commit, found)
    return found


# --- the overlay: hash, changes, save rules ----------------------------------------------------------------


def _normal(text: str) -> str:
    """``text`` as the editor shows it: no byte order mark, ``\\n`` line ends."""
    return text.removeprefix("\ufeff").replace("\r\n", "\n").replace("\r", "\n")


def _matching(worktree: Path, globs: Sequence[str], start: str = "") -> list[str]:
    """The files below folder ``start`` that match a glob, sorted (through ``files.tree``: no links or run output)."""
    found: list[str] = []
    pending = [start]
    while pending:
        try:
            entries = files.tree(worktree, pending.pop(), globs)
        except (files.Forbidden, OSError):
            continue
        for entry in entries:
            if entry["editable"]:  # for a folder: something below may match; for a file: it does
                (pending if entry["dir"] else found).append(entry["path"])
    return sorted(found)


def _view(worktree: Path, rel: str) -> files.FileView | None:
    """The file, or ``None`` when it cannot be read (missing, a link, over 200 KB)."""
    try:
        return files.read(worktree, rel, (), max_bytes=files.MAX_BYTES)
    except (files.Invalid, files.Forbidden, OSError):
        return None


def config_hash(worktree: Path, overlay: Sequence[str]) -> str:
    """SHA-256 over the overlay files (``overlay`` globs) in sorted path order: each path, then the length and the
    text of the file (line ends and a BOM do not count; a binary file counts by its git blob id)."""
    worktree, digest = Path(worktree), hashlib.sha256()
    for rel in _matching(worktree, overlay):
        view = _view(worktree, rel)
        if view is None:
            data = _UNREADABLE
        elif view.content is None:
            data = b"binary:" + view.sha.encode("ascii")
        else:
            data = _normal(view.content).encode("utf-8")
        digest.update(rel.encode("utf-8") + b"\0" + len(data).to_bytes(8, "big") + data)
    return digest.hexdigest()


def _tree_blobs(git: Callable[..., str], ref: str, *paths: str) -> dict[str, str]:
    """``{path: blob id}`` of the files at ``ref`` (only below ``paths`` when given; no links)."""
    found = {}
    for entry in git("ls-tree", "-r", "-z", ref, *(["--", *paths] if paths else [])).split("\0"):
        info, _, path = entry.partition("\t")
        mode, _, rest = info.partition(" ")
        kind, _, blob = rest.partition(" ")
        if path and kind == "blob" and mode != "120000":
            found[path] = blob
    return found


def _blobs_at(git: Callable[..., str], ref: str, overlay: Sequence[str]) -> dict[str, str]:
    """``{path: blob id}`` of the overlay files at ``ref``."""
    return {path: blob for path, blob in _tree_blobs(git, ref).items() if files.is_editable(path, overlay)[0]}


def changed(worktree: Path, start_ref: str, overlay: Sequence[str], git: Callable[..., str]) -> list[str]:
    """The overlay files that differ from ``start_ref`` (edited, added or removed), sorted.

    ``git`` runs git on the unit's worktree and returns stdout (``UnitStore.git_for(unit_id)``). The files as they are
    now are compared with the ones at ``start_ref`` by blob id; ones whose ids differ are compared as text, so a
    different line ending alone is no change. ``ValueError`` for a ref that is not a plain ref name.
    """
    if not files.is_plain_ref(start_ref):
        raise ValueError(f"{start_ref!r} is not a usable ref")
    worktree = Path(worktree)
    base = _blobs_at(git, start_ref, overlay)
    now = _matching(worktree, overlay)
    out = []
    for rel in sorted(set(base) | set(now)):
        view = _view(worktree, rel) if rel in base and rel in now else None
        if view is not None and view.sha == base[rel]:
            continue
        if view is not None and view.content is not None:  # not the same bytes: perhaps only other line ends
            if _normal(view.content) == _normal(git("show", f"{start_ref}:{rel}")):
                continue
        out.append(rel)
    return out


def overlay_errors(worktree: Path, overlay: Sequence[str], check: Callable[[str, str], list[str]]) -> list[str]:
    """What the save rules (``check(path, text)``, e.g. ``App.check``) say about every overlay file, file by file."""
    worktree, errors = Path(worktree), []
    for rel in _matching(worktree, overlay):
        view = _view(worktree, rel)
        if view is None:
            errors.append(f"{rel}: cannot be read")
        elif view.content is not None:  # a binary file has nothing to check
            errors += check(rel, view.content)
    return list(dict.fromkeys(errors))


# --- weakening: what a unit gave up -----------------------------------------------------------------------------
#
# A flag is ``{rule, detail, file}``: ``rule`` names the check, ``detail`` says it in a plain sentence (no host path;
# the harness files are named relative to the worktree) and ``file`` is the harness file to look at. A flag asks a
# question ("on purpose?"), it does not forbid anything: the save rules (``validate``) do that, and workflows are the
# part of the harness they leave free. A section that did not parse (``{"error": ...}``) cannot be judged, on either
# side, and gives no flag.

_WATCHED_PROFILES = ("ladder", "fast")  # the gate profiles a run goes through
_RETRY_LIMIT = 5  # retries per step above this are flagged
_TIMEOUT_FACTOR = 2  # a gate timeout above this many times the start harness's is flagged
_FLAG_LISTED = 4  # entries a flag names before it counts the rest
_FLAG_ITEM_MAX = 60
_FLAG_MAX = 300
_FUZZ = "TS_FUZZ"  # the variable that turns the fuzz smoke of gate fuzz on (gates.yaml)
_WILDCARDS = "*?"  # [ is no wildcard to the harness
_GATING = ("agent_gated", "gates")  # the kinds of step that run gates


def _flag(rule: str, detail: str, file: str) -> dict:
    return {"rule": rule, "detail": " ".join(detail.split())[:_FLAG_MAX], "file": file}


def _named(items: Sequence[str]) -> str:
    """``a, b, c, d and 3 more``: the first entries of a list, one line each, and how many are left."""
    shown = [" ".join(item.split())[:_FLAG_ITEM_MAX] for item in items[:_FLAG_LISTED]]
    rest = len(items) - len(shown)
    return ", ".join(shown) + (f" and {rest} more" if rest else "")


def _amount(value: float) -> str:
    return str(int(value)) if float(value).is_integer() else f"{value:g}"


def _number(value: Any) -> int | float | None:
    return value if isinstance(value, (int, float)) and not isinstance(value, bool) else None


def _dict(value: Any) -> Mapping:
    return value if isinstance(value, Mapping) else {}


def _entries(value: Any) -> list[str]:
    return [item for item in value if isinstance(item, str)] if isinstance(value, list) else []


def _mapping_section(data: Any, key: str) -> Mapping | None:
    """Section ``key`` of a structure when it is there and parsed; ``None`` otherwise."""
    value = data.get(key) if isinstance(data, Mapping) else None
    return value if isinstance(value, Mapping) and "error" not in value else None


def _list_section(data: Any, key: str) -> list | None:
    value = data.get(key) if isinstance(data, Mapping) else None
    return value if isinstance(value, list) else None  # a roles section that did not parse is a mapping


def _gate_label(gid: str, gates: Mapping) -> str:
    name = _dict(gates.get(gid)).get("name")
    return f"{gid} ({name})" if isinstance(name, str) and name else gid


def _gate_flags(unit: Any, start: Any) -> list[dict]:
    new, old = _mapping_section(unit, "gates"), _mapping_section(start, "gates")
    if new is None or old is None:
        return []
    new_gates, old_gates = _dict(new.get("gates")), _dict(old.get("gates"))
    flags = []
    for profile in _WATCHED_PROFILES:
        kept = set(_entries(_dict(new.get("profiles")).get(profile)))
        gone = [gid for gid in _entries(_dict(old.get("profiles")).get(profile)) if gid not in kept]
        if gone:
            labels = [_gate_label(gid, old_gates) for gid in gone]
            flags.append(_flag("gate-removed", f"The {profile} profile no longer runs {_named(labels)}", _GATES))
    for gid, gate in old_gates.items():
        now = _dict(new_gates.get(gid))
        before, after = _number(_dict(gate).get("timeout_s")), _number(now.get("timeout_s"))
        if before is not None and after is not None and after > _TIMEOUT_FACTOR * before:
            text = (
                f"Gate {_gate_label(gid, old_gates)} has a timeout of {_amount(after)} s, "
                f"more than double the {_amount(before)} s of the start harness"
            )
            flags.append(_flag("gate-timeout", text, _GATES))
        if not now:  # a gate that is gone is the profile's business, above
            continue
        fuzz = _dict(_dict(gate).get("env")).get(_FUZZ)
        set_to = _dict(now.get("env")).get(_FUZZ)
        if fuzz is not None and str(set_to) != str(fuzz):  # as text: check.sh runs the fuzz smoke for exactly "1"
            shown = "unset" if set_to is None else " ".join(str(set_to).split())[:_FLAG_ITEM_MAX]
            text = f"Gate {_gate_label(gid, old_gates)} runs with {_FUZZ} {shown}, not the {fuzz} of the start harness"
            flags.append(_flag("gate-env", text, _GATES))
        old_fn, new_fn = _dict(gate).get("fn"), now.get("fn")
        if isinstance(old_fn, str) and old_fn and old_fn != new_fn:
            shown = new_fn if isinstance(new_fn, str) and new_fn else "nothing"
            flags.append(_flag("gate-fn", f"Gate {gid} now runs {shown} instead of {old_fn}", _GATES))
    return flags


def _policy_flags(unit: Any, start: Any) -> list[dict]:
    new, old = _mapping_section(unit, "policy"), _mapping_section(start, "policy")
    if new is None or old is None:
        return []
    flags = []
    for rule, key, text in (
        ("protected-removed", "protected", "Protected paths no longer include"),
        ("shell-deny-removed", "shell_deny", "Denied shell fragments no longer include"),
    ):
        kept = set(_entries(new.get(key)))
        gone = [entry for entry in _entries(old.get(key)) if entry not in kept]
        if gone:
            flags.append(_flag(rule, f"{text} {_named(gone)}", _HARNESS))
    now = _dict(new.get("forbidden_calls"))
    for glob, calls in _dict(old.get("forbidden_calls")).items():
        kept = set(_entries(now.get(glob)))
        gone = [call for call in _entries(calls) if call not in kept]
        if gone:
            flags.append(_flag("forbidden-calls-removed", f"Forbidden calls under {glob} no longer include {_named(gone)}", _HARNESS))
    return flags


def _part_covers(head: str, first: str) -> bool:
    """Whether the part ``head`` of a start glob covers the part ``first`` of a new one (neither is ``**`` as a whole)."""
    if head == first:
        return True
    if "**" in first:  # more than one folder level for all we know: only an equal part or a ** part covers it
        return False
    if head == "*":
        return True
    if any(c in first for c in _WILDCARDS):  # a wildcard in the new part: only an equal part or a lone * covers it
        return False
    # * and ? are the wildcards of the harness's globs, [ is a letter: src/[ab] covers the file named [ab], not src/a
    pattern = "".join("[^/]*" if c == "*" else "[^/]" if c == "?" else re.escape(c) for c in head)
    return re.fullmatch(pattern, first) is not None


def _segments_cover(start: Sequence[str], new: Sequence[str]) -> bool:
    if not start:
        return not new
    head = start[0]
    if head == "**":  # any number of folders; as the last part, at least one more part
        if len(start) == 1:
            return bool(new)
        return any(_segments_cover(start[1:], new[skip:]) for skip in range(len(new) + 1))
    if not new or new[0] == "**":  # a new entry that goes any depth is not covered by a plain part
        return False
    return _part_covers(head, new[0]) and _segments_cover(start[1:], new[1:])


def _glob_covers(start: str, new: str) -> bool:
    """Whether the start-harness path glob ``start`` covers the new glob ``new``: they are the same, or ``new`` only
    names what ``start`` names. Part by part (``/``): a literal part is matched by the start part (``*`` and ``?``
    work inside it, ``[`` is a letter), a part of ``new`` with a wildcard only by an equal part or a lone ``*``, a part
    with ``**`` inside it only by an equal part or ``**``, a whole ``**`` only by ``**``; so ``src/**`` covers
    ``src/net/**`` and ``src/*.c``, ``src/*`` covers neither ``src/**`` nor ``src/**.c``. A ``.`` or ``..`` part is
    never covered: ``src/../scripts/**`` is not under ``src/**``."""
    if start == new:
        return True
    parts = new.split("/")
    return not {".", ".."} & set(parts) and _segments_cover(start.split("/"), parts)


def _shell_covers(start: str, new: str) -> bool:
    """Whether the start-harness shell entry ``start`` covers ``new`` the way the harness reads an allowlist entry
    (``policy.py``, ``_shell_allowed``): the words of ``start`` are the first words of ``new``, so ``grep`` covers
    ``grep -n`` and ``git status`` covers ``git status -s``."""
    want = start.split()
    return new.split()[: len(want)] == want


_ROLE_RULES = (
    ("role-shell", "shell", "shell now allows {}, which the start harness does not", _shell_covers),
    ("role-writes", "writes", "may now write {}, which the start harness does not allow", _glob_covers),
)


def _role_flags(unit: Any, start: Any) -> list[dict]:
    new, old = _list_section(unit, "roles"), _list_section(start, "roles")
    if new is None or old is None:
        return []
    named = [role for role in old if isinstance(role, Mapping) and isinstance(role.get("name"), str)]
    before = {role["name"]: role for role in named}
    every = {key: [entry for role in named for entry in _entries(role.get(key))] for _, key, _, _ in _ROLE_RULES}
    flags = []
    for role in new:
        if not isinstance(role, Mapping) or not isinstance(role.get("name"), str):
            continue
        was = before.get(role["name"])
        for rule, key, text, covers in _ROLE_RULES:
            # a role the start harness has is compared with itself; one it lacks (a new or a renamed one) with all of them
            known = every[key] if was is None else _entries(was.get(key))
            added = [entry for entry in _entries(role.get(key)) if not any(covers(start_entry, entry) for start_entry in known)]
            if added:
                flags.append(_flag(rule, f"{role['name']}: " + text.format(_named(added)), _HARNESS))
    return flags


def _loop_flags(unit: Any, start: Any) -> list[dict]:
    new = _mapping_section(unit, "loop")
    if new is None:
        return []
    flags = []
    retries = _number(new.get("retry_budget"))
    if retries is not None and retries > _RETRY_LIMIT:
        text = f"Retries per step is {_amount(retries)}: more than {_RETRY_LIMIT} gate-triggered retries per step"
        flags.append(_flag("retry-budget", text, _HARNESS))
    rounds = new.get("review_rounds")
    if rounds is False or (_number(rounds) is not None and rounds == 0):  # YAML ``false`` is 0 to the harness
        text = "Review rounds is 0: the reviewer's rejections are not sent back for another try"
        flags.append(_flag("review-rounds", text, _HARNESS))
    before, after = _number(_dict(_mapping_section(start, "loop")).get("ladder_rounds")), _number(new.get("ladder_rounds"))
    if before is not None and after is not None and after > before:
        text = f"Ladder rounds is {_amount(after)}: more than the {_amount(before)} of the start harness"
        flags.append(_flag("ladder-rounds", text, _HARNESS))
    return flags


def _hook_key(hook: Mapping) -> tuple[str, ...]:
    """What makes a hook the same hook: its event, its matcher and both of its commands."""
    return tuple(str(hook.get(key) or "") for key in ("event", "matcher", "bash", "powershell"))


def _hook_flags(unit: Any, start: Any) -> list[dict]:
    new, old = _list_section(unit, "hooks"), _list_section(start, "hooks")
    if new is None or old is None:
        return []
    have: dict[tuple[str, ...], Mapping] = {}
    for hook in new:
        if isinstance(hook, Mapping) and "error" not in hook:
            have.setdefault(_hook_key(hook), hook)
    flags: list[dict] = []
    seen: set[tuple[str, ...]] = set()
    for hook in old:  # a hook of a file that does not parse does not run: its hooks count as gone
        if not isinstance(hook, Mapping) or "error" in hook or _hook_key(hook) in seen:
            continue
        seen.add(_hook_key(hook))  # one flag for a hook that is listed twice
        file = hook.get("file") if isinstance(hook.get("file"), str) else _HOOKS
        command = hook.get("command") or hook.get("bash") or hook.get("powershell")
        now = have.get(_hook_key(hook))
        if now is None:
            flags.append(_flag("hook-removed", f"The {hook.get('event')} hook {command} is gone or changed", file))
            continue
        before, after = _number(hook.get("timeout_s")), _number(now.get("timeout_s"))
        if before is not None and after is not None and after < before:
            text = f"The {hook.get('event')} hook {command} has a timeout of {_amount(after)} s, cut from {_amount(before)} s"
            flags.append(_flag("hook-timeout", text, file))
    return flags


def _step(workflow: Mapping, step_id: str) -> Mapping:
    steps = workflow.get("steps")
    found = (s for s in steps if isinstance(s, Mapping) and s.get("id") == step_id) if isinstance(steps, list) else ()
    return next(iter(found), {})


def _uses_role(workflow: Mapping, role: str) -> bool:
    steps = workflow.get("steps")
    return isinstance(steps, list) and any(isinstance(s, Mapping) and s.get("role") == role for s in steps)


def _step_flags(name: str, file: str, step: Mapping, there: Mapping, loops: Mapping) -> list[dict]:
    """The flags of one step of a start-harness workflow. ``there`` is the same step of the unit's workflow (``{}`` when
    it is gone), ``loops`` the loop section of the start harness: what a step without a count of its own uses."""
    step_id, kind = step["id"], step.get("kind")
    flags = []
    if step.get("approval") == "human" and there.get("approval") != "human":  # gone, or no longer asks
        if step_id == "merge-approval":
            flags.append(_flag("merge-approval-auto", f"{name}: the merge approval no longer asks a person", file))
        else:
            flags.append(_flag("approval-removed", f"{name}: step {step_id} no longer asks a person", file))
    profile = step.get("gates") if isinstance(step.get("gates"), str) and step.get("gates") else None
    runs_gates = kind in _GATING or profile is not None
    if runs_gates:
        was = f"the {profile} gates" if profile else "its gates"
        now_profile = there.get("gates") if isinstance(there.get("gates"), str) and there.get("gates") else None
        if not there:
            flags.append(_flag("step-gates", f"{name}: step {step_id} no longer runs {was}", file))
        elif kind in _GATING and there.get("kind") not in _GATING:  # another kind of step: its gates are not run
            flags.append(_flag("step-gates", f"{name}: step {step_id} no longer runs its gates", file))
        elif profile is not None and now_profile is None:
            flags.append(_flag("step-gates", f"{name}: step {step_id} no longer runs {was}", file))
        elif profile is not None and now_profile != profile:
            flags.append(_flag("step-gates", f"{name}: step {step_id} now runs the {now_profile} gates instead of {profile}", file))
    # a count a step sets for itself is compared with the one it had, or with the loop's when it had none
    # (a step that is gone sets nothing: ``there`` is empty and none of the rest applies)
    defaults = {"retry_budget": loops.get("retry_budget"), "max_rounds": {"gates": loops.get("ladder_rounds"), "review": loops.get("review_rounds")}.get(kind)}
    for key, default in defaults.items():
        before = _number(step.get(key)) if _number(step.get(key)) is not None else _number(default)
        after = _number(there.get(key))
        if before is not None and after is not None and after > before:
            flags.append(_flag("step-rounds", f"{name}: step {step_id} has {key} {_amount(after)}, more than the {_amount(before)} of the start harness", file))
        if key == "max_rounds" and kind == "review" and after == 0 and before != 0:
            text = f"{name}: step {step_id} has max_rounds 0: the reviewer's rejections are not sent back for another try"
            flags.append(_flag("step-review-rounds", text, file))
    if runs_gates and there.get("on_fail") == "continue" and step.get("on_fail") != "continue":
        flags.append(_flag("step-on-fail", f"{name}: step {step_id} now goes on when its gates fail", file))
    return flags


def _workflow_flags(unit: Any, start: Any) -> list[dict]:
    new, old = _list_section(unit, "workflows"), _list_section(start, "workflows")
    if new is None or old is None:
        return []
    mine = {w["name"]: w for w in new if isinstance(w, Mapping) and "error" not in w and isinstance(w.get("name"), str)}
    loops = _dict(_mapping_section(start, "loop"))
    flags = []
    for first in old:
        if not isinstance(first, Mapping) or "error" in first or not isinstance(first.get("name"), str):
            continue
        name, now = first["name"], mine.get(first["name"])
        if now is None:  # the workflow is not there (or does not parse): nothing to compare
            continue
        file = first.get("file") if isinstance(first.get("file"), str) else f"{_WORKFLOWS}/{name}.yaml"
        for step in first.get("steps") if isinstance(first.get("steps"), list) else ():
            if isinstance(step, Mapping) and isinstance(step.get("id"), str):
                flags.extend(_step_flags(name, file, step, _step(now, step["id"]), loops))
        if name == "jira-to-pr" and _uses_role(first, "reviewer") and not _uses_role(now, "reviewer"):
            flags.append(_flag("reviewer-off", f"{name}: no step uses the reviewer role any more", file))
    known = {w["name"] for w in old if isinstance(w, Mapping) and isinstance(w.get("name"), str)}
    for one in new:  # a workflow of the unit's own: nothing to compare it with, so it is only mentioned
        if isinstance(one, Mapping) and isinstance(one.get("name"), str) and one["name"] not in known:
            file = one.get("file") if isinstance(one.get("file"), str) else f"{_WORKFLOWS}/{one['name']}.yaml"
            flags.append(_flag("workflow-new", f"new workflow {one['name']}: its steps are not compared with the start harness", file))
    return flags


def weakening(unit: Any, golden: Any) -> list[dict]:
    """The guardrails ``unit`` (a ``read`` result) has given up compared with ``golden`` (the same at ``start_ref``:
    ``golden()``), as flags ``{rule, detail, file}`` in a fixed order. The rules:

    * ``gate-removed``: a gate of the ``ladder`` or ``fast`` profile is no longer in it (one flag per profile);
      ``gate-timeout``: a gate's ``timeout_s`` is more than double the start harness's; ``gate-env``: ``TS_FUZZ`` of a
      gate (the fuzz smoke) is not the text it has at the start (check.sh runs it for exactly ``1``); ``gate-fn``: a
      gate runs another function than at the start;
    * ``protected-removed``, ``shell-deny-removed`` and ``forbidden-calls-removed``: an entry of ``policy.protected``,
      ``policy.shell_deny`` or ``policy.forbidden_calls`` is gone (the save rules keep the first two: this is a second
      line);
    * ``role-shell`` and ``role-writes``: a role's ``shell`` or ``writes`` lists an entry that no entry of the start
      harness's same role covers (``_glob_covers``: ``src/**`` covers ``src/net/**``; ``_shell_covers``: the words of a
      start entry are the first words of the new one, as the harness reads the allowlist); a role the start harness
      lacks is compared with all of its roles together, so a renamed role is no flag;
    * ``retry-budget``: ``loop.retry_budget`` is above 5; ``review-rounds``: ``loop.review_rounds`` is 0;
      ``ladder-rounds``: ``loop.ladder_rounds`` is above the start harness's;
    * ``hook-removed``: a hook of the start harness (event, matcher, bash and powershell command) is gone or changed;
      ``hook-timeout``: its ``timeoutSec`` is cut;
    * per step of a start-harness workflow: ``merge-approval-auto`` (the ``merge-approval`` step) and
      ``approval-removed`` (any other step) when a step that asked a person (``approval: human``) is gone or does not
      any more; ``step-gates`` when a step that ran gates is gone, is of a kind that runs none, has no profile or has
      another one (named: "now runs the tests gates instead of fast"); ``step-rounds`` when its ``retry_budget`` or
      ``max_rounds`` is larger than the step had, or than the loop's count when it had none; ``step-review-rounds``
      when a review step's ``max_rounds`` is 0; ``step-on-fail`` when a step that ran gates has ``on_fail: continue``
      and had not;
    * ``workflow-new``: a workflow the start harness does not have (informational: its steps are not compared);
    * ``reviewer-off``: no step of ``jira-to-pr`` uses the reviewer role any more.

    Never fails on a structure it does not understand: that part gives no flag.
    """
    flags: list[dict] = []
    for rule in (_gate_flags, _policy_flags, _role_flags, _loop_flags, _hook_flags, _workflow_flags):
        flags.extend(rule(unit, golden))
    return flags
