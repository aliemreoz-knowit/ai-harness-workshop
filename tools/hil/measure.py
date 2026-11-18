#!/usr/bin/env python3
"""measure.py -- one on-target check of the HIL station (runs where the receiver runs).

    python3 tools/hil/measure.py --spec build-hil/runs/<id>/file.spec.json

The spec (JSON, written by hil.py) says what to start and for how long:

  * local mode: the daemon under test (`daemon.cmd`, e.g. qemu-aarch64-static +
    the static pi binary, or the host build) with a file or pipe source; for
    the pipe source tools/hil/pipe_source.py feeds it (the rpicam-vid stand-in)
  * remote mode: the daemon already runs on the Pi (systemd); only the
    receiver side starts here
  * always: tsnetem as a UDP relay in front of tsrecv, with a scenario that
    cuts the link (`cut.at`, `cut.s`) plus extra steps (`scenario`), and tsrecv
    writing metrics.json every `out_ms`; `events` run commands at fixed times
    (on the Pi: pausing rpicam-vid with kill -STOP / -CONT)
  * optional viewer traffic: `press` (tsrecv --press, e.g. "k:2000") with
    `heartbeat_ms` (tsrecv --heartbeat-ms: the rate heartbeat, a SET_RATE at
    the current rate every N ms), `kbps` (tsrecv --kbps, the viewer's budget)

The metrics file is sampled every `sample_ms` into a time series; the summary
(fps, NACK ratio, CRC failures, freezes, recovery after the cut, longest gap
between daemon STATS, tears, exact match, viewer keys answered / ignored) is computed from it by summarize(),
a pure function the tests call directly. Result: <out_dir>/<name>.result.json.

Python 3.9+, standard library only (it runs in the dev container and on the
station laptop). Times in the series are seconds since t0, the moment the
relay starts; the cut and the pause are scheduled against the same t0.
"""
from __future__ import annotations

import argparse
import json
import os
import signal
import subprocess
import sys
import time
from typing import Any, Optional

SERIES_KEYS = ("frames_decoded", "fps", "pkts_rx", "pkts_expected", "nacks_sent", "crc_fail", "freezes", "freeze_ms_total",
               "stats_rx", "reconnects", "pkts_lost", "retx_rx", "keyframe_reqs", "exact_match_pct")
HERE = os.path.dirname(os.path.abspath(__file__))


# ---- summary (pure) -----------------------------------------------------------------------

def _val(sample: dict, key: str) -> float:
    v = sample.get(key, 0)
    return float(v) if isinstance(v, (int, float)) else 0.0


def _at(series: list[dict], t: float) -> Optional[dict]:
    """Last sample at or before t (None before the first sample)."""
    best = None
    for s in series:
        if s["t"] <= t:
            best = s
        else:
            break
    return best


def _window_rate(series: list[dict], key: str, t_a: float, t_b: float) -> Optional[float]:
    """Increase of `key` per second between the samples nearest t_a and t_b (None if too short)."""
    a = _at(series, t_a)
    b = _at(series, t_b)
    if a is None or b is None or b["t"] - a["t"] < 0.25:
        return None
    return (_val(b, key) - _val(a, key)) / (b["t"] - a["t"])


def _window_delta(series: list[dict], key: str, t_a: float, t_b: float) -> Optional[float]:
    a = _at(series, t_a)
    b = _at(series, t_b)
    if a is None or b is None:
        return None
    return _val(b, key) - _val(a, key)


def _step_times(spec: dict) -> list[float]:
    """Times (s) of the extra scenario steps (spec.scenario), t=0 steps excluded."""
    out = []
    for line in spec.get("scenario", []) or []:
        head = str(line).split(None, 1)[0]
        if head.startswith("t=") and head.endswith("s"):
            try:
                t = float(head[2:-1])
            except ValueError:
                continue
            if t > 0.0:
                out.append(t)
    return out


