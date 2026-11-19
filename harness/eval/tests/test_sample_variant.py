"""The sample unit experiment (variants-unit.yaml) loads and runs with the fake orchestrator."""
from __future__ import annotations

from pathlib import Path

from harness.eval import cli, runner, variants
from harness.eval.fake import run_workflow as fake_run

PROMPTS = Path(variants.DEFAULT_FILE).resolve().parents[1] / "prompts"


def test_unit_matrix_settings():
    m = variants.load_matrix(variants.find_variants_file("variants-unit.yaml"))
    assert list(m.variants) == ["team", "U1"]
    u1, v4 = m.variants["U1"].settings, m.variants["team"].settings
    assert u1["prompt_template"] == "team" and v4["prompt_template"] == "v1"
    assert u1["retry_budget"] == 2 and all(u1["roles"].values())
    assert u1["models"] == v4["models"]
    assert (PROMPTS / "team").is_dir()


def test_unit_matrix_with_the_fake(repo, hidden, tmp_path):
    m = variants.load_matrix(variants.find_variants_file("variants-unit.yaml"))
    cfg = runner.EvalConfig(repo=repo, hidden_dir=hidden, variants=m.select(None), tickets=["EV-1"],
                            experiment="unit", results_dir=tmp_path / "results",
                            worktree_root=tmp_path / "wt", gate_weights=m.gate_weights, parallel=2,
                            repeats=1, golden_ref="golden", hidden_cmd=cli.FAKE_HIDDEN_CMD,
                            backend=fake_run, log=lambda s: None)
    recs = runner.Runner(cfg).run()
    by = {r["variant"]: r for r in recs}
    assert sorted(by) == ["U1", "team"]
    assert by["U1"]["settings"]["prompt_template"] == "team"
