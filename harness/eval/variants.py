"""variants.yaml: the experiment matrix (design doc section 4.4).

    version: 1
    workflow: jira-to-pr          # default workflow of every variant
    defaults: {...}               # settings every variant starts from
    gate_weights: {no-heap: 3, ...}    # weighted gate-violation score (default weight 1)
    variants:
      team: {label: ..., roles: {...}}
      team-small: {extends: team, models: {...}}

A variant is `defaults`, then its `extends` chain (resolved recursively), then
its own keys; mappings (roles, models, extra) merge key by key, everything else
replaces.

`overlay_from: <dir>` (a variant key, not a run setting; null clears an inherited one) makes the
eval worktree take its harness structure from <dir>, a workshop unit's worktree: see
worktree.apply_overlay. A relative path is relative to the variants file's directory; the
directory must exist. The run then loads `<worktree>/harness/harness.yaml` (run_settings sets
extra.config to it, so an explicit extra.config is an error) instead of the package's, except for the
host-side keys (copilot, paths.bash, paths.check_log, ticket.jira, runtime, runtimes), which stay the
package's: backend.load_run_config.

An overlay variant applies only the settings it names itself, so "evaluate the unit's own harness" does
not quietly evaluate the file's defaults: a knob counts as named when the variant's own entry or an entry
of its `extends` chain sets it, not when it only comes from `defaults:` (nor from the RunSettings
defaults). Knobs are retry_budget, custom_instructions, prompt_template, autonomy, gates and, per role,
roles.<role> and models.<role>; whatever is not named stays as the unit's harness.yaml and workflows have
it. max_ai_credits is the exception: the cap always applies (the default's or the variant's), so a unit's
file can never raise the cost of a run beyond the run cap (the credit guard stops a run at most one model
call above it; without a variant cap the package's runtimes.<mode> cap binds, and that is pinned too).
The extra knobs (escalation, role_overrides, runtime_mode, baseline) apply as for any other variant. Variant.applied_knobs lists what applies (sorted; None without an
overlay: everything applies); run_settings passes it to the backend as extra.applied_knobs, the runner
records it, and a variants file cannot set extra.applied_knobs itself.
"""
from __future__ import annotations

import copy
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Optional

from .. import names
from ..api import AUTONOMY, CAP_KNOB, ROLE_KNOBS, ROLES, SCALAR_KNOBS, RunSettings
from .yamlio import load_yaml

SETTING_KEYS = {"roles", "models", "retry_budget", "custom_instructions", "prompt_template",
                "autonomy", "gates", "max_ai_credits", "timeout_s", "extra"}
META_KEYS = {"label", "tests", "extends", "workflow", "repeats", "overlay_from"}
MERGE_KEYS = {"roles", "models", "extra"}
# SCALAR_KNOBS, ROLE_KNOBS (one knob per role: roles.<role>, models.<role>) and CAP_KNOB: harness/api.py,
# the one list the backend checks extra.applied_knobs against as well


class VariantError(ValueError):
    pass


@dataclass
class Variant:
    name: str
    label: str
    tests: str
    workflow: str
    repeats: int
    settings: dict[str, Any] = field(default_factory=dict)
    overlay_from: Optional[str] = None       # as written in the variants file
    overlay_dir: Optional[Path] = None       # the same, resolved and checked to be a directory
    applied_knobs: Optional[list[str]] = None   # overlay only: the settings the run applies; None: all

    def run_settings(self, workdir: str, run_dir: str) -> RunSettings:
        s = RunSettings.from_dict(copy.deepcopy(self.settings))
        s.workdir = workdir
        s.run_dir = run_dir
        s.variant = self.name
        if self.overlay_dir is not None:
            # the backend loads harness.yaml, and gates.yaml / workflows/ / prompts/ beside it, from the
            # eval worktree (where the overlay has just put the unit's files); the code stays the package's
            s.extra["config"] = str(Path(workdir) / "harness" / "harness.yaml")
            s.extra["applied_knobs"] = list(self.applied_knobs or [])
        return s