def summarize(series: list[dict], final: dict, spec: dict) -> dict:
    """Metrics of one check from the sampled series and the final metrics.json."""
    out: dict[str, Any] = {}
    seconds = float(spec.get("seconds", 10))
    cut = spec.get("cut") or None
    pause = spec.get("pause") or None
    end = series[-1]["t"] if series else 0.0

    first = next((s for s in series if _val(s, "frames_decoded") > 0), None)
    out["first_frame_s"] = round(first["t"], 3) if first else None
    out["frames_decoded"] = int(final.get("frames_decoded", 0))

    # the clean window: from 0.5 s after the first frame to the first scripted event
    # (the cut, the pause or a later scenario step such as "t=5s loss=2%")
    events = [e for e in (cut and float(cut["at"]), pause and float(pause["at"])) if e] + _step_times(spec)
    clean_end = (min(events) - 0.25) if events else end     # a sample at the event is already affected
    t_a = (first["t"] + 0.5) if first else end
    fps_clean = _window_rate(series, "frames_decoded", t_a, clean_end) if first else None
    out["fps_clean"] = round(fps_clean, 2) if fps_clean is not None else 0.0
    d_pkts = _window_delta(series, "pkts_rx", t_a, clean_end) if first else None
    d_nack = _window_delta(series, "nacks_sent", t_a, clean_end) if first else None
    d_crc = _window_delta(series, "crc_fail", t_a, clean_end) if first else None
    out["nack_ratio_clean_pct"] = (round(100.0 * d_nack / d_pkts, 2)
                                   if (d_pkts and d_nack is not None and d_pkts > 0) else None)
    out["crc_fail_clean"] = int(d_crc) if d_crc is not None else None
    pkt_rate = _window_rate(series, "pkts_rx", t_a, clean_end) if first else None
    out["pkts_per_s"] = round(pkt_rate, 1) if pkt_rate is not None else None
    # when pkt_seq wraps (seq-start near 65535), estimated with the measured rate. The daemon
    # numbers packets from its own start, before the viewer joins: spec.preroll_s (hil.yaml
    # checks.<c>.wrap.preroll_s) is that head start in seconds of packets at this rate.
    seq_start = int(spec.get("seq_start", 0))
    if pkt_rate and seq_start > 0:
        if spec.get("preroll_s") is not None:
            out["pkt_seq_wrap_s"] = round((65536 - seq_start) / pkt_rate - float(spec["preroll_s"]), 1)
        else:
            out["pkt_seq_wrap_s"] = round((first["t"] if first else 0.0) + (65536 - seq_start) / pkt_rate, 1)

    # after the last event (cut, pause, scenario step): the stream must be back at full rate
    ev_end = max(_step_times(spec) or [0.0])
    if cut:
        ev_end = max(ev_end, float(cut["at"]) + float(cut["s"]))
    if pause:
        ev_end = max(ev_end, float(pause["at"]) + float(pause["s"]))
    if ev_end > 0.0:
        fps_after = _window_rate(series, "frames_decoded", ev_end + 1.5, end)
        out["fps_after"] = round(fps_after, 2) if fps_after is not None else 0.0

    # recovery after the link cut: first sustained frames after the cut ended
    if cut:
        cut_end = float(cut["at"]) + float(cut["s"])
        out["recovery_ms"] = _recovery_ms(series, cut_end, fps_clean or 0.0)

    # longest silence of the daemon's STATS (1 per second when it runs its loop)
    out["stats_gap_ms"] = _stats_gap_ms(series, first["t"] if first else 0.0, end)

    for k in ("freezes", "freeze_ms_total", "reconnects", "crc_fail", "nacks_sent", "pkts_rx", "pkts_lost",
              "retx_rx", "keyframe_reqs", "stats_rx", "decode_errors", "frames_dropped"):
        out[k] = int(final.get(k, 0))
    for k in ("loss_pct", "residual_loss_pct"):
        out[k] = float(final.get(k, 0.0))
    tear = final.get("tear") or {}
    out["tear_frames"] = int(tear.get("tear_frames", 0)) if tear else None
    out["tear_rows"] = int(tear.get("tear_rows", 0)) if tear else None
    out["tear_frames_checked"] = int(tear.get("frames_checked", 0)) if tear else None
    out["picture_changes"] = int(tear.get("picture_changes", 0)) if tear else None
    if spec.get("ref"):
        out["exact_match_pct"] = int(final.get("exact_match_pct", 0))
        out["truth_frames"] = int(final.get("truth_frames", 0))
    cmds = final.get("commands") or {}
    if spec.get("press"):
        out["keys_sent"] = int(cmds.get("keyframe_reqs", 0))
        out["keys_answered"] = int(cmds.get("keyframe_answered", 0))
        out["keys_ignored"] = int(cmds.get("keyframe_ignored", 0))
        out["heartbeats"] = int(cmds.get("heartbeats", 0))
    daemon = final.get("daemon") or {}
    out["daemon_frames_sent"] = int(daemon.get("frames_sent", 0))
    out["nack_too_old"] = int(daemon.get("nack_too_old", 0))
    out["seconds"] = seconds
    return out


