"""What the bridge writes back: the single updating comment and the check-run summary."""

from __future__ import annotations

from typing import Any, Optional

from harness_runner import RunOutcome

ICON = {"done": "PASS", "failed": "FAIL", "escalated": "ESCALATED", "aborted": "ABORTED", "error": "ERROR",
        "running": "RUNNING", "queued": "QUEUED"}
SECTION_ORDER = ("agent", "hil")


def _cell(v: Any) -> str:
    return str(v if v not in (None, "") else "-").replace("|", "\\|").replace("\n", " ")


def started(src_key: str, title: str, workflow: str, runtime: str, fake: bool, trigger: str) -> str:
    rt = f"{runtime} (fake runtime: no model)" if fake else runtime
    return (f"### Agent run: {ICON['running']}\n\n"
            f"`{src_key}: {_cell(title)}` -- workflow `{workflow}`, runtime `{rt}`, trigger `{trigger}`.\n\n"
            "The result (gates, review, credits, branch) replaces this text when the run ends.")


NO_ACCEPTANCE_TESTS = ("**Note:** no acceptance tests: the harness accepts any progress; name the tests "
                       "that must pass (an `## Acceptance tests` section with the `test_*` names, see the "
                       "issue template). Informational, the run goes ahead.")


def ticket_notes(acceptance_tests: list[str]) -> str:
    """Notes on the ticket itself, appended to the issue comment ("" when there is nothing)."""
    return "" if acceptance_tests else NO_ACCEPTANCE_TESTS


def outcome_table(o: RunOutcome) -> str:
    s = o.summary or {}
    counters = s.get("counters") or {}
    credits = s.get("credits")
    rows = [
        ("status", o.status + (f" at `{s.get('stopped_at')}` ({s.get('reason')})" if s.get("stopped_at") else "")),
        ("run", f"`{o.run_id}`" if o.run_id else "-"),
        ("workflow / mode / runtime", f"{s.get('workflow', '-')} / {s.get('mode', '-')} / {s.get('runtime', '-')}"),
        ("gates", o.gates or "-"),
        ("review", s.get("review") or "-"),
        ("credits", f"{credits:.2f}" if isinstance(credits, (int, float)) else "-"),
        ("interventions / retries", f"{s.get('interventions', '-')} / {counters.get('retries', '-')}"),
        ("branch", f"`{o.branch}`" if o.branch else "-"),
        ("commit", f"`{o.commit[:12]}`" if o.commit else "-"),
    ]
    steps = s.get("steps") or {}
    if steps:
        rows.append(("steps", " ".join(f"{k}:{v}" for k, v in steps.items())))
    if o.error:
        rows.append(("error", o.error[:300]))
    return "| | |\n|---|---|\n" + "\n".join(f"| {k} | {_cell(v)} |" for k, v in rows)


def finished(src_key: str, title: str, o: RunOutcome, pr_url: Optional[str] = None) -> str:
    head = f"### Agent run: {ICON.get(o.status, o.status.upper())}\n\n`{src_key}: {_cell(title)}`"
    if pr_url:
        head += f" -> {pr_url}"
    body = head + "\n\n" + outcome_table(o)
    if not o.ok and o.log_tail:
        body += "\n\n<details><summary>harness output (tail)</summary>\n\n```\n" + o.log_tail[-3000:] + "\n```\n</details>"
    if o.ok:
        body += ("\n\nNext: review the PR; when the team is happy, label it `hil-ready` for a hardware run.")
    elif o.run_id:
        body += (f"\n\nThe run folder is kept in the bridge worktree; resume with "
                 f"`python -m harness run --resume {o.run_id}` there, or fix the ticket and re-add the label.")
    return body


def conclusion(o: RunOutcome) -> str:
    return {"done": "success", "failed": "failure", "escalated": "action_required",
            "aborted": "cancelled"}.get(o.status, "failure")


def hil_queued(sha: str, position: int, entry_id: str) -> str:
    return (f"### HIL: {ICON['queued']}\n\nCommit `{sha[:12]}` queued for the hardware window "
            f"(position {position}, entry `{entry_id}`). The facilitator approves each run; "
            "hil.py runs gate hardware on the Pi; the bridge updates this section with its verdict.")


HIL_HEAD = {"running": "RUNNING", "passed": "PASS", "failed": "FAIL", "skipped": "DECLINED"}


def hil_result(entry: dict[str, Any], report_md: str = "") -> str:
    """The `hil` section once hil.py has taken the entry (running) or judged it."""
    st = str(entry.get("status"))
    sha, eid = str(entry.get("sha", ""))[:12], entry.get("id")
    if st == "running":
        return f"### HIL: {HIL_HEAD[st]}\n\nCommit `{sha}` is on the hardware-in-the-loop station now (entry `{eid}`)."
    body = f"### HIL: {HIL_HEAD.get(st, st.upper())}\n\nCommit `{sha}`, entry `{eid}`"
    if entry.get("report"):
        body += f", report `{entry['report']}` on the station"
    body += "."
    if st == "skipped":
        body += " The facilitator declined the deploy; push a new commit to queue it again."
    if report_md.strip():
        body += "\n\n<details><summary>HIL report</summary>\n\n" + report_md.strip()[:20000] + "\n\n</details>"
    return body


def hil_refused(reason: str) -> str:
    return f"### HIL: REFUSED\n\n{reason}"


def compose(sections: dict[str, str]) -> str:
    keys = [k for k in SECTION_ORDER if k in sections] + sorted(k for k in sections if k not in SECTION_ORDER)
    return "\n\n---\n\n".join(sections[k] for k in keys) + "\n\n<sub>tinystream bridge -- this comment is edited in place.</sub>"


def pr_body(src_key: str, title: str, src_url: str, o: RunOutcome, closes: Optional[int]) -> str:
    lines = [f"Agent change for **{src_key}: {title}** ({src_url}).", "",
             f"Produced by the harness (`{(o.summary or {}).get('workflow', 'jira-to-pr')}`, run `{o.run_id}`): "
             "planner -> implementer <-> gates -> test-writer -> ladder -> reviewer. "
             "The gate results and costs are in the bridge comment and the `agent-harness` check.", ""]
    if closes:
        lines.append(f"Closes #{closes}")
    return "\n".join(lines)
