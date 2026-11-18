"""The names people see: gates, variants, branches and sections, each a plain word.

Earlier versions of this repository used short codes (gates G0 .. G14, DP, DS; variants B0, V1 .. V11; branches
seed/TS-1xx and b1-done .. b5-done; sections b1 .. b5). Those codes still work everywhere a name is read (the command
line, a result file, a config file, a branch argument): ``gate``, ``variant``, ``branch`` and ``section`` turn an old
code into today's name and leave every other text as it is. Everything the harness writes or prints uses the new names.
"""
from __future__ import annotations

import re

# gate code -> gate name, in the order of the old numbering
GATES: dict[str, str] = {
    "G0": "build",
    "G1": "misra",
    "G2": "no-heap",
    "G3": "unit-tests",
    "G4": "property-tests",
    "G5": "sanitizers",
    "G6": "fuzz",
    "G7": "loopback",
    "G8": "budgets",
    "G9": "coverage",
    "G10": "compliance",
    "G11": "hardware",
    "G12": "held-back-tests",
    "G13": "style",
    "G14": "comments",
    "DP": "diff-policy",
    "DS": "diff-size",
}

# variant code -> variant name
VARIANTS: dict[str, str] = {
    "B0": "raw",
    "V1": "gates",
    "V2": "gates-planner",
    "V3": "gates-reviewer",
    "V4": "team",
    "V5": "team-prompt-v2",
    "V6": "team-one-retry",
    "V7": "team-small",
    "V8": "team-large",
    "V9": "team-autonomous",
    "V10": "team-no-rules",
    "V11": "cheap",
}

# section code -> section name (file names, experiment names, the Today page's sections)
SECTIONS: dict[str, str] = {
    "b1": "section1",
    "b2": "section2",
    "b3": "section3",
    "b4": "section4",
    "b4a": "section4a",
    "b4b": "section4b",
    "b5": "section5",
}

TICKET_BRANCH = "ticket/"            # ticket/TS-104: the code where only that ticket's defect is open
_OLD_TICKET_BRANCH = re.compile(r"seed/(TS-\d+)")
_OLD_SECTION_BRANCH = re.compile(r"b([1-5])-done")
_GATES_CI = {k.lower(): v for k, v in GATES.items()}


def gate(name: str) -> str:
    """Today's gate name for ``name`` (an old code such as G0, g0 or DP, or already a name)."""
    return _GATES_CI.get(name.strip().lower(), name.strip())


def variant(name: str) -> str:
    """Today's variant name for ``name`` (an old code such as B0 or V11, or already a name)."""
    return VARIANTS.get(name.strip().upper(), name.strip())


def section(name: str) -> str:
    """Today's section name for ``name`` (an old code such as b1 or b4b, or already a name)."""
    return SECTIONS.get(name.strip().lower(), name.strip())


def branch(name: str) -> str:
    """Today's branch name: seed/TS-104 -> ticket/TS-104, b3-done -> after-section-3, anything else unchanged."""
    m = _OLD_TICKET_BRANCH.fullmatch(name)
    if m:
        return TICKET_BRANCH + m.group(1)
    m = _OLD_SECTION_BRANCH.fullmatch(name)
    if m:
        return f"after-section-{m.group(1)}"
    return name


def ticket_branch(ticket: str) -> str:
    """The branch an experiment starts every run of ``ticket`` on."""
    return TICKET_BRANCH + ticket
