"""Budgets, verdicts, the report, and hil.yaml itself."""
from __future__ import annotations

import sys

import pytest

import budgets
import hilcore
import hilreport as report


def test_evaluate_pass_fail_missing_and_skip():
    rows = budgets.evaluate({"fps_clean": 29.9, "crc_fail_clean": 3, "exact_match_pct": None},
                            {"fps_min": 25, "crc_fail_max": 0, "recovery_ms_max": 1000, "exact_match_min_pct": 99},
                            has_ref=False)
    st = {r["key"]: r["status"] for r in rows}
    assert st == {"fps_min": "PASS", "crc_fail_max": "FAIL", "recovery_ms_max": "FAIL",   # not measured = FAIL
                  "exact_match_min_pct": "SKIP"}
    assert budgets.verdict(rows) == "FAIL"
    assert "datagrams fail the CRC check on a clean link" in budgets.symptoms(rows)


def test_explicit_skip_and_extra_rows():
    rows = budgets.evaluate({}, {"tear_frames_max": 0}, skip={"tear_frames": "no detector"},
                            extra={"daemon_alive": (False, "no", "daemon still running")})
    assert [r["status"] for r in rows] == ["SKIP", "FAIL"]
    assert "no detector" in rows[0]["label"]


def test_unknown_budget_is_an_error():
    with pytest.raises(ValueError):
        budgets.evaluate({}, {"fps_minimum": 25})


def test_repo_config_loads_with_and_without_pyyaml(monkeypatch):
    cfg = hilcore.load_config()
    monkeypatch.setitem(sys.modules, "yaml", None)          # import yaml -> ImportError: yamlmini
    cfg2 = hilcore.load_config()
    assert cfg == cfg2
    assert cfg["checks"]["file"]["cut"] == {"at": 3, "s": 1}
    assert cfg["checks"]["file"]["scenario"] == ["t=5s loss=2%"]
    assert cfg["targets"]["pi"]["approval"] == "human"
    assert set(cfg["budgets"]["file"]) <= set(budgets.RULES)


def test_bad_config(tmp_path):
    p = tmp_path / "hil.yaml"
    p.write_text("version: 1\nchecks: {file: {}, camera: {}}\nbudgets: {file: {fps: 1}, camera: {}}\n")
    with pytest.raises(hilcore.HilError):
        hilcore.load_config(str(p))


def test_report_markdown():
    rows = budgets.evaluate({"fps_clean": 23.4, "tear_frames": 0}, {"fps_min": 25, "tear_frames_max": 0})
    run = {"candidate": {"ref": "agent/x", "sha": "0123456789abcdef"}, "target": "local", "verdict": "FAIL",
           "checks": [{"name": "file", "title": "file source", "verdict": "FAIL", "rows": rows,
                       "summary": {"frames_decoded": 200, "pkts_per_s": 60.1},
                       "spec": {"seconds": 10, "seq_start": 65270, "cut": {"at": 3, "s": 2}}, "errors": []}],
           "commands": ["git archive ..."], "run_dir": "build-hil/runs/x"}
    md = report.render(run)
    assert md.startswith(report.MARKER)
    assert "HIL FAIL: `agent/x` (0123456789)" in md
    assert "| frame rate before the scripted event (fps) | 23.4 | >= 25 | FAIL |" in md
    assert "link cut 2 s at t=3 s" in md and "Symptoms:" in md
    assert "packets/s 60.1" in md


def test_report_describes_keys_and_scenario():
    rows = budgets.evaluate({"keys_answered": 0, "keys_ignored": 4}, {"keys_answered_min": 3, "keys_ignored_max": 0})
    run = {"candidate": {"ref": "agent/x", "sha": "0123456789abcdef"}, "target": "local", "verdict": "FAIL",
           "checks": [{"name": "keys", "title": "viewer keys", "verdict": "FAIL", "rows": rows,
                       "summary": {"keys_sent": 4, "keys_answered": 0, "keys_ignored": 4},
                       "spec": {"seconds": 10, "seq_start": 0, "press": "k:2000", "heartbeat_ms": 1000,
                                "scenario": ["t=5s loss=2%"]}, "errors": []}]}
    md = report.render(run)
    assert "viewer presses 'k' every 2000 ms with the rate heartbeat on (every 1000 ms)" in md
    assert "tsnetem t=5s loss=2%" in md
    assert "keys pressed 4 (answered 0, ignored 4)" in md
    assert "viewer keys are ignored: a 'k' brings no keyframe" in md


