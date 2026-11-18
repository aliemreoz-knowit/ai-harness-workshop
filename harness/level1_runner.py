"""Level 1 (block 1): one transparent `copilot -p` call, no SDK, no magic.

    python harness/level1_runner.py implementer "Fix the bug described in ticket.md" view,edit 30

The CLI refuses a session limit below 30 AI credits: smaller values are raised to 30.
"""
import json, os, shutil, subprocess, sys, tempfile
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))   # run as a script: the repository root, for harness.*
from harness import envdetect                                  # noqa: E402

MIN_CREDITS = 30                                           # the CLI's floor for --max-ai-credits


def run(role, prompt, allow=("view",), credits=MIN_CREDITS, cwd=".", runner=subprocess.run):
    usage_file = Path(tempfile.mkdtemp()) / "usage.json"
    cli = envdetect.find_copilot_cli() or "copilot"          # the pinned one in .tools/copilot first
    cmd = [cli, "-p", prompt, "--output-format", "json", "-s", "--no-ask-user", "--agent", role,
           "--max-ai-credits", f"{max(MIN_CREDITS, float(credits)):g}", "--usage-output-file", str(usage_file)]
    cmd += [f"--allow-tool={t}" for t in allow]           # everything else needs approval -> refused
    proc = runner(cmd, cwd=cwd, capture_output=True, text=True, encoding="utf-8", errors="replace",
                  env={**os.environ, **envdetect.NO_AUTO_UPDATE})
    events = []
    for line in (proc.stdout or "").splitlines():         # defensive JSONL: skip banners and partial lines
        try:
            ev = json.loads(line)
        except ValueError:
            continue
        if isinstance(ev, dict):
            events.append(ev)
    data = lambda e: e.get("data") if isinstance(e.get("data"), dict) else {}
    tools = [data(e) for e in events if e.get("type") == "tool.execution_complete"]
    usage = json.loads(usage_file.read_text(encoding="utf-8")) if usage_file.is_file() else {}
    return {"answer": "\n".join(data(e).get("content", "") for e in events if e.get("type") == "assistant.message"),
            "tool_calls": len(tools), "tool_failures": sum(1 for t in tools if t.get("success") is not True),
            "usage": usage, "exit_code": proc.returncode}      # judge by tool results, never by the exit code


if __name__ == "__main__":
    role, prompt, allow = sys.argv[1], sys.argv[2], sys.argv[3].split(",")
    credits = float(sys.argv[4]) if len(sys.argv) > 4 else MIN_CREDITS
    print(json.dumps(run(role, prompt, allow, credits), indent=2))