@dataclass
class Matrix:
    variants: dict[str, Variant]
    gate_weights: dict[str, float]
    source: str = ""

    def select(self, wanted: Optional[list[str]]) -> list[Variant]:
        """The variants named in ``wanted`` (today's names, or the old codes raw, gates ...); all of them when empty."""
        if not wanted:
            return list(self.variants.values())
        wanted = [names.variant(n) for n in wanted]
        missing = [n for n in wanted if n not in self.variants]
        if missing:
            raise VariantError(f"unknown variant(s): {', '.join(missing)} (have {', '.join(self.variants)})")
        return [self.variants[n] for n in wanted]


def _merge(base: dict[str, Any], over: dict[str, Any]) -> dict[str, Any]:
    out = copy.deepcopy(base)
    for k, v in over.items():
        if k in MERGE_KEYS and isinstance(v, dict) and isinstance(out.get(k), dict):
            out[k] = {**out[k], **copy.deepcopy(v)}
        else:
            out[k] = copy.deepcopy(v)
    return out


def _named_knobs(entry: dict[str, Any]) -> set[str]:
    """The knobs a variants-file entry sets itself (see the module docstring)."""
    knobs = {k for k in SCALAR_KNOBS if k in entry}
    for group in ROLE_KNOBS:
        if isinstance(entry.get(group), dict):
            knobs |= {f"{group}.{role}" for role in entry[group]}
    return knobs


def _check_keys(where: str, d: dict[str, Any], allowed: set[str]) -> None:
    bad = sorted(set(d) - allowed)
    if bad:
        raise VariantError(f"{where}: unknown key(s) {', '.join(bad)}")


def _validate(name: str, s: dict[str, Any]) -> None:
    roles = s.get("roles") or {}
    _check_keys(f"{name}.roles", roles, set(ROLES))
    if not roles.get("implementer", False):
        raise VariantError(f"{name}: the implementer role cannot be switched off")
    models = s.get("models") or {}
    _check_keys(f"{name}.models", models, set(ROLES))
    for role, on in roles.items():
        if on and not models.get(role):
            raise VariantError(f"{name}: role {role} is on but has no model")
    if s.get("autonomy", "human") not in AUTONOMY:
        raise VariantError(f"{name}: autonomy must be one of {AUTONOMY}")
    rb = s.get("retry_budget", 3)
    if not isinstance(rb, int) or isinstance(rb, bool) or rb < 0:
        raise VariantError(f"{name}: retry_budget must be an integer >= 0")
    if not isinstance(s.get("custom_instructions", True), bool):
        raise VariantError(f"{name}: custom_instructions must be true or false")
    if not str(s.get("prompt_template", "v1")):
        raise VariantError(f"{name}: prompt_template must not be empty")


def _overlay(name: str, value: Any, base_dir: Path) -> tuple[Optional[str], Optional[Path]]:
    if value is None:
        return None, None
    if not isinstance(value, str) or not value.strip():
        raise VariantError(f"{name}: overlay_from must be a non-empty path string")
    directory = (base_dir / value).resolve()          # an absolute value stays as it is
    if not directory.is_dir():
        raise VariantError(f"{name}: overlay_from {value!r} is not an existing directory ({directory})")
    return value, directory


def _applied_knobs(named: set[str], settings: dict[str, Any]) -> list[str]:
    """What an overlay variant applies: the knobs it names (a model only for a role that has one) and the cap."""
    knobs = {k for k in named if not k.startswith("models.") or k.split(".", 1)[1] in settings["models"]}
    if settings.get(CAP_KNOB) is not None:
        knobs.add(CAP_KNOB)
    return sorted(knobs)