def _recovery_ms(series: list[dict], cut_end: float, fps_base: float) -> Optional[int]:
    need = max(1.0, 0.25 * fps_base)      # half the base rate over half a second
    prev = _at(series, cut_end)
    if prev is None:
        return None
    for i, s in enumerate(series):
        if s["t"] <= cut_end:
            continue
        before = series[i - 1]
        if _val(s, "frames_decoded") > _val(before, "frames_decoded"):
            later = _at(series, s["t"] + 0.5)
            if later is not None and later["t"] - s["t"] >= 0.4 and \
                    _val(later, "frames_decoded") - _val(s, "frames_decoded") >= need:
                mid = (before["t"] + s["t"]) / 2.0 if before["t"] > cut_end else s["t"]
                return int(round(max(0.0, mid - cut_end) * 1000.0))
    return None


def _stats_gap_ms(series: list[dict], t_from: float, t_end: float) -> Optional[int]:
    last_t = None
    last_v = None
    gap = 0.0
    for s in series:
        if s["t"] < t_from:
            continue
        v = _val(s, "stats_rx")
        if last_v is None:
            last_t, last_v = s["t"], v
        elif v != last_v:
            gap = max(gap, s["t"] - last_t)
            last_t, last_v = s["t"], v
    if last_t is None:
        return None
    gap = max(gap, t_end - last_t)
    return int(round(gap * 1000.0))


# ---- running the processes -------------------------------------------------------------------

class Proc:
    def __init__(self, name: str, cmd: list[str], log_dir: str, **kw: Any) -> None:
        self.name = name
        self.cmd = cmd
        self.log_path = os.path.join(log_dir, name + ".log")
        self.log = open(self.log_path, "wb")
        kw.setdefault("stdout", self.log)
        kw.setdefault("stdin", subprocess.DEVNULL)
        self.p = subprocess.Popen(cmd, stderr=self.log, **kw)

    def alive(self) -> bool:
        return self.p.poll() is None

    def stop(self, timeout: float = 3.0) -> Optional[int]:
        if self.p.poll() is None:
            try:
                self.p.send_signal(signal.SIGTERM)
                self.p.wait(timeout=timeout)
            except subprocess.TimeoutExpired:
                self.p.kill()
                self.p.wait()
        self.log.close()
        return self.p.returncode


