"""diff-size, the diff-size gate (harness/sample_gates.py)."""

from __future__ import annotations

from dataclasses import replace
from pathlib import Path

from harness import sample_gates
from harness.config import HarnessConfig
from harness.gates import GateContext


def run(cfg: HarnessConfig, repo: Path, limit: str | None = None):
    gate = cfg.gates.gates["diff-size"]
    if limit is not None:
        gate = replace(gate, env={"MAX_DIFF_LINES": limit})
    return sample_gates.diff_size(GateContext(repo, ["src/**", "tests/**"], cfg.policy), gate)


def test_registered_in_the_ladder_before_the_container(cfg: HarnessConfig) -> None:
    assert [g.id for g in cfg.gates.resolve("ladder")][:5] == ["compliance", "style", "comments", "diff-size", "diff-policy"]
    assert "diff-size" not in [g.id for g in cfg.gates.resolve("fast")]


def test_small_change_passes(cfg: HarnessConfig, repo: Path) -> None:
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8").replace("x + 1", "x + 2"), encoding="utf-8")
    r = run(cfg, repo)
    assert r.status == "PASS" and r.reason.startswith("2 of 150")


def test_big_change_fails_biggest_file_first_tests_exempt(cfg: HarnessConfig, repo: Path) -> None:
    (repo / "src" / "proto" / "big.c").write_text("int a;\n" * 8, encoding="utf-8")
    src = repo / "src" / "proto" / "proto.c"
    src.write_text(src.read_text(encoding="utf-8") + "int b;\n", encoding="utf-8")
    (repo / "tests" / "unit" / "test_big.c").write_text("int t;\n" * 50, encoding="utf-8")
    r = run(cfg, repo, limit="5")
    assert r.status == "FAIL" and r.reason == "9 changed lines > 5"
    assert [i.file for i in r.items] == ["src/proto/big.c", "src/proto/proto.c"]
    assert all(i.rule == "diff-size" for i in r.items)


def test_bad_limit_is_an_error(cfg: HarnessConfig, repo: Path) -> None:
    assert run(cfg, repo, limit="many").status == "ERROR"


def test_changed_lines_ignores_headers() -> None:
    diff = ("diff --git a/x.c b/x.c\n--- a/x.c\n+++ b/x.c\n@@ -1 +1 @@\n-a\n+b\n context\n")
    assert sample_gates.changed_lines(diff) == {"x.c": 2}
