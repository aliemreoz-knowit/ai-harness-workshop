"""End to end: hil.py run --target local against real candidates (dev container, about 20 s per check).

  golden     (HIL_GOLDEN_REF, optional)            file + camera + keys checks PASS
  fail       (HIL_FAIL_REF, default b0-start)      file check FAILs with the clean-link symptoms
  pipe-bad   (HIL_PIPE_BAD_REF, optional)          file check PASS, camera check FAILs: torn frames,
                                                   the daemon stalls while the source pauses, wrong picture
  stream-bad (HIL_STREAM_BAD_REF, optional)        file check FAILs after the cut (no recovery), keys
                                                   check FAILs (presses ignored); the clean link is fine
No default for the golden ref: which branch is known-good depends on the checkout
(the station's reference build, not necessarily `main`). A ref that does not exist
in the checkout skips its test.
"""
from __future__ import annotations

import json
import os
import subprocess
import sys
from pathlib import Path

import pytest

HIL = Path(__file__).resolve().parent.parent
ROOT = HIL.parent.parent

pytestmark = pytest.mark.docker


def hil_run(ref: str, checks: str) -> tuple[int, dict, str]:
    p = subprocess.run([sys.executable, str(HIL / "hil.py"), "run", "--target", "local", "--ref", ref,
                        "--checks", checks], cwd=str(ROOT), capture_output=True, text=True, timeout=900)
    report = ""
    run = {}
    for line in p.stdout.splitlines():
        if " -> " in line and line.endswith("report.md"):
            rp = ROOT / line.rsplit(" -> ", 1)[1]
            report = rp.read_text(encoding="utf-8")
            run = json.loads((rp.parent / "run.json").read_text(encoding="utf-8"))
    assert run, "no report from hil.py:\n%s\n%s" % (p.stdout, p.stderr)
    return p.returncode, run, report


def check(run: dict, name: str) -> dict:
    return next(c for c in run["checks"] if c["name"] == name)


def failing(c: dict) -> set:
    return {r["metric"] for r in c["rows"] if r["status"] == "FAIL"}


def _ref(var: str, default: str = "") -> str:
    ref = os.environ.get(var, default)
    if not ref:
        pytest.skip("%s not set" % var)
    if subprocess.run(["git", "rev-parse", "--verify", "--quiet", ref + "^{commit}"], cwd=str(ROOT),
                      capture_output=True).returncode != 0:
        pytest.skip("ref %s not in this repository" % ref)
    return ref


def test_golden_passes():
    rc, run, report = hil_run(_ref("HIL_GOLDEN_REF"), "file,camera,keys")
    assert rc == 0 and run["verdict"] == "PASS", report
    f, cam, keys = check(run, "file"), check(run, "camera"), check(run, "keys")
    assert keys["summary"]["keys_ignored"] == 0 and keys["summary"]["keys_answered"] >= 3
    assert f["summary"]["tear_frames"] == 0 and f["summary"]["exact_match_pct"] == 100
    assert f["summary"]["recovery_ms"] is not None and f["summary"]["recovery_ms"] <= 1000
    assert cam["summary"]["tear_frames"] == 0 and cam["summary"]["stats_gap_ms"] <= 1800
    assert cam["summary"]["exact_match_pct"] == 100


def test_known_bad_candidate_fails_with_its_symptoms():
    rc, run, report = hil_run(_ref("HIL_FAIL_REF", "b0-start"), "file")
    assert rc == 1 and run["verdict"] == "FAIL", report
    f = check(run, "file")
    # a clean link that needs repairs: CRC failures, NACK traffic, a picture that is not the clip
    assert {"crc_fail_clean", "nack_ratio_clean_pct", "exact_match_pct"} <= failing(f), report
    assert "datagrams fail the CRC check on a clean link" in report


def test_stream_that_does_not_recover_and_ignores_keys():
    rc, run, report = hil_run(_ref("HIL_STREAM_BAD_REF"), "file,keys")
    assert rc == 1 and run["verdict"] == "FAIL", report
    f, keys = check(run, "file"), check(run, "keys")
    assert "fps_after" in failing(f), report                      # the picture does not come back
    assert {"nack_ratio_clean_pct", "crc_fail_clean"}.isdisjoint(failing(f)), report
    assert {"keys_ignored", "keys_answered"} <= failing(keys), report


def test_pipe_source_that_reads_frames_in_pieces():
    rc, run, report = hil_run(_ref("HIL_PIPE_BAD_REF"), "file,camera")
    assert rc == 1 and run["verdict"] == "FAIL", report
    assert check(run, "file")["verdict"] == "PASS", report      # only the camera path is broken
    cam = check(run, "camera")
    assert {"tear_frames", "stats_gap_ms", "exact_match_pct"} <= failing(cam), report
    assert cam["summary"]["tear_frames"] >= 10
    assert cam["summary"]["stats_gap_ms"] >= 2500                 # no STATS while the source is paused