def _read_json(path: str) -> Optional[dict]:
    try:
        with open(path, "r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError):
        return None


def _wait_log(path: str, needle: str, timeout: float) -> bool:
    t_end = time.time() + timeout
    while time.time() < t_end:
        try:
            with open(path, "rb") as f:
                if needle.encode() in f.read():
                    return True
        except OSError:
            pass
        time.sleep(0.05)
    return False


def scenario_text(spec: dict) -> str:
    """tsnetem timeline: clean at 0, the cut, extra steps (spec.scenario), sorted by time."""
    steps = [(0.0, "t=0s clean")]
    cut = spec.get("cut")
    if cut:
        steps.append((float(cut["at"]), "t=%gs blackout %gs" % (float(cut["at"]), float(cut["s"]))))
    for line in spec.get("scenario", []):
        head = str(line).split(None, 1)[0]
        if not (head.startswith("t=") and head.endswith("s")):
            raise ValueError("scenario step must start with t=<seconds>s: %r" % line)
        steps.append((float(head[2:-1]), str(line)))
    steps.sort(key=lambda s: s[0])
    return "".join(s[1] + "\n" for s in steps)


def daemon_args(spec: dict) -> list[str]:
    d = spec["daemon"]
    src = d.get("source", "file")
    args = ["--source", {"file": "file:" + str(d.get("clip", "")), "pipe": "pipe", "synth": "synth"}[src],
            "--udp-port", str(d.get("udp_port", 5700)), "--tcp-port", str(spec.get("tcp_port", 5701)),
            "--seq-start", str(spec.get("seq_start", 0)), "--stream-id", "1"]
    return args + [str(x) for x in d.get("extra_args", [])]


def run(spec: dict) -> dict:
    out_dir = spec["out_dir"]
    os.makedirs(out_dir, exist_ok=True)
    name = spec.get("name", "check")
    tools = spec["tools_dir"]
    seconds = float(spec.get("seconds", 10))
    rx_port = int(spec.get("rx_udp_port", 5710))
    relay_port = int(spec.get("relay_port", 5720))
    relay_bind = spec.get("relay_bind", "127.0.0.1")
    server = spec.get("server", "127.0.0.1")
    tcp_port = int(spec.get("tcp_port", 5701))
    sample_s = float(spec.get("sample_ms", 50)) / 1000.0
    metrics_path = os.path.join(out_dir, name + ".metrics.json")
    frame_path = os.path.join(out_dir, name + ".frame.pgm")
    for p in (metrics_path, frame_path):
        if os.path.exists(p):
            os.remove(p)
    procs: list[Proc] = []
    errors: list[str] = []
    result: dict[str, Any] = {"name": name, "spec": spec, "errors": errors}

    scen = os.path.join(out_dir, name + ".scenario.txt")
    with open(scen, "w", encoding="utf-8", newline="\n") as f:
        f.write(scenario_text(spec))
    lead = float(spec.get("lead_s", 1.5))
    t0 = time.time() + lead
    daemon = None
    producer = None
    try:
        if spec.get("mode", "local") == "local":
            d = spec["daemon"]
            cmd = [str(x) for x in d["cmd"]] + daemon_args(spec)
            if d.get("source", "file") == "pipe":
                pause = spec.get("pause") or {}
                pcmd = [sys.executable, os.path.join(HERE, "pipe_source.py"), "--clip", d["clip"],
                        "--fps", str(d.get("fps", 30)), "--seconds", str(seconds + lead + 5.0),
                        "--t0", repr(t0)]
                if pause:
                    pcmd += ["--pause-at", str(pause["at"]), "--pause-s", str(pause["s"])]
                if d.get("chunk"):
                    pcmd += ["--chunk", str(d["chunk"]), "--chunk-gap-ms", str(d.get("chunk_gap_ms", 0))]
                producer = Proc("producer", pcmd, out_dir, stdout=subprocess.PIPE)
                procs.append(producer)
                daemon = Proc("daemon", cmd, out_dir, stdin=producer.p.stdout)
                producer.p.stdout.close()   # the daemon holds the read end
            else:
                daemon = Proc("daemon", cmd, out_dir)
            procs.append(daemon)
            if d.get("source", "file") != "pipe" and \
                    not _wait_log(daemon.log_path, "streaming on", float(spec.get("start_timeout_s", 10))):
                errors.append("daemon did not start (see daemon.log)")
        wait = t0 - time.time()
        if wait > 0:
            time.sleep(wait)
        if errors:
            raise RuntimeError(errors[-1])
        t0 = time.time()
        relay = Proc("relay", [os.path.join(tools, "tsnetem"), "--listen", "%s:%d" % (relay_bind, relay_port),
                               "--forward", "127.0.0.1:%d" % rx_port, "--scenario", scen, "--seed", "7",
                               "--quiet"], out_dir)
        procs.append(relay)
        time.sleep(0.1)
        rx_cmd = [spec.get("tsrecv") or os.path.join(tools, "tsrecv"), "--server", "%s:%d" % (server, tcp_port), "--udp-port", str(rx_port),
                  "--announce-port", str(relay_port), "--seconds", str(int(round(seconds))),
                  "--metrics", metrics_path, "--frame", frame_path, "--out-ms", str(spec.get("out_ms", 100)),
                  "--seq-start", str(spec.get("seq_start", 0)), "--quiet"]
        if spec.get("ref"):
            rx_cmd += ["--ref", spec["ref"]]
        if spec.get("kbps"):
            rx_cmd += ["--kbps", str(int(spec["kbps"]))]
        if spec.get("press"):
            rx_cmd += ["--press", str(spec["press"])]
            if spec.get("heartbeat_ms"):
                rx_cmd += ["--heartbeat-ms", str(int(spec["heartbeat_ms"]))]
        rx = Proc("tsrecv", rx_cmd, out_dir)
        procs.append(rx)
        events = sorted(spec.get("events", []), key=lambda e: float(e["at"]))
        series: list[dict] = []
        deadline = t0 + seconds + float(spec.get("grace_s", 5.0))
        while rx.alive() and time.time() < deadline:
            now = time.time() - t0
            while events and float(events[0]["at"]) <= now:
                ev = events.pop(0)
                rc = subprocess.call([str(x) for x in ev["cmd"]], stdout=subprocess.DEVNULL,
                                     stderr=subprocess.DEVNULL)
                if rc != 0:
                    errors.append("event at %ss failed (%d): %s" % (ev["at"], rc, " ".join(ev["cmd"])))
            m = _read_json(metrics_path)
            if m is not None:
                s = {"t": round(now, 3)}
                for k in SERIES_KEYS:
                    if k in m:
                        s[k] = m[k]
                if not series or any(series[-1].get(k) != s.get(k) for k in SERIES_KEYS) or \
                        now - series[-1]["t"] >= 0.5:
                    series.append(s)
            time.sleep(sample_s)
        if rx.alive():
            errors.append("tsrecv did not finish in time")
        result["daemon_alive"] = daemon.alive() if daemon else None
    except (OSError, RuntimeError) as exc:
        if str(exc) not in errors:
            errors.append(str(exc))
        series = locals().get("series", []) or []
    finally:
        codes = {}
        for p in reversed(procs):
            codes[p.name] = p.stop()
        result["exit_codes"] = codes
        result["logs"] = {p.name: p.log_path for p in procs}
    final = _read_json(metrics_path) or {}
    if series and final:
        series.append(dict({"t": round(time.time() - t0, 3)}, **{k: final[k] for k in SERIES_KEYS if k in final}))
    result["series"] = series
    result["final"] = final
    result["summary"] = summarize(series, final, spec) if final else {}
    if not final:
        errors.append("no metrics from tsrecv (see tsrecv.log)")
    return result


def main(argv: Optional[list[str]] = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("--spec", required=True)
    a = ap.parse_args(argv)
    with open(a.spec, "r", encoding="utf-8") as f:
        spec = json.load(f)
    res = run(spec)
    path = os.path.join(spec["out_dir"], spec.get("name", "check") + ".result.json")
    with open(path, "w", encoding="utf-8", newline="\n") as f:
        json.dump(res, f, indent=1)
        f.write("\n")
    s = res.get("summary", {})
    print("measure %s: %s" % (spec.get("name"), json.dumps(s, sort_keys=True)))
    for e in res["errors"]:
        print("measure %s: error: %s" % (spec.get("name"), e), file=sys.stderr)
    return 0 if not res["errors"] else 2


if __name__ == "__main__":
    sys.exit(main())
