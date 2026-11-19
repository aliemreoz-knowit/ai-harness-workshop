"""Guardrail sample: the allocation guard is protected in the harness policy too (as in
scripts/protected_paths.txt and the Copilot hooks), and no file anywhere may add process
creation. Checked at the hook (preToolUse) and again on the diff (compliance)."""

from __future__ import annotations

from pathlib import Path

from harness import compliance
from harness.config import HarnessConfig
from harness.gates import GateContext
from harness.policy import RolePolicy


def test_malloc_guard_is_protected_for_every_writing_role(cfg: HarnessConfig) -> None:
    for role in ("implementer", "solo", "test-writer"):
        pol = RolePolicy(cfg.role(role), cfg.policy, cfg.repo)
        assert pol.check_write("src/malloc_guard.c").rule == "protected-path", role
        hook = pol.pre_tool_use({"toolName": "edit", "toolArgs": {"path": "src/malloc_guard.c"}})
        assert hook["permissionDecision"] == "deny"
    impl = RolePolicy(cfg.role("implementer"), cfg.policy, cfg.repo)
    assert impl.check_write("src/main.c").allow


def test_process_creation_refused_outside_src_too(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "tools").mkdir(exist_ok=True)
    (repo / "tools" / "helper.c").write_text(
        "/*\n * tinystream v2 -- tools/helper.c\n * SPDX-License-Identifier: MIT\n */\n"
        "int run(void) { return fork() + posix_spawn(0, 0, 0, 0, 0, 0); }\n", encoding="utf-8")
    (repo / "src" / "malloc_guard.c").write_text(
        "/*\n * tinystream v2 -- src/malloc_guard.c\n * SPDX-License-Identifier: MIT\n */\nint g;\n",
        encoding="utf-8")
    r = compliance.check(GateContext(repo, ["src/**", "tools/**"], cfg.policy), cfg.gates.gates["compliance"])
    got = {(i.rule, i.file) for i in r.items}
    assert r.status == "FAIL"
    assert ("forbidden-call:fork", "tools/helper.c") in got
    assert ("protected-path", "src/malloc_guard.c") in got