def test_bad_check_source_and_missing_budgets(tmp_path):
    p = tmp_path / "hil.yaml"
    p.write_text("version: 1\nchecks: {file: {}, camera: {}, x: {source: v4l2}}\n"
                 "budgets: {file: {}, camera: {}, x: {}}\n")
    with pytest.raises(hilcore.HilError, match="source"):
        hilcore.load_config(str(p))
    p.write_text("version: 1\nchecks: {file: {}, camera: {}, x: {}}\nbudgets: {file: {}, camera: {}}\n")
    with pytest.raises(hilcore.HilError, match="budgets.x"):
        hilcore.load_config(str(p))


def test_seq_start_derived_from_the_packet_rate(cfg):
    assert hilcore.seq_start_of({"seq_start": 7}) == 7
    assert hilcore.seq_start_of({}) == 0
    # 61 packets/s, wrap at t=3.5 s after a 3.3 s head start: 415 packets before the wrap
    assert hilcore.seq_start_of({"wrap": {"at": 3.5, "pkts_per_s": 61, "preroll_s": 3.3}}) == 65536 - 415
    with pytest.raises(ValueError):
        hilcore.seq_start_of({"wrap": {"at": 2000, "pkts_per_s": 61}})
    spec = hilcore.check_spec(cfg, "file", "build-hil/runs/r", "clip.y8", "build-host")
    w = cfg["checks"]["file"]["wrap"]
    assert spec["seq_start"] == hilcore.seq_start_of(cfg["checks"]["file"])
    assert spec["wrap_at"] == w["at"] and spec["preroll_s"] == w["preroll_s"]
    assert w["at"] - 0.5 >= cfg["checks"]["file"]["cut"]["at"]          # the wrap lies inside the cut
    assert w["at"] + 0.5 <= cfg["checks"]["file"]["cut"]["at"] + cfg["checks"]["file"]["cut"]["s"]


def test_keys_and_synth_specs(cfg):
    spec = hilcore.check_spec(cfg, "keys", "build-hil/runs/r", "clip.y8", "build-host")
    assert spec["source"] == "file" and spec["ref"] == "clip.y8"
    assert spec["press"] == "k:2000" and spec["heartbeat_ms"] == 1000 and "cut" not in spec
    cfg["checks"]["link"] = {"source": "synth", "seconds": 20, "kbps": 1000,
                             "scenario": ["t=0s throttle=1000kbps", "t=3s loss=5%"]}
    spec = hilcore.check_spec(cfg, "link", "build-hil/runs/r", "clip.y8", "build-host")
    assert spec["source"] == "synth" and "ref" not in spec and spec["kbps"] == 1000
    assert spec["scenario"][0].startswith("t=0s throttle=")


def test_selected_checks(cfg):
    assert hilcore.selected_checks(cfg, None) == ["file", "camera", "keys"]
    assert hilcore.selected_checks(cfg, ["camera"]) == ["camera"]
    assert hilcore.selected_checks(cfg, ["file", "keys"]) == ["file", "keys"]
    with pytest.raises(hilcore.HilError):
        hilcore.selected_checks(cfg, ["wifi"])


def test_check_spec_receiver(cfg):
    spec = hilcore.check_spec(cfg, "file", "build-hil/runs/r", "clip.y8", "build-host")
    assert spec["receiver"] == "station" and "tsrecv" not in spec and spec["ref"] == "clip.y8"
    spec = hilcore.check_spec(cfg, "camera", "build-hil/runs/r", "clip.y8", "build-host", "c/build-host/tsrecv")
    assert spec["receiver"] == "candidate" and spec["tsrecv"] == "c/build-host/tsrecv"
    assert spec["pause"] == {"at": 4, "s": 3} and "cut" not in spec


def test_slug():
    assert hilcore.slug("refs/heads/Agent/Fix stripes!") == "refs-heads-agent-fix-stripes"
    assert hilcore.slug("///") == "candidate"
