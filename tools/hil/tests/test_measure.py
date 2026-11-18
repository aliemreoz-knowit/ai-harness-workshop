"""measure.summarize and the scenario: pure functions on synthetic time series."""
from __future__ import annotations

import pytest

import measure


def series_30fps(seconds=10.0, step=0.1, cut=None, stats_stop=None, nack_per_pkt=0.0, crc_per_s=0.0,
                 resume_delay=0.2):
    """A receiver at 30 fps / 60 pkt/s; no frames during cut=(at, s) until resume_delay after it."""
    out = []
    frames = pkts = nacks = crc = stats = 0.0
    t = 0.0
    while t <= seconds + 1e-9:
        dead = cut is not None and cut[0] <= t < cut[0] + cut[1] + resume_delay
        if t > 0 and not dead:
            frames += 30 * step
            pkts += 60 * step
            nacks += 60 * step * nack_per_pkt
            crc += crc_per_s * step
        if not (stats_stop and stats_stop[0] <= t < stats_stop[1]) and int(t * 10) % 10 == 0 and t > 0:
            stats += 1
        out.append({"t": round(t, 3), "frames_decoded": round(frames), "pkts_rx": round(pkts),
                    "nacks_sent": int(nacks), "crc_fail": int(crc), "stats_rx": int(stats)})
        t += step
    return out


FINAL = {"frames_decoded": 240, "freezes": 1, "reconnects": 0, "loss_pct": 17.5, "residual_loss_pct": 0.0,
         "exact_match_pct": 100, "truth_frames": 240,
         "tear": {"frames_checked": 240, "tear_frames": 0, "tear_rows": 0, "picture_changes": 0, "last_row": 0},
         "daemon": {"frames_sent": 300, "nack_too_old": 0}}


def test_clean_rates_and_recovery():
    spec = {"seconds": 10, "cut": {"at": 3, "s": 2}, "ref": "clip.y8", "seq_start": 65270}
    s = measure.summarize(series_30fps(cut=(3, 2)), FINAL, spec)
    assert s["fps_clean"] == pytest.approx(30, abs=1)
    assert s["fps_after"] == pytest.approx(30, abs=1)
    assert s["nack_ratio_clean_pct"] == 0.0
    assert s["crc_fail_clean"] == 0
    assert 100 <= s["recovery_ms"] <= 400            # resumed 0.2 s after the cut, sampled every 0.1 s
    assert s["pkts_per_s"] == pytest.approx(60, abs=2)
    assert s["pkt_seq_wrap_s"] == pytest.approx((65536 - 65270) / 60.0, abs=0.5)
    assert s["exact_match_pct"] == 100 and s["tear_frames"] == 0 and s["nack_too_old"] == 0


def test_wrap_estimate_with_the_daemon_head_start():
    spec = {"seconds": 10, "cut": {"at": 3, "s": 1}, "seq_start": 65536 - 420, "preroll_s": 3.0}
    s = measure.summarize(series_30fps(cut=(3, 1)), FINAL, spec)
    assert s["pkt_seq_wrap_s"] == pytest.approx(420 / 60.0 - 3.0, abs=0.3)


def test_scenario_step_ends_the_clean_window_and_starts_the_after_window():
    # NACK traffic only from t=5 s (a loss step): the clean window ends there, fps_after starts at 6.5 s
    series = series_30fps(cut=(3, 1))
    for x in series:
        x["nacks_sent"] = int(max(0.0, x["t"] - 5.0) * 6)
    spec = {"seconds": 10, "cut": {"at": 3, "s": 1}, "scenario": ["t=5s loss=2%"]}
    s = measure.summarize(series, FINAL, spec)
    assert s["nack_ratio_clean_pct"] == 0.0
    assert s["fps_after"] == pytest.approx(30, abs=1)
    series = series_30fps(cut=(3, 1), resume_delay=99)     # never back after the cut
    s = measure.summarize(series, FINAL, spec)
    assert s["fps_after"] == 0.0


def test_viewer_keys_summary():
    final = dict(FINAL, commands={"heartbeats": 9, "keyframe_reqs": 4, "keyframe_answered": 1,
                                  "keyframe_ignored": 3, "set_rate": 0})
    s = measure.summarize(series_30fps(), final, {"seconds": 10, "press": "k:2000", "heartbeat_ms": 1000})
    assert (s["keys_sent"], s["keys_answered"], s["keys_ignored"], s["heartbeats"]) == (4, 1, 3, 9)
    assert "keys_sent" not in measure.summarize(series_30fps(), final, {"seconds": 10})


def test_never_recovers():
    spec = {"seconds": 10, "cut": {"at": 3, "s": 2}}
    s = measure.summarize(series_30fps(cut=(3, 2), resume_delay=99), FINAL, spec)
    assert s["recovery_ms"] is None
    assert s["fps_after"] == 0.0
    assert "exact_match_pct" not in s                 # no reference: not measured


def test_nack_and_crc_on_the_clean_link():
    spec = {"seconds": 10, "cut": {"at": 6, "s": 2}}
    s = measure.summarize(series_30fps(cut=(6, 2), nack_per_pkt=0.15, crc_per_s=3), FINAL, spec)
    assert s["nack_ratio_clean_pct"] == pytest.approx(15, abs=2)
    assert s["crc_fail_clean"] >= 10


def test_stats_gap_shows_a_stalled_daemon():
    spec = {"seconds": 10, "pause": {"at": 4, "s": 3}}
    ok = measure.summarize(series_30fps(), FINAL, spec)
    stalled = measure.summarize(series_30fps(stats_stop=(4, 7.5)), FINAL, spec)
    assert ok["stats_gap_ms"] <= 1100
    assert stalled["stats_gap_ms"] >= 3500


def test_no_frames_at_all():
    s = measure.summarize([{"t": 0.1, "frames_decoded": 0}, {"t": 9.9, "frames_decoded": 0}], {}, {"seconds": 10})
    assert s["first_frame_s"] is None and s["fps_clean"] == 0.0
    assert s["tear_frames"] is None                   # a receiver without the detector reports nothing


def test_scenario_sorted_and_checked():
    txt = measure.scenario_text({"cut": {"at": 3, "s": 2}, "scenario": ["t=6s loss=1%", "t=2s dup=1"]})
    assert txt.splitlines() == ["t=0s clean", "t=2s dup=1", "t=3s blackout 2s", "t=6s loss=1%"]
    with pytest.raises(ValueError):
        measure.scenario_text({"scenario": ["loss=1%"]})


def test_daemon_args():
    spec = {"daemon": {"source": "file", "clip": "c.y8", "udp_port": 5700}, "tcp_port": 5701, "seq_start": 65270}
    args = measure.daemon_args(spec)
    assert args[:2] == ["--source", "file:c.y8"] and "65270" in args
    spec["daemon"]["source"] = "pipe"
    assert measure.daemon_args(spec)[:2] == ["--source", "pipe"]
    spec["daemon"]["source"] = "synth"
    assert measure.daemon_args(spec)[:2] == ["--source", "synth"]