def parse_matrix(doc: dict[str, Any], source: str = "", base_dir: Optional[Path] = None) -> Matrix:
    """`base_dir` resolves relative overlay_from paths: default the directory of `source`, else the cwd."""
    if not isinstance(doc, dict):
        raise VariantError("variants file must be a mapping")
    _check_keys("variants file", doc, {"version", "workflow", "defaults", "gate_weights", "variants"})
    if int(doc.get("version", 1)) != 1:
        raise VariantError("only version 1 is supported")
    raw = doc.get("variants") or {}
    if not isinstance(raw, dict) or not raw:
        raise VariantError("no variants defined")
    raw = {names.variant(str(k)): v for k, v in raw.items()}         # an older file's codes (raw, team) as names
    for own in raw.values():
        if isinstance(own, dict) and own.get("extends"):
            own["extends"] = names.variant(str(own["extends"]))
    defaults = dict(doc.get("defaults") or {})
    _check_keys("defaults", defaults, SETTING_KEYS | {"repeats"})
    default_workflow = str(doc.get("workflow") or "jira-to-pr")
    if base_dir is None:
        base_dir = Path(source).resolve().parent if source else Path.cwd()
    resolved: dict[str, dict[str, Any]] = {}
    named: dict[str, set[str]] = {}         # per variant: the knobs set by its entry or its extends chain

    def resolve(name: str, chain: tuple[str, ...]) -> dict[str, Any]:
        if name in resolved:
            return resolved[name]
        if name in chain:
            raise VariantError(f"extends cycle: {' -> '.join(chain + (name,))}")
        if name not in raw:
            raise VariantError(f"{chain[-1] if chain else '?'} extends unknown variant {name}")
        own = raw[name] or {}
        if not isinstance(own, dict):
            raise VariantError(f"{name}: must be a mapping")
        _check_keys(name, own, SETTING_KEYS | META_KEYS)
        base = resolve(str(own["extends"]), chain + (name,)) if own.get("extends") else defaults
        merged = _merge(base, {k: v for k, v in own.items() if k not in ("extends", "label", "tests")})
        named[name] = _named_knobs(own) | (named[str(own["extends"])] if own.get("extends") else set())
        resolved[name] = merged
        return merged

    variants: dict[str, Variant] = {}
    for name in raw:
        merged = resolve(str(name), ())
        _validate(str(name), merged)
        overlay_from, overlay_dir = _overlay(str(name), merged.get("overlay_from"), base_dir)
        if overlay_from is not None and isinstance(merged.get("extra"), dict) and "config" in merged["extra"]:
            raise VariantError(f"{name}: overlay_from and extra.config exclude each other "
                               "(the run loads the overlaid harness/harness.yaml)")
        if isinstance(merged.get("extra"), dict) and "applied_knobs" in merged["extra"]:
            raise VariantError(f"{name}: extra.applied_knobs is set by overlay_from, not by the file")
        own = raw[name] or {}
        settings = {k: v for k, v in merged.items() if k in SETTING_KEYS}
        settings["roles"] = {r: bool(settings.get("roles", {}).get(r, False)) for r in ROLES}
        # a model is only used by a role that is on; with an overlay a role the variant does not switch is
        # the unit workflow's to run, so its model stays
        unit_decides = {r for r in ROLES if overlay_dir is not None and f"roles.{r}" not in named[str(name)]}
        settings["models"] = {r: m for r, m in (settings.get("models") or {}).items()
                              if settings["roles"].get(r) or r in unit_decides}
        repeats = int(merged.get("repeats", 1))
        if repeats < 1:
            raise VariantError(f"{name}: repeats must be >= 1")
        variants[str(name)] = Variant(
            name=str(name),
            label=str(own.get("label") or name),
            tests=str(own.get("tests") or ""),
            workflow=str(merged.get("workflow") or default_workflow),
            repeats=repeats,
            settings=settings,
            overlay_from=overlay_from,
            overlay_dir=overlay_dir,
            applied_knobs=_applied_knobs(named[str(name)], settings) if overlay_dir is not None else None,
        )
    weights = {names.gate(str(k)): float(v) for k, v in (doc.get("gate_weights") or {}).items()}
    return Matrix(variants=variants, gate_weights=weights, source=source)


DEFAULT_FILE = Path(__file__).resolve().parent / "variants.yaml"


def find_variants_file(arg: Optional[str]) -> Path:
    """The path as given, else relative to harness/eval/, else the shipped variants.yaml."""
    if not arg:
        return DEFAULT_FILE
    p = Path(arg)
    if p.is_file():
        return p
    alt = DEFAULT_FILE.parent / arg
    if alt.is_file():
        return alt
    raise VariantError(f"variants file not found: {arg}")


def load_matrix(path: Path) -> Matrix:
    return parse_matrix(load_yaml(path), source=str(path))
