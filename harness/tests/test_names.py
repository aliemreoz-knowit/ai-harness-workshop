"""The plain names (harness/names.py): what is shown, and the old codes that still work wherever a name is read."""
from __future__ import annotations

import json
import shutil
import subprocess
from pathlib import Path

import pytest

from harness import names
from harness.config import load_config
from harness.eval import results, tickets, variants

REPO = Path(__file__).resolve().parents[2]


def test_every_old_code_has_a_plain_name():
    assert names.gate("G0") == names.gate("g0") == "build" and names.gate("DP") == "diff-policy"
    assert names.gate("build") == "build" and names.gate("not-a-gate") == "not-a-gate"
    assert names.variant("B0") == "raw" and names.variant("v11") == "cheap" and names.variant("team") == "team"
    assert names.branch("seed/TS-104") == "ticket/TS-104" and names.branch("b5-done") == "after-section-5"
    assert names.branch("main") == "main" and names.section("b4b") == "section4b"
    plain = list(names.GATES.values()) + list(names.VARIANTS.values())
    assert len(plain) == len(set(plain)) and all(n == n.lower() and " " not in n for n in plain)


def test_the_config_reads_old_gate_codes_as_names():
    cfg = load_config(REPO / "harness" / "harness.yaml", env={}, repo=REPO)
    assert [g.id for g in cfg.gates.resolve(["G0", "unit-tests", "dp"])] == ["build", "unit-tests", "diff-policy"]
    assert all(g in names.GATES.values() for g in cfg.gates.gates)


def test_old_variant_codes_select_the_named_variants():
    m = variants.load_matrix(variants.DEFAULT_FILE)
    assert [v.name for v in m.select(["B0", "V11", "team-small"])] == ["raw", "cheap", "team-small"]


def test_an_old_result_file_reads_with_the_names(tmp_path):
    old = {"schema": results.SCHEMA, "variant": "V11", "ticket": "TS-104", "repeat": 0,
           "gate_results": {"G0": "PASS", "DP": "PASS"}}
    f = tmp_path / "old.jsonl"
    f.write_text(json.dumps(old) + "\n", encoding="utf-8")
    (rec,) = results.load_records([f])
    assert rec["variant"] == "cheap" and rec["gate_results"] == {"build": "PASS", "diff-policy": "PASS"}


def test_a_ticket_branch_from_an_older_meta_file(tmp_path):
    t = tmp_path / "tickets" / "TS-104"
    t.mkdir(parents=True)
    (t / "meta.yaml").write_text("seed_branch: seed/TS-104\n", encoding="utf-8")
    assert tickets.load_ticket(tmp_path, "TS-104").ticket_branch == "ticket/TS-104"


def test_check_sh_takes_old_codes_and_names():
    bash = shutil.which("bash")
    if bash is None or "system32" in bash.lower():
        pytest.skip("no bash here")
    script = (REPO / "scripts" / "check.sh").read_text(encoding="utf-8")
    for code, name in (("g0", "build"), ("g10", "compliance"), ("dp", "diff-policy")):
        assert f"[{code}]={name}" in script
    done = subprocess.run([bash, "scripts/check.sh", "no-such-gate"], cwd=REPO, capture_output=True, text=True,
                          env={"PATH": "/usr/bin:/bin", "TS_IN_CONTAINER": "1", "TS_LITE": "0"}, timeout=120)
    assert done.returncode == 2 and "unknown gate: no-such-gate" in done.stdout
