"""hilreport.py -- the markdown report of one HIL run (also the PR comment body)."""
from __future__ import annotations

from typing import Any

import budgets as bud

MARKER = "<!-- tinystream-hil-report -->"


def _fmt(v: Any) -> str:
    if v is None:
        return "n/a"
    if isinstance(v, float):
        return ("%.2f" % v).rstrip("0").rstrip(".")
    return str(v)


def _describe(check: dict) -> str:
    spec = check.get("spec", {})
    parts = ["%s s" % _fmt(spec.get("seconds")), "seq-start %s" % spec.get("seq_start", 0)]
    if spec.get("source_desc"):
        parts.insert(0, spec["source_desc"])
    if spec.get("receiver") == "candidate":
        parts.append("the candidate's tsrecv")
    if spec.get("kbps"):
        parts.append("viewer budget %s kbit/s" % _fmt(spec["kbps"]))
    if spec.get("cut"):
        parts.append("link cut %s s at t=%s s (tsnetem)" % (_fmt(spec["cut"]["s"]), _fmt(spec["cut"]["at"])))
    if spec.get("wrap_at") is not None:
        parts.append("pkt_seq wraps at about t=%s s" % _fmt(spec["wrap_at"]))
    if spec.get("scenario"):
        parts.append("tsnetem %s" % "; ".join(str(s) for s in spec["scenario"]))
    if spec.get("press"):
        keys, _, ms = str(spec["press"]).partition(":")
        hb = (" with the rate heartbeat on (every %s ms)" % spec["heartbeat_ms"]
              if spec.get("heartbeat_ms") else "")
        parts.append("viewer presses '%s' every %s ms%s" % (keys, ms, hb))
    if spec.get("pause"):
        parts.append("source paused %s s at t=%s s" % (_fmt(spec["pause"]["s"]), _fmt(spec["pause"]["at"])))
    if spec.get("ref"):
        parts.append("compared with the clip")
    return ", ".join(parts)


def render(run: dict) -> str:
    """run: {candidate, target, verdict, checks: [{name, title, verdict, rows, summary, spec, errors}], ...}"""
    cand = run["candidate"]
    lines = [MARKER,
             "## HIL %s: `%s` (%s)" % (run["verdict"], cand["ref"], cand["sha"][:10]),
             "",
             "| | |", "|---|---|",
             "| target | %s |" % run["target"],
             "| daemon | %s |" % run.get("daemon_desc", ""),
             "| station | %s |" % run.get("station", ""),
             "| started | %s |" % run.get("started", ""),
             "| duration | %s s |" % _fmt(run.get("duration_s")),
             ""]
    if run.get("dry_run"):
        lines += ["**Dry run**: nothing was deployed or measured; the commands are listed below.", ""]
    for chk in run.get("checks", []):
        lines += ["### %s -- %s" % (chk.get("title", chk["name"]), chk["verdict"]), "",
                  _describe(chk), "",
                  "| check | value | budget | result |", "|---|---|---|---|"]
        for r in chk.get("rows", []):
            budget = ("%s %s" % (r["op"], _fmt(r["limit"]))) if r["op"] else ""
            lines.append("| %s | %s | %s | %s |" % (r["label"], _fmt(r["value"]), budget, r["status"]))
        s = chk.get("summary", {})
        info = ["frames decoded %s" % _fmt(s.get("frames_decoded")),
                "loss %s %%" % _fmt(s.get("loss_pct")),
                "NACKs %s" % _fmt(s.get("nacks_sent")),
                "retransmits %s" % _fmt(s.get("retx_rx")),
                "keyframe requests %s" % _fmt(s.get("keyframe_reqs")),
                "frames checked for tears %s" % _fmt(s.get("tear_frames_checked")),
                "packets/s %s" % _fmt(s.get("pkts_per_s"))]
        if s.get("pkt_seq_wrap_s") is not None:
            info.append("pkt_seq wraps at about t=%s s (estimate from packets/s)" % _fmt(s.get("pkt_seq_wrap_s")))
        if s.get("keys_sent") is not None:
            info.append("keys pressed %s (answered %s, ignored %s)" % (_fmt(s.get("keys_sent")),
                                                                     _fmt(s.get("keys_answered")),
                                                                     _fmt(s.get("keys_ignored"))))
        lines += ["", "Also measured: " + ", ".join(info) + "."]
        sym = bud.symptoms(chk.get("rows", []))
        if sym:
            lines += ["", "Symptoms:"] + ["- " + x for x in sym]
        for e in chk.get("errors", []):
            lines.append("- error: %s" % e)
        lines.append("")
    if run.get("commands"):
        lines += ["<details><summary>Commands</summary>", "", "```"] + run["commands"] + ["```", "", "</details>", ""]
    if run.get("run_dir"):
        lines += ["Run folder: `%s`" % run["run_dir"], ""]
    return "\n".join(lines)
